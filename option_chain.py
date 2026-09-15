"""
option_chain.py

Parses the raw Dhan option-chain payload into a typed, strike-sorted list
of OptionChainRow, and provides window-selection helpers (ATM ± N).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import NIFTY_LOT_SIZE


@dataclass
class OptionChainRow:
    strike: float

    ce_ltp: Optional[float]
    ce_oi: float
    ce_prev_oi: float
    ce_volume: float
    ce_iv: Optional[float]
    ce_delta: Optional[float]
    ce_gamma: Optional[float]
    ce_theta: Optional[float]
    ce_vega: Optional[float]

    pe_ltp: Optional[float]
    pe_oi: float
    pe_prev_oi: float
    pe_volume: float
    pe_iv: Optional[float]
    pe_delta: Optional[float]
    pe_gamma: Optional[float]
    pe_theta: Optional[float]
    pe_vega: Optional[float]

    @property
    def ce_change_oi(self) -> float:
        return self.ce_oi - self.ce_prev_oi

    @property
    def pe_change_oi(self) -> float:
        return self.pe_oi - self.pe_prev_oi


def _leg(leg_data: dict) -> dict:
    """Pull the fields we care about out of one ce/pe sub-dict, tolerating
    missing keys (Dhan omits `greeks` for illiquid/far strikes sometimes)."""
    greeks = leg_data.get("greeks") or {}
    return {
        "ltp": _to_float(leg_data.get("last_price")),
        "oi": _to_float(leg_data.get("oi"), default=0.0),
        "prev_oi": _to_float(leg_data.get("previous_oi"), default=0.0),
        "volume": _to_float(leg_data.get("volume"), default=0.0),
        "iv": _to_float(leg_data.get("implied_volatility")),
        "delta": _to_float(greeks.get("delta")),
        "gamma": _to_float(greeks.get("gamma")),
        "theta": _to_float(greeks.get("theta")),
        "vega": _to_float(greeks.get("vega")),
    }


def _to_float(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_option_chain(raw_payload: dict) -> tuple[Optional[float], list[OptionChainRow]]:
    """
    Returns (spot_last_price, sorted_rows).

    raw_payload is the dict returned by DhanClient.get_option_chain().
    """
    data = raw_payload.get("data") or {}
    spot = _to_float(data.get("last_price"))
    oc = data.get("oc") or {}

    rows: list[OptionChainRow] = []
    for strike_str, legs in oc.items():
        strike = _to_float(strike_str)
        if strike is None:
            continue
        ce = _leg(legs.get("ce") or {})
        pe = _leg(legs.get("pe") or {})
        rows.append(
            OptionChainRow(
                strike=strike,
                ce_ltp=ce["ltp"], ce_oi=ce["oi"], ce_prev_oi=ce["prev_oi"],
                ce_volume=ce["volume"], ce_iv=ce["iv"], ce_delta=ce["delta"],
                ce_gamma=ce["gamma"], ce_theta=ce["theta"], ce_vega=ce["vega"],
                pe_ltp=pe["ltp"], pe_oi=pe["oi"], pe_prev_oi=pe["prev_oi"],
                pe_volume=pe["volume"], pe_iv=pe["iv"], pe_delta=pe["delta"],
                pe_gamma=pe["gamma"], pe_theta=pe["theta"], pe_vega=pe["vega"],
            )
        )
    rows.sort(key=lambda r: r.strike)
    return spot, rows


def nearest_strike(rows: list[OptionChainRow], spot: float) -> Optional[float]:
    if not rows:
        return None
    return min((r.strike for r in rows), key=lambda s: abs(s - spot))


def strike_window(
    rows: list[OptionChainRow], spot: float, n_strikes: int
) -> list[OptionChainRow]:
    """
    Returns the rows for the n_strikes above and below the ATM strike
    (inclusive of ATM). If the chain doesn't have a uniform strike step,
    this still works because it operates on rank order, not distance.
    """
    if not rows:
        return []
    atm = nearest_strike(rows, spot)
    atm_idx = next(i for i, r in enumerate(rows) if r.strike == atm)
    lo = max(0, atm_idx - n_strikes)
    hi = min(len(rows), atm_idx + n_strikes + 1)
    return rows[lo:hi]
