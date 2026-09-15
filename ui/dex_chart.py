"""ui/dex_chart.py (V2) — strike-wise CE/PE delta exposure, with Delta Exposure
and Rupee Delta Notional shown as clearly separate, labeled units."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dex_engine import DEXResult
from utils import format_inr_lakh_crore


def render_dex_chart(dex_result: DEXResult) -> None:
    strikes = [s.strike for s in dex_result.strike_dex]
    ce_vals = [s.ce_dex / 100_000 for s in dex_result.strike_dex]
    pe_vals = [s.pe_dex / 100_000 for s in dex_result.strike_dex]

    fig = go.Figure()
    fig.add_bar(x=strikes, y=ce_vals, name="CE Delta Exposure (L units)", marker_color="#2ecc71")
    fig.add_bar(x=strikes, y=pe_vals, name="PE Delta Exposure (L units)", marker_color="#e74c3c")
    fig.update_layout(
        title="Modeled Dealer DEX by Strike",
        xaxis_title="Strike",
        yaxis_title="Delta Exposure (L units — NOT rupees, see below)",
        barmode="relative",
        template="plotly_dark",
        height=360,
        margin=dict(l=10, r=10, t=40, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("**Delta Exposure** (Delta × OI × Lot Size — a contract-equivalent count, NOT a rupee amount):")
    c1, c2, c3 = st.columns(3)
    c1.metric("Total CE DEX", format_inr_lakh_crore(dex_result.total_ce_dex))
    c2.metric("Total PE DEX", format_inr_lakh_crore(dex_result.total_pe_dex))
    c3.metric("Net DEX", format_inr_lakh_crore(dex_result.net_dex))

    st.markdown("**Rupee Delta Notional** (Delta × OI × Lot Size × Spot — an actual currency exposure estimate):")
    r1, r2, r3 = st.columns(3)
    r1.metric("CE Rupee Notional", format_inr_lakh_crore(dex_result.total_ce_rupee_notional, unit="Cr"))
    r2.metric("PE Rupee Notional", format_inr_lakh_crore(dex_result.total_pe_rupee_notional, unit="Cr"))
    r3.metric("Net Rupee Notional", format_inr_lakh_crore(dex_result.net_rupee_notional, unit="Cr"))
