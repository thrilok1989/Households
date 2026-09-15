"""
futures_data.py (V2.1)

Live NIFTY futures data layer — the primary new capability in V2.1.
Everything here is OBSERVED data (Dhan's numbers) plus one INTERPRETED
label (`positioning_label`, e.g. "Long buildup") that is explicitly
never claimed as fact (spec V2.1 §5: "Futures Positioning Interpretation",
not "Confirmed Institutional Position").

Two things this module deliberately does NOT do:
  - It never hard-codes a futures Security ID. NIFTY futures contracts
    roll over every expiry, so a hard-coded ID goes silently stale.
    Instead it resolves the current-month contract from Dhan's public
    instrument master CSV (config.DHAN_INSTRUMENT_MASTER_URL), with an
    optional manual override (config.load_futures_security_id_override)
    for restricted environments. Resolution failure -> "unavailable",
    never a guessed ID.
  - It never fabricates OI history. "Previous OI/LTP" comes from our own
    SQLite storage (storage.SnapshotStore.save/get_futures_snapshot) —
    the same previous-LTP fix pattern already used for options.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from cache import cache
from config import (
    DHAN_INSTRUMENT_MASTER_URL,
    FUTURES_INSTRUMENT_CACHE_TTL_SECONDS,
    NIFTY_FUTURES_SEGMENT,
    load_futures_security_id_override,
)
from dhan_client import DhanClient, DhanAPIError
from utils import now_ist

# Plausible column names across Dhan instrument-master CSV revisions.
# NOTE: verify these against the current file if resolution starts
# failing — Dhan has changed this schema before and may again. We try
# several candidates per field rather than assuming one exact name, and
# fail loudly (return None) rather than guess if none match.
_EXCHANGE_COLS = ["SEM_EXM_EXCH_ID", "EXCH_ID", "SEM_EXCH_INSTRUMENT_TYPE"]
_INSTRUMENT_COLS = ["SEM_INSTRUMENT_NAME", "INSTRUMENT_TYPE", "SEM_EXCH_INSTRUMENT_TYPE"]
_SYMBOL_COLS = ["SEM_TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL", "SYMBOL_NAME"]
_SECURITY_ID_COLS = ["SEM_SMST_SECURITY_ID", "SECURITY_ID"]
_EXPIRY_COLS = ["SEM_EXPIRY_DATE", "EXPIRY_DATE"]


@dataclass
class ResolvedFuture:
    security_id: int
    symbol: str
    expiry: Optional[str]


@dataclass
class FuturesSnapshot:
    symbol: Optional[str]
    expiry: Optional[str]
    ltp: Optional[float]
    previous_ltp: Optional[float]
    price_change: Optional[float]
    price_change_pct: Optional[float]
    oi: Optional[float]
    previous_oi: Optional[float]
    oi_change: Optional[float]
    oi_change_pct: Optional[float]
    volume: Optional[float]
    timestamp: Optional[str]
    available: bool
    error: Optional[str] = None
    # V2.1.1 — order-book flow proxy (NOT true aggressor-side CVD; see
    # compute_flow_proxy() docstring for exactly what this is and isn't).
    buy_quantity: Optional[float] = None
    sell_quantity: Optional[float] = None
    flow_proxy: Optional[float] = None          # buy_quantity - sell_quantity, this snapshot
    flow_proxy_change: Optional[float] = None   # vs previous snapshot's flow_proxy
    flow_proxy_available: bool = False


def _first_matching_column(fieldnames: list[str], candidates: list[str]) -> Optional[str]:
    upper_map = {f.strip().upper(): f for f in fieldnames}
    for c in candidates:
        if c.upper() in upper_map:
            return upper_map[c.upper()]
    return None


def _parse_instrument_master(csv_text: str) -> Optional[ResolvedFuture]:
    """
    Parses the instrument master looking for NIFTY index futures
    (NSE, FUTIDX-style instrument, symbol containing "NIFTY" but not
    "BANKNIFTY"/"FINNIFTY"/other NIFTY-family indices), picks the
    nearest unexpired contract. Returns None if the schema doesn't match
    any known column-name variant, or no matching row is found — this
    function never guesses.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames or []
    if not fieldnames:
        return None

    exch_col = _first_matching_column(fieldnames, _EXCHANGE_COLS)
    instr_col = _first_matching_column(fieldnames, _INSTRUMENT_COLS)
    symbol_col = _first_matching_column(fieldnames, _SYMBOL_COLS)
    secid_col = _first_matching_column(fieldnames, _SECURITY_ID_COLS)
    expiry_col = _first_matching_column(fieldnames, _EXPIRY_COLS)

    if not (symbol_col and secid_col):
        return None  # can't identify the columns we need — fail honestly

    candidates: list[tuple[datetime, ResolvedFuture]] = []
    today_date = now_ist().date()

    for row in reader:
        symbol = (row.get(symbol_col) or "").upper()
        if "NIFTY" not in symbol:
            continue
        if any(x in symbol for x in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT")):
            continue
        if instr_col and "FUT" not in (row.get(instr_col) or "").upper():
            continue
        if exch_col and (row.get(exch_col) or "").upper() not in ("NSE", ""):
            continue

        raw_secid = row.get(secid_col)
        if not raw_secid:
            continue
        try:
            security_id = int(float(raw_secid))
        except (TypeError, ValueError):
            continue

        expiry_str = row.get(expiry_col) if expiry_col else None
        expiry_dt = None
        if expiry_str:
            for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y"):
                try:
                    expiry_dt = datetime.strptime(expiry_str.strip(), fmt)
                    break
                except ValueError:
                    continue
        if expiry_dt is None or expiry_dt.date() < today_date:
            continue

        candidates.append((expiry_dt, ResolvedFuture(security_id=security_id, symbol=symbol, expiry=expiry_str)))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def resolve_nifty_futures_security_id(client: DhanClient) -> Optional[ResolvedFuture]:
    """
    Returns the current-month NIFTY futures contract, preferring:
      1. A manual override (config.load_futures_security_id_override), if set.
      2. A cached resolution (valid for FUTURES_INSTRUMENT_CACHE_TTL_SECONDS).
      3. A fresh download + parse of Dhan's instrument master.
    Returns None (never a guess) if none of these succeed.
    """
    override = load_futures_security_id_override()
    if override is not None:
        return ResolvedFuture(security_id=override, symbol="NIFTY-FUT (manual override)", expiry=None)

    cache_key = "nifty_futures_resolution"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        csv_text = client.get_instrument_master_csv_text(DHAN_INSTRUMENT_MASTER_URL)
    except DhanAPIError:
        return None

    resolved = _parse_instrument_master(csv_text)
    if resolved is not None:
        cache.set(cache_key, resolved, FUTURES_INSTRUMENT_CACHE_TTL_SECONDS)
    return resolved


def classify_futures_positioning(price_change: Optional[float], oi_change: Optional[float]) -> str:
    """
    Price + OI heuristic (spec V2.1 §5) — an INTERPRETATION, never proof
    of trader intent. Labeled "Futures Positioning Interpretation" by
    the caller, not "Confirmed Institutional Position".
    """
    if price_change is None or oi_change is None:
        return "Unavailable — missing price or OI change"
    if price_change > 0 and oi_change > 0:
        return "Possible long buildup"
    if price_change < 0 and oi_change > 0:
        return "Possible short buildup"
    if price_change > 0 and oi_change < 0:
        return "Possible short covering"
    if price_change < 0 and oi_change < 0:
        return "Possible long unwinding"
    return "Mixed / Unclear"


def compute_flow_proxy(
    buy_quantity: Optional[float],
    sell_quantity: Optional[float],
    previous_buy_quantity: Optional[float],
    previous_sell_quantity: Optional[float],
) -> tuple[Optional[float], Optional[float], bool]:
    """
    IMPORTANT — WHAT THIS IS AND ISN'T:

    True CVD requires aggressor-side TRADE flow (which side initiated
    each executed trade). Dhan's quote endpoint does not expose that for
    this app's integration; what it exposes instead is aggregate PENDING
    order-book quantity at the buy side vs. sell side ("buy_quantity" /
    "sell_quantity" — total resting quantity, not executed trades).

    This function turns that into a documented, honestly-labeled PROXY:
        flow_proxy       = buy_quantity - sell_quantity        (this snapshot)
        flow_proxy_change = flow_proxy_now - flow_proxy_previous

    A rising flow_proxy means resting buy-side interest is growing
    relative to resting sell-side interest — a directional hint, NOT a
    measurement of actual buy/sell trade volume. Callers must label this
    CVDStatus.PROXY, never CVDStatus.OBSERVED, and must never call it
    "CVD" without qualification. If a genuine tick-level trade feed
    becomes available later, replace this function's internals — every
    caller already treats the output as "the flow proxy," not as this
    specific formula, so the swap is architecturally isolated here.

    Returns (flow_proxy, flow_proxy_change, available).
    """
    if buy_quantity is None or sell_quantity is None:
        return None, None, False
    proxy = buy_quantity - sell_quantity
    change = None
    if previous_buy_quantity is not None and previous_sell_quantity is not None:
        change = proxy - (previous_buy_quantity - previous_sell_quantity)
    return proxy, change, True


def fetch_futures_snapshot(client: DhanClient, store) -> FuturesSnapshot:
    """
    store: storage.SnapshotStore — used for the previous-LTP/OI lookup,
    same pattern as option legs. Never raises; any failure produces an
    `available=False` snapshot with `.error` explaining why, so callers
    can show "Futures: UNAVAILABLE — <reason>" rather than crashing or
    silently rendering a zero.
    """
    resolved = resolve_nifty_futures_security_id(client)
    if resolved is None:
        return FuturesSnapshot(
            symbol=None, expiry=None, ltp=None, previous_ltp=None, price_change=None,
            price_change_pct=None, oi=None, previous_oi=None, oi_change=None, oi_change_pct=None,
            volume=None, timestamp=None, available=False,
            error="Could not resolve the current NIFTY futures contract from the instrument master.",
        )

    try:
        quote = client.get_quote(resolved.security_id, NIFTY_FUTURES_SEGMENT)
    except DhanAPIError as exc:
        return FuturesSnapshot(
            symbol=resolved.symbol, expiry=resolved.expiry, ltp=None, previous_ltp=None,
            price_change=None, price_change_pct=None, oi=None, previous_oi=None,
            oi_change=None, oi_change_pct=None, volume=None, timestamp=None,
            available=False, error=str(exc),
        )

    def _f(key: str) -> Optional[float]:
        val = quote.get(key)
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    ltp = _f("last_price")
    oi = _f("oi")
    volume = _f("volume")
    # Candidate field names for aggregate pending order-book quantity —
    # tried defensively, same spirit as the instrument-master column
    # matching above; Dhan's quote schema has used both stylings.
    buy_quantity = _f("buy_quantity") or _f("total_buy_quantity")
    sell_quantity = _f("sell_quantity") or _f("total_sell_quantity")
    timestamp = now_ist().isoformat()

    prev = store.get_previous_futures_snapshot(timestamp)
    previous_ltp = prev.get("ltp") if prev else None
    previous_oi = prev.get("oi") if prev else None
    previous_buy_quantity = prev.get("buy_quantity") if prev else None
    previous_sell_quantity = prev.get("sell_quantity") if prev else None

    price_change = (ltp - previous_ltp) if (ltp is not None and previous_ltp is not None) else None
    price_change_pct = (price_change / previous_ltp * 100.0) if (price_change is not None and previous_ltp) else None
    oi_change = (oi - previous_oi) if (oi is not None and previous_oi is not None) else None
    oi_change_pct = (oi_change / previous_oi * 100.0) if (oi_change is not None and previous_oi) else None

    flow_proxy, flow_proxy_change, flow_proxy_available = compute_flow_proxy(
        buy_quantity, sell_quantity, previous_buy_quantity, previous_sell_quantity,
    )

    store.save_futures_snapshot(timestamp, ltp, oi, buy_quantity, sell_quantity)

    return FuturesSnapshot(
        symbol=resolved.symbol, expiry=resolved.expiry, ltp=ltp, previous_ltp=previous_ltp,
        price_change=price_change, price_change_pct=price_change_pct, oi=oi, previous_oi=previous_oi,
        oi_change=oi_change, oi_change_pct=oi_change_pct, volume=volume, timestamp=timestamp,
        available=ltp is not None,
        error=None if ltp is not None else "Quote payload did not include a usable last_price.",
        buy_quantity=buy_quantity, sell_quantity=sell_quantity,
        flow_proxy=flow_proxy, flow_proxy_change=flow_proxy_change, flow_proxy_available=flow_proxy_available,
    )
