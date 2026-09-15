"""
dealer_regime.py (V2)

Classifies the MODELED gamma regime and identifies pin zones / gamma
walls. Regime classification never rests on a single number — it
combines total modeled GEX, spot's distance from the (now spot-swept)
Gamma Flip, and local gamma concentration at the current spot, and it
always returns WHY it reached that conclusion (spec V2 §6, §31).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from config import RegimeThresholds
from gex_engine import GEXResult
from option_chain import nearest_strike


class GammaRegime(str, Enum):
    PIN_CHOP = "PIN / CHOP"
    EXPANSION = "EXPANSION"
    TRANSITION = "TRANSITION"
    MIXED = "MIXED"


REGIME_EXPECTATION = {
    GammaRegime.PIN_CHOP: (
        "Estimated hedging pressure may dampen movement: mean reversion, pinning, chop, "
        "rejection from extremes."
    ),
    GammaRegime.EXPANSION: (
        "Estimated hedging pressure may amplify movement: directional expansion, faster "
        "moves, larger intraday swings, breakout continuation risk."
    ),
    GammaRegime.TRANSITION: (
        "Spot is close to the modeled Gamma Flip, or modeled gamma is near zero — the "
        "market can transition rapidly between chop and expansion."
    ),
    GammaRegime.MIXED: (
        "The aggregate modeled GEX sign and the local gamma at the current strike disagree "
        "— the dealer-exposure model itself is giving conflicting signals here."
    ),
}


class FlipProximity(str, Enum):
    ABOVE = "🟢 ABOVE FLIP"
    BELOW = "🔴 BELOW FLIP"
    NEAR = "🟡 NEAR FLIP"
    UNKNOWN = "⚪ FLIP UNKNOWN"


@dataclass
class PinZone:
    low: float
    high: float
    peak_strike: float
    peak_gex: float


@dataclass
class GammaWalls:
    upper_wall: Optional[float]
    lower_wall: Optional[float]


@dataclass
class RegimeResult:
    regime: GammaRegime
    expectation: str
    reasons: list[str]                 # explicit "WHY" — spec requirement
    flip_proximity: FlipProximity
    distance_from_flip: Optional[float]
    pin_zone: Optional[PinZone]
    walls: GammaWalls


def _local_gamma_at_spot(gex_result: GEXResult) -> Optional[float]:
    """Total GEX at the strike nearest current spot — used to sanity-check
    the aggregate sign against what's actually happening right where
    price is (spec V2 §6: 'local gamma concentration')."""
    if not gex_result.strike_gex:
        return None
    strikes = [s.strike for s in gex_result.strike_gex]
    atm = min(strikes, key=lambda s: abs(s - gex_result.spot))
    match = next((s for s in gex_result.strike_gex if s.strike == atm), None)
    return match.total_gex if match else None


def classify_flip_proximity(
    gex_result: GEXResult, thresholds: RegimeThresholds
) -> tuple[FlipProximity, Optional[float]]:
    flip = gex_result.gamma_flip
    if flip is None:
        return FlipProximity.UNKNOWN, None

    distance = gex_result.spot - flip
    pct_threshold = thresholds.flip_proximity_pct * gex_result.spot
    proximity_threshold = max(thresholds.flip_proximity_points, pct_threshold)

    if abs(distance) <= proximity_threshold:
        return FlipProximity.NEAR, distance
    return (FlipProximity.ABOVE if distance > 0 else FlipProximity.BELOW), distance


def classify_gamma_regime(
    gex_result: GEXResult, thresholds: RegimeThresholds
) -> tuple[GammaRegime, list[str]]:
    """
    Returns (regime, reasons). Combines:
      1. Spot proximity to Gamma Flip (point AND percentage threshold)
      2. Total modeled GEX magnitude vs the configurable transition band
      3. Local gamma sign (at the strike nearest spot) vs aggregate sign
    Never decided from a single one of these in isolation.
    """
    reasons: list[str] = []
    flip_proximity, distance = classify_flip_proximity(gex_result, thresholds)

    if flip_proximity == FlipProximity.NEAR:
        reasons.append(f"Spot is within the configured proximity band of the modeled Gamma Flip "
                        f"({distance:+.0f} pts).")
        return GammaRegime.TRANSITION, reasons

    if abs(gex_result.total_gex) <= thresholds.gex_transition_band_lakh * 100_000:
        reasons.append("Total modeled GEX magnitude is inside the configured transition band "
                        "(too small to call a clean regime).")
        return GammaRegime.TRANSITION, reasons

    local_gamma = _local_gamma_at_spot(gex_result)
    if local_gamma is not None and (local_gamma > 0) != (gex_result.total_gex > 0):
        reasons.append("Aggregate modeled GEX sign and local gamma at the nearest strike to spot "
                        "disagree — the model is internally conflicted at this snapshot.")
        return GammaRegime.MIXED, reasons

    if gex_result.total_gex > 0:
        reasons.append("Total modeled GEX is positive and local gamma at spot agrees; "
                        "spot is not near the modeled Gamma Flip.")
        return GammaRegime.PIN_CHOP, reasons

    reasons.append("Total modeled GEX is negative and local gamma at spot agrees; "
                    "spot is not near the modeled Gamma Flip.")
    return GammaRegime.EXPANSION, reasons


def find_pin_zone(gex_result: GEXResult, thresholds: RegimeThresholds) -> Optional[PinZone]:
    """
    Pin zone = a band around the strike with the largest POSITIVE total
    GEX (the strike dealers are most likely to defend / pin toward under
    a dampening hedging model). Only meaningful in a PIN/CHOP regime, but
    computed regardless — caller decides whether to display it.
    """
    if not gex_result.strike_gex:
        return None
    peak = max(gex_result.strike_gex, key=lambda s: s.total_gex)
    if peak.total_gex <= 0:
        return None
    half = thresholds.pin_zone_width_points / 2
    return PinZone(low=peak.strike - half, high=peak.strike + half, peak_strike=peak.strike, peak_gex=peak.total_gex)


def find_gamma_walls(gex_result: GEXResult) -> GammaWalls:
    """
    Upper/Lower Gamma Wall: largest-magnitude positive-GEX concentrations
    above/below spot. Requires magnitude >= 10% of the single largest
    |GEX| strike in the chain so we never label every high-OI strike a
    "wall" (explicit spec prohibition).
    """
    if not gex_result.strike_gex:
        return GammaWalls(None, None)

    max_abs = max(abs(s.total_gex) for s in gex_result.strike_gex) or 1.0
    material = [s for s in gex_result.strike_gex if abs(s.total_gex) >= 0.10 * max_abs]

    above = [s for s in material if s.strike > gex_result.spot and s.total_gex > 0]
    below = [s for s in material if s.strike < gex_result.spot and s.total_gex > 0]

    upper = min(above, key=lambda s: s.strike).strike if above else None
    lower = max(below, key=lambda s: s.strike).strike if below else None
    return GammaWalls(upper_wall=upper, lower_wall=lower)


def run_dealer_regime(gex_result: GEXResult, thresholds: RegimeThresholds) -> RegimeResult:
    regime, reasons = classify_gamma_regime(gex_result, thresholds)
    flip_proximity, distance = classify_flip_proximity(gex_result, thresholds)
    return RegimeResult(
        regime=regime,
        expectation=REGIME_EXPECTATION[regime],
        reasons=reasons,
        flip_proximity=flip_proximity,
        distance_from_flip=distance,
        pin_zone=find_pin_zone(gex_result, thresholds),
        walls=find_gamma_walls(gex_result),
    )
