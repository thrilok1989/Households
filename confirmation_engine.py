"""
confirmation_engine.py (V2)

Observed Market Confirmation layer (Layer B). This layer must NEVER look
at GEX/DEX/dealer regime — it only looks at actual observed market
behaviour, so it can genuinely confirm or conflict with the dealer
environment computed in dealer_regime.py / dex_engine.py.

Inputs are all optional because not every deployment will have futures
OI, CVD, or FII/DII wired in — the engine degrades gracefully, reports
"Data unavailable" per missing metric (never fabricates one), and the
per-family `status` dict lets the dashboard show 🟢/🔴/🟡/⚪ per family
(spec V2 §17: Cash Market / CVD / Futures OI / Option Flow).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from levels_engine import Levels
from positioning_engine import OptionFlowConfirmation


class Confirmation(str, Enum):
    BULLISH = "BULLISH CONFIRMATION"
    BEARISH = "BEARISH CONFIRMATION"
    NONE = "NO CONFIRMATION"
    CONFLICTING = "CONFLICTING"


class FamilyStatus(str, Enum):
    BULLISH = "🟢"
    BEARISH = "🔴"
    NEUTRAL = "🟡"
    UNAVAILABLE = "⚪"


class CVDStatus(str, Enum):
    """
    Spec V2.1 §7 — CVD must never be silently approximated and labeled
    as if it were real. True CVD requires directional buy/sell tick
    volume, which Dhan's documented option-chain/marketfeed endpoints
    (the ones this app uses) do not provide. Until a tick-level feed is
    wired in, CVD is always UNAVAILABLE — never OBSERVED, and never
    silently computed as a PROXY without this status being surfaced.
    """
    OBSERVED = "Observed"
    PROXY = "Proxy"
    UNAVAILABLE = "Unavailable"


@dataclass
class ConfirmationInputs:
    spot: float
    price_change: Optional[float] = None       # cash/spot price change vs previous snapshot
    price_change_pct: Optional[float] = None    # cash/spot price change, as a percentage
    volume: Optional[float] = None
    cvd: Optional[float] = None                 # cumulative volume delta, ONLY if cvd_status == OBSERVED/PROXY
    cvd_change: Optional[float] = None
    cvd_status: CVDStatus = CVDStatus.UNAVAILABLE
    futures_price: Optional[float] = None       # raw observed futures LTP (display only)
    futures_oi: Optional[float] = None          # raw observed futures OI (display only)
    futures_price_change: Optional[float] = None
    futures_oi_change: Optional[float] = None
    option_flow: Optional[OptionFlowConfirmation] = None  # from positioning_engine.aggregate_for_confirmation
    levels: Optional[Levels] = None


@dataclass
class ConfirmationResult:
    verdict: Confirmation
    bullish_signals: list[str] = field(default_factory=list)
    bearish_signals: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    family_status: dict[str, FamilyStatus] = field(default_factory=dict)
    # V2.1.1 — divergence/caution notes (e.g. "price up but flow down").
    # These never vote bull/bear on their own (spec: "CVD/flow must not
    # independently generate BUY/SELL") — they're surfaced separately so
    # the person can see the market is sending mixed signals even when
    # the bull/bear vote count alone looks clean.
    cautions: list[str] = field(default_factory=list)


def _score_cash_market_price(inp: ConfirmationInputs, bull, bear, missing, family) -> None:
    """
    Cash Market family, part 1: NIFTY spot (cash) price direction vs the
    previous snapshot. This uses the EXISTING spot pipeline (`inp.spot` /
    `inp.price_change`) — there is deliberately no second, duplicate
    spot-fetching path for "cash market" data, since cash IS spot.
    """
    if inp.price_change is None:
        missing.append("cash price change")
        family["Cash Market"] = FamilyStatus.UNAVAILABLE
        return
    if inp.price_change > 0:
        bull.append("Cash Market: price rising vs previous snapshot.")
        family["Cash Market"] = FamilyStatus.BULLISH
    elif inp.price_change < 0:
        bear.append("Cash Market: price falling vs previous snapshot.")
        family["Cash Market"] = FamilyStatus.BEARISH
    else:
        family["Cash Market"] = FamilyStatus.NEUTRAL


def _score_cash_market_levels(inp: ConfirmationInputs, bull, bear, missing, family) -> None:
    """Cash Market family, part 2: NIFTY spot vs support/resistance/VWAP."""
    if not inp.levels or (inp.levels.support is None and inp.levels.resistance is None and inp.levels.vwap is None):
        missing.append("cash market support/resistance/VWAP levels")
        family.setdefault("Cash Market", FamilyStatus.UNAVAILABLE)
        return
    lv = inp.levels
    hit = False
    if lv.resistance is not None and inp.spot >= lv.resistance:
        bull.append(f"Cash Market: price at/above resistance ({lv.resistance:.0f}).")
        hit = True
    if lv.support is not None and inp.spot <= lv.support:
        bear.append(f"Cash Market: price at/below support ({lv.support:.0f}).")
        hit = True
    if lv.vwap is not None:
        if inp.spot > lv.vwap:
            bull.append("Cash Market: price above VWAP.")
            hit = True
        elif inp.spot < lv.vwap:
            bear.append("Cash Market: price below VWAP.")
            hit = True
    if not hit:
        family.setdefault("Cash Market", FamilyStatus.NEUTRAL)



def _score_cvd(inp: ConfirmationInputs, bull, bear, missing, family, cautions) -> None:
    """
    V2.1.1: previously voted purely on cvd_change sign. Now, when price
    context is available, applies the price+flow confirmation/divergence
    matrix from spec V2.1.1 §9: agreement votes bull/bear; disagreement
    is a DIVERGENCE — recorded as a caution, never as an independent
    bull/bear vote (flow must confirm, not independently force a
    direction). `inp.cvd_status` (OBSERVED/PROXY/UNAVAILABLE) is always
    named in the message so a proxy is never mistaken for real CVD.
    """
    if inp.cvd_status == CVDStatus.UNAVAILABLE or inp.cvd_change is None:
        missing.append(
            "CVD/flow (Dhan's option-chain/marketfeed endpoints don't expose true aggressor-side "
            "trade volume for this integration; see the futures order-book flow proxy for the "
            "closest available signal)"
        )
        family["CVD"] = FamilyStatus.UNAVAILABLE
        return

    label_prefix = f"Flow ({inp.cvd_status.value})"

    if inp.price_change is None:
        # No price context to check for divergence against — fall back
        # to a flow-only read, same as before.
        if inp.cvd_change > 0:
            bull.append(f"{label_prefix} rising (buy-side interest dominant).")
            family["CVD"] = FamilyStatus.BULLISH
        elif inp.cvd_change < 0:
            bear.append(f"{label_prefix} falling (sell-side interest dominant).")
            family["CVD"] = FamilyStatus.BEARISH
        else:
            family["CVD"] = FamilyStatus.NEUTRAL
        return

    price_up, price_down = inp.price_change > 0, inp.price_change < 0
    flow_up, flow_down = inp.cvd_change > 0, inp.cvd_change < 0

    if price_up and flow_up:
        bull.append(f"{label_prefix}: price up + flow up -> bullish flow confirmation.")
        family["CVD"] = FamilyStatus.BULLISH
    elif price_down and flow_down:
        bear.append(f"{label_prefix}: price down + flow down -> bearish flow confirmation.")
        family["CVD"] = FamilyStatus.BEARISH
    elif price_up and flow_down:
        cautions.append(f"{label_prefix}: price up but flow down -> bearish divergence / caution.")
        family["CVD"] = FamilyStatus.NEUTRAL
    elif price_down and flow_up:
        cautions.append(f"{label_prefix}: price down but flow up -> bullish divergence / caution.")
        family["CVD"] = FamilyStatus.NEUTRAL
    else:
        family["CVD"] = FamilyStatus.NEUTRAL


def _score_futures(inp: ConfirmationInputs, bull, bear, missing, family) -> None:
    if inp.futures_price_change is None or inp.futures_oi_change is None:
        missing.append("futures price/OI")
        family["Futures OI"] = FamilyStatus.UNAVAILABLE
        return
    price_up = inp.futures_price_change > 0
    oi_up = inp.futures_oi_change > 0
    if price_up and oi_up:
        bull.append("Futures: price up + OI up -> long buildup.")
        family["Futures OI"] = FamilyStatus.BULLISH
    elif (not price_up) and oi_up:
        bear.append("Futures: price down + OI up -> short buildup.")
        family["Futures OI"] = FamilyStatus.BEARISH
    elif price_up and (not oi_up):
        bear.append("Futures: price up + OI down -> short covering (weak bullish signal, treated neutral-bear lean).")
        family["Futures OI"] = FamilyStatus.NEUTRAL
    else:
        bull.append("Futures: price down + OI down -> long unwinding (weak bearish signal, treated neutral-bull lean).")
        family["Futures OI"] = FamilyStatus.NEUTRAL


def _score_option_flow(inp: ConfirmationInputs, bull, bear, missing, family) -> None:
    """
    V2.1 fix: previously scored raw net ΔOI sign directly (CE ΔOI up =
    "bearish", PE ΔOI up = "bullish") with no regard to price — exactly
    the "ΔOI alone proves nothing" mistake. Now consumes
    positioning_engine.aggregate_for_confirmation(), which only counts
    per-strike classifications that combined price change WITH ΔOI (and
    excludes low-confidence OI-only fallbacks), and explicitly reports
    MIXED/UNCLEAR when the evidence doesn't clear that bar.
    """
    flow = inp.option_flow
    if flow is None or flow.label == "NO DATA":
        missing.append("option flow (price+OI+volume classification)")
        family["Option Flow"] = FamilyStatus.UNAVAILABLE
        return
    if flow.label == "BULLISH":
        bull.append(f"Option flow: {flow.bullish_evidence} bullish vs {flow.bearish_evidence} bearish "
                     f"strike classifications (price+OI+volume evidence).")
        family["Option Flow"] = FamilyStatus.BULLISH
    elif flow.label == "BEARISH":
        bear.append(f"Option flow: {flow.bearish_evidence} bearish vs {flow.bullish_evidence} bullish "
                     f"strike classifications (price+OI+volume evidence).")
        family["Option Flow"] = FamilyStatus.BEARISH
    else:
        family["Option Flow"] = FamilyStatus.NEUTRAL


def run_confirmation_engine(inp: ConfirmationInputs) -> ConfirmationResult:
    bull: list[str] = []
    bear: list[str] = []
    missing: list[str] = []
    family: dict[str, FamilyStatus] = {}
    cautions: list[str] = []

    _score_cash_market_price(inp, bull, bear, missing, family)
    _score_cash_market_levels(inp, bull, bear, missing, family)
    _score_cvd(inp, bull, bear, missing, family, cautions)
    _score_futures(inp, bull, bear, missing, family)
    _score_option_flow(inp, bull, bear, missing, family)

    if bull and bear:
        verdict = Confirmation.CONFLICTING
    elif bull:
        verdict = Confirmation.BULLISH
    elif bear:
        verdict = Confirmation.BEARISH
    else:
        verdict = Confirmation.NONE

    return ConfirmationResult(
        verdict=verdict, bullish_signals=bull, bearish_signals=bear, missing=missing,
        family_status=family, cautions=cautions,
    )
