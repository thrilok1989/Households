"""ui/positioning.py — probable option positioning table."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from positioning_engine import LegPositioning


def render_positioning(results: list[LegPositioning]) -> None:
    st.caption("All labels are **probable positioning**, derived from price + OI change + volume — never certainty.")
    df = pd.DataFrame(
        {
            "Strike": [r.strike for r in results],
            "Leg": [r.leg for r in results],
            "Probable Behaviour": [r.label.value for r in results],
            "Confidence": [r.confidence for r in results],
            "Why": [r.rationale for r in results],
        }
    )
    df = df[df["Probable Behaviour"] != "unclear"].sort_values(["Strike", "Leg"])
    st.dataframe(df, use_container_width=True, hide_index=True)
