"""
greeks.py

Dhan's option-chain endpoint normally returns Greeks directly, and those
should be preferred (they reflect the exchange/vendor's own IV surface).
This module exists as a fallback Black-Scholes-76 (futures-style, since
NIFTY index options are cash-settled European options) calculator for
strikes where Dhan omits Greeks, so downstream GEX/DEX math never
silently drops a strike.

Rates: a flat risk-free rate is used since NIFTY dealer-hedging estimates
are insensitive to small rate changes over short (weekly/monthly) expiries.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

RISK_FREE_RATE = 0.065  # approximate short-term INR risk-free rate


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass
class Greeks:
    delta: float
    gamma: float
    theta: float
    vega: float


def black_scholes_greeks(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    iv: float,
    option_type: str,  # "CE" or "PE"
    risk_free_rate: float = RISK_FREE_RATE,
) -> Optional[Greeks]:
    """
    Standard Black-Scholes Greeks for a European option. Returns None for
    degenerate inputs (expired, zero/negative IV) rather than raising, so
    callers can treat it the same as "Greeks unavailable".
    """
    if time_to_expiry_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return None

    sqrt_t = math.sqrt(time_to_expiry_years)
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * iv ** 2) * time_to_expiry_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t

    gamma = _norm_pdf(d1) / (spot * iv * sqrt_t)
    vega = spot * _norm_pdf(d1) * sqrt_t / 100.0  # per 1% IV move

    if option_type.upper() == "CE":
        delta = _norm_cdf(d1)
        theta = (
            -(spot * _norm_pdf(d1) * iv) / (2 * sqrt_t)
            - risk_free_rate * strike * math.exp(-risk_free_rate * time_to_expiry_years) * _norm_cdf(d2)
        ) / 365.0
    else:
        delta = _norm_cdf(d1) - 1
        theta = (
            -(spot * _norm_pdf(d1) * iv) / (2 * sqrt_t)
            + risk_free_rate * strike * math.exp(-risk_free_rate * time_to_expiry_years) * _norm_cdf(-d2)
        ) / 365.0

    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega)


def fill_missing_greeks(row, spot: float, time_to_expiry_years: float) -> None:
    """
    Mutates an OptionChainRow in place, filling ce_/pe_ delta/gamma/theta
    only where the API returned None and IV is available. Never overwrites
    a value Dhan already supplied.
    """
    if row.ce_gamma is None and row.ce_iv:
        g = black_scholes_greeks(spot, row.strike, time_to_expiry_years, row.ce_iv / 100.0, "CE")
        if g:
            row.ce_delta = row.ce_delta if row.ce_delta is not None else g.delta
            row.ce_gamma = g.gamma
            row.ce_theta = row.ce_theta if row.ce_theta is not None else g.theta
            row.ce_vega = row.ce_vega if row.ce_vega is not None else g.vega

    if row.pe_gamma is None and row.pe_iv:
        g = black_scholes_greeks(spot, row.strike, time_to_expiry_years, row.pe_iv / 100.0, "PE")
        if g:
            row.pe_delta = row.pe_delta if row.pe_delta is not None else g.delta
            row.pe_gamma = g.gamma
            row.pe_theta = row.pe_theta if row.pe_theta is not None else g.theta
            row.pe_vega = row.pe_vega if row.pe_vega is not None else g.vega
