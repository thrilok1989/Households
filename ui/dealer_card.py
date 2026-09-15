"""
ui/dealer_card.py (V2)

The large "Dealer Intelligence" card — first thing rendered, must be
understandable within 5 seconds. Uses V2 terminology throughout:
"Modeled Dealer GEX/DEX", "Estimated Hedging Pressure", "Observed Market
Confirmation" — never bare "Dealer GEX" implying a fact.
"""
from __future__ import annotations

import streamlit as st

from dex_engine import ModeledDeltaBalance
from utils import format_inr_lakh_crore


def render_dealer_card(
    regime_label: str,
    total_gex: float,
    net_dex: float,
    gamma_flip,
    distance_from_flip,
    flip_proximity_label: str,
    spot: float,
    delta_balance: ModeledDeltaBalance,
    hedging_environment: str,
    confirmation_label: str,
    final_headline: str,
) -> None:
    st.markdown("### 🧨 DEALER INTELLIGENCE — CURRENT REGIME")

    top1, top2 = st.columns([2, 1])
    with top1:
        st.markdown(f"## {regime_label}")
    with top2:
        st.metric("Spot", f"₹{spot:,.2f}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Modeled Dealer GEX", format_inr_lakh_crore(total_gex))
    c2.metric("Modeled Dealer DEX", format_inr_lakh_crore(net_dex))
    c3.metric("Modeled Gamma Flip", f"₹{gamma_flip:,.0f}" if gamma_flip is not None else "N/A")

    c4, c5, c6 = st.columns(3)
    dist_str = f"{distance_from_flip:+.0f} pts" if distance_from_flip is not None else "N/A"
    c4.metric("Distance from Flip", dist_str, delta=flip_proximity_label)
    c5.metric("Modeled Delta Balance", delta_balance.label)
    c6.metric("Estimated Hedging Pressure", hedging_environment)

    st.metric("Observed Market Confirmation", confirmation_label)

    st.markdown("#### Final State")
    st.markdown(f"**{final_headline}**")
    st.caption(delta_balance.interpretation)
