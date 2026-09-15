"""ui/option_chain.py — clean option-chain table, ATM ± N display window."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from dealer_regime import GammaWalls
from gex_engine import GEXResult
from option_chain import OptionChainRow, nearest_strike


def render_option_chain(
    rows: list[OptionChainRow],
    spot: float,
    gex_result: GEXResult,
    walls: GammaWalls,
) -> None:
    atm = nearest_strike(rows, spot)
    gex_by_strike = {s.strike: s.total_gex for s in gex_result.strike_gex}
    max_abs_gex = max((abs(v) for v in gex_by_strike.values()), default=1.0) or 1.0

    records = []
    for r in rows:
        tags = []
        if r.strike == atm:
            tags.append("ATM")
        if walls.upper_wall is not None and r.strike == walls.upper_wall:
            tags.append("CE WALL")
        if walls.lower_wall is not None and r.strike == walls.lower_wall:
            tags.append("PE WALL")
        if abs(gex_by_strike.get(r.strike, 0.0)) >= 0.5 * max_abs_gex:
            tags.append("GAMMA CONC.")

        records.append(
            {
                "CE OI": r.ce_oi, "CE ΔOI": r.ce_change_oi, "CE Vol": r.ce_volume,
                "CE IV": r.ce_iv, "CE LTP": r.ce_ltp,
                "Strike": r.strike,
                "PE LTP": r.pe_ltp, "PE IV": r.pe_iv, "PE Vol": r.pe_volume,
                "PE ΔOI": r.pe_change_oi, "PE OI": r.pe_oi,
                "Tags": " ".join(tags),
            }
        )

    df = pd.DataFrame(records)

    def highlight_row(row):
        if "ATM" in row["Tags"]:
            return ["background-color: #2c3e50"] * len(row)
        if "GAMMA CONC." in row["Tags"]:
            return ["background-color: #34495e"] * len(row)
        return [""] * len(row)

    st.dataframe(
        df.style.apply(highlight_row, axis=1),
        use_container_width=True,
        hide_index=True,
    )
    st.caption("ATM = at-the-money · CE/PE WALL = major gamma-backed OI concentration · "
               "GAMMA CONC. = strike holds ≥50% of the largest single-strike GEX magnitude")
