"""
utils.py

Small shared helpers: IST time handling, market-hours detection, and
Indian-style unit formatting (lakh / crore). Kept dependency-free so it can
be unit-tested in isolation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import (
    IST_OFFSET_HOURS,
    MARKET_OPEN_HOUR,
    MARKET_OPEN_MIN,
    MARKET_CLOSE_HOUR,
    MARKET_CLOSE_MIN,
    EXPIRY_HOUR_IST,
    EXPIRY_MINUTE_IST,
    MIN_TTE_SECONDS,
    LAKH,
    CRORE,
)

IST = timezone(timedelta(hours=IST_OFFSET_HOURS))


def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open(dt: datetime | None = None) -> bool:
    """
    Returns True only Mon-Fri within NSE equity-derivatives hours
    (09:15-15:30 IST). Does NOT account for exchange holidays — the
    Data Health panel should be treated as a floor, not a full holiday
    calendar, unless a holiday list is wired in separately.
    """
    dt = dt or now_ist()
    if dt.weekday() >= 5:  # Sat/Sun
        return False
    open_t = dt.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0, microsecond=0)
    close_t = dt.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0, microsecond=0)
    return open_t <= dt <= close_t


def time_to_expiry_years(expiry_date_str: str, now: datetime | None = None) -> tuple[float, bool]:
    """
    Computes time-to-expiry in years for use in Black-Scholes, treating
    expiry as occurring at config.EXPIRY_HOUR_IST:EXPIRY_MINUTE_IST IST on
    the expiry date — NOT midnight (the V1/V2 bug: `strptime("%Y-%m-%d")`
    implicitly sets expiry at 00:00, which made same-day-expiry options
    look like they had negative time value for most of expiry day).

    Returns (tte_years, floored). `floored` is True if the raw computed
    TTE was at or below config.MIN_TTE_SECONDS and was clamped to that
    floor — callers/UI can use this to show an "expiry-day floor
    assumption applied" note rather than silently presenting a floored
    value as an exact calculation.

    expiry_date_str is expected in "YYYY-MM-DD" form (Dhan's expiry-list
    format). Malformed input raises ValueError — callers should treat
    that the same as "no valid expiry selected."
    """
    now = now or now_ist()
    expiry_date = datetime.strptime(expiry_date_str, "%Y-%m-%d")
    expiry_dt = expiry_date.replace(
        hour=EXPIRY_HOUR_IST, minute=EXPIRY_MINUTE_IST, second=0, microsecond=0, tzinfo=IST,
    )
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)

    seconds_remaining = (expiry_dt - now).total_seconds()
    floored = seconds_remaining <= MIN_TTE_SECONDS
    seconds_used = max(seconds_remaining, MIN_TTE_SECONDS)
    return seconds_used / (365.0 * 86400.0), floored


def format_inr_lakh_crore(value: float, unit: str = "L") -> str:
    """
    Format a raw notional-exposure number into lakh ('L') or crore ('Cr')
    display units, e.g. format_inr_lakh_crore(13150600) -> '+131.51L'

    `value` is assumed to already be in absolute rupee-equivalent notional
    units (see gex_engine / dex_engine for how that's derived). Sign is
    preserved and shown explicitly.
    """
    sign = "+" if value >= 0 else "-"
    absval = abs(value)
    if unit == "Cr":
        scaled = absval / CRORE
        return f"{sign}{scaled:,.2f}Cr"
    scaled = absval / LAKH
    return f"{sign}{scaled:,.0f}L"


def pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default
