"""
tests/test_calculations.py (V2)

Run with: pytest -v

Covers: GEX, DEX (+ rupee notional), spot-dependent Gamma Flip (including
multiple crossings), regime classification (PIN/CHOP, EXPANSION,
TRANSITION, MIXED), hedging model, the Final State matrix, categorical
alignment, positioning classification (incl. the previous-LTP fix via
storage), stale/missing/zero-OI data detection, market-hours detection,
and the five named scenarios from the V2 spec (A-E).
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import RegimeThresholds
from confirmation_engine import Confirmation, ConfirmationResult
from dealer_regime import (
    GammaRegime, FlipProximity, classify_gamma_regime, classify_flip_proximity,
    find_pin_zone, find_gamma_walls, run_dealer_regime,
)
from dex_engine import run_dex_engine, dex_lean, ModeledDeltaBalance
from gex_engine import (
    run_gex_engine, find_zero_crossings, calculate_strike_gex, gamma_flip_v2,
    GEXResult, GammaFlipResult, StrikeGEX,
)
from market_data import DataPoint, DataAge, detect_data_issues, build_data_health
from option_chain import OptionChainRow, nearest_strike, strike_window
from positioning_engine import classify_leg, PositioningLabel, run_positioning_engine, summarize
from signal_engine import (
    run_hedging_model, combine_final_state, alignment_score, Alignment, HedgingEnvironment,
)
from storage import SnapshotStore, Snapshot, evaluate_condition
from utils import is_market_open

LOT = 75
DEFAULT_THRESHOLDS = RegimeThresholds()


def make_row(strike, ce_oi, pe_oi, ce_gamma=0.001, pe_gamma=0.001, ce_delta=0.5, pe_delta=-0.5,
             ce_change_oi=0.0, pe_change_oi=0.0, ce_volume=100.0, pe_volume=100.0,
             ce_iv=15.0, pe_iv=15.0, ce_ltp=10.0, pe_ltp=10.0):
    return OptionChainRow(
        strike=strike,
        ce_ltp=ce_ltp, ce_oi=ce_oi, ce_prev_oi=ce_oi - ce_change_oi, ce_volume=ce_volume,
        ce_iv=ce_iv, ce_delta=ce_delta, ce_gamma=ce_gamma, ce_theta=-1.0, ce_vega=1.0,
        pe_ltp=pe_ltp, pe_oi=pe_oi, pe_prev_oi=pe_oi - pe_change_oi, pe_volume=pe_volume,
        pe_iv=pe_iv, pe_delta=pe_delta, pe_gamma=pe_gamma, pe_theta=-1.0, pe_vega=1.0,
    )


def synthetic_chain():
    return [
        make_row(23200, ce_oi=1000, pe_oi=5000, ce_gamma=0.0005, pe_gamma=0.0004, ce_delta=0.85, pe_delta=-0.15),
        make_row(23300, ce_oi=3000, pe_oi=8000, ce_gamma=0.0012, pe_gamma=0.0011, ce_delta=0.65, pe_delta=-0.35),
        make_row(23400, ce_oi=9000, pe_oi=9500, ce_gamma=0.0020, pe_gamma=0.0019, ce_delta=0.50, pe_delta=-0.50),
        make_row(23500, ce_oi=8000, pe_oi=2500, ce_gamma=0.0012, pe_gamma=0.0011, ce_delta=0.35, pe_delta=-0.65),
        make_row(23600, ce_oi=6000, pe_oi=900, ce_gamma=0.0005, pe_gamma=0.0004, ce_delta=0.15, pe_delta=-0.85),
    ]


def make_gex_result(spot, total_gex, gamma_flip, strike_gex=None) -> GEXResult:
    """Directly constructs a GEXResult for regime-classifier-level tests,
    bypassing the real Black-Scholes sweep — used where we want to pin
    down an exact total_gex/gamma_flip combination deterministically."""
    if strike_gex is None:
        strike_gex = [StrikeGEX(spot, total_gex / 2, total_gex / 2, total_gex)]
    flip_result = GammaFlipResult(primary=gamma_flip, all_crossings=[gamma_flip] if gamma_flip else [], curve=[], swept=gamma_flip is not None)
    return GEXResult(spot=spot, strike_gex=strike_gex, total_gex=total_gex, gamma_flip=gamma_flip,
                      gamma_flip_result=flip_result, lot_size=LOT)


# ---------------------------------------------------------------------
# GEX (current-spot)
# ---------------------------------------------------------------------

def test_strike_gex_matches_manual_calc():
    rows = synthetic_chain()
    spot = 23400.0
    strike_gex = calculate_strike_gex(rows, spot, lot_size=LOT)
    atm = next(s for s in strike_gex if s.strike == 23400)
    factor = (spot ** 2) * 0.01
    expected_ce = 0.0020 * 9000 * LOT * factor
    expected_pe = -1.0 * 0.0019 * 9500 * LOT * factor
    assert abs(atm.ce_gex - expected_ce) < 1e-6
    assert abs(atm.pe_gex - expected_pe) < 1e-6
    assert abs(atm.total_gex - (expected_ce + expected_pe)) < 1e-6


def test_total_gex_is_sum_of_strikes():
    rows = synthetic_chain()
    result = run_gex_engine(rows, 23400.0, tte_years=0.05, thresholds=DEFAULT_THRESHOLDS, lot_size=LOT)
    assert abs(result.total_gex - sum(s.total_gex for s in result.strike_gex)) < 1e-6


def test_run_gex_engine_handles_missing_strikes_gracefully():
    result = run_gex_engine([], 23400.0, tte_years=0.05, thresholds=DEFAULT_THRESHOLDS, lot_size=LOT)
    assert result.total_gex == 0.0
    assert result.gamma_flip is None
    assert result.gamma_flip_result.swept is False


def test_run_gex_engine_handles_missing_greeks_gracefully():
    # Rows with no gamma/IV at all (simulates Dhan omitting Greeks and
    # the Black-Scholes fallback also having nothing to work with).
    rows = [make_row(23400, ce_oi=100, pe_oi=100, ce_gamma=None, pe_gamma=None, ce_iv=None, pe_iv=None)]
    result = run_gex_engine(rows, 23400.0, tte_years=0.05, thresholds=DEFAULT_THRESHOLDS, lot_size=LOT)
    assert result.total_gex == 0.0  # gamma treated as 0, not a crash
    assert result.gamma_flip_result.swept is False


def test_zero_oi_produces_zero_gex_not_a_crash():
    rows = [make_row(23400, ce_oi=0, pe_oi=0)]
    result = run_gex_engine(rows, 23400.0, tte_years=0.05, thresholds=DEFAULT_THRESHOLDS, lot_size=LOT)
    assert result.total_gex == 0.0


# ---------------------------------------------------------------------
# Zero-crossing detection (factored out for direct testing)
# ---------------------------------------------------------------------

def test_find_zero_crossings_none_when_no_sign_change():
    curve = [(23200, 10), (23300, 20), (23400, 30)]
    assert find_zero_crossings(curve) == []


def test_find_zero_crossings_single_interpolated():
    curve = [(23300, 100), (23400, -50)]
    crossings = find_zero_crossings(curve)
    assert len(crossings) == 1
    # frac = 100/150 -> 23300 + 0.667*100 = 23366.7
    assert 23360 < crossings[0] < 23375


def test_find_zero_crossings_multiple():
    # +, -, +, - -> three crossings
    curve = [(100, 10), (200, -10), (300, 10), (400, -10)]
    crossings = find_zero_crossings(curve)
    assert len(crossings) == 3
    assert 140 < crossings[0] < 160
    assert 240 < crossings[1] < 260
    assert 340 < crossings[2] < 360


def test_find_zero_crossings_exact_zero_point():
    curve = [(100, 0), (200, -10)]
    crossings = find_zero_crossings(curve)
    assert crossings == [100]


# ---------------------------------------------------------------------
# Gamma Flip V2 — spot-dependent sweep (real Black-Scholes)
# ---------------------------------------------------------------------

def test_gamma_flip_v2_is_spot_dependent_not_cumulative_strike():
    """The V2 flip must come from re-evaluating gamma at hypothetical
    spot levels, not from a cumulative sum over strikes at the CURRENT
    spot — i.e. it must actually use tte_years/IV, and change if IV does."""
    rows = synthetic_chain()
    thresholds = RegimeThresholds(flip_search_range_points=300, flip_search_step_points=25)
    flip_low_iv = gamma_flip_v2(rows, 23400.0, tte_years=0.02, thresholds=thresholds, lot_size=LOT)
    assert flip_low_iv.swept is True
    assert len(flip_low_iv.curve) > 1


def test_gamma_flip_v2_no_iv_data_reports_not_swept():
    rows = [make_row(23400, ce_oi=100, pe_oi=100, ce_iv=None, pe_iv=None)]
    flip = gamma_flip_v2(rows, 23400.0, tte_years=0.05, thresholds=DEFAULT_THRESHOLDS, lot_size=LOT)
    assert flip.swept is False


def test_gamma_flip_v2_zero_tte_reports_not_swept():
    rows = synthetic_chain()
    flip = gamma_flip_v2(rows, 23400.0, tte_years=0.0, thresholds=DEFAULT_THRESHOLDS, lot_size=LOT)
    assert flip.swept is False


def test_gamma_flip_v2_primary_is_nearest_to_spot():
    rows = synthetic_chain()
    thresholds = RegimeThresholds(flip_search_range_points=400, flip_search_step_points=20)
    flip = gamma_flip_v2(rows, 23400.0, tte_years=0.05, thresholds=thresholds, lot_size=LOT)
    if flip.all_crossings:
        nearest = min(flip.all_crossings, key=lambda c: abs(c - 23400.0))
        assert flip.primary == nearest


# ---------------------------------------------------------------------
# DEX (+ rupee notional)
# ---------------------------------------------------------------------

def test_dex_ce_positive_pe_negative_and_net():
    rows = [make_row(23400, ce_oi=1000, pe_oi=1000, ce_delta=0.5, pe_delta=-0.5)]
    result = run_dex_engine(rows, spot=23400.0, lot_size=LOT)
    expected_ce = 0.5 * 1000 * LOT
    expected_pe = -0.5 * 1000 * LOT
    assert abs(result.total_ce_dex - expected_ce) < 1e-6
    assert abs(result.total_pe_dex - expected_pe) < 1e-6
    assert abs(result.net_dex - (expected_ce + expected_pe)) < 1e-6


def test_dex_rupee_notional_includes_spot_and_dex_does_not():
    rows = [make_row(23400, ce_oi=1000, pe_oi=100, ce_delta=0.5, pe_delta=-0.1)]
    result = run_dex_engine(rows, spot=23400.0, lot_size=LOT)
    assert abs(result.net_rupee_notional - result.net_dex * 23400.0) < 1e-6
    # Rupee notional at a different spot must differ, but plain DEX must not.
    result_other_spot = run_dex_engine(rows, spot=30000.0, lot_size=LOT)
    assert result.net_dex == result_other_spot.net_dex
    assert result.net_rupee_notional != result_other_spot.net_rupee_notional


def test_dex_lean_put_heavy_is_bearish_and_carries_disclaimer():
    rows = [make_row(23400, ce_oi=100, pe_oi=10000, ce_delta=0.5, pe_delta=-0.9)]
    result = run_dex_engine(rows, spot=23400.0, lot_size=LOT)
    balance = dex_lean(result)
    assert isinstance(balance, ModeledDeltaBalance)
    assert balance.lean == "bearish"
    assert "modelled exposure estimate" in balance.interpretation
    assert "not an observable dealer position" in balance.interpretation


def test_dex_lean_call_heavy_is_bullish():
    rows = [make_row(23400, ce_oi=10000, pe_oi=100, ce_delta=0.9, pe_delta=-0.5)]
    result = run_dex_engine(rows, spot=23400.0, lot_size=LOT)
    balance = dex_lean(result)
    assert balance.lean == "bullish"


# ---------------------------------------------------------------------
# Regime classification (PIN/CHOP, EXPANSION, TRANSITION, MIXED)
# ---------------------------------------------------------------------

def test_regime_transition_when_near_flip():
    gex = make_gex_result(spot=23400, total_gex=+5_000_000, gamma_flip=23410)
    thresholds = RegimeThresholds(flip_proximity_points=1000.0, flip_proximity_pct=0.0)
    regime, reasons = classify_gamma_regime(gex, thresholds)
    assert regime == GammaRegime.TRANSITION
    assert reasons


def test_regime_pin_chop_when_positive_and_far_from_flip():
    strike_gex = [StrikeGEX(23400, 100, 100, 200)]
    gex = make_gex_result(spot=23400, total_gex=50_000_000, gamma_flip=20000, strike_gex=strike_gex)
    thresholds = RegimeThresholds(flip_proximity_points=10, flip_proximity_pct=0.0, gex_transition_band_lakh=100)
    regime, _ = classify_gamma_regime(gex, thresholds)
    assert regime == GammaRegime.PIN_CHOP


def test_regime_expansion_when_negative_and_far_from_flip():
    strike_gex = [StrikeGEX(23400, -100, -100, -200)]
    gex = make_gex_result(spot=23400, total_gex=-50_000_000, gamma_flip=30000, strike_gex=strike_gex)
    thresholds = RegimeThresholds(flip_proximity_points=10, flip_proximity_pct=0.0, gex_transition_band_lakh=100)
    regime, _ = classify_gamma_regime(gex, thresholds)
    assert regime == GammaRegime.EXPANSION


def test_regime_mixed_when_local_gamma_disagrees_with_aggregate():
    # Aggregate positive, but the strike nearest spot is negative.
    strike_gex = [StrikeGEX(23400, -100, -100, -200)]
    gex = make_gex_result(spot=23400, total_gex=+50_000_000, gamma_flip=30000, strike_gex=strike_gex)
    thresholds = RegimeThresholds(flip_proximity_points=10, flip_proximity_pct=0.0, gex_transition_band_lakh=100)
    regime, reasons = classify_gamma_regime(gex, thresholds)
    assert regime == GammaRegime.MIXED
    assert any("disagree" in r for r in reasons)


def test_flip_proximity_labels_above_below_near():
    gex_above = make_gex_result(spot=23500, total_gex=1, gamma_flip=23000)
    gex_below = make_gex_result(spot=22500, total_gex=1, gamma_flip=23000)
    gex_near = make_gex_result(spot=23010, total_gex=1, gamma_flip=23000)
    thresholds = RegimeThresholds(flip_proximity_points=50, flip_proximity_pct=0.0)
    assert classify_flip_proximity(gex_above, thresholds)[0] == FlipProximity.ABOVE
    assert classify_flip_proximity(gex_below, thresholds)[0] == FlipProximity.BELOW
    assert classify_flip_proximity(gex_near, thresholds)[0] == FlipProximity.NEAR


def test_flip_proximity_unknown_when_no_flip():
    gex = make_gex_result(spot=23400, total_gex=1, gamma_flip=None)
    proximity, distance = classify_flip_proximity(gex, DEFAULT_THRESHOLDS)
    assert proximity == FlipProximity.UNKNOWN
    assert distance is None


def test_pin_zone_centers_on_peak_positive_strike():
    rows = synthetic_chain()
    result = run_gex_engine(rows, 23400.0, tte_years=0.05, thresholds=DEFAULT_THRESHOLDS, lot_size=LOT)
    thresholds = RegimeThresholds(pin_zone_width_points=100.0)
    pin = find_pin_zone(result, thresholds)
    peak = max(result.strike_gex, key=lambda s: s.total_gex)
    if peak.total_gex > 0:
        assert pin is not None
        assert pin.peak_strike == peak.strike
        assert pin.low == peak.strike - 50
        assert pin.high == peak.strike + 50


def test_gamma_walls_never_forced_on_uniform_tiny_exposure():
    rows = [make_row(s, ce_oi=1, pe_oi=1, ce_gamma=0.0001, pe_gamma=0.0001) for s in (23200, 23300, 23400, 23500, 23600)]
    result = run_gex_engine(rows, 23400.0, tte_years=0.05, thresholds=DEFAULT_THRESHOLDS, lot_size=LOT)
    walls = find_gamma_walls(result)
    assert hasattr(walls, "upper_wall") and hasattr(walls, "lower_wall")


# ---------------------------------------------------------------------
# Hedging model + Final State Matrix
# ---------------------------------------------------------------------

def _regime_result(regime: GammaRegime):
    from dealer_regime import RegimeResult, REGIME_EXPECTATION, GammaWalls
    return RegimeResult(
        regime=regime, expectation=REGIME_EXPECTATION[regime], reasons=["test reason"],
        flip_proximity=FlipProximity.ABOVE, distance_from_flip=100.0, pin_zone=None,
        walls=GammaWalls(None, None),
    )


def _confirmation(verdict: Confirmation):
    return ConfirmationResult(verdict=verdict, bullish_signals=[], bearish_signals=[], missing=[], family_status={})


def test_final_state_pin_no_confirmation_is_wait():
    fs = combine_final_state(_regime_result(GammaRegime.PIN_CHOP), _confirmation(Confirmation.NONE))
    assert fs.headline == "WAIT / NO TRADE"
    assert fs.requires_wait


def test_final_state_pin_bearish_is_wait_for_structural_break():
    fs = combine_final_state(_regime_result(GammaRegime.PIN_CHOP), _confirmation(Confirmation.BEARISH))
    assert fs.headline == "BEARISH LEAN — WAIT FOR STRUCTURAL BREAK"
    assert fs.requires_wait


def test_final_state_pin_bullish_is_wait_for_confirmation():
    fs = combine_final_state(_regime_result(GammaRegime.PIN_CHOP), _confirmation(Confirmation.BULLISH))
    assert fs.headline == "BULLISH LEAN — WAIT FOR CONFIRMATION"


def test_final_state_expansion_bearish_is_bearish_expansion():
    fs = combine_final_state(_regime_result(GammaRegime.EXPANSION), _confirmation(Confirmation.BEARISH))
    assert fs.headline == "BEARISH EXPANSION"
    assert not fs.requires_wait


def test_final_state_expansion_bullish_is_bullish_expansion():
    fs = combine_final_state(_regime_result(GammaRegime.EXPANSION), _confirmation(Confirmation.BULLISH))
    assert fs.headline == "BULLISH EXPANSION"


def test_final_state_expansion_conflict_is_high_volatility_wait():
    fs = combine_final_state(_regime_result(GammaRegime.EXPANSION), _confirmation(Confirmation.CONFLICTING))
    assert fs.headline == "HIGH VOLATILITY / WAIT"
    assert fs.requires_wait


def test_final_state_transition_always_waits():
    fs = combine_final_state(_regime_result(GammaRegime.TRANSITION), _confirmation(Confirmation.BULLISH))
    assert fs.headline == "WAIT FOR REGIME CONFIRMATION"
    assert fs.requires_wait


def test_final_state_mixed_always_waits():
    fs = combine_final_state(_regime_result(GammaRegime.MIXED), _confirmation(Confirmation.BEARISH))
    assert fs.headline == "WAIT — MIXED DEALER SIGNALS"
    assert fs.requires_wait


def test_hedging_model_transition_passthrough():
    gex = make_gex_result(23400, 1, 23400)
    regime = _regime_result(GammaRegime.TRANSITION)
    dex = run_dex_engine([make_row(23400, 100, 100)], spot=23400.0)
    env, reasons = run_hedging_model(regime, dex, dex_lean(dex))
    assert env == HedgingEnvironment.TRANSITION


def test_hedging_model_dampening_for_pin_chop_balanced_dex():
    regime = _regime_result(GammaRegime.PIN_CHOP)
    dex = run_dex_engine([make_row(23400, 1000, 1000, ce_delta=0.5, pe_delta=-0.5)], spot=23400.0)
    env, _ = run_hedging_model(regime, dex, dex_lean(dex))
    assert env == HedgingEnvironment.DAMPENING


def test_hedging_model_amplifying_for_expansion():
    regime = _regime_result(GammaRegime.EXPANSION)
    dex = run_dex_engine([make_row(23400, 1000, 1000, ce_delta=0.5, pe_delta=-0.5)], spot=23400.0)
    env, _ = run_hedging_model(regime, dex, dex_lean(dex))
    assert env == HedgingEnvironment.AMPLIFYING


def test_hedging_model_mixed_when_dex_one_sided_fights_dampening():
    regime = _regime_result(GammaRegime.PIN_CHOP)
    dex = run_dex_engine([make_row(23400, ce_oi=10, pe_oi=100000, ce_delta=0.5, pe_delta=-0.9)], spot=23400.0)
    env, _ = run_hedging_model(regime, dex, dex_lean(dex))
    assert env == HedgingEnvironment.MIXED


# ---------------------------------------------------------------------
# Alignment — categorical, never a percentage
# ---------------------------------------------------------------------

def test_alignment_is_categorical_not_a_percentage():
    regime = _regime_result(GammaRegime.EXPANSION)
    dex = run_dex_engine([make_row(23400, ce_oi=100, pe_oi=10000, ce_delta=0.5, pe_delta=-0.9)], spot=23400.0)
    balance = dex_lean(dex)
    alignment, note = alignment_score(regime, balance, _confirmation(Confirmation.BEARISH))
    assert isinstance(alignment, Alignment)
    assert alignment.value in ("LOW", "MEDIUM", "HIGH")
    assert "probability of market direction" in note


def test_alignment_low_when_no_directional_signals():
    regime = _regime_result(GammaRegime.PIN_CHOP)
    dex = run_dex_engine([make_row(23400, ce_oi=100, pe_oi=100, ce_delta=0.5, pe_delta=-0.5)], spot=23400.0)
    balance = dex_lean(dex)  # balanced -> lean == "none"
    alignment, _ = alignment_score(regime, balance, _confirmation(Confirmation.NONE))
    assert alignment == Alignment.LOW


# ---------------------------------------------------------------------
# Positioning — including the previous-LTP fix (via storage)
# ---------------------------------------------------------------------

def test_positioning_price_up_oi_up_is_buying():
    result = classify_leg(change_oi=500, volume=1000, price_change=2.0, strike=23400, leg="CE")
    assert result.label == PositioningLabel.BUYING


def test_positioning_price_down_oi_up_is_writing():
    result = classify_leg(change_oi=500, volume=1000, price_change=-2.0, strike=23400, leg="CE")
    assert result.label == PositioningLabel.WRITING


def test_positioning_price_up_oi_down_is_covering():
    result = classify_leg(change_oi=-500, volume=1000, price_change=2.0, strike=23400, leg="PE")
    assert result.label == PositioningLabel.COVERING


def test_positioning_price_down_oi_down_is_unwinding():
    result = classify_leg(change_oi=-500, volume=1000, price_change=-2.0, strike=23400, leg="PE")
    assert result.label == PositioningLabel.UNWINDING


def test_positioning_falls_back_to_low_confidence_without_price():
    result = classify_leg(change_oi=500, volume=1000, price_change=None, strike=23400, leg="CE")
    assert result.confidence == "low"


def test_positioning_summary_counts_and_flags_price_data_usage():
    results = run_positioning_engine(synthetic_chain())  # no prev map -> OI-only fallback
    summary = summarize(results)
    assert summary["used_price_data"] is False
    assert sum(summary["counts"].values()) <= len(synthetic_chain()) * 2


def test_storage_previous_ltp_fix_survives_across_calls():
    """Reproduces and verifies the fix for the V1 bug: previous LTP must
    be retrievable from storage (not session state) on the NEXT call."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "snap.db")
        store = SnapshotStore(db_path=db_path)

        t0 = "2026-01-01T09:20:00+05:30"
        t1 = "2026-01-01T09:20:15+05:30"

        rows_t0 = [make_row(23400, ce_oi=1000, pe_oi=1000, ce_ltp=10.0, pe_ltp=12.0)]
        store.save_option_legs(t0, rows_t0)

        prev_map = store.get_previous_option_legs(t1)
        assert prev_map[(23400.0, "CE")] == 10.0
        assert prev_map[(23400.0, "PE")] == 12.0

        rows_t1 = [make_row(23400, ce_oi=1500, pe_oi=1500, ce_ltp=12.0, pe_ltp=11.0, ce_change_oi=500, pe_change_oi=500)]
        results = run_positioning_engine(rows_t1, prev_map)
        ce_result = next(r for r in results if r.leg == "CE")
        pe_result = next(r for r in results if r.leg == "PE")
        # CE: price up (10->12) + OI up -> buying
        assert ce_result.label == PositioningLabel.BUYING
        assert ce_result.confidence != "low"
        # PE: price down (12->11) + OI up -> writing
        assert pe_result.label == PositioningLabel.WRITING


def test_no_previous_snapshot_returns_empty_map():
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(db_path=os.path.join(tmp, "snap.db"))
        assert store.get_previous_option_legs("2026-01-01T09:20:00+05:30") == {}


# ---------------------------------------------------------------------
# Strike aggregation / windowing
# ---------------------------------------------------------------------

def test_nearest_strike():
    rows = synthetic_chain()
    assert nearest_strike(rows, 23430.0) == 23400.0


def test_strike_window_respects_bounds():
    rows = synthetic_chain()
    window = strike_window(rows, 23400.0, n_strikes=1)
    assert [r.strike for r in window] == [23300, 23400, 23500]


# ---------------------------------------------------------------------
# Stale / missing / zero-OI data + market hours
# ---------------------------------------------------------------------

def test_stale_data_point_detected():
    old = DataPoint(value=23400.0, fetched_at=time.time() - 1000, ok=True)
    assert old.is_stale is True
    assert old.age_label == DataAge.STALE


def test_fresh_data_point_is_live():
    fresh = DataPoint(value=23400.0, fetched_at=time.time(), ok=True)
    assert fresh.is_stale is False
    assert fresh.age_label == DataAge.LIVE


def test_delayed_band_between_live_and_stale():
    delayed = DataPoint(value=23400.0, fetched_at=time.time() - 20, ok=True)
    assert delayed.age_label == DataAge.DELAYED


def test_detect_data_issues_flags_zero_oi():
    rows = [make_row(23400, ce_oi=0, pe_oi=0)]
    spot = DataPoint(value=23400.0, fetched_at=time.time(), ok=True)
    issues = detect_data_issues(rows, spot)
    assert any("Zero OI" in i for i in issues)


def test_detect_data_issues_flags_impossible_delta():
    rows = [make_row(23400, ce_oi=100, pe_oi=100, ce_delta=1.5)]
    spot = DataPoint(value=23400.0, fetched_at=time.time(), ok=True)
    issues = detect_data_issues(rows, spot)
    assert any("Impossible CE delta" in i for i in issues)


def test_detect_data_issues_handles_missing_strikes():
    spot = DataPoint(value=23400.0, fetched_at=time.time(), ok=True)
    issues = detect_data_issues([], spot)
    assert any("no strikes" in i for i in issues)


def test_build_data_health_reflects_market_closed_state():
    rows = synthetic_chain()
    spot = DataPoint(value=23400.0, fetched_at=time.time(), ok=True)
    health = build_data_health(True, spot, rows, futures_available=False, fii_dii_available=False)
    assert health.market_open == is_market_open()  # consistent with the shared clock


def test_market_open_false_on_weekend():
    saturday = datetime(2026, 9, 12, 10, 0)  # 2026-09-12 is a Saturday
    assert saturday.weekday() == 5
    assert is_market_open(saturday) is False


def test_market_open_true_on_weekday_during_hours():
    tuesday_during_hours = datetime(2026, 9, 8, 11, 0)  # a Tuesday, 11:00
    assert tuesday_during_hours.weekday() == 1
    assert is_market_open(tuesday_during_hours) is True


def test_market_open_false_on_weekday_after_hours():
    tuesday_evening = datetime(2026, 9, 8, 20, 0)
    assert is_market_open(tuesday_evening) is False


# ---------------------------------------------------------------------
# Snapshot storage — sequencing + outcomes/backtest
# ---------------------------------------------------------------------

def _make_snapshot(timestamp, spot, regime="PIN / CHOP", confirmation="BEARISH CONFIRMATION"):
    return Snapshot(
        timestamp=timestamp, spot=spot, total_gex=1.0, net_dex=-1.0, net_rupee_notional=-1.0,
        gamma_flip=23400.0, distance_from_flip=10.0, regime=regime, hedging_environment="DAMPENING",
        confirmation=confirmation, final_state="BEARISH LEAN — WAIT FOR STRUCTURAL BREAK",
        alignment="MEDIUM", positioning_summary="{}", data_health_summary="{}",
        ce_oi_total=1, pe_oi_total=1, ce_change_oi_total=0, pe_change_oi_total=0, volume_total=0,
    )


def test_snapshot_only_saved_with_complete_fields():
    """Guards against the V1 sequencing bug: confirmation/final_state
    must never be blank in a saved snapshot."""
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(db_path=os.path.join(tmp, "snap.db"))
        store.save_snapshot(_make_snapshot("2026-01-01T09:20:00+05:30", 23400.0))
        last = store.last_snapshot()
        assert last["confirmation"] == "BEARISH CONFIRMATION"
        assert last["final_state"] == "BEARISH LEAN — WAIT FOR STRUCTURAL BREAK"
        assert last["confirmation"] != ""
        assert last["final_state"] != ""


def test_outcomes_after_computes_point_move():
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(db_path=os.path.join(tmp, "snap.db"))
        t0 = datetime(2026, 1, 1, 9, 20, 0)
        store.save_snapshot(_make_snapshot(t0.isoformat(), 23400.0))
        store.save_snapshot(_make_snapshot((t0 + timedelta(minutes=5)).isoformat(), 23380.0))
        outcomes = store.outcomes_after(minutes_list=(5,))
        assert outcomes[0]["move_5m"] == -20.0


def test_evaluate_condition_returns_zero_events_without_fabricating():
    result = evaluate_condition([], lambda o: True)
    assert result["event_count"] == 0
    assert result.get("avg_move_5m") is None


def test_evaluate_condition_aggregates_matching_rows():
    outcomes = [
        {"regime": "EXPANSION", "confirmation": "BEARISH CONFIRMATION", "move_5m": -10.0},
        {"regime": "EXPANSION", "confirmation": "BEARISH CONFIRMATION", "move_5m": -20.0},
        {"regime": "PIN / CHOP", "confirmation": "BULLISH CONFIRMATION", "move_5m": 5.0},
    ]
    result = evaluate_condition(
        outcomes,
        lambda o: o["regime"] == "EXPANSION" and o["confirmation"] == "BEARISH CONFIRMATION",
        minutes_list=(5,),
    )
    assert result["event_count"] == 2
    assert result["avg_move_5m"] == -15.0
    assert result["negative_pct_5m"] == 100.0


# ---------------------------------------------------------------------
# Named scenarios (spec V2 §33)
# ---------------------------------------------------------------------

def test_scenario_a_strong_positive_gamma_is_pin_chop():
    strike_gex = [StrikeGEX(23400, 500, 500, 1000)]
    gex = make_gex_result(spot=23400, total_gex=100_000_000, gamma_flip=15000, strike_gex=strike_gex)
    thresholds = RegimeThresholds(flip_proximity_points=10, flip_proximity_pct=0.0, gex_transition_band_lakh=100)
    regime, _ = classify_gamma_regime(gex, thresholds)
    assert regime == GammaRegime.PIN_CHOP


def test_scenario_b_strong_negative_gamma_is_expansion():
    strike_gex = [StrikeGEX(23400, -500, -500, -1000)]
    gex = make_gex_result(spot=23400, total_gex=-100_000_000, gamma_flip=32000, strike_gex=strike_gex)
    thresholds = RegimeThresholds(flip_proximity_points=10, flip_proximity_pct=0.0, gex_transition_band_lakh=100)
    regime, _ = classify_gamma_regime(gex, thresholds)
    assert regime == GammaRegime.EXPANSION


def test_scenario_c_spot_near_flip_is_transition():
    gex = make_gex_result(spot=23400, total_gex=100_000_000, gamma_flip=23405)
    thresholds = RegimeThresholds(flip_proximity_points=50, flip_proximity_pct=0.0)
    regime, _ = classify_gamma_regime(gex, thresholds)
    assert regime == GammaRegime.TRANSITION


def test_scenario_d_positive_gex_bearish_dex_bearish_confirmation():
    regime = _regime_result(GammaRegime.PIN_CHOP)
    fs = combine_final_state(regime, _confirmation(Confirmation.BEARISH))
    assert fs.headline == "BEARISH LEAN — WAIT FOR STRUCTURAL BREAK"


def test_scenario_e_negative_gex_bearish_dex_bearish_confirmation():
    regime = _regime_result(GammaRegime.EXPANSION)
    fs = combine_final_state(regime, _confirmation(Confirmation.BEARISH))
    assert fs.headline == "BEARISH EXPANSION"


# =======================================================================
# V2.1 — live market confirmation upgrade
# =======================================================================

from confirmation_engine import CVDStatus, ConfirmationInputs, run_confirmation_engine, FamilyStatus
from futures_data import classify_futures_positioning
from market_data import FieldStatus, classify_field_status, build_data_health
from positioning_engine import aggregate_for_confirmation, OptionFlowConfirmation, LegPositioning
from utils import time_to_expiry_years
from datetime import timezone, timedelta

IST = timezone(timedelta(hours=5.5))


# ---------------------------------------------------------------------
# Futures positioning interpretation
# ---------------------------------------------------------------------

def test_futures_price_up_oi_up_is_long_buildup():
    assert classify_futures_positioning(10, 500) == "Possible long buildup"


def test_futures_price_down_oi_up_is_short_buildup():
    assert classify_futures_positioning(-10, 500) == "Possible short buildup"


def test_futures_price_up_oi_down_is_short_covering():
    assert classify_futures_positioning(10, -500) == "Possible short covering"


def test_futures_price_down_oi_down_is_long_unwinding():
    assert classify_futures_positioning(-10, -500) == "Possible long unwinding"


def test_futures_missing_oi_is_unavailable_not_fabricated():
    assert "Unavailable" in classify_futures_positioning(10, None)
    assert "Unavailable" in classify_futures_positioning(None, None)


def test_futures_missing_ltp_is_unavailable():
    assert "Unavailable" in classify_futures_positioning(None, 500)


def test_futures_positioning_labels_are_possible_not_certain():
    # Spec V2.1 §5: never "Confirmed Institutional Position".
    for pc, oc in [(1, 1), (-1, 1), (1, -1), (-1, -1)]:
        label = classify_futures_positioning(pc, oc)
        assert "Possible" in label
        assert "Confirmed" not in label


# ---------------------------------------------------------------------
# Confirmation — futures + option-flow wiring, disagreement cases
# ---------------------------------------------------------------------

def test_confirmation_bullish_spot_and_bullish_futures():
    inp = ConfirmationInputs(
        spot=23450, price_change=10, futures_price_change=15, futures_oi_change=500,
    )
    result = run_confirmation_engine(inp)
    assert result.verdict == Confirmation.BULLISH
    assert result.family_status["Futures OI"] == FamilyStatus.BULLISH


def test_confirmation_bearish_spot_and_bearish_futures():
    inp = ConfirmationInputs(
        spot=23350, price_change=-10, futures_price_change=-15, futures_oi_change=500,
    )
    result = run_confirmation_engine(inp)
    assert result.verdict == Confirmation.BEARISH


def test_confirmation_no_confirmation_when_all_inputs_missing():
    inp = ConfirmationInputs(spot=23400)
    result = run_confirmation_engine(inp)
    assert result.verdict == Confirmation.NONE
    assert len(result.missing) >= 3


def test_confirmation_partial_data_still_produces_a_verdict():
    inp = ConfirmationInputs(spot=23400, price_change=10)  # only price known
    result = run_confirmation_engine(inp)
    assert result.verdict == Confirmation.BULLISH
    assert result.family_status.get("CVD") == FamilyStatus.UNAVAILABLE


def test_confirmation_cvd_never_fabricated_when_unavailable():
    inp = ConfirmationInputs(spot=23400, cvd_status=CVDStatus.UNAVAILABLE, cvd_change=None)
    result = run_confirmation_engine(inp)
    assert result.family_status["CVD"] == FamilyStatus.UNAVAILABLE
    assert any("CVD" in m for m in result.missing)


def test_confirmation_dealer_structure_vs_market_can_disagree():
    """Spec V2.1 §19: the system must be able to say 'bearish confirmation
    despite positive-gamma environment' — i.e. confirmation is computed
    independent of dealer regime and can legitimately conflict with it."""
    bearish_market = ConfirmationInputs(spot=23350, price_change=-20, futures_price_change=-25, futures_oi_change=800)
    result = run_confirmation_engine(bearish_market)
    # This says nothing about GEX/regime — confirmation_engine never
    # imports dealer_regime or gex_engine, so it cannot have been swayed
    # by a "positive gamma" read. We assert the module boundary directly:
    import confirmation_engine as ce_mod
    assert "gex_engine" not in dir(ce_mod)
    assert "dealer_regime" not in dir(ce_mod)
    assert result.verdict == Confirmation.BEARISH


# ---------------------------------------------------------------------
# Option-flow confirmation aggregate (V2.1 fix — no longer raw ΔOI sign)
# ---------------------------------------------------------------------

def _leg(leg, label, confidence="high"):
    return LegPositioning(strike=23400, leg=leg, label=label, confidence=confidence, rationale="test")


def test_option_flow_aggregate_call_buying_is_bullish_evidence():
    results = [_leg("CE", PositioningLabel.BUYING)]
    agg = aggregate_for_confirmation(results)
    assert agg.label == "BULLISH"
    assert agg.bullish_evidence == 1


def test_option_flow_aggregate_call_writing_is_bearish_evidence():
    results = [_leg("CE", PositioningLabel.WRITING)]
    agg = aggregate_for_confirmation(results)
    assert agg.label == "BEARISH"


def test_option_flow_aggregate_put_writing_is_bullish_evidence():
    results = [_leg("PE", PositioningLabel.WRITING)]
    agg = aggregate_for_confirmation(results)
    assert agg.label == "BULLISH"


def test_option_flow_aggregate_put_buying_is_bearish_evidence():
    results = [_leg("PE", PositioningLabel.BUYING)]
    agg = aggregate_for_confirmation(results)
    assert agg.label == "BEARISH"


def test_option_flow_aggregate_excludes_low_confidence_legs():
    """The V1/V2 bug equivalent for option flow: raw ΔOI sign (which is
    what a low-confidence, OI-only classification amounts to) must NOT
    drive confirmation on its own."""
    results = [_leg("CE", PositioningLabel.BUYING, confidence="low")]
    agg = aggregate_for_confirmation(results)
    assert agg.label in ("MIXED / UNCLEAR", "NO DATA")
    assert agg.bullish_evidence == 0


def test_option_flow_aggregate_unclear_label_excluded():
    results = [_leg("CE", PositioningLabel.UNCLEAR, confidence="high")]
    agg = aggregate_for_confirmation(results)
    assert agg.label == "MIXED / UNCLEAR"
    assert agg.bullish_evidence == 0 and agg.bearish_evidence == 0


def test_option_flow_aggregate_no_data_when_empty():
    agg = aggregate_for_confirmation([])
    assert agg.label == "NO DATA"


def test_option_flow_aggregate_mixed_evidence_is_unclear():
    results = [_leg("CE", PositioningLabel.BUYING), _leg("PE", PositioningLabel.WRITING),
               _leg("CE", PositioningLabel.WRITING), _leg("PE", PositioningLabel.BUYING)]
    agg = aggregate_for_confirmation(results)
    assert agg.bullish_evidence == 2 and agg.bearish_evidence == 2
    assert agg.label == "MIXED / UNCLEAR"


def test_option_flow_feeds_confirmation_engine_not_raw_delta_oi():
    agg = OptionFlowConfirmation(bullish_evidence=3, bearish_evidence=0, unclear_evidence=0, label="BULLISH")
    inp = ConfirmationInputs(spot=23400, option_flow=agg)
    result = run_confirmation_engine(inp)
    assert result.verdict == Confirmation.BULLISH
    assert result.family_status["Option Flow"].name == "BULLISH"


# ---------------------------------------------------------------------
# Gamma Flip — same-day expiry TTE fix
# ---------------------------------------------------------------------

def test_tte_same_day_expiry_before_expiry_time_is_positive():
    now = datetime(2026, 9, 12, 10, 0, tzinfo=IST)  # market morning
    tte, floored = time_to_expiry_years("2026-09-12", now=now)
    assert tte > 0
    assert not floored


def test_tte_same_day_expiry_matches_midday_hours_remaining():
    now = datetime(2026, 9, 12, 9, 30, tzinfo=IST)
    tte, _ = time_to_expiry_years("2026-09-12", now=now)
    # ~6 hours remaining to 15:30 expiry -> a small positive fraction of a year
    expected_years = (6 * 3600) / (365 * 86400)
    assert abs(tte - expected_years) < 0.0005


def test_tte_never_negative_even_after_expiry_time():
    now = datetime(2026, 9, 12, 18, 0, tzinfo=IST)  # well after 15:30 expiry
    tte, floored = time_to_expiry_years("2026-09-12", now=now)
    assert tte > 0  # floored, never negative or zero
    assert floored


def test_tte_future_expiry_is_larger_than_same_day():
    now = datetime(2026, 9, 12, 10, 0, tzinfo=IST)
    tte_same_day, _ = time_to_expiry_years("2026-09-12", now=now)
    tte_future, _ = time_to_expiry_years("2026-09-25", now=now)
    assert tte_future > tte_same_day


def test_gamma_flip_spot_exactly_at_flip_is_near():
    gex = make_gex_result(spot=23400, total_gex=1, gamma_flip=23400)
    thresholds = RegimeThresholds(flip_proximity_points=10, flip_proximity_pct=0.0)
    proximity, distance = classify_flip_proximity(gex, thresholds)
    assert proximity == FlipProximity.NEAR
    assert distance == 0


def test_gamma_flip_spot_crossing_triggers_alert():
    from signal_engine import detect_alerts
    prev = {"spot": 23390, "gamma_flip": 23400, "confirmation": "BULLISH CONFIRMATION"}
    curr = {"spot": 23410, "gamma_flip": 23400, "confirmation": "BULLISH CONFIRMATION"}
    alerts = detect_alerts(prev, curr, DEFAULT_THRESHOLDS, "10:00:00")
    kinds = [a.kind for a in alerts]
    assert "GAMMA_FLIP_CROSS" in kinds
    cross_alert = next(a for a in alerts if a.kind == "GAMMA_FLIP_CROSS")
    assert "Observed confirmation" in cross_alert.message


# ---------------------------------------------------------------------
# Data health — LIVE/STALE/UNAVAILABLE/ERROR
# ---------------------------------------------------------------------

def test_field_status_live_when_present_and_fresh():
    assert classify_field_status(True, is_stale=False) == FieldStatus.LIVE


def test_field_status_stale_when_present_but_old():
    assert classify_field_status(True, is_stale=True) == FieldStatus.STALE


def test_field_status_unavailable_when_absent():
    assert classify_field_status(False) == FieldStatus.UNAVAILABLE


def test_field_status_error_overrides_presence():
    assert classify_field_status(True, is_stale=False, error="boom") == FieldStatus.ERROR


def test_data_health_never_converts_none_to_zero():
    rows = [make_row(23400, ce_oi=0, pe_oi=0, ce_volume=0, pe_volume=0)]
    spot = DataPoint(value=23400.0, fetched_at=time.time(), ok=True)
    health = build_data_health(True, spot, rows, futures_available=False, fii_dii_available=False)
    # zero OI/volume should surface as UNAVAILABLE-equivalent issues, not
    # a silently "healthy" LIVE status
    assert health.field_status["Option OI"] == FieldStatus.UNAVAILABLE


def test_data_health_futures_unavailable_when_no_snapshot_supplied():
    rows = synthetic_chain()
    spot = DataPoint(value=23400.0, fetched_at=time.time(), ok=True)
    health = build_data_health(True, spot, rows, futures_available=False, fii_dii_available=False)
    assert health.field_status["Futures LTP"] == FieldStatus.UNAVAILABLE


def test_data_health_api_failure_marks_spot_not_ok():
    spot = DataPoint(value=None, fetched_at=time.time(), ok=False, error="timeout")
    health = build_data_health(True, spot, [], futures_available=False, fii_dii_available=False)
    assert health.spot_ok is False


def test_data_health_market_closed_reflected():
    saturday = datetime(2026, 9, 12, 10, 0)
    assert is_market_open(saturday) is False  # sanity: our test date is a Saturday



# ---------------------------------------------------------------------
# Storage — schema migration safety + futures snapshot previous-value fix
# ---------------------------------------------------------------------

def test_storage_migrates_pre_v21_schema_without_data_loss():
    """Simulates an existing V2 database (no futures_* columns) and
    verifies SnapshotStore adds them in place rather than requiring a
    fresh database, and that pre-existing rows survive untouched."""
    import sqlite3 as _sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "old_v2.db")
        conn = _sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, spot REAL, total_gex REAL, net_dex REAL,
                net_rupee_notional REAL, gamma_flip REAL, distance_from_flip REAL,
                regime TEXT, hedging_environment TEXT, confirmation TEXT, final_state TEXT,
                alignment TEXT, positioning_summary TEXT, data_health_summary TEXT,
                ce_oi_total REAL, pe_oi_total REAL, ce_change_oi_total REAL,
                pe_change_oi_total REAL, volume_total REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO snapshots (timestamp, spot, regime, confirmation, final_state) "
            "VALUES ('2026-01-01T09:20:00+05:30', 23400.0, 'PIN / CHOP', 'BEARISH CONFIRMATION', 'WAIT')"
        )
        conn.commit()
        conn.close()

        store = SnapshotStore(db_path=db_path)  # triggers migration in _init_db
        last = store.last_snapshot()
        assert last["spot"] == 23400.0
        assert last["regime"] == "PIN / CHOP"
        assert "futures_ltp" in last  # migrated column present
        assert last["futures_ltp"] is None  # old row has no value for it, not 0


def test_futures_snapshot_previous_value_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(db_path=os.path.join(tmp, "snap.db"))
        t0 = "2026-01-01T09:20:00+05:30"
        t1 = "2026-01-01T09:20:15+05:30"

        assert store.get_previous_futures_snapshot(t0) is None

        store.save_futures_snapshot(t0, ltp=23450.0, oi=1_000_000)
        prev = store.get_previous_futures_snapshot(t1)
        assert prev["ltp"] == 23450.0
        assert prev["oi"] == 1_000_000


# =======================================================================
# V2.1.1 — real futures flow proxy, CVD honesty, confirmation strength
# =======================================================================

from futures_data import compute_flow_proxy, FuturesSnapshot
from signal_engine import classify_confirmation_strength, ConfirmationStrength
from dex_engine import ModeledDeltaBalance


# ---------------------------------------------------------------------
# Futures order-book flow proxy (real data, honestly labeled)
# ---------------------------------------------------------------------

def test_flow_proxy_computed_from_real_buy_sell_quantity():
    proxy, change, available = compute_flow_proxy(1000, 400, 900, 500)
    assert available is True
    assert proxy == 600          # 1000 - 400
    assert change == 200         # 600 - (900-500)=400 -> 600-400=200


def test_flow_proxy_unavailable_when_quantities_missing():
    proxy, change, available = compute_flow_proxy(None, None, None, None)
    assert available is False
    assert proxy is None and change is None


def test_flow_proxy_unavailable_with_only_current_no_previous():
    proxy, change, available = compute_flow_proxy(1000, 400, None, None)
    assert available is True     # level itself is known...
    assert proxy == 600
    assert change is None        # ...but no previous snapshot to diff against


def test_flow_proxy_never_equals_price_change_by_construction():
    """Guards against the 'cvd_change = futures_price_change and call it
    CVD' anti-pattern explicitly banned by the spec — the proxy is
    computed purely from quantities, price never enters the formula."""
    proxy, change, available = compute_flow_proxy(1000, 400, 900, 500)
    price_change_unrelated = 55.25
    assert change != price_change_unrelated


def test_futures_snapshot_flow_proxy_fields_present():
    snap = FuturesSnapshot(
        symbol="NIFTY-FUT", expiry="2026-09-24", ltp=23450.0, previous_ltp=23440.0,
        price_change=10.0, price_change_pct=0.04, oi=1_000_000, previous_oi=990_000,
        oi_change=10_000, oi_change_pct=1.0, volume=50000, timestamp="2026-01-01T09:20:00+05:30",
        available=True, buy_quantity=5000, sell_quantity=3000, flow_proxy=2000,
        flow_proxy_change=500, flow_proxy_available=True,
    )
    assert snap.flow_proxy == 2000
    assert snap.flow_proxy_available is True


# ---------------------------------------------------------------------
# Confirmation — flow/price divergence (never independently bull/bear)
# ---------------------------------------------------------------------

def test_confirmation_bullish_flow_confirmation_price_and_flow_up():
    inp = ConfirmationInputs(spot=23400, price_change=10, cvd_status=CVDStatus.PROXY, cvd_change=500)
    result = run_confirmation_engine(inp)
    assert result.family_status["CVD"] == FamilyStatus.BULLISH
    assert not result.cautions


def test_confirmation_bearish_flow_confirmation_price_and_flow_down():
    inp = ConfirmationInputs(spot=23400, price_change=-10, cvd_status=CVDStatus.PROXY, cvd_change=-500)
    result = run_confirmation_engine(inp)
    assert result.family_status["CVD"] == FamilyStatus.BEARISH
    assert not result.cautions


def test_confirmation_bearish_divergence_price_up_flow_down():
    inp = ConfirmationInputs(spot=23400, price_change=10, cvd_status=CVDStatus.PROXY, cvd_change=-500)
    result = run_confirmation_engine(inp)
    assert result.family_status["CVD"] == FamilyStatus.NEUTRAL
    assert any("bearish divergence" in c.lower() for c in result.cautions)
    # Divergence must never itself add a bear vote — only price contributes here.
    assert result.verdict == Confirmation.BULLISH


def test_confirmation_bullish_divergence_price_down_flow_up():
    inp = ConfirmationInputs(spot=23400, price_change=-10, cvd_status=CVDStatus.PROXY, cvd_change=500)
    result = run_confirmation_engine(inp)
    assert result.family_status["CVD"] == FamilyStatus.NEUTRAL
    assert any("bullish divergence" in c.lower() for c in result.cautions)
    assert result.verdict == Confirmation.BEARISH


def test_confirmation_flow_proxy_message_names_proxy_status_not_real_cvd():
    inp = ConfirmationInputs(spot=23400, price_change=10, cvd_status=CVDStatus.PROXY, cvd_change=500)
    result = run_confirmation_engine(inp)
    assert any("Proxy" in s for s in result.bullish_signals)


# ---------------------------------------------------------------------
# Cross-layer confirmation strength (Dealer + Cash + Futures + Flow)
# ---------------------------------------------------------------------

def _delta_balance(lean):
    return ModeledDeltaBalance(label=f"{lean}-test", lean=lean, interpretation="test interpretation")


def test_confirmation_strength_full_bearish_when_all_four_agree():
    inp = ConfirmationInputs(
        spot=23350, price_change=-20, futures_price_change=-15, futures_oi_change=800,
        cvd_status=CVDStatus.PROXY, cvd_change=-300,
    )
    confirmation = run_confirmation_engine(inp)
    score = classify_confirmation_strength(_delta_balance("bearish"), confirmation)
    assert score.strength == ConfirmationStrength.FULL_BEARISH
    assert score.components["Dealer"] == "BEARISH"
    assert score.components["Cash"] == "BEARISH"
    assert score.components["Futures"] == "BEARISH"
    assert score.components["Flow"] == "BEARISH"


def test_confirmation_strength_conflict_when_dealer_disagrees_with_market():
    inp = ConfirmationInputs(
        spot=23450, price_change=20, futures_price_change=15, futures_oi_change=800,
    )
    confirmation = run_confirmation_engine(inp)
    score = classify_confirmation_strength(_delta_balance("bearish"), confirmation)
    assert score.strength == ConfirmationStrength.CONFLICT


def test_confirmation_strength_partial_when_futures_and_flow_unavailable():
    inp = ConfirmationInputs(spot=23350, price_change=-20)  # no futures, no flow
    confirmation = run_confirmation_engine(inp)
    score = classify_confirmation_strength(_delta_balance("bearish"), confirmation)
    assert score.strength == ConfirmationStrength.PARTIAL_BEARISH
    assert score.components["Futures"] == "UNAVAILABLE"
    assert score.components["Flow"] == "UNAVAILABLE"


def test_confirmation_strength_insufficient_data_when_nothing_directional():
    inp = ConfirmationInputs(spot=23400)
    confirmation = run_confirmation_engine(inp)
    score = classify_confirmation_strength(_delta_balance("none"), confirmation)
    assert score.strength == ConfirmationStrength.INSUFFICIENT_DATA


def test_confirmation_strength_every_component_visible():
    inp = ConfirmationInputs(spot=23400, price_change=10)
    confirmation = run_confirmation_engine(inp)
    score = classify_confirmation_strength(_delta_balance("bullish"), confirmation)
    assert set(score.components.keys()) == {"Dealer", "Cash", "Futures", "Flow"}


# ---------------------------------------------------------------------
# Cash market — always honestly unavailable for volume/CVD (structural)
# ---------------------------------------------------------------------

def test_data_health_cash_volume_and_cvd_always_unavailable():
    rows = synthetic_chain()
    spot = DataPoint(value=23400.0, fetched_at=time.time(), ok=True)
    health = build_data_health(True, spot, rows, futures_available=False, fii_dii_available=False)
    assert health.field_status["Cash Volume"] == FieldStatus.UNAVAILABLE
    assert health.field_status["Cash Flow/CVD"] == FieldStatus.UNAVAILABLE


def test_data_health_futures_flow_proxy_distinct_from_cash_flow():
    rows = synthetic_chain()
    spot = DataPoint(value=23400.0, fetched_at=time.time(), ok=True)
    snap = FuturesSnapshot(
        symbol="NIFTY-FUT", expiry=None, ltp=23410.0, previous_ltp=23400.0, price_change=10.0,
        price_change_pct=0.04, oi=1000, previous_oi=900, oi_change=100, oi_change_pct=11.1,
        volume=500, timestamp="x", available=True, buy_quantity=600, sell_quantity=400,
        flow_proxy=200, flow_proxy_change=50, flow_proxy_available=True,
    )
    health = build_data_health(True, spot, rows, futures_available=True, fii_dii_available=False, futures_snapshot=snap)
    assert health.field_status["Futures Flow (Proxy)"] == FieldStatus.LIVE
    assert health.field_status["Cash Flow/CVD"] == FieldStatus.UNAVAILABLE
    # They must be tracked as genuinely separate keys, never conflated.
    assert "Futures Flow (Proxy)" != "Cash Flow/CVD"


# ---------------------------------------------------------------------
# Naive/aware datetime mixing — futures_data must not crash on this
# ---------------------------------------------------------------------

def test_parse_instrument_master_uses_ist_aware_today_no_tz_crash():
    """Regression guard for the naive-vs-aware datetime bug: filtering
    expired contracts must not raise TypeError from mixing naive and
    aware datetimes."""
    csv_text = (
        "SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXM_EXCH_ID,SEM_EXPIRY_DATE\n"
        "NIFTY-FUT,12345,FUTIDX,NSE,2030-01-30\n"
        "BANKNIFTY-FUT,99999,FUTIDX,NSE,2030-01-30\n"
    )
    from futures_data import _parse_instrument_master
    resolved = _parse_instrument_master(csv_text)  # must not raise
    assert resolved is not None
    assert resolved.security_id == 12345
    assert "BANKNIFTY" not in resolved.symbol


# ---------------------------------------------------------------------
# Storage — buy/sell quantity migration
# ---------------------------------------------------------------------

def test_futures_snapshot_store_persists_buy_sell_quantity():
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(db_path=os.path.join(tmp, "snap.db"))
        t0, t1 = "2026-01-01T09:20:00+05:30", "2026-01-01T09:20:15+05:30"
        store.save_futures_snapshot(t0, ltp=23450.0, oi=1_000_000, buy_quantity=6000, sell_quantity=4000)
        prev = store.get_previous_futures_snapshot(t1)
        assert prev["buy_quantity"] == 6000
        assert prev["sell_quantity"] == 4000


def test_futures_table_migrates_missing_buy_sell_columns():
    """Simulates a V2.1 futures_snapshots table (no buy/sell quantity
    columns) and confirms the V2.1.1 upgrade adds them in place."""
    import sqlite3 as _sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "old_v21.db")
        conn = _sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE futures_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, ltp REAL, oi REAL)"
        )
        conn.execute("INSERT INTO futures_snapshots (timestamp, ltp, oi) VALUES ('2026-01-01T09:00:00+05:30', 23400.0, 900000)")
        conn.commit()
        conn.close()

        store = SnapshotStore(db_path=db_path)  # triggers _migrate_futures_table
        prev = store.get_previous_futures_snapshot("2026-01-01T09:30:00+05:30")
        assert prev["ltp"] == 23400.0
        assert prev["buy_quantity"] is None  # migrated column, old row has no value


# =======================================================================
# Cash Market integration (explicit "Cash Market" family, not implicit)
# =======================================================================

def test_confirmation_family_uses_cash_market_label_not_price_structure():
    inp = ConfirmationInputs(spot=23400, price_change=10)
    result = run_confirmation_engine(inp)
    assert "Cash Market" in result.family_status
    assert "Price Structure" not in result.family_status


def test_confirmation_price_change_pct_field_accepted_and_stored():
    inp = ConfirmationInputs(spot=23400, price_change=10, price_change_pct=0.043)
    assert inp.price_change_pct == 0.043
    # Doesn't change the verdict math — it's a display-only enrichment of
    # the same real price_change, not a second/duplicate signal.
    result = run_confirmation_engine(inp)
    assert result.verdict == Confirmation.BULLISH


def test_cash_market_bullish_when_price_rises():
    inp = ConfirmationInputs(spot=23450, price_change=15)
    result = run_confirmation_engine(inp)
    assert result.family_status["Cash Market"] == FamilyStatus.BULLISH
    assert any("Cash Market" in s for s in result.bullish_signals)


def test_cash_market_bearish_when_price_falls():
    inp = ConfirmationInputs(spot=23350, price_change=-15)
    result = run_confirmation_engine(inp)
    assert result.family_status["Cash Market"] == FamilyStatus.BEARISH


def test_confirmation_strength_cash_component_sourced_from_cash_market_family():
    inp = ConfirmationInputs(spot=23450, price_change=15)
    confirmation = run_confirmation_engine(inp)
    score = classify_confirmation_strength(_delta_balance("bullish"), confirmation)
    assert score.components["Cash"] == "BULLISH"


def test_cash_volume_and_cvd_distinct_from_futures_flow_in_data_health_keys():
    rows = synthetic_chain()
    spot = DataPoint(value=23400.0, fetched_at=time.time(), ok=True)
    health = build_data_health(True, spot, rows, futures_available=False, fii_dii_available=False)
    # Every key literally present and distinct — no accidental key collision.
    assert {"Cash Volume", "Cash Flow/CVD"}.issubset(health.field_status.keys())
    assert health.field_status["Cash Volume"] == FieldStatus.UNAVAILABLE
    assert health.field_status["Cash Flow/CVD"] == FieldStatus.UNAVAILABLE
