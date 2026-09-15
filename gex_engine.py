"""
gex_engine.py (V2)

Calculates MODELED dealer Gamma Exposure (GEX) from option-chain gamma +
OI, and derives Gamma Flip as a genuinely spot-dependent quantity rather
than reading it off a single cumulative-strike sum.

SIGN CONVENTION (a MODEL ASSUMPTION, not an observed fact — documented
explicitly, as required):

    strike_call_gex =  +(Gamma_CE * OI_CE * LOT_SIZE * Spot^2 * 0.01)
    strike_put_gex  =  -(Gamma_PE * OI_PE * LOT_SIZE * Spot^2 * 0.01)
    strike_total_gex = strike_call_gex + strike_put_gex

    This follows the widely-replicated public "dealers assumed net long
    calls / net short puts" convention. Option-chain OI does NOT tell us
    who actually holds which side — this is a modeling choice, and every
    number derived from it is labeled "Modeled Dealer GEX", never
    "Dealer GEX" or "actual dealer position".

    Total current GEX > 0  -> regime candidate: PIN / CHOP (dampening)
    Total current GEX < 0  -> regime candidate: EXPANSION (amplifying)

UNITS: raw values are rupee-notional-per-1%-spot-move at the SPOT USED IN
THE CALCULATION. Display layer converts to lakh (L) / crore (Cr) via
utils.format_inr_lakh_crore().

--------------------------------------------------------------------
GAMMA FLIP V2 — WHY IT CHANGED
--------------------------------------------------------------------
V1 found the level where the CUMULATIVE SUM of strike-level GEX (computed
at the CURRENT spot) changed sign as you walked up the strike ladder.
That is a static snapshot artifact, not a real "flip" — it doesn't tell
you where aggregate GEX would actually be zero if spot itself moved,
because gamma itself changes as spot moves (gamma is a function of
moneyness).

V2 instead:
    1. Sweeps a range of HYPOTHETICAL spot values around the current
       spot (config.REGIME_THRESHOLDS.flip_search_range_points, in
       flip_search_step_points increments).
    2. At each hypothetical spot S, recomputes gamma for every strike
       from Black-Scholes (using each leg's OWN implied volatility and
       time-to-expiry, held fixed — OI is also held fixed, since this is
       a "what would aggregate gamma be if spot were here" model, not a
       forecast of how OI/IV would actually respond).
    3. Aggregates modeled GEX(S) the same way as the current-spot
       calculation, at that hypothetical S.
    4. Finds where the resulting GEX(S) curve crosses zero (there can be
       more than one crossing) via linear interpolation between grid
       points, and reports the crossing NEAREST the current spot as the
       primary Gamma Flip, plus the full list of crossings.

This requires implied volatility per leg (from Dhan, or estimatable) and
time-to-expiry — see gamma_flip_v2().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config import NIFTY_LOT_SIZE, RegimeThresholds
from greeks import black_scholes_greeks
from option_chain import OptionChainRow


@dataclass
class StrikeGEX:
    strike: float
    ce_gex: float
    pe_gex: float
    total_gex: float


@dataclass
class GammaFlipResult:
    primary: Optional[float]            # nearest-to-spot zero crossing
    all_crossings: list[float]          # every zero crossing found in the swept range
    curve: list[tuple[float, float]]    # [(hypothetical_spot, aggregate_gex), ...] for charting
    swept: bool                         # False if the sweep couldn't run (e.g. no IV data)
    note: str = ""                      # explains why swept=False, or any caveat


@dataclass
class GEXResult:
    spot: float
    strike_gex: list[StrikeGEX]
    total_gex: float
    gamma_flip: Optional[float]         # convenience alias for gamma_flip_result.primary
    gamma_flip_result: GammaFlipResult
    lot_size: int


def _spot_factor(spot: float) -> float:
    return (spot ** 2) * 0.01


def calculate_strike_gex(
    rows: list[OptionChainRow], spot: float, lot_size: int = NIFTY_LOT_SIZE
) -> list[StrikeGEX]:
    """Modeled GEX per strike AT THE GIVEN (actual, current) spot, using
    whatever gamma is already on each row (API value or BS fallback —
    see greeks.fill_missing_greeks, applied upstream in app.py)."""
    factor = _spot_factor(spot)
    out: list[StrikeGEX] = []
    for r in rows:
        ce_gamma = r.ce_gamma or 0.0
        pe_gamma = r.pe_gamma or 0.0
        ce_gex = ce_gamma * r.ce_oi * lot_size * factor
        pe_gex = -1.0 * pe_gamma * r.pe_oi * lot_size * factor
        out.append(StrikeGEX(strike=r.strike, ce_gex=ce_gex, pe_gex=pe_gex, total_gex=ce_gex + pe_gex))
    return out


def _aggregate_gex_at_hypothetical_spot(
    rows: list[OptionChainRow],
    hypothetical_spot: float,
    tte_years: float,
    lot_size: int,
) -> Optional[float]:
    """
    Recomputes gamma for every leg via Black-Scholes AT hypothetical_spot
    (holding each leg's own IV, strike, and TTE fixed) and aggregates
    modeled GEX the same way as calculate_strike_gex. Returns None if no
    leg has usable IV (nothing to compute).
    """
    factor = _spot_factor(hypothetical_spot)
    total = 0.0
    any_computed = False

    for r in rows:
        if r.ce_iv:
            g = black_scholes_greeks(hypothetical_spot, r.strike, tte_years, r.ce_iv / 100.0, "CE")
            if g:
                total += g.gamma * r.ce_oi * lot_size * factor
                any_computed = True
        if r.pe_iv:
            g = black_scholes_greeks(hypothetical_spot, r.strike, tte_years, r.pe_iv / 100.0, "PE")
            if g:
                total -= g.gamma * r.pe_oi * lot_size * factor
                any_computed = True

    return total if any_computed else None


def find_zero_crossings(curve: list[tuple[float, float]]) -> list[float]:
    """
    Given an ordered [(x, y), ...] curve, returns the x-values where y
    crosses zero, via linear interpolation between bracketing points.
    Factored out from gamma_flip_v2 so the crossing-detection logic can
    be unit-tested directly against synthetic curves without depending
    on Black-Scholes evaluation.
    """
    crossings: list[float] = []
    for i in range(1, len(curve)):
        x_prev, y_prev = curve[i - 1]
        x_curr, y_curr = curve[i]
        if y_prev == 0:
            crossings.append(x_prev)
            continue
        if (y_prev < 0) != (y_curr < 0):
            frac = abs(y_prev) / (abs(y_prev) + abs(y_curr))
            crossings.append(x_prev + frac * (x_curr - x_prev))
    return crossings


def gamma_flip_v2(
    rows: list[OptionChainRow],
    spot: float,
    tte_years: float,
    thresholds: RegimeThresholds,
    lot_size: int = NIFTY_LOT_SIZE,
) -> GammaFlipResult:
    """
    Sweeps hypothetical spot in
    [spot - flip_search_range_points, spot + flip_search_range_points]
    at flip_search_step_points increments, evaluates modeled aggregate
    GEX at each point, and finds zero crossings via linear interpolation.
    Returns the crossing nearest current spot as `.primary`.
    """
    if tte_years <= 0 or not rows:
        return GammaFlipResult(None, [], [], swept=False, note="No time-to-expiry / no strikes available.")

    lo = spot - thresholds.flip_search_range_points
    hi = spot + thresholds.flip_search_range_points
    step = max(thresholds.flip_search_step_points, 1.0)

    curve: list[tuple[float, float]] = []
    s = lo
    while s <= hi + 1e-9:
        val = _aggregate_gex_at_hypothetical_spot(rows, s, tte_years, lot_size)
        if val is not None:
            curve.append((s, val))
        s += step

    if len(curve) < 2:
        return GammaFlipResult(None, [], curve, swept=False, note="Insufficient IV data to sweep hypothetical spot.")

    crossings = find_zero_crossings(curve)

    if not crossings:
        return GammaFlipResult(None, [], curve, swept=True, note="No zero crossing found in swept range.")

    primary = min(crossings, key=lambda c: abs(c - spot))
    return GammaFlipResult(primary, crossings, curve, swept=True)


def run_gex_engine(
    rows: list[OptionChainRow],
    spot: float,
    tte_years: float,
    thresholds: RegimeThresholds,
    lot_size: int = NIFTY_LOT_SIZE,
) -> GEXResult:
    strike_gex = calculate_strike_gex(rows, spot, lot_size)
    total = sum(sg.total_gex for sg in strike_gex)
    flip_result = gamma_flip_v2(rows, spot, tte_years, thresholds, lot_size)
    return GEXResult(
        spot=spot,
        strike_gex=strike_gex,
        total_gex=total,
        gamma_flip=flip_result.primary,
        gamma_flip_result=flip_result,
        lot_size=lot_size,
    )
