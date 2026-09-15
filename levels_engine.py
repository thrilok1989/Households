"""
levels_engine.py

Derives simple price levels used by the confirmation engine and the
Price + Dealer Map chart: naive support/resistance from recent snapshot
history, and VWAP when intraday tick history is available.

Kept deliberately simple — this is not a full technical-analysis engine,
just enough structure for "is price near a level" confirmation checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Levels:
    support: Optional[float]
    resistance: Optional[float]
    vwap: Optional[float]


def naive_support_resistance(recent_spots: list[float], lookback: int = 60) -> tuple[Optional[float], Optional[float]]:
    """
    recent_spots: chronological list of spot prices from stored snapshots
    (see signal_engine.SnapshotStore). Returns (support, resistance) as
    the min/max over the lookback window. This is intentionally simple —
    a true S/R engine would use swing points / volume profile, which can
    be swapped in here without changing callers.
    """
    window = recent_spots[-lookback:] if recent_spots else []
    if not window:
        return None, None
    return min(window), max(window)


def compute_vwap(price_volume_pairs: list[tuple[float, float]]) -> Optional[float]:
    """price_volume_pairs: [(price, volume), ...] for the session so far."""
    total_vol = sum(v for _, v in price_volume_pairs)
    if total_vol <= 0:
        return None
    return sum(p * v for p, v in price_volume_pairs) / total_vol


def build_levels(recent_spots: list[float], price_volume_pairs: Optional[list[tuple[float, float]]] = None) -> Levels:
    support, resistance = naive_support_resistance(recent_spots)
    vwap = compute_vwap(price_volume_pairs) if price_volume_pairs else None
    return Levels(support=support, resistance=resistance, vwap=vwap)
