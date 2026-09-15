"""
app.py (Dealer Intelligence V2.1)

Run with: streamlit run app.py

Pipeline (spec V2 §19 / V2.1 §22 — order matters; snapshot is saved only
once, at the very end, with every field populated):

    1. Collect data (spot, option chain, futures)
    2. Fill missing Greeks (Black-Scholes fallback)
    3. Calculate GEX (current-spot) + Gamma Flip (hypothetical-spot sweep)
    4. Calculate DEX (Delta Exposure + Rupee Delta Notional)
    5. Calculate positioning (using PREVIOUS snapshot's LTP, from SQLite)
    6. Calculate market confirmation (now wired to REAL futures + a
       price-aware option-flow aggregate, not raw ΔOI sign)
    7. Calculate dealer regime + estimated hedging pressure
    8. Calculate final state (matrix combination)
    9. SAVE COMPLETE SNAPSHOT (only now — every field populated)

ADVISORY ONLY. No order placement, modification, or execution exists
anywhere in this codebase. The Dhan integration is read-only throughout,
including the new futures/instrument-master calls added in V2.1.
"""
from __future__ import annotations

import json
import time

import streamlit as st

import config
import futures_data
import greeks as greeks_mod
import levels_engine
import market_data
import option_chain as oc_mod
from confirmation_engine import ConfirmationInputs, CVDStatus, run_confirmation_engine
from dealer_regime import run_dealer_regime
from dex_engine import run_dex_engine, dex_lean
from dhan_client import DhanClient, DhanAPIError
from futures_data import classify_futures_positioning
from gex_engine import run_gex_engine
from positioning_engine import run_positioning_engine, summarize as summarize_positioning, aggregate_for_confirmation
from signal_engine import (
    run_hedging_model, combine_final_state, alignment_score, detect_alerts,
    classify_confirmation_strength,
)
from storage import SnapshotStore, Snapshot, FiiDiiProvider
from utils import now_ist, time_to_expiry_years

from ui.dealer_card import render_dealer_card
from ui.gex_chart import render_gex_chart, render_gamma_flip_curve
from ui.dex_chart import render_dex_chart
from ui.option_chain import render_option_chain
from ui.positioning import render_positioning
from ui.dashboard import (
    render_fii_dii, render_data_health, render_event_log,
    render_alerts, render_price_dealer_map, render_calc_explainer,
    render_transparency_panel, render_backtest_screen, render_live_market_confirmation,
    render_confirmation_score, render_diagnostic_panel,
)

st.set_page_config(page_title="NIFTY Dealer Intelligence V2.1", layout="wide", page_icon="🧨")

st.markdown("<style>.block-container {padding-top: 1.2rem;}</style>", unsafe_allow_html=True)


@st.cache_resource
def get_store() -> SnapshotStore:
    return SnapshotStore(supabase_config=config.load_supabase_config())


def get_client() -> DhanClient | None:
    creds = config.load_credentials()
    if not creds.is_configured:
        return None
    try:
        return DhanClient(creds)
    except DhanAPIError:
        return None


def main() -> None:
    st.title("🧨 NIFTY DEALER INTELLIGENCE V2.1")
    st.caption(
        "Modeled dealer exposure, plus live observed market confirmation (spot, futures, "
        "option flow). Analysis only — this application never places, modifies, or executes "
        "trades."
    )

    with st.sidebar:
        st.header("Settings")
        refresh_seconds = st.select_slider(
            "Refresh interval", options=config.REFRESH_INTERVALS_SECONDS,
            value=config.DEFAULT_REFRESH_SECONDS,
        )
        exposure_window = st.slider(
            "Exposure strike window (ATM ± N)", min_value=10,
            max_value=config.EXPOSURE_STRIKE_WINDOW_MAX,
            value=config.EXPOSURE_STRIKE_WINDOW_DEFAULT,
        )
        display_window = st.slider(
            "Display strike window (ATM ± N)", min_value=3, max_value=10,
            value=config.DISPLAY_STRIKE_WINDOW,
        )
        auto_refresh = st.checkbox("Auto-refresh", value=False)

    client = get_client()
    store = get_store()

    if client is None:
        st.error(
            "Dhan credentials are not configured. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN "
            "via Streamlit secrets or a .env file (see .env.example)."
        )
        return

    # --- STEP 1: Collect data (spot, option chain, futures) ---
    conn_status = client.check_connection()
    spot_dp = market_data.fetch_spot(client)

    expiries = []
    try:
        expiries = client.get_expiry_list()
    except DhanAPIError as exc:
        st.error(f"Could not fetch expiry list: {exc}")

    if not expiries:
        render_data_health(market_data.build_data_health(conn_status.connected, spot_dp, [], False, False))
        return

    expiry = st.sidebar.selectbox("Expiry", expiries)

    try:
        raw_chain = client.get_option_chain(expiry)
    except DhanAPIError as exc:
        st.error(f"Could not fetch option chain: {exc}")
        return

    spot, all_rows = oc_mod.parse_option_chain(raw_chain)
    spot = spot or spot_dp.value
    if spot is None:
        st.error("No spot price available from either the option chain or LTP feed.")
        return

    # V2.1 fix: same-day-expiry TTE no longer implicitly midnight-anchored.
    try:
        tte_years, tte_floored = time_to_expiry_years(expiry)
    except ValueError:
        tte_years, tte_floored = 1.0 / 365.0, True

    futures_snapshot = futures_data.fetch_futures_snapshot(client, store)
    futures_positioning = classify_futures_positioning(
        futures_snapshot.price_change, futures_snapshot.oi_change
    )
    # V2.1.1: real order-book flow proxy (buy_quantity - sell_quantity,
    # from the SAME futures quote call above) — never fabricated, never
    # borrowed from spot/futures price. See futures_data.compute_flow_proxy
    # and confirmation_engine.CVDStatus for exactly what this is/isn't.
    if futures_snapshot.flow_proxy_available:
        cvd_status = CVDStatus.PROXY
        cvd_change_value = futures_snapshot.flow_proxy_change
    else:
        cvd_status = CVDStatus.UNAVAILABLE
        cvd_change_value = None

    # --- STEP 2: Fill missing Greeks ---
    for row in all_rows:
        greeks_mod.fill_missing_greeks(row, spot, tte_years)

    levels_placeholder = levels_engine.build_levels(store.recent_spots())  # for data-health only
    health = market_data.build_data_health(
        conn_status.connected, spot_dp, all_rows, futures_available=futures_snapshot.available,
        fii_dii_available=False, futures_snapshot=futures_snapshot, cvd_status=cvd_status,
        levels_available=levels_placeholder.support is not None, vwap_available=levels_placeholder.vwap is not None,
    )
    if not futures_snapshot.available and futures_snapshot.error:
        health.issues.append(f"Futures unavailable: {futures_snapshot.error} "
                              f"(see 'Futures Resolution Diagnostics' in the Live Diagnostic Panel below).")
    render_data_health(health)
    if tte_floored:
        st.caption(
            f"⚠️ Time-to-expiry floored to {config.MIN_TTE_SECONDS:.0f}s for Greeks/Gamma-Flip math "
            f"(expiry {expiry} at {config.EXPIRY_HOUR_IST:02d}:{config.EXPIRY_MINUTE_IST:02d} IST has "
            "effectively passed or is seconds away) — a numerical-stability floor, not a claim about "
            "actual remaining time value."
        )

    if not all_rows:
        st.warning("No option-chain rows available.")
        return

    exposure_rows = oc_mod.strike_window(all_rows, spot, exposure_window)
    display_rows = oc_mod.strike_window(all_rows, spot, display_window)
    timestamp = now_ist().isoformat()

    # --- STEP 3: GEX + Gamma Flip ---
    gex_result = run_gex_engine(exposure_rows, spot, tte_years, config.REGIME_THRESHOLDS)

    # --- STEP 4: DEX ---
    dex_result = run_dex_engine(exposure_rows, spot)
    delta_balance = dex_lean(dex_result)

    # --- STEP 5: Positioning (previous LTP fix — pulled from SQLite, not session state) ---
    prev_ltp_map = store.get_previous_option_legs(timestamp)
    positioning_results = run_positioning_engine(exposure_rows, prev_ltp_map)
    positioning_summary = summarize_positioning(positioning_results)
    # V2.1 fix: option-flow confirmation now uses price+OI+volume
    # classifications (with a confidence floor), not raw ΔOI sign.
    option_flow = aggregate_for_confirmation(positioning_results)

    # --- STEP 6: Market confirmation (cash spot + real futures + real flow proxy + fixed option flow) ---
    recent_spots = store.recent_spots()
    levels = levels_engine.build_levels(recent_spots)
    prev_spot = recent_spots[-1] if recent_spots else None
    cash_price_change = (spot - prev_spot) if prev_spot is not None else None
    cash_price_change_pct = (cash_price_change / prev_spot * 100.0) if (cash_price_change is not None and prev_spot) else None

    net_call_change = sum(r.ce_change_oi for r in exposure_rows)
    net_put_change = sum(r.pe_change_oi for r in exposure_rows)

    confirmation_inputs = ConfirmationInputs(
        spot=spot,
        price_change=cash_price_change,   # this IS the cash-market read — no duplicate spot pipeline
        price_change_pct=cash_price_change_pct,
        levels=levels,
        cvd_status=cvd_status,
        cvd_change=cvd_change_value,
        futures_price=futures_snapshot.ltp,
        futures_oi=futures_snapshot.oi,
        futures_price_change=futures_snapshot.price_change,
        futures_oi_change=futures_snapshot.oi_change,
        option_flow=option_flow,
    )
    confirmation_result = run_confirmation_engine(confirmation_inputs)

    # --- STEP 7: Dealer regime + hedging pressure ---
    regime_result = run_dealer_regime(gex_result, config.REGIME_THRESHOLDS)
    hedging_env, hedging_reasons = run_hedging_model(regime_result, dex_result, delta_balance)

    # --- STEP 8: Final state ---
    final_state = combine_final_state(regime_result, confirmation_result)
    alignment, alignment_note = alignment_score(regime_result, delta_balance, confirmation_result)
    confirmation_score = classify_confirmation_strength(delta_balance, confirmation_result)

    # --- Alerts (compare against the last COMPLETE snapshot before this one) ---
    prev_snap = store.last_snapshot()
    curr_metrics = {
        "spot": spot,
        "gamma_flip": gex_result.gamma_flip,
        "regime": regime_result.regime.value,
        "net_dex": dex_result.net_dex,
        "peak_gex_strike": max(gex_result.strike_gex, key=lambda s: s.total_gex).strike if gex_result.strike_gex else None,
        "pin_low": regime_result.pin_zone.low if regime_result.pin_zone else None,
        "pin_high": regime_result.pin_zone.high if regime_result.pin_zone else None,
        "support": levels.support,
        "resistance": levels.resistance,
        "confirmation": confirmation_result.verdict.value,
        "final_headline": final_state.headline,
        "futures_positioning": futures_positioning,
        "alignment": alignment.value,
        "market_open": health.market_open,
        "data_ok": health.data_ok,
    }
    alerts = detect_alerts(prev_snap, curr_metrics, config.REGIME_THRESHOLDS, now_ist().strftime("%H:%M:%S"))
    for a in alerts:
        store.log_event(a.timestamp, a.kind, a.message)

    # --- STEP 9: SAVE COMPLETE SNAPSHOT (only now, with every field populated) ---
    store.save_snapshot(Snapshot(
        timestamp=timestamp,
        spot=spot,
        total_gex=gex_result.total_gex,
        net_dex=dex_result.net_dex,
        net_rupee_notional=dex_result.net_rupee_notional,
        gamma_flip=gex_result.gamma_flip,
        distance_from_flip=regime_result.distance_from_flip,
        regime=regime_result.regime.value,
        hedging_environment=hedging_env.value,
        confirmation=confirmation_result.verdict.value,
        final_state=final_state.headline,
        alignment=alignment.value,
        positioning_summary=json.dumps(positioning_summary),
        data_health_summary=json.dumps({"issues": health.issues, "market_open": health.market_open}),
        ce_oi_total=sum(r.ce_oi for r in exposure_rows),
        pe_oi_total=sum(r.pe_oi for r in exposure_rows),
        ce_change_oi_total=net_call_change,
        pe_change_oi_total=net_put_change,
        volume_total=sum(r.ce_volume + r.pe_volume for r in exposure_rows),
        futures_ltp=futures_snapshot.ltp,
        futures_oi=futures_snapshot.oi,
        futures_oi_change=futures_snapshot.oi_change,
        futures_price_change=futures_snapshot.price_change,
    ))
    store.save_option_legs(timestamp, exposure_rows)
    store.prune_option_legs()
    store.prune_futures_snapshots()

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------
    render_alerts(alerts)

    render_live_market_confirmation(
        spot=spot,
        cash_price_change=cash_price_change,
        cash_price_change_pct=cash_price_change_pct,
        futures_snapshot=futures_snapshot,
        futures_positioning=futures_positioning,
        confirmation=confirmation_result,
        cvd_status=cvd_status,
        levels=levels,
        alignment=alignment,
        alignment_note=alignment_note,
    )

    render_confirmation_score(confirmation_score, delta_balance.interpretation)

    render_diagnostic_panel({
        "Cash / NIFTY Spot": {
            "LTP": spot, "Price change": cash_price_change, "Price change %": cash_price_change_pct,
            "Volume": "UNAVAILABLE (index — no traded volume)",
            "Flow/CVD": "UNAVAILABLE (index — no order book)",
        },
        "NIFTY Futures": {
            "Symbol": futures_snapshot.symbol, "Expiry": futures_snapshot.expiry,
            "LTP": futures_snapshot.ltp, "Price change": futures_snapshot.price_change,
            "OI": futures_snapshot.oi, "ΔOI": futures_snapshot.oi_change,
            "Open": futures_snapshot.open_price, "High": futures_snapshot.high_price,
            "Low": futures_snapshot.low_price, "Prev Close": futures_snapshot.close_price,
            "Avg Price": futures_snapshot.average_price, "Last Trade Time": futures_snapshot.last_trade_time,
            "Positioning": futures_positioning,
            "Error": futures_snapshot.error,
        },
        **({"Futures Resolution Diagnostics": {
            "Fetch OK": futures_snapshot.diagnostics.fetch_ok,
            "Fetch error": futures_snapshot.diagnostics.fetch_error,
            "Rows scanned": futures_snapshot.diagnostics.row_count,
            "Column headers seen (first 15)": futures_snapshot.diagnostics.fieldnames_sample,
            "Matched exchange col": futures_snapshot.diagnostics.matched_exchange_col,
            "Matched instrument col": futures_snapshot.diagnostics.matched_instrument_col,
            "Matched symbol col": futures_snapshot.diagnostics.matched_symbol_col,
            "Matched security-ID col": futures_snapshot.diagnostics.matched_security_id_col,
            "Matched expiry col": futures_snapshot.diagnostics.matched_expiry_col,
            "Rows with 'NIFTY' in symbol": futures_snapshot.diagnostics.nifty_symbol_rows,
            "...passing instrument/exchange filter": futures_snapshot.diagnostics.futidx_candidate_rows,
            "...with a valid unexpired expiry": futures_snapshot.diagnostics.unexpired_candidates,
            "...parsed fine but already expired": futures_snapshot.diagnostics.parsed_but_expired,
            "Raw expiry values that failed to parse": futures_snapshot.diagnostics.sample_expiry_values,
            "Diagnosis": futures_snapshot.diagnostics.note,
        }} if futures_snapshot.diagnostics else {}),
        "Flow / CVD": {
            "Status": cvd_status.value, "Buy qty": futures_snapshot.buy_quantity,
            "Sell qty": futures_snapshot.sell_quantity, "Flow proxy": futures_snapshot.flow_proxy,
            "Flow proxy change": futures_snapshot.flow_proxy_change,
        },
        "Options / Dealer Model": {
            "Modeled GEX": gex_result.total_gex, "Modeled DEX": dex_result.net_dex,
            "Modeled Gamma Flip": gex_result.gamma_flip, "Dealer Regime": regime_result.regime.value,
            "Modeled Delta Balance": delta_balance.label,
        },
        "Final": {
            "Cash": confirmation_score.components.get("Cash"),
            "Futures": confirmation_score.components.get("Futures"),
            "Flow": confirmation_score.components.get("Flow"),
            "Dealer": confirmation_score.components.get("Dealer"),
            "Confirmation": confirmation_result.verdict.value,
            "Confirmation Score": confirmation_score.strength.value,
            "Final State": final_state.headline,
            "TTE (years)": round(tte_years, 6), "TTE floored": tte_floored,
        },
    })

    st.divider()

    render_dealer_card(
        regime_label=regime_result.regime.value,
        total_gex=gex_result.total_gex,
        net_dex=dex_result.net_dex,
        gamma_flip=gex_result.gamma_flip,
        distance_from_flip=regime_result.distance_from_flip,
        flip_proximity_label=regime_result.flip_proximity.value,
        spot=spot,
        delta_balance=delta_balance,
        hedging_environment=hedging_env.value,
        confirmation_label=confirmation_result.verdict.value,
        final_headline=final_state.headline,
    )
    st.caption(final_state.detail)
    with st.expander("Why this regime / hedging read?"):
        for reason in regime_result.reasons:
            st.write(f"- {reason}")
        for reason in hedging_reasons:
            if reason not in regime_result.reasons:
                st.write(f"- {reason}")

    if confirmation_result.missing:
        st.caption("Confirmation inputs not wired to a live source yet: " + ", ".join(confirmation_result.missing))

    tabs = st.tabs([
        "Gamma Flip / Pin Zone", "GEX Chart", "DEX Chart", "Dealer Positioning",
        "Option Chain", "FII/DII", "Event Timeline", "Backtest", "How This Works",
    ])

    with tabs[0]:
        st.write(f"**Modeled Gamma Flip:** ₹{gex_result.gamma_flip:,.0f}" if gex_result.gamma_flip else "Modeled Gamma Flip: N/A")
        st.write(f"**Spot vs Flip:** {regime_result.flip_proximity.value}")
        if regime_result.pin_zone:
            st.write(f"**Dealer Pin Zone:** {regime_result.pin_zone.low:,.0f} – {regime_result.pin_zone.high:,.0f}")
        st.write(f"**Upper Gamma Wall:** {regime_result.walls.upper_wall or 'N/A'}")
        st.write(f"**Lower Gamma Wall:** {regime_result.walls.lower_wall or 'N/A'}")
        render_gamma_flip_curve(gex_result)
        render_price_dealer_map(recent_spots, gex_result, levels, regime_result.pin_zone, regime_result.walls)
        render_calc_explainer(
            "the Modeled Gamma Flip",
            "Sweep hypothetical spot ± flip_search_range_points; at each point recompute gamma "
            "via Black-Scholes (fixed IV/strike/TTE) and aggregate; find the zero crossing "
            "nearest current spot",
            "Option chain IV + strike + time-to-expiry (NOT the current-spot Greeks alone)",
            "A model of 'what aggregate gamma would be if spot were here', not a forecast — see the "
            "How This Works tab",
            "NIFTY points",
        )

    with tabs[1]:
        render_gex_chart(gex_result)
        render_calc_explainer(
            "Modeled GEX",
            "Gamma × OI × Lot Size × Spot² × 0.01, CE positive / PE negative",
            f"Option chain Greeks (API or Black-Scholes fallback), OI, spot; lot size = {config.NIFTY_LOT_SIZE}",
            "Dealers modeled as net long calls, net short puts (standard convention) — see gex_engine.py docstring",
            "₹ notional per 1% spot move, displayed in lakh (L) / crore (Cr)",
        )

    with tabs[2]:
        render_dex_chart(dex_result)
        render_calc_explainer(
            "Modeled DEX",
            "Delta Exposure = Delta × OI × Lot Size; Rupee Delta Notional = Delta × OI × Lot Size × Spot",
            f"Option chain Greeks (API or Black-Scholes fallback), OI, spot; lot size = {config.NIFTY_LOT_SIZE}",
            "Raw delta-weighted OI under the 'dealers are primary counterparty' heuristic — see dex_engine.py docstring",
            "Delta Exposure = contract-equivalent units (L); Rupee Delta Notional = actual currency estimate (Cr)",
        )

    with tabs[3]:
        render_positioning(positioning_results)
        st.caption(
            f"Option-flow confirmation evidence: {option_flow.bullish_evidence} bullish, "
            f"{option_flow.bearish_evidence} bearish, {option_flow.unclear_evidence} mixed/unclear "
            f"(low-confidence or ambiguous classifications are excluded from confirmation scoring)."
        )
        if not positioning_summary["used_price_data"]:
            st.caption(
                "⚠️ No previous-snapshot price data was available yet this session — all "
                "classifications above fell back to the low-confidence, ΔOI-only path. This "
                "resolves itself after the first refresh cycle."
            )

    with tabs[4]:
        render_option_chain(display_rows, spot, gex_result, regime_result.walls)

    with tabs[5]:
        render_fii_dii(FiiDiiProvider().fetch())

    with tabs[6]:
        render_event_log(store.recent_events())

    with tabs[7]:
        render_backtest_screen(store)

    with tabs[8]:
        render_transparency_panel()

    if auto_refresh:
        time.sleep(refresh_seconds)
        st.rerun()


if __name__ == "__main__":
    main()
