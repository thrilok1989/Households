"""
positioning_engine.py

Classifies PROBABLE option positioning behaviour per leg (call/put) using
the standard price + change-in-OI heuristic, augmented with volume as a
confidence modifier. This is never "certain" — every result carries the
"probable positioning" label, per spec.

Standard heuristic (single-timeframe, comparing current LTP to previous
LTP implied by price direction, and OI change):

    Price UP   + OI UP    -> "Long buildup" (buying)      [bullish for that leg's buyer]
    Price DOWN + OI UP    -> "Short buildup" (writing)    [bearish for that leg's buyer]
    Price UP   + OI DOWN  -> "Short covering" (covering)
    Price DOWN + OI DOWN  -> "Long unwinding" (unwinding)

Applied per-leg (CE / PE) using that leg's own LTP change and ΔOI. We
don't have true tick-by-tick previous price here, so `price_change` must
be supplied by the caller (e.g. from consecutive snapshots); if
unavailable, the engine falls back to OI-change-only classification with
lower confidence, and says so explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from option_chain import OptionChainRow


class PositioningLabel(str, Enum):
    WRITING = "writing"
    BUYING = "buying"
    UNWINDING = "unwinding"
    COVERING = "covering"
    UNCLEAR = "unclear"


@dataclass
class LegPositioning:
    strike: float
    leg: str  # "CE" | "PE"
    label: PositioningLabel
    confidence: str  # "low" | "medium" | "high"
    rationale: str


def classify_leg(
    change_oi: float,
    volume: float,
    price_change: Optional[float],
    strike: float,
    leg: str,
) -> LegPositioning:
    if change_oi == 0:
        return LegPositioning(strike, leg, PositioningLabel.UNCLEAR, "low", "No material change in OI.")

    oi_up = change_oi > 0

    if price_change is None:
        # OI-only fallback — explicitly lower confidence, per spec's ban
        # on determining behaviour from OI alone where avoidable.
        label = PositioningLabel.WRITING if oi_up else PositioningLabel.COVERING
        return LegPositioning(
            strike, leg, label, "low",
            "Price change unavailable — classified from ΔOI only (low confidence).",
        )

    price_up = price_change > 0

    if price_up and oi_up:
        label, rationale = PositioningLabel.BUYING, "Price up + OI up -> probable long buildup (buying)."
    elif (not price_up) and oi_up:
        label, rationale = PositioningLabel.WRITING, "Price down + OI up -> probable short buildup (writing)."
    elif price_up and (not oi_up):
        label, rationale = PositioningLabel.COVERING, "Price up + OI down -> probable short covering."
    else:
        label, rationale = PositioningLabel.UNWINDING, "Price down + OI down -> probable long unwinding."

    confidence = "high" if volume > 0 and abs(change_oi) > 0 else "medium"
    return LegPositioning(strike, leg, label, confidence, rationale)


def run_positioning_engine(
    rows: list[OptionChainRow],
    prev_ltp_by_strike: Optional[dict[tuple[float, str], float]] = None,
) -> list[LegPositioning]:
    """
    prev_ltp_by_strike: optional {(strike, "CE"|"PE"): previous_ltp} from
    the last snapshot, used to derive price_change. If not supplied, all
    legs fall back to OI-only (low confidence) classification.
    """
    results: list[LegPositioning] = []
    for r in rows:
        ce_price_change = None
        pe_price_change = None
        if prev_ltp_by_strike and r.ce_ltp is not None:
            prev = prev_ltp_by_strike.get((r.strike, "CE"))
            if prev is not None:
                ce_price_change = r.ce_ltp - prev
        if prev_ltp_by_strike and r.pe_ltp is not None:
            prev = prev_ltp_by_strike.get((r.strike, "PE"))
            if prev is not None:
                pe_price_change = r.pe_ltp - prev

        results.append(classify_leg(r.ce_change_oi, r.ce_volume, ce_price_change, r.strike, "CE"))
        results.append(classify_leg(r.pe_change_oi, r.pe_volume, pe_price_change, r.strike, "PE"))
    return results


def summarize(results: list[LegPositioning]) -> dict:
    """
    Compact, JSON-serializable summary for snapshot storage — counts by
    label, and whether any classification actually had real price data
    (as opposed to falling back to the low-confidence OI-only path).
    """
    counts: dict[str, int] = {}
    used_price_data = False
    for r in results:
        counts[r.label.value] = counts.get(r.label.value, 0) + 1
        if r.confidence != "low":
            used_price_data = True
    return {"counts": counts, "used_price_data": used_price_data}


# ---------------------------------------------------------------------
# Confirmation-engine feed (V2.1 fix)
# ---------------------------------------------------------------------
# V2's confirmation engine scored option flow from raw ΔOI sign alone
# (CE ΔOI up -> "bearish", PE ΔOI up -> "bullish") — exactly the
# "ΔOI alone proves nothing" mistake this module's docstring warns
# against elsewhere. This aggregates the same price+OI+volume
# classifications used everywhere else in the app, so option-flow
# confirmation actually respects the same evidentiary bar.

@dataclass
class OptionFlowConfirmation:
    bullish_evidence: int
    bearish_evidence: int
    unclear_evidence: int
    label: str   # "BULLISH" | "BEARISH" | "MIXED / UNCLEAR" | "NO DATA"


# Interpretation mapping used broadly in Indian options-flow commentary:
# call writing / put buying skew bearish; call buying / put writing skew
# bullish; covering/unwinding are the milder, secondary-direction cases.
_BULLISH_LABELS = {
    ("CE", PositioningLabel.BUYING),
    ("PE", PositioningLabel.WRITING),
    ("PE", PositioningLabel.UNWINDING),
}
_BEARISH_LABELS = {
    ("CE", PositioningLabel.WRITING),
    ("CE", PositioningLabel.UNWINDING),
    ("PE", PositioningLabel.BUYING),
}
_MILD_BULLISH_LABELS = {("CE", PositioningLabel.COVERING)}
_MILD_BEARISH_LABELS = {("PE", PositioningLabel.COVERING)}


def aggregate_for_confirmation(
    results: list[LegPositioning], min_confidence: str = "medium"
) -> OptionFlowConfirmation:
    """
    Only counts classifications at or above `min_confidence` (default:
    excludes the "low"-confidence OI-only fallback — spec V2.1 §8:
    "Only classify when the evidence is sufficient. Otherwise: Mixed /
    Unclear."). UNCLEAR/low-confidence legs are tracked but never voted.
    """
    confidence_rank = {"low": 0, "medium": 1, "high": 2}
    threshold = confidence_rank.get(min_confidence, 1)

    bull = bear = unclear = 0
    for r in results:
        if r.label == PositioningLabel.UNCLEAR or confidence_rank.get(r.confidence, 0) < threshold:
            unclear += 1
            continue
        key = (r.leg, r.label)
        if key in _BULLISH_LABELS:
            bull += 1
        elif key in _BEARISH_LABELS:
            bear += 1
        elif key in _MILD_BULLISH_LABELS:
            bull += 1
        elif key in _MILD_BEARISH_LABELS:
            bear += 1
        else:
            unclear += 1

    if bull == 0 and bear == 0:
        label = "NO DATA" if (bull + bear + unclear) == 0 else "MIXED / UNCLEAR"
    elif bull > bear:
        label = "BULLISH"
    elif bear > bull:
        label = "BEARISH"
    else:
        label = "MIXED / UNCLEAR"

    return OptionFlowConfirmation(bullish_evidence=bull, bearish_evidence=bear, unclear_evidence=unclear, label=label)
