"""
signal_engine.py (V2)

Ties everything together:
  1. Estimated Hedging Pressure model (DAMPENING / AMPLIFYING / MIXED /
     TRANSITION), now considering GEX, DEX, Gamma Flip distance, and
     local gamma/OI concentration together rather than gamma regime alone.
  2. Final State — a transparent MATRIX (spec V2 §14), never a bare
     buy/sell signal. Dealer environment alone never produces a
     directional call; missing or conflicting confirmation always
     resolves to a WAIT state (§15/§18 "NO FOMO").
  3. Categorical alignment (LOW / MEDIUM / HIGH) — false precision like
     "72% chance" is explicitly banned (§18) until enough historical
     outcome data exists to validate a number statistically.
  4. Alert engine, comparing consecutive snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from config import RegimeThresholds
from confirmation_engine import Confirmation, ConfirmationResult, FamilyStatus
from dealer_regime import GammaRegime, RegimeResult
from dex_engine import DEXResult, ModeledDeltaBalance


class HedgingEnvironment(str, Enum):
    DAMPENING = "DAMPENING"
    AMPLIFYING = "AMPLIFYING"
    MIXED = "MIXED"
    TRANSITION = "TRANSITION"


def run_hedging_model(
    regime: RegimeResult, dex: DEXResult, delta_balance: ModeledDeltaBalance
) -> tuple[HedgingEnvironment, list[str]]:
    """
    Returns (environment, reasons). Considers:
      - Gamma regime (PIN/CHOP vs EXPANSION vs TRANSITION vs MIXED)
      - Whether DEX's lean is one-sided enough to fight a dampening read
      - Regime's own MIXED/TRANSITION flags (passed straight through,
        since those already encode "the model disagrees with itself" /
        "too close to the flip to say")
    """
    reasons: list[str] = list(regime.reasons)

    if regime.regime == GammaRegime.TRANSITION:
        reasons.append("Gamma regime is in transition — hedging environment inherits that uncertainty.")
        return HedgingEnvironment.TRANSITION, reasons

    if regime.regime == GammaRegime.MIXED:
        reasons.append("Gamma regime itself is mixed — hedging environment inherits that conflict.")
        return HedgingEnvironment.MIXED, reasons

    gamma_says_dampen = regime.regime == GammaRegime.PIN_CHOP

    gross = abs(dex.total_ce_dex) + abs(dex.total_pe_dex)
    # "one-sided" = net delta exposure is a large fraction of gross
    # exposure (i.e. NOT well-hedged/balanced between calls and puts) —
    # a heavily skewed book is harder to reconcile with a dampening
    # read, so we flag it as MIXED rather than forcing DAMPENING.
    one_sided = gross > 0 and (abs(dex.net_dex) / gross) > 0.5
    if gamma_says_dampen and one_sided:
        reasons.append(
            f"Gamma regime implies dampening, but modeled delta exposure is heavily one-sided "
            f"({delta_balance.label}) — treating as MIXED rather than forcing a single bucket."
        )
        return HedgingEnvironment.MIXED, reasons

    if gamma_says_dampen:
        reasons.append("Gamma regime is PIN/CHOP and delta exposure is not one-sided enough to override it.")
        return HedgingEnvironment.DAMPENING, reasons

    reasons.append("Gamma regime is EXPANSION.")
    return HedgingEnvironment.AMPLIFYING, reasons


@dataclass
class FinalState:
    headline: str
    detail: str
    requires_wait: bool


def combine_final_state(
    regime: RegimeResult, confirmation: ConfirmationResult
) -> FinalState:
    """
    Implements the explicit Final State Matrix (spec V2 §14). Dealer
    environment alone NEVER produces a directional call — every branch
    that lacks confirmation, or has conflicting confirmation, resolves to
    a WAIT state.

        PIN + NO CONFIRMATION        -> WAIT / NO TRADE
        PIN + BEARISH CONFIRMATION   -> BEARISH LEAN — WAIT FOR STRUCTURAL BREAK
        PIN + BULLISH CONFIRMATION   -> BULLISH LEAN — WAIT FOR CONFIRMATION
        EXPANSION + BEARISH          -> BEARISH EXPANSION
        EXPANSION + BULLISH          -> BULLISH EXPANSION
        EXPANSION + CONFLICT         -> HIGH VOLATILITY / WAIT
        TRANSITION (any)             -> WAIT FOR REGIME CONFIRMATION
        MIXED (any)                  -> WAIT — MIXED DEALER SIGNALS
        PIN + CONFLICT               -> WAIT / NO TRADE (conflicting confirmation never resolves alone)
    """
    if regime.regime == GammaRegime.TRANSITION:
        return FinalState(
            "WAIT FOR REGIME CONFIRMATION",
            "Spot is close to the modeled Gamma Flip — the dealer environment itself is unstable "
            "right now, independent of what the market is doing.",
            True,
        )

    if regime.regime == GammaRegime.MIXED:
        return FinalState(
            "WAIT — MIXED DEALER SIGNALS",
            "The dealer-exposure model is internally conflicted at this snapshot (aggregate vs "
            "local gamma disagree). Treat any directional read with extra caution.",
            True,
        )

    if regime.regime == GammaRegime.PIN_CHOP:
        if confirmation.verdict == Confirmation.NONE:
            return FinalState("WAIT / NO TRADE", "PIN/CHOP dealer environment with no market confirmation yet.", True)
        if confirmation.verdict == Confirmation.CONFLICTING:
            return FinalState("WAIT / NO TRADE", "PIN/CHOP dealer environment; market confirmation signals disagree with each other.", True)
        if confirmation.verdict == Confirmation.BEARISH:
            return FinalState(
                "BEARISH LEAN — WAIT FOR STRUCTURAL BREAK",
                "Estimated hedging pressure may dampen the move even though price is confirming bearish.",
                True,
            )
        if confirmation.verdict == Confirmation.BULLISH:
            return FinalState(
                "BULLISH LEAN — WAIT FOR CONFIRMATION",
                "Estimated hedging pressure may dampen the move even though price is confirming bullish.",
                True,
            )

    if regime.regime == GammaRegime.EXPANSION:
        if confirmation.verdict == Confirmation.CONFLICTING:
            return FinalState(
                "HIGH VOLATILITY / WAIT",
                "EXPANSION dealer environment with conflicting market confirmation — hedging could "
                "amplify a move in either direction; no clean read available.",
                True,
            )
        if confirmation.verdict == Confirmation.NONE:
            return FinalState("WAIT / NO TRADE", "EXPANSION dealer environment with no market confirmation yet.", True)
        if confirmation.verdict == Confirmation.BEARISH:
            return FinalState(
                "BEARISH EXPANSION",
                "Estimated hedging pressure may amplify the confirmed bearish move.",
                False,
            )
        if confirmation.verdict == Confirmation.BULLISH:
            return FinalState(
                "BULLISH EXPANSION",
                "Estimated hedging pressure may amplify the confirmed bullish move.",
                False,
            )

    return FinalState("WAIT / NO TRADE", "No clean combination reached; defaulting to caution.", True)


class Alignment(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


def alignment_score(
    regime: RegimeResult,
    delta_balance: ModeledDeltaBalance,
    confirmation: ConfirmationResult,
) -> tuple[Alignment, str]:
    """
    Counts how many of the independent signal families (gamma regime
    direction, modeled delta balance, market confirmation) agree on one
    direction, and returns a CATEGORICAL label — never a percentage —
    per the explicit ban on false precision (spec V2 §18). Only gamma
    EXPANSION carries a direction (dampening regimes don't vote a
    direction by themselves, they only vote "muted").
    """
    bull_votes = 0
    bear_votes = 0
    voting_families = 0

    if regime.regime == GammaRegime.EXPANSION:
        voting_families += 1
        # Expansion doesn't have its own bull/bear lean; it amplifies
        # whatever confirmation says, so it agrees with confirmation by
        # construction when paired — don't double count here.

    if delta_balance.lean != "none":
        voting_families += 1
        if delta_balance.lean == "bullish":
            bull_votes += 1
        else:
            bear_votes += 1

    if confirmation.verdict in (Confirmation.BULLISH, Confirmation.BEARISH):
        voting_families += 1
        if confirmation.verdict == Confirmation.BULLISH:
            bull_votes += 1
        else:
            bear_votes += 1

    agreeing = max(bull_votes, bear_votes)
    total_directional = bull_votes + bear_votes

    label_note = (
        "Counts agreement across independent signal families. NOT a probability of market "
        "direction — until enough historical outcome data exists to validate one statistically, "
        "alignment reflects signal agreement only."
    )

    if voting_families == 0 or total_directional == 0:
        return Alignment.LOW, label_note
    if agreeing == total_directional and voting_families >= 2:
        return Alignment.HIGH, label_note
    if agreeing >= 1 and total_directional >= 1 and agreeing / max(total_directional, 1) >= 0.5:
        return Alignment.MEDIUM, label_note
    return Alignment.LOW, label_note


# ---------------------------------------------------------------------
# Cross-layer confirmation strength (V2.1.1) — the "Dealer + Cash +
# Futures + Flow" combination the spec calls for, kept OUTSIDE
# confirmation_engine.py on purpose: confirmation_engine deliberately
# never imports dealer_regime/gex_engine (see its module docstring and
# the architecture-boundary test in tests/test_calculations.py), so the
# combination that includes the dealer model's own directional read
# lives here instead, where both sides are already in scope.
# ---------------------------------------------------------------------

class ConfirmationStrength(str, Enum):
    FULL_BULLISH = "FULL BULLISH CONFIRMATION"
    FULL_BEARISH = "FULL BEARISH CONFIRMATION"
    PARTIAL_BULLISH = "PARTIAL BULLISH CONFIRMATION"
    PARTIAL_BEARISH = "PARTIAL BEARISH CONFIRMATION"
    CONFLICT = "CONFLICT"
    INSUFFICIENT_DATA = "INSUFFICIENT DATA"


@dataclass
class ConfirmationScore:
    strength: ConfirmationStrength
    components: dict[str, str]   # e.g. {"Dealer": "BEARISH", "Cash": "BEARISH", "Futures": "UNAVAILABLE", "Flow": "UNAVAILABLE"}


def _family_to_direction(status: Optional[FamilyStatus]) -> str:
    if status is None or status == FamilyStatus.UNAVAILABLE:
        return "UNAVAILABLE"
    if status == FamilyStatus.BULLISH:
        return "BULLISH"
    if status == FamilyStatus.BEARISH:
        return "BEARISH"
    return "NEUTRAL"


def classify_confirmation_strength(
    delta_balance: ModeledDeltaBalance, confirmation: ConfirmationResult
) -> ConfirmationScore:
    """
    Combines four components — every one of them individually visible in
    `.components`, per spec V2.1.1 §11 ("every component must be
    visible"):
        Dealer  <- Modeled Delta Balance lean (bullish/bearish/none)
        Cash    <- confirmation's "Cash Market" family (real spot
                   price change + levels — this IS the cash-market read;
                   spec §12 of the prior message explicitly forbids
                   building a second, duplicate spot pipeline for this)
        Futures <- confirmation's "Futures OI" family (real futures
                   price+OI positioning)
        Flow    <- confirmation's "CVD" family (real flow proxy, or
                   UNAVAILABLE — see confirmation_engine.CVDStatus)

    FULL requires ALL FOUR components available AND agreeing.
    PARTIAL requires at least one available component agreeing, with at
    least one other component unavailable or neutral (never "full" on
    partial evidence — spec's explicit example).
    CONFLICT is any disagreement between available directional components.
    INSUFFICIENT_DATA is no directional signal from any component.
    """
    components = {
        "Dealer": {"bullish": "BULLISH", "bearish": "BEARISH", "none": "NEUTRAL"}[delta_balance.lean],
        "Cash": _family_to_direction(confirmation.family_status.get("Cash Market")),
        "Futures": _family_to_direction(confirmation.family_status.get("Futures OI")),
        "Flow": _family_to_direction(confirmation.family_status.get("CVD")),
    }

    total = len(components)
    directional = {k: v for k, v in components.items() if v in ("BULLISH", "BEARISH")}
    bull_count = sum(1 for v in directional.values() if v == "BULLISH")
    bear_count = sum(1 for v in directional.values() if v == "BEARISH")

    if bull_count and bear_count:
        strength = ConfirmationStrength.CONFLICT
    elif not directional:
        strength = ConfirmationStrength.INSUFFICIENT_DATA
    elif bull_count:
        strength = ConfirmationStrength.FULL_BULLISH if bull_count == total else ConfirmationStrength.PARTIAL_BULLISH
    else:
        strength = ConfirmationStrength.FULL_BEARISH if bear_count == total else ConfirmationStrength.PARTIAL_BEARISH

    return ConfirmationScore(strength=strength, components=components)


# ---------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------

@dataclass
class Alert:
    kind: str
    message: str
    timestamp: str


def detect_alerts(
    prev: Optional[dict],
    curr: dict,
    thresholds: RegimeThresholds,
    timestamp: str,
) -> list[Alert]:
    """
    curr / prev are plain dicts of key metrics for the current and
    previous snapshot (see app.py for the exact keys it builds). Section
    20's example events (Gamma Flip crossed, DEX became more negative,
    PE gamma concentration increased, spot entered pin zone, support
    broken, confirmation turned bearish, final state changed) are all
    covered below.
    """
    alerts: list[Alert] = []
    if prev is None:
        return alerts

    if prev.get("gamma_flip") is not None and curr.get("gamma_flip") is not None:
        prev_above = prev["spot"] > prev["gamma_flip"]
        curr_above = curr["spot"] > curr["gamma_flip"]
        if prev_above != curr_above:
            direction = "above" if curr_above else "below"
            confirmation_note = f" Observed confirmation: {curr['confirmation']}." if curr.get("confirmation") else ""
            alerts.append(Alert(
                "GAMMA_FLIP_CROSS",
                f"MODELED GAMMA FLIP CROSSED — spot moved {direction} the modeled Gamma Flip "
                f"({curr['gamma_flip']:.0f}).{confirmation_note}",
                timestamp,
            ))

    if prev.get("regime") and curr.get("regime") and prev["regime"] != curr["regime"]:
        alerts.append(Alert("GAMMA_REGIME_CHANGE", f"Modeled gamma regime changed: {prev['regime']} -> {curr['regime']}.", timestamp))

    if prev.get("net_dex") is not None and curr.get("net_dex") is not None:
        shift = curr["net_dex"] - prev["net_dex"]
        if abs(shift) >= thresholds.dex_shift_alert_lakh * 100_000:
            direction = "more negative" if shift < 0 else "more positive"
            alerts.append(Alert("DEX_SHIFT", f"Net modeled DEX became {direction} by {abs(shift)/100_000:,.0f}L (now {curr['net_dex']/100_000:,.0f}L).", timestamp))

    if prev.get("peak_gex_strike") is not None and curr.get("peak_gex_strike") is not None:
        if prev["peak_gex_strike"] != curr["peak_gex_strike"]:
            alerts.append(Alert("GAMMA_CONCENTRATION_SHIFT", f"Peak modeled gamma concentration moved from {prev['peak_gex_strike']:.0f} to {curr['peak_gex_strike']:.0f}.", timestamp))

    if prev.get("pin_low") is not None and prev.get("pin_high") is not None:
        was_inside = prev["pin_low"] <= prev["spot"] <= prev["pin_high"]
        now_inside = prev["pin_low"] <= curr["spot"] <= prev["pin_high"]
        if not was_inside and now_inside:
            alerts.append(Alert("PIN_ZONE_ENTERED", f"Spot entered the dominant pin zone ({prev['pin_low']:.0f}-{prev['pin_high']:.0f}).", timestamp))
        elif was_inside and not now_inside:
            alerts.append(Alert("PIN_BREAK", f"Price left the dominant pin zone ({prev['pin_low']:.0f}-{prev['pin_high']:.0f}).", timestamp))

    if prev.get("support") is not None and curr.get("spot") is not None:
        if prev.get("spot", curr["spot"]) >= prev["support"] > curr["spot"]:
            alerts.append(Alert("SUPPORT_BROKEN", f"Price broke below support ({prev['support']:.0f}).", timestamp))

    if prev.get("resistance") is not None and curr.get("spot") is not None:
        if prev.get("spot", curr["spot"]) <= prev["resistance"] < curr["spot"]:
            alerts.append(Alert("RESISTANCE_BROKEN", f"Price broke above resistance ({prev['resistance']:.0f}).", timestamp))

    if prev.get("confirmation") and curr.get("confirmation") and prev["confirmation"] != curr["confirmation"]:
        alerts.append(Alert("CONFIRMATION_CHANGED", f"Market confirmation changed: {prev['confirmation']} -> {curr['confirmation']}.", timestamp))

    if prev.get("final_headline") and curr.get("final_headline") and prev["final_headline"] != curr["final_headline"]:
        alerts.append(Alert("FINAL_STATE_CHANGED", f"Final state changed: {prev['final_headline']} -> {curr['final_headline']}.", timestamp))

    if prev.get("futures_positioning") and curr.get("futures_positioning") and prev["futures_positioning"] != curr["futures_positioning"]:
        alerts.append(Alert("FUTURES_POSITIONING_CHANGE", f"Futures positioning interpretation changed: {prev['futures_positioning']} -> {curr['futures_positioning']}.", timestamp))

    if prev.get("alignment") and curr.get("alignment") and prev["alignment"] != curr["alignment"]:
        alerts.append(Alert("ALIGNMENT_CHANGE", f"Alignment changed: {prev['alignment']} -> {curr['alignment']}.", timestamp))

    if prev.get("market_open") is not None and curr.get("market_open") is not None:
        if not prev["market_open"] and curr["market_open"]:
            alerts.append(Alert("MARKET_OPEN", "Market has opened.", timestamp))
        elif prev["market_open"] and not curr["market_open"]:
            alerts.append(Alert("MARKET_CLOSE", "Market has closed.", timestamp))

    if prev.get("data_ok") is not None and curr.get("data_ok") is not None:
        if prev["data_ok"] and not curr["data_ok"]:
            alerts.append(Alert("DATA_STALE", "Data quality degraded (stale/unavailable field detected).", timestamp))
        elif not prev["data_ok"] and curr["data_ok"]:
            alerts.append(Alert("DATA_RECOVERY", "Data quality recovered.", timestamp))

    return alerts
