"""ui/gex_chart.py (V2) — strike-wise modeled GEX + the spot-sweep Gamma Flip curve."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from gex_engine import GEXResult


def render_gex_chart(gex_result: GEXResult) -> None:
    df = pd.DataFrame(
        {
            "strike": [s.strike for s in gex_result.strike_gex],
            "total_gex_lakh": [s.total_gex / 100_000 for s in gex_result.strike_gex],
        }
    )
    colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in df["total_gex_lakh"]]

    fig = go.Figure()
    fig.add_bar(x=df["strike"], y=df["total_gex_lakh"], marker_color=colors, name="Total Modeled GEX (L)")
    fig.add_vline(x=gex_result.spot, line_dash="dash", line_color="#f1c40f",
                   annotation_text="Spot", annotation_position="top")
    if gex_result.gamma_flip is not None:
        fig.add_vline(x=gex_result.gamma_flip, line_dash="dot", line_color="#3498db",
                       annotation_text="Modeled Gamma Flip", annotation_position="bottom")

    fig.update_layout(
        title="Modeled Dealer GEX by Strike (at current spot)",
        xaxis_title="Strike",
        yaxis_title="Modeled GEX (₹ Lakh, per 1% spot move)",
        template="plotly_dark",
        height=360,
        margin=dict(l=10, r=10, t=40, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("Strike-level modeled GEX table"):
        table = pd.DataFrame(
            {
                "Strike": [s.strike for s in gex_result.strike_gex],
                "CE GEX (L)": [round(s.ce_gex / 100_000, 1) for s in gex_result.strike_gex],
                "PE GEX (L)": [round(s.pe_gex / 100_000, 1) for s in gex_result.strike_gex],
                "Total GEX (L)": [round(s.total_gex / 100_000, 1) for s in gex_result.strike_gex],
            }
        )
        st.dataframe(table, use_container_width=True, hide_index=True)


def render_gamma_flip_curve(gex_result: GEXResult) -> None:
    """The V2 spot-sweep curve: aggregate modeled GEX evaluated at
    hypothetical spot levels, with the zero crossing(s) marked."""
    flip = gex_result.gamma_flip_result
    if not flip.swept:
        st.info(f"Gamma Flip sweep unavailable: {flip.note}")
        return

    xs = [pt[0] for pt in flip.curve]
    ys = [pt[1] / 100_000 for pt in flip.curve]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name="Modeled aggregate GEX (L)", line=dict(color="#9b59b6")))
    fig.add_hline(y=0, line_color="#7f8c8d", line_dash="dash")
    fig.add_vline(x=gex_result.spot, line_dash="dash", line_color="#f1c40f", annotation_text="Current Spot")
    for crossing in flip.all_crossings:
        fig.add_vline(x=crossing, line_dash="dot", line_color="#3498db")
    if flip.primary is not None:
        fig.add_vline(x=flip.primary, line_color="#2ecc71", annotation_text="Primary Flip", annotation_position="top")

    fig.update_layout(
        title="Aggregate Modeled GEX vs Hypothetical Spot (Gamma Flip sweep)",
        xaxis_title="Hypothetical Spot",
        yaxis_title="Aggregate Modeled GEX (₹ Lakh)",
        template="plotly_dark",
        height=340,
        margin=dict(l=10, r=10, t=40, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)

    if len(flip.all_crossings) > 1:
        st.caption(
            f"{len(flip.all_crossings)} zero crossings found in the swept range: "
            + ", ".join(f"{c:,.0f}" for c in flip.all_crossings)
            + f". Nearest to spot ({flip.primary:,.0f}) is shown as the primary flip."
        )
