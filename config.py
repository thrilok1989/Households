"""
config.py

Central configuration for NIFTY Dealer Intelligence.

Nothing here is a secret. Credentials are loaded via load_credentials()
from Streamlit secrets (preferred) or environment variables (.env), never
hard-coded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

try:
    import streamlit as st
    _HAS_STREAMLIT = True
except ImportError:  # allows config to be imported in non-Streamlit contexts (tests)
    _HAS_STREAMLIT = False

from dotenv import load_dotenv

load_dotenv()


# --------------------------------------------------------------------------
# Instrument defaults
# --------------------------------------------------------------------------

NIFTY_SECURITY_ID = 13
NIFTY_SEGMENT = "IDX_I"
NIFTY_LOT_SIZE = 75

# Strikes shown in the option-chain table
DISPLAY_STRIKE_WINDOW = 5

# Strikes used internally for GEX / DEX aggregation. Configurable because
# dealer exposure calculated only on the displayed window understates true
# gamma concentration at wings.
EXPOSURE_STRIKE_WINDOW_DEFAULT = 15
EXPOSURE_STRIKE_WINDOW_MAX = 20

# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------
# GEX/DEX are expressed in "L" = lakh (1,00,000) of the underlying's
# notional exposure units, matching common Indian dealer-flow reporting
# conventions. 1 crore (Cr) = 100 lakh. See utils.format_inr_lakh_crore().
LAKH = 100_000
CRORE = 100 * LAKH

# --------------------------------------------------------------------------
# Regime thresholds (all configurable — do NOT hardcode a single cutoff)
# --------------------------------------------------------------------------

@dataclass
class RegimeThresholds:
    # Gamma regime: total GEX magnitude, in lakh notional, below which the
    # market is considered "near zero" / TRANSITION rather than a clean
    # POSITIVE or NEGATIVE gamma regime.
    gex_transition_band_lakh: float = 15_000.0

    # Spot proximity to Gamma Flip (in NIFTY points) within which the
    # market is treated as TRANSITION regardless of GEX sign.
    flip_proximity_points: float = 40.0

    # DEX shift alert: change in net DEX (lakh) between two snapshots that
    # counts as a "large" shift worth alerting on.
    dex_shift_alert_lakh: float = 1_000.0

    # Gamma concentration shift: fraction of total |GEX| that must move
    # from one strike to another between snapshots to trigger an alert.
    gamma_concentration_shift_pct: float = 0.15

    # Dealer pin zone: strikes within this many points of the peak GEX
    # strike are grouped into a single "pin zone" band.
    pin_zone_width_points: float = 100.0

    # Stale data: seconds since last tick after which data is treated as
    # stale and no new signal is generated from it.
    stale_data_seconds: int = 90

    # Data-age bands for the Data Health panel (section 25 v2): LIVE,
    # DELAYED, STALE. `stale_data_seconds` above remains the hard cutoff
    # past which a signal is refused; DELAYED is a softer warning band
    # between live and that cutoff.
    live_data_seconds: int = 10
    delayed_data_seconds: int = 30

    # --- Gamma Flip v2 (spot-dependent search) ---
    # How far above/below current spot to sweep when searching for
    # zero-gamma-crossing spot levels.
    flip_search_range_points: float = 500.0
    # Step size of the sweep. Smaller = more precise crossing location
    # but more Black-Scholes evaluations per refresh.
    flip_search_step_points: float = 25.0
    # "Near flip" can alternatively be expressed as a percentage of spot
    # rather than a fixed point distance — both are exposed; the
    # point-based `flip_proximity_points` above is used by default.
    flip_proximity_pct: float = 0.0025  # 0.25% of spot


REGIME_THRESHOLDS = RegimeThresholds()


# --------------------------------------------------------------------------
# Refresh intervals
# --------------------------------------------------------------------------

REFRESH_INTERVALS_SECONDS = [5, 10, 15, 30, 60]
DEFAULT_REFRESH_SECONDS = 15

# --------------------------------------------------------------------------
# Market hours (IST). Used to detect MARKET CLOSED state.
# --------------------------------------------------------------------------

MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 15
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 30
IST_OFFSET_HOURS = 5.5

# --------------------------------------------------------------------------
# Expiry / time-to-expiry (V2.1 fix — same-day expiry TTE bug)
# --------------------------------------------------------------------------
# NSE index F&O contracts expire intraday, not at midnight. Using
# datetime.strptime("%Y-%m-%d") (implicit midnight) for TTE math made
# same-day-expiry options look like they had *negative* time value for
# roughly the first 15h15m of expiry day. We instead treat expiry as
# occurring at this configured time (IST) on the expiry date.
EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST = 15, 30

# Floor on time-to-expiry (in years) passed into Black-Scholes. Once
# actual time-to-expiry drops below this (e.g. the last minute before
# expiry, or the sweep is evaluated fractionally after expiry due to
# clock skew), we clamp to this floor rather than passing zero/negative
# TTE into the Greeks formulas (which would divide by zero or produce
# nonsense). This is a numerical-stability floor, NOT a claim that the
# option actually retains this much time value at expiry.
MIN_TTE_SECONDS = 60.0

# --------------------------------------------------------------------------
# Futures (V2.1 — live futures data module)
# --------------------------------------------------------------------------

NIFTY_FUTURES_SEGMENT = "NSE_FNO"

# Dhan publishes a public instrument master (compact CSV) that maps every
# tradable security to its Security ID, including the currently-listed
# NIFTY futures contracts. We resolve the current-month contract from
# this dynamically instead of hard-coding a Security ID that goes stale
# every expiry. Verify this URL against Dhan's current API docs — vendors
# occasionally relocate these files.
DHAN_INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

# How long a resolved futures Security ID is cached for. The underlying
# contract only changes once a month (on expiry rollover), so this can be
# long — we just don't want to re-download the full instrument master on
# every Streamlit rerun.
FUTURES_INSTRUMENT_CACHE_TTL_SECONDS = 6 * 60 * 60

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

LOCAL_SNAPSHOT_DB = os.path.join(os.path.dirname(__file__), "data", "snapshots.db")


@dataclass
class SupabaseConfig:
    url: Optional[str] = None
    key: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)


@dataclass
class DhanCredentials:
    client_id: Optional[str] = None
    access_token: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.access_token)


def _get(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a config value from Streamlit secrets first, then env vars."""
    if _HAS_STREAMLIT:
        try:
            if key in st.secrets:
                return st.secrets[key]
        except Exception:
            pass
    return os.environ.get(key, default)


def load_credentials() -> DhanCredentials:
    return DhanCredentials(
        client_id=_get("DHAN_CLIENT_ID"),
        access_token=_get("DHAN_ACCESS_TOKEN"),
    )


def load_supabase_config() -> SupabaseConfig:
    return SupabaseConfig(
        url=_get("SUPABASE_URL"),
        key=_get("SUPABASE_KEY"),
    )


def load_futures_security_id_override() -> Optional[int]:
    """
    Optional escape hatch: if set, this security ID is used for NIFTY
    futures instead of dynamic resolution from the instrument master.
    Useful if the instrument master URL/format changes before this app
    is updated, or in restricted network environments that can't reach
    it. Never used as a *default* — dynamic resolution is preferred so
    the contract doesn't silently go stale after expiry rollover.
    """
    raw = _get("DHAN_NIFTY_FUT_SECURITY_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
