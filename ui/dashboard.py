"""
ui/dashboard.py (V2)

FII/DII panel, Data Health (with LIVE/DELAYED/STALE age bands), Event
Timeline, Price + Dealer Map, alert feed, categorical alignment readout,
"How is this calculated?" expanders, the mandatory Transparency Panel
("How this works" — spec V2 §30), and the condition-based Backtest
screen (spec V2 §29).
"""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from confirmation_engine import ConfirmationResult, FamilyStatus
from dealer_regime import RegimeResult
from gex_engine import GEXResult
from levels_engine import Levels
from market_data import DataHealth
from signal_engine import Alert, Alignment
from storage import FiiDiiFlow


def render_fii_dii(flow: FiiDiiFlow) -> None:
    st.markdown("#### FII / DII Flow")
    if not flow.available:
        st.warning(flow.note)
        st.caption(
            "If a licensed feed is added later, cash flow, futures position, and options "
            "position are kept in separate, clearly labeled fields — never mixed."
        )
        return
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**FII Cash** ({flow.cash_flow_kind})")
        st.write(f"Buy: {flow.fii_cash_buy}")
        st.write(f"Sell: {flow.fii_cash_sell}")
        st.write(f"Net: {flow.fii_cash_net}")
        st.markdown(f"**FII Index Futures** ({flow.futures_kind})")
        st.write(f"Buy: {flow.fii_index_fut_buy}")
        st.write(f"Sell: {flow.fii_index_fut_sell}")
        st.write(f"Net: {flow.fii_index_fut_net}")
    with c2:
        st.markdown(f"**DII Cash** ({flow.cash_flow_kind})")
        st.write(f"Buy: {flow.dii_cash_buy}")
        st.write(f"Sell: {flow.dii_cash_sell}")
        st.write(f"Net: {flow.dii_cash_net}")
        st.markdown(f"**FII Index Options** ({flow.options_kind})")
        st.write(f"CE positioning: {flow.fii_index_opt_ce_positioning}")
        st.write(f"PE positioning: {flow.fii_index_opt_pe_positioning}")


def render_data_health(health: DataHealth) -> None:
    st.markdown("#### Data Health")
    if not health.market_open:
        st.info("🔒 MARKET CLOSED — showing the last valid snapshot, not live data.")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.markdown(f"Dhan\n\n{health.status_icon(health.dhan_connected)}")
    c2.markdown(f"Spot\n\n{health.spot_age.value}")
    c3.markdown(f"Option Chain\n\n{health.status_icon(health.option_chain_ok)}")
    c4.markdown(f"Greeks\n\n{health.status_icon(health.greeks_ok)}")
    c5.markdown(f"Futures\n\n{health.status_icon(health.futures_ok)}")

    st.caption(f"Last update: {health.last_update or 'N/A'} · Spot age: {health.spot_age_display}")

    with st.expander("V2.1 Data Health — every field"):
        icon = {"LIVE": "🟢", "STALE": "🟡", "UNAVAILABLE": "⚪", "ERROR": "🔴"}
        for name, status in health.field_status.items():
            st.write(f"{icon.get(status.value, '⚪')} **{name}** — {status.value}")

    if health.issues:
        with st.expander(f"⚠️ {len(health.issues)} data quality issue(s)"):
            for issue in health.issues:
                st.write(f"- {issue}")


def render_confirmation_status(confirmation: ConfirmationResult) -> None:
    st.markdown("#### Observed Market Confirmation")
    families = ["Cash Market", "CVD", "Futures OI", "Option Flow"]
    cols = st.columns(len(families))
    for col, fam in zip(cols, families):
        status = confirmation.family_status.get(fam, FamilyStatus.UNAVAILABLE)
        col.markdown(f"{fam}\n\n{status.value}")
    st.markdown(f"**Overall Confirmation: {confirmation.verdict.value}**")
    if confirmation.missing:
        st.caption("Data unavailable for: " + ", ".join(confirmation.missing))
    if confirmation.cautions:
        for c in confirmation.cautions:
            st.warning(f"⚠️ {c}")


def render_confirmation_score(score, dealer_lean_interpretation: str) -> None:
    """Spec V2.1.1 §11: every component visible, never a single
    unexplained number."""
    st.markdown("#### Confirmation Score")
    icon = {"BULLISH": "🟩", "BEARISH": "🟥", "NEUTRAL": "🟨", "UNAVAILABLE": "⚪"}
    cols = st.columns(len(score.components))
    for col, (name, direction) in zip(cols, score.components.items()):
        col.markdown(f"{name}\n\n{icon.get(direction, '⚪')} {direction}")
    st.markdown(f"**FINAL: {score.strength.value}**")
    if score.components.get("Dealer") != "UNAVAILABLE":
        st.caption(f"Dealer component: {dealer_lean_interpretation}")


def render_diagnostic_panel(values: dict) -> None:
    """Spec V2.1.1 §19 — a compact, literal dump of every raw value the
    engines used this cycle, so the implementation can be verified by
    inspection rather than taken on faith."""
    with st.expander("🔍 Live Diagnostic Panel"):
        for section, rows in values.items():
            st.markdown(f"**{section}**")
            for label, val in rows.items():
                st.write(f"{label}: `{val}`")


def render_live_market_confirmation(
    spot: float,
    cash_price_change,           # real spot price change vs previous snapshot
    cash_price_change_pct,        # real spot price change, as a percentage
    futures_snapshot,             # futures_data.FuturesSnapshot
    futures_positioning: str,
    confirmation: ConfirmationResult,
    cvd_status,                    # confirmation_engine.CVDStatus
    levels,                         # levels_engine.Levels
    alignment,
    alignment_note: str,
) -> None:
    """The 'LIVE MARKET CONFIRMATION' section (spec V2.1 §24 / V2.1.1
    §2-6) — observed data first, separate from the modeled dealer
    structure card that follows it. Cash Market and Futures are shown
    as two explicit, parallel cards so cash is a first-class layer
    rather than an implicit byproduct of the spot display."""
    st.markdown("### 📡 LIVE MARKET CONFIRMATION")

    st.markdown("#### 🏦 Cash Market (NIFTY Spot)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("NIFTY Spot", f"₹{spot:,.2f}",
              delta=f"{cash_price_change:+.2f}" if cash_price_change is not None else None)
    c2.metric("Change %", f"{cash_price_change_pct:+.2f}%" if cash_price_change_pct is not None else "N/A")
    c3.metric("Cash Volume", "UNAVAILABLE")
    c4.metric("Cash Flow/CVD", "UNAVAILABLE")
    st.caption(
        "Cash Volume / Cash Flow-CVD are structurally unavailable — NIFTY spot is an index "
        "value, not a traded instrument, so it has no order book or traded volume of its own. "
        "This is a fact about what an index is, not a missing integration."
    )

    st.markdown("#### 📈 Futures")
    c1, c2, c3 = st.columns(3)
    if futures_snapshot and futures_snapshot.available:
        c1.metric(
            "Futures LTP", f"₹{futures_snapshot.ltp:,.2f}",
            delta=f"{futures_snapshot.price_change:+.2f}" if futures_snapshot.price_change is not None else None,
        )
        oi_display = f"{futures_snapshot.oi:,.0f}" if futures_snapshot.oi is not None else "N/A"
        oi_delta = f"{futures_snapshot.oi_change:+,.0f}" if futures_snapshot.oi_change is not None else None
        c2.metric("Futures OI", oi_display, delta=oi_delta)
        c3.metric("Positioning", futures_positioning)

        if any(v is not None for v in (
            futures_snapshot.open_price, futures_snapshot.high_price,
            futures_snapshot.low_price, futures_snapshot.close_price,
        )):
            o1, o2, o3, o4 = st.columns(4)
            o1.metric("Open", f"₹{futures_snapshot.open_price:,.2f}" if futures_snapshot.open_price is not None else "N/A")
            o2.metric("High", f"₹{futures_snapshot.high_price:,.2f}" if futures_snapshot.high_price is not None else "N/A")
            o3.metric("Low", f"₹{futures_snapshot.low_price:,.2f}" if futures_snapshot.low_price is not None else "N/A")
            o4.metric("Prev Close", f"₹{futures_snapshot.close_price:,.2f}" if futures_snapshot.close_price is not None else "N/A")
    else:
        err = futures_snapshot.error if futures_snapshot else "not fetched"
        c1.metric("Futures LTP", "UNAVAILABLE")
        c2.caption(f"Futures data unavailable: {err}")

    render_confirmation_status(confirmation)

    cvd_label = cvd_status.value if cvd_status is not None else "Unavailable"
    if cvd_label == "Proxy":
        st.caption(
            "Futures Flow status: Proxy — derived from NIFTY futures order-book buy/sell quantity "
            "imbalance (resting orders), NOT true aggressor-side traded CVD. Distinct from Cash "
            "Flow/CVD above, which is a separate, always-unavailable metric."
        )
    else:
        st.caption(
            "Futures Flow status: Unavailable — Dhan's option-chain/marketfeed endpoints don't "
            "expose buy/sell tick volume needed for true CVD."
        )

    level_bits = []
    if levels and levels.support is not None:
        level_bits.append(f"support {levels.support:,.0f}")
    if levels and levels.resistance is not None:
        level_bits.append(f"resistance {levels.resistance:,.0f}")
    st.caption("Level structure: " + (", ".join(level_bits) if level_bits else "not enough history yet"))

    render_alignment(alignment, alignment_note)


def render_event_log(events: list[dict]) -> None:
    st.markdown("#### Dealer Event Log")
    st.caption("Shows what changed between snapshots — not just the current numbers.")
    if not events:
        st.caption("No events logged yet this session.")
        return
    for e in events:
        st.write(f"**{e['timestamp']}** — {e['kind'].replace('_', ' ')}: {e['message']}")


def render_alerts(alerts: list[Alert]) -> None:
    if not alerts:
        return
    for a in alerts:
        st.warning(f"🧨 **{a.kind.replace('_', ' ')}**\n\n{a.message}")


def render_alignment(alignment: Alignment, note: str) -> None:
    st.markdown("#### Alignment")
    color = {"LOW": "🔴", "MEDIUM": "🟡", "HIGH": "🟢"}[alignment.value]
    st.markdown(f"**{color} {alignment.value}**")
    st.caption(note)


def render_price_dealer_map(
    recent_spots: list[float],
    gex_result: GEXResult,
    levels: Levels,
    pin_zone,
    walls,
) -> None:
    st.markdown("#### Price + Dealer Map")
    if not recent_spots:
        st.caption("Not enough snapshot history yet to plot price.")
        return

    fig = go.Figure()
    fig.add_trace(go.Scatter(y=recent_spots, mode="lines", name="Spot", line=dict(color="#f1c40f")))

    if gex_result.gamma_flip is not None:
        fig.add_hline(y=gex_result.gamma_flip, line_dash="dot", line_color="#3498db", annotation_text="Modeled Gamma Flip")
    if levels.support is not None:
        fig.add_hline(y=levels.support, line_dash="dash", line_color="#2ecc71", annotation_text="Support")
    if levels.resistance is not None:
        fig.add_hline(y=levels.resistance, line_dash="dash", line_color="#e74c3c", annotation_text="Resistance")
    if levels.vwap is not None:
        fig.add_hline(y=levels.vwap, line_dash="dashdot", line_color="#9b59b6", annotation_text="VWAP")
    if pin_zone is not None:
        fig.add_hrect(y0=pin_zone.low, y1=pin_zone.high, fillcolor="#2ecc71", opacity=0.15,
                       annotation_text="Dealer Pin Zone")
    if walls.upper_wall is not None:
        fig.add_hline(y=walls.upper_wall, line_color="#e67e22", annotation_text="Upper Gamma Wall")
    if walls.lower_wall is not None:
        fig.add_hline(y=walls.lower_wall, line_color="#e67e22", annotation_text="Lower Gamma Wall")

    fig.update_layout(template="plotly_dark", height=360, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, use_container_width=True)


def render_calc_explainer(title: str, formula: str, source: str, sign_convention: str, units: str) -> None:
    with st.expander(f"How is {title} calculated?"):
        st.markdown(f"**Formula:** `{formula}`")
        st.markdown(f"**Source data:** {source}")
        st.markdown(f"**Sign convention / assumption:** {sign_convention}")
        st.markdown(f"**Units:** {units}")


def render_transparency_panel() -> None:
    """Mandatory 'How this works' panel — spec V2 §30."""
    st.markdown("### How this works")
    st.markdown("""
**1. What comes directly from Dhan (OBSERVED DATA):**
Spot LTP, option-chain OI, ΔOI, LTP, volume, implied volatility, and
Greeks (when the API supplies them) for the selected expiry.

**2. What is calculated (MODELED EXPOSURE):**
Gamma Exposure (GEX), Delta Exposure (DEX), Rupee Delta Notional, and
the Gamma Flip level — all derived from observed data plus an explicit
positioning assumption (see below). None of these are values Dhan
returns directly.

**3. What assumptions are made:**
- Dealers are modeled as net LONG calls and net SHORT puts (the
  standard convention used by most public GEX tools).
- Gamma Flip is found by recomputing Black-Scholes gamma at hypothetical
  spot levels around the current spot, holding each option's own IV,
  strike, and time-to-expiry fixed — it is a "what would aggregate
  gamma be here" model, not a forecast of how OI/IV would actually
  change if spot moved there.
- Regime and hedging-pressure classification use configurable
  thresholds (proximity to Gamma Flip, GEX transition band, one-sided
  DEX ratio) — different thresholds can produce different classifications.

**4. What cannot be observed:**
Actual dealer positions. Option-chain OI never reveals who is long or
short a given contract — the "dealer" framing throughout this app is a
modeling convention applied to publicly visible OI, not a measurement
of any real market participant's book. FII/DII participant-level flow
is also not observable through Dhan's documented market-data APIs.

**5. Why the model can be wrong:**
- OI can reflect retail, institutional, or dealer positioning in any mix
  — the long-calls/short-puts assumption may not hold at a given moment.
- Implied volatility used for the Gamma Flip sweep is a current snapshot,
  not a forecast — it can shift materially if spot actually moves.
- Confirmation is limited to whatever live inputs are wired in; missing
  inputs (CVD, futures OI, FII/DII) reduce the model's ability to
  disagree with itself, which can look like false confidence.
- Regime and alignment outputs are unvalidated until enough historical
  outcome data accumulates in the Backtest screen — treat every output
  here as a hypothesis, not a conclusion.
""")


def render_backtest_screen(store) -> None:
    """Condition-based backtest screen (spec V2 §29)."""
    from storage import evaluate_condition

    st.markdown("#### Backtest — Historical Outcomes by Condition")
    st.caption(
        "Aggregates your OWN accumulated snapshot history. With few snapshots, treat these "
        "numbers as provisional — no predictive accuracy is claimed until sample sizes are large."
    )

    outcomes = store.outcomes_after()
    if not outcomes:
        st.info("No historical snapshots yet — come back after the app has been running for a while.")
        return

    regimes = sorted({o["regime"] for o in outcomes if o.get("regime")})
    confirmations = sorted({o["confirmation"] for o in outcomes if o.get("confirmation")})

    c1, c2 = st.columns(2)
    regime_filter = c1.selectbox("Regime", ["Any"] + regimes)
    confirmation_filter = c2.selectbox("Confirmation", ["Any"] + confirmations)

    def condition(o: dict) -> bool:
        if regime_filter != "Any" and o.get("regime") != regime_filter:
            return False
        if confirmation_filter != "Any" and o.get("confirmation") != confirmation_filter:
            return False
        return True

    result = evaluate_condition(outcomes, condition)
    st.metric("Matching events", result["event_count"])

    if result["event_count"] == 0:
        st.caption("No events match this condition yet.")
        return

    cols = st.columns(4)
    for col, m in zip(cols, (5, 15, 30, 60)):
        avg = result.get(f"avg_move_{m}m")
        pos = result.get(f"positive_pct_{m}m")
        with col:
            st.metric(f"{m}m avg move", f"{avg:+.1f} pts" if avg is not None else "N/A")
            if pos is not None:
                st.caption(f"+{pos:.0f}% / -{result[f'negative_pct_{m}m']:.0f}% / ={result[f'neutral_pct_{m}m']:.0f}%")
