"""
dex_engine.py (V2)

Calculates MODELED Dealer Delta Exposure (DEX): the net directional
delta implied by the visible option-chain open interest, under an
explicit, documented positioning convention.

SIGN CONVENTION (a MODEL ASSUMPTION, not an observed fact):

    dex_CE(strike) = Delta_CE * OI_CE * LOT_SIZE   (call delta >= 0)
    dex_PE(strike) = Delta_PE * OI_PE * LOT_SIZE   (put delta <= 0)
    net_dex        = sum(dex_CE) + sum(dex_PE)

    This reports the raw delta-weighted OI, under the heuristic that
    dealers are the primary counterparty to customer-held option OI. A
    positive Net DEX means the options market's aggregate delta
    positioning is skewed bullish; under this convention that implies
    dealers carry an offsetting short-delta book and would need to BUY
    the underlying to stay hedged as price rises (procyclical). This is
    a MODEL, never a confirmed measurement of any dealer's actual book —
    option-chain OI does not reveal who holds which side of a contract.

TWO EXPLICITLY LABELED UNITS (spec V2 §7 — do not conflate them):

    1. "Delta Exposure" (dimensionless-contract-equivalent):
           Delta * OI * Lot Size
       This is what most public DEX trackers report. It is NOT a rupee
       amount — it's a lot-and-delta-weighted OI count.

    2. "Rupee Delta Notional" (an actual currency exposure estimate):
           Delta * OI * Lot Size * Spot
       Only this second figure should ever be described as a rupee
       amount. The first must never be called "rupee exposure".
"""
from __future__ import annotations

from dataclasses import dataclass

from config import NIFTY_LOT_SIZE
from option_chain import OptionChainRow


@dataclass
class StrikeDEX:
    strike: float
    ce_dex: float          # Delta Exposure units (Delta * OI * lot)
    pe_dex: float
    net_dex: float
    ce_rupee_notional: float   # Delta * OI * lot * spot
    pe_rupee_notional: float
    net_rupee_notional: float


@dataclass
class DEXResult:
    strike_dex: list[StrikeDEX]
    total_ce_dex: float
    total_pe_dex: float
    net_dex: float
    total_ce_rupee_notional: float
    total_pe_rupee_notional: float
    net_rupee_notional: float


def calculate_strike_dex(
    rows: list[OptionChainRow], spot: float, lot_size: int = NIFTY_LOT_SIZE
) -> list[StrikeDEX]:
    out: list[StrikeDEX] = []
    for r in rows:
        ce_delta = r.ce_delta if r.ce_delta is not None else 0.0
        pe_delta = r.pe_delta if r.pe_delta is not None else 0.0
        ce_dex = ce_delta * r.ce_oi * lot_size
        pe_dex = pe_delta * r.pe_oi * lot_size
        out.append(
            StrikeDEX(
                strike=r.strike,
                ce_dex=ce_dex, pe_dex=pe_dex, net_dex=ce_dex + pe_dex,
                ce_rupee_notional=ce_dex * spot, pe_rupee_notional=pe_dex * spot,
                net_rupee_notional=(ce_dex + pe_dex) * spot,
            )
        )
    return out


def run_dex_engine(rows: list[OptionChainRow], spot: float, lot_size: int = NIFTY_LOT_SIZE) -> DEXResult:
    strike_dex = calculate_strike_dex(rows, spot, lot_size)
    total_ce = sum(s.ce_dex for s in strike_dex)
    total_pe = sum(s.pe_dex for s in strike_dex)
    return DEXResult(
        strike_dex=strike_dex,
        total_ce_dex=total_ce,
        total_pe_dex=total_pe,
        net_dex=total_ce + total_pe,
        total_ce_rupee_notional=total_ce * spot,
        total_pe_rupee_notional=total_pe * spot,
        net_rupee_notional=(total_ce + total_pe) * spot,
    )


@dataclass
class ModeledDeltaBalance:
    label: str              # "PUT-DELTA HEAVY" | "CALL-DELTA HEAVY" | "BALANCED" | "NEUTRAL — no data"
    lean: str                # "bullish" | "bearish" | "none"
    interpretation: str      # plain-language sentence, always hedged with the convention disclaimer


def dex_lean(result: DEXResult) -> ModeledDeltaBalance:
    """
    Returns the modeled delta balance — NEVER phrased as a fact about
    what dealers are doing. Every interpretation string carries the
    "modelled exposure, not an observable dealer position" caveat.
    """
    caveat = "This is a modelled exposure estimate under the selected positioning convention, not an observable dealer position."

    if abs(result.total_ce_dex) == 0 and abs(result.total_pe_dex) == 0:
        return ModeledDeltaBalance("NEUTRAL — no data", "none", f"No delta exposure data available. {caveat}")

    if abs(result.total_pe_dex) > abs(result.total_ce_dex):
        return ModeledDeltaBalance(
            "PUT-DELTA HEAVY", "bearish",
            f"Bearish directional lean under the selected positioning convention. {caveat}",
        )
    if abs(result.total_ce_dex) > abs(result.total_pe_dex):
        return ModeledDeltaBalance(
            "CALL-DELTA HEAVY", "bullish",
            f"Bullish directional lean under the selected positioning convention. {caveat}",
        )
    return ModeledDeltaBalance("BALANCED", "none", f"No clear lean. {caveat}")
