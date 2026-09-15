"""
market_data.py

Spot price retrieval + data-quality/staleness tracking. Every value the
rest of the app uses is wrapped in a DataPoint so consumers can check
`.is_stale` before trusting it for a signal, per the "never calculate a
strong signal from stale or incomplete data" requirement.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from config import REGIME_THRESHOLDS
from dhan_client import DhanClient, DhanAPIError
from utils import is_market_open, now_ist


class DataAge(str, Enum):
    LIVE = "🟢 LIVE"
    DELAYED = "🟡 DELAYED"
    STALE = "🔴 STALE"
    NONE = "⚪ NO DATA"


class FieldStatus(str, Enum):
    """
    Per-field status for the expanded Data Health panel (spec V2.1 §20):
    futures, futures OI, option LTP/OI/ΔOI, volume, CVD, VWAP, levels.
    UNAVAILABLE means "we never had this data" (e.g. CVD); STALE means
    "we had it, but it's too old to trust"; ERROR means "the last fetch
    attempt failed." None of these ever get silently rendered as 0.
    """
    LIVE = "LIVE"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


def classify_field_status(
    value_present: bool, is_stale: bool = False, error: Optional[str] = None
) -> FieldStatus:
    """Small shared helper so every field's status is derived the same
    way rather than each caller inventing its own if/else."""
    if error:
        return FieldStatus.ERROR
    if not value_present:
        return FieldStatus.UNAVAILABLE
    if is_stale:
        return FieldStatus.STALE
    return FieldStatus.LIVE


@dataclass
class DataPoint:
    value: Optional[float]
    fetched_at: float
    ok: bool
    error: Optional[str] = None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.fetched_at

    @property
    def is_stale(self) -> bool:
        if not self.ok:
            return True
        return self.age_seconds > REGIME_THRESHOLDS.stale_data_seconds

    @property
    def age_label(self) -> DataAge:
        if not self.ok:
            return DataAge.NONE
        age = self.age_seconds
        if age <= REGIME_THRESHOLDS.live_data_seconds:
            return DataAge.LIVE
        if age <= REGIME_THRESHOLDS.delayed_data_seconds:
            return DataAge.DELAYED
        return DataAge.STALE

    def age_display(self) -> str:
        if not self.ok:
            return "no data"
        return f"{self.age_label.value} — {self.age_seconds:.0f} sec old"


@dataclass
class DataHealth:
    dhan_connected: bool
    spot_ok: bool
    option_chain_ok: bool
    greeks_ok: bool
    futures_ok: bool
    fii_dii_ok: bool
    market_open: bool
    last_update: Optional[str]
    spot_age: DataAge = DataAge.NONE
    spot_age_display: str = "no data"
    issues: list[str] = field(default_factory=list)
    # V2.1 — granular per-field status (futures, futures OI, option
    # LTP/OI/ΔOI, volume, CVD, VWAP, levels). Additive: existing
    # booleans above are untouched so V2 callers keep working.
    field_status: dict[str, FieldStatus] = field(default_factory=dict)

    def status_icon(self, ok: bool) -> str:
        return "🟢" if ok else "🔴"

    @property
    def data_ok(self) -> bool:
        """Overall health flag used by signal_engine.detect_alerts to
        raise DATA_STALE/DATA_RECOVERY events. Deliberately conservative:
        any ERROR field, or spot/option-chain trouble, fails this."""
        if not (self.spot_ok and self.option_chain_ok):
            return False
        return not any(v == FieldStatus.ERROR for v in self.field_status.values())


def fetch_spot(client: DhanClient) -> DataPoint:
    try:
        price = client.get_spot_ltp()
        return DataPoint(value=price, fetched_at=time.time(), ok=True)
    except DhanAPIError as exc:
        return DataPoint(value=None, fetched_at=time.time(), ok=False, error=str(exc))


def detect_data_issues(chain_rows: list, spot: DataPoint) -> list[str]:
    """
    Scans option-chain rows and spot for the specific quality problems
    called out in the spec: stale data, missing strikes, zero OI across
    the board, impossible Greeks, duplicate timestamps.
    """
    issues: list[str] = []

    if spot.is_stale:
        issues.append("Spot price is stale.")

    if not chain_rows:
        issues.append("Option chain returned no strikes.")
        return issues

    if all((r.ce_oi == 0 and r.pe_oi == 0) for r in chain_rows):
        issues.append("Zero OI across all strikes — likely bad snapshot.")

    for r in chain_rows:
        if r.ce_delta is not None and not (-1.0 <= r.ce_delta <= 1.0):
            issues.append(f"Impossible CE delta at strike {r.strike}: {r.ce_delta}")
        if r.pe_delta is not None and not (-1.0 <= r.pe_delta <= 1.0):
            issues.append(f"Impossible PE delta at strike {r.strike}: {r.pe_delta}")
        if r.ce_gamma is not None and r.ce_gamma < 0:
            issues.append(f"Negative CE gamma at strike {r.strike} (gamma must be >= 0).")
        if r.pe_gamma is not None and r.pe_gamma < 0:
            issues.append(f"Negative PE gamma at strike {r.strike} (gamma must be >= 0).")

    strikes = [r.strike for r in chain_rows]
    if len(strikes) != len(set(strikes)):
        issues.append("Duplicate strikes detected in option-chain snapshot.")

    return issues


def build_data_health(
    dhan_connected: bool,
    spot: DataPoint,
    chain_rows: list,
    futures_available: bool,
    fii_dii_available: bool,
    futures_snapshot=None,           # Optional[futures_data.FuturesSnapshot] — V2.1
    cvd_status=None,                  # Optional[confirmation_engine.CVDStatus] — V2.1
    levels_available: Optional[bool] = None,   # V2.1
    vwap_available: Optional[bool] = None,      # V2.1
) -> DataHealth:
    market_open = is_market_open()
    issues = detect_data_issues(chain_rows, spot)

    field_status: dict[str, FieldStatus] = {
        "Spot": classify_field_status(spot.ok, spot.is_stale),
        "Option Chain": classify_field_status(bool(chain_rows), False),
        "Option LTP": classify_field_status(
            bool(chain_rows) and any(r.ce_ltp is not None or r.pe_ltp is not None for r in chain_rows)
        ),
        "Option OI": classify_field_status(
            bool(chain_rows) and any((r.ce_oi or r.pe_oi) for r in chain_rows)
        ),
        "Option ΔOI": classify_field_status(
            bool(chain_rows) and any((r.ce_change_oi or r.pe_change_oi) for r in chain_rows)
        ),
        "Volume": classify_field_status(
            bool(chain_rows) and any((r.ce_volume or r.pe_volume) for r in chain_rows)
        ),
    }

    if futures_snapshot is not None:
        field_status["Futures LTP"] = classify_field_status(
            futures_snapshot.ltp is not None, False, futures_snapshot.error if not futures_snapshot.available else None,
        )
        field_status["Futures OI"] = classify_field_status(
            futures_snapshot.oi is not None, False, futures_snapshot.error if not futures_snapshot.available else None,
        )
        field_status["Futures ΔOI"] = classify_field_status(futures_snapshot.oi_change is not None)
        # Order-book flow proxy — see futures_data.compute_flow_proxy and
        # confirmation_engine.CVDStatus. Explicitly NOT "Cash Flow/CVD".
        field_status["Futures Flow (Proxy)"] = classify_field_status(futures_snapshot.flow_proxy_available)
    else:
        field_status["Futures LTP"] = FieldStatus.UNAVAILABLE
        field_status["Futures OI"] = FieldStatus.UNAVAILABLE
        field_status["Futures ΔOI"] = FieldStatus.UNAVAILABLE
        field_status["Futures Flow (Proxy)"] = FieldStatus.UNAVAILABLE

    # Structural fact, not a Dhan limitation: NIFTY spot is an INDEX
    # VALUE, not a traded instrument — it has no order book, no traded
    # volume, and no aggressor-side flow to measure. These are always
    # UNAVAILABLE, and deliberately kept separate from the Futures flow
    # proxy above so the two are never confused with each other.
    field_status["Cash Volume"] = FieldStatus.UNAVAILABLE
    field_status["Cash Flow/CVD"] = FieldStatus.UNAVAILABLE

    if cvd_status is not None:
        field_status["CVD"] = FieldStatus.LIVE if str(cvd_status.value) != "Unavailable" else FieldStatus.UNAVAILABLE
    else:
        field_status["CVD"] = FieldStatus.UNAVAILABLE

    field_status["VWAP"] = classify_field_status(bool(vwap_available))
    field_status["Levels"] = classify_field_status(bool(levels_available))

    return DataHealth(
        dhan_connected=dhan_connected,
        spot_ok=spot.ok and not spot.is_stale,
        option_chain_ok=bool(chain_rows) and not any("Zero OI" in i for i in issues),
        greeks_ok=bool(chain_rows) and not any("Impossible" in i or "Negative" in i for i in issues),
        futures_ok=futures_available,
        fii_dii_ok=fii_dii_available,
        market_open=market_open,
        last_update=now_ist().strftime("%H:%M:%S") if spot.ok else None,
        spot_age=spot.age_label,
        spot_age_display=spot.age_display(),
        issues=issues,
        field_status=field_status,
    )
