"""
storage.py (V2)

Three responsibilities:

1. SnapshotStore — persists a COMPLETE snapshot only after every
   calculation stage has run (GEX -> DEX -> Gamma Flip -> positioning ->
   confirmation -> regime -> final state), fixing the V1 sequencing bug
   where confirmation/final_state were saved as blank placeholders
   because the snapshot was written before those stages ran. Callers
   (app.py) must call `save_snapshot()` exactly once, at the END of the
   pipeline, with every field populated.

2. Per-leg option snapshots — a reliable, Streamlit-rerun-proof way to
   get "previous LTP/OI by strike+leg" for positioning_engine.py,
   replacing the V1 bug where the positioning engine was never actually
   given a previous-snapshot map. Backed by SQLite, not st.session_state.

3. FiiDiiProvider — unchanged principle from V1: never fabricate FII/DII
   numbers. Dhan does not publish participant-wise FII/DII flow through
   its documented market APIs; returns "unavailable" for every field by
   default.

Also includes the historical Outcome/Backtest engine (spec V2 §28-29).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from dataclasses import dataclass, asdict, field
from typing import Callable, Optional

from config import LOCAL_SNAPSHOT_DB, SupabaseConfig


@dataclass
class Snapshot:
    timestamp: str
    spot: float
    total_gex: float
    net_dex: float
    net_rupee_notional: float
    gamma_flip: Optional[float]
    distance_from_flip: Optional[float]
    regime: str
    hedging_environment: str
    confirmation: str
    final_state: str
    alignment: str
    positioning_summary: str   # JSON-encoded — see positioning_engine.summarize()
    data_health_summary: str   # JSON-encoded — see market_data.DataHealth
    ce_oi_total: float
    pe_oi_total: float
    ce_change_oi_total: float
    pe_change_oi_total: float
    volume_total: float
    # V2.1 — observed futures fields (spec V2.1 §22). Optional/defaulted
    # so existing callers/tests that predate futures wiring still work.
    futures_ltp: Optional[float] = None
    futures_oi: Optional[float] = None
    futures_oi_change: Optional[float] = None
    futures_price_change: Optional[float] = None


# Columns added after the original V2 schema. Used by _migrate_snapshots_table
# to ALTER TABLE ADD COLUMN on existing databases rather than requiring a
# fresh delete — "do not break working V2" applies to people's saved
# snapshot history too, not just the code.
_SNAPSHOT_MIGRATION_COLUMNS = [
    ("futures_ltp", "REAL"),
    ("futures_oi", "REAL"),
    ("futures_oi_change", "REAL"),
    ("futures_price_change", "REAL"),
]


class SnapshotStore:
    def __init__(self, db_path: str = LOCAL_SNAPSHOT_DB, supabase_config: Optional[SupabaseConfig] = None):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self.supabase_config = supabase_config
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    spot REAL,
                    total_gex REAL,
                    net_dex REAL,
                    net_rupee_notional REAL,
                    gamma_flip REAL,
                    distance_from_flip REAL,
                    regime TEXT,
                    hedging_environment TEXT,
                    confirmation TEXT,
                    final_state TEXT,
                    alignment TEXT,
                    positioning_summary TEXT,
                    data_health_summary TEXT,
                    ce_oi_total REAL,
                    pe_oi_total REAL,
                    ce_change_oi_total REAL,
                    pe_change_oi_total REAL,
                    volume_total REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL
                )
                """
            )
            # Per-leg snapshots: the reliable "previous LTP/OI by
            # strike+leg" mechanism used to fix the positioning-engine bug.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_leg_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    strike REAL NOT NULL,
                    leg TEXT NOT NULL,
                    ltp REAL,
                    oi REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leg_ts ON option_leg_snapshots(timestamp)"
            )
            # Futures snapshots: mirrors option_leg_snapshots' previous-
            # value pattern, for futures LTP/OI (V2.1) and order-book
            # buy/sell quantity used for the flow proxy (V2.1.1).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS futures_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    ltp REAL,
                    oi REAL,
                    buy_quantity REAL,
                    sell_quantity REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_futures_ts ON futures_snapshots(timestamp)"
            )
            conn.commit()
            self._migrate_snapshots_table(conn)
            self._migrate_futures_table(conn)

    def _migrate_snapshots_table(self, conn: sqlite3.Connection) -> None:
        """Adds any V2.1 columns missing from a pre-existing `snapshots`
        table (created by V2), so upgrading in place never breaks an
        existing snapshots.db. Additive only — never drops/renames."""
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
        for col_name, col_type in _SNAPSHOT_MIGRATION_COLUMNS:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col_name} {col_type}")
        conn.commit()

    def _migrate_futures_table(self, conn: sqlite3.Connection) -> None:
        """Adds the V2.1.1 buy_quantity/sell_quantity columns to a
        pre-existing `futures_snapshots` table (created by V2.1)."""
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(futures_snapshots)").fetchall()}
        for col_name in ("buy_quantity", "sell_quantity"):
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE futures_snapshots ADD COLUMN {col_name} REAL")
        conn.commit()

    # ------------------------------------------------------------------
    # Full snapshots
    # ------------------------------------------------------------------

    def save_snapshot(self, snap: Snapshot) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO snapshots (
                    timestamp, spot, total_gex, net_dex, net_rupee_notional, gamma_flip,
                    distance_from_flip, regime, hedging_environment, confirmation, final_state,
                    alignment, positioning_summary, data_health_summary,
                    ce_oi_total, pe_oi_total, ce_change_oi_total, pe_change_oi_total, volume_total,
                    futures_ltp, futures_oi, futures_oi_change, futures_price_change
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap.timestamp, snap.spot, snap.total_gex, snap.net_dex, snap.net_rupee_notional,
                    snap.gamma_flip, snap.distance_from_flip, snap.regime, snap.hedging_environment,
                    snap.confirmation, snap.final_state, snap.alignment, snap.positioning_summary,
                    snap.data_health_summary, snap.ce_oi_total, snap.pe_oi_total,
                    snap.ce_change_oi_total, snap.pe_change_oi_total, snap.volume_total,
                    snap.futures_ltp, snap.futures_oi, snap.futures_oi_change, snap.futures_price_change,
                ),
            )
            conn.commit()

        if self.supabase_config and self.supabase_config.enabled:
            self._push_supabase_best_effort(snap)

    def _push_supabase_best_effort(self, snap: Snapshot) -> None:
        try:
            import requests
            requests.post(
                f"{self.supabase_config.url}/rest/v1/dealer_snapshots",
                headers={
                    "apikey": self.supabase_config.key,
                    "Authorization": f"Bearer {self.supabase_config.key}",
                    "Content-Type": "application/json",
                },
                json=asdict(snap),
                timeout=5,
            )
        except Exception:
            pass  # local SQLite write already succeeded; Supabase is best-effort

    def recent_spots(self, limit: int = 200) -> list[float]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT spot FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r[0] for r in reversed(rows)]

    def last_snapshot(self) -> Optional[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def all_snapshots(self) -> list[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM snapshots ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    def log_event(self, timestamp: str, kind: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_log (timestamp, kind, message) VALUES (?, ?, ?)",
                (timestamp, kind, message),
            )
            conn.commit()

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Per-leg snapshots (previous LTP/OI fix)
    # ------------------------------------------------------------------

    def save_option_legs(self, timestamp: str, rows: list) -> None:
        """rows: list[OptionChainRow]. Persists CE and PE LTP/OI per
        strike for this timestamp so the NEXT cycle can look up
        'previous LTP/OI by strike+leg' reliably, independent of
        Streamlit reruns or session state."""
        with self._connect() as conn:
            payload = []
            for r in rows:
                payload.append((timestamp, r.strike, "CE", r.ce_ltp, r.ce_oi))
                payload.append((timestamp, r.strike, "PE", r.pe_ltp, r.pe_oi))
            conn.executemany(
                "INSERT INTO option_leg_snapshots (timestamp, strike, leg, ltp, oi) VALUES (?, ?, ?, ?, ?)",
                payload,
            )
            conn.commit()

    def get_previous_option_legs(self, before_timestamp: str) -> dict[tuple[float, str], float]:
        """
        Returns {(strike, "CE"|"PE"): previous_ltp} from the most recent
        distinct timestamp strictly before `before_timestamp`. Empty dict
        if there's no prior snapshot yet (first run of the session).
        """
        with self._connect() as conn:
            ts_row = conn.execute(
                "SELECT DISTINCT timestamp FROM option_leg_snapshots WHERE timestamp < ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (before_timestamp,),
            ).fetchone()
            if not ts_row:
                return {}
            prev_ts = ts_row[0]
            rows = conn.execute(
                "SELECT strike, leg, ltp FROM option_leg_snapshots WHERE timestamp = ?",
                (prev_ts,),
            ).fetchall()
        return {(strike, leg): ltp for strike, leg, ltp in rows if ltp is not None}

    def prune_option_legs(self, keep_timestamps: int = 20) -> None:
        """Keeps only the most recent N distinct timestamps of per-leg
        data — this table grows fast (2 rows per strike per refresh)."""
        with self._connect() as conn:
            timestamps = [r[0] for r in conn.execute(
                "SELECT DISTINCT timestamp FROM option_leg_snapshots ORDER BY timestamp DESC LIMIT -1 OFFSET ?",
                (keep_timestamps,),
            ).fetchall()]
            if timestamps:
                conn.executemany(
                    "DELETE FROM option_leg_snapshots WHERE timestamp = ?",
                    [(t,) for t in timestamps],
                )
                conn.commit()

    # ------------------------------------------------------------------
    # Futures snapshots (previous LTP/OI fix — same pattern as options)
    # ------------------------------------------------------------------

    def save_futures_snapshot(
        self, timestamp: str, ltp: Optional[float], oi: Optional[float],
        buy_quantity: Optional[float] = None, sell_quantity: Optional[float] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO futures_snapshots (timestamp, ltp, oi, buy_quantity, sell_quantity) "
                "VALUES (?, ?, ?, ?, ?)",
                (timestamp, ltp, oi, buy_quantity, sell_quantity),
            )
            conn.commit()

    def get_previous_futures_snapshot(self, before_timestamp: str) -> Optional[dict]:
        """Returns {'ltp':.., 'oi':.., 'buy_quantity':.., 'sell_quantity':..}
        from the most recent distinct timestamp strictly before
        `before_timestamp`, or None if there's no prior futures snapshot yet."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT ltp, oi, buy_quantity, sell_quantity FROM futures_snapshots "
                "WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1",
                (before_timestamp,),
            ).fetchone()
        return dict(row) if row else None

    def prune_futures_snapshots(self, keep: int = 500) -> None:
        with self._connect() as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM futures_snapshots ORDER BY id DESC LIMIT -1 OFFSET ?", (keep,),
            ).fetchall()]
            if ids:
                conn.executemany("DELETE FROM futures_snapshots WHERE id = ?", [(i,) for i in ids])
                conn.commit()

    # ------------------------------------------------------------------
    # Outcome / Backtest engine (spec V2 §28-29)
    # ------------------------------------------------------------------

    def outcomes_after(self, minutes_list: tuple[int, ...] = (5, 15, 30, 60)) -> list[dict]:
        """
        For each historical snapshot, computes:
          - move_{m}m: point move from entry to the first snapshot at/after
            entry + m minutes (None if we don't have data that far yet)
          - mfe_{m}m: max favourable excursion within [entry, entry+m]
          - mae_{m}m: max adverse excursion within [entry, entry+m]
        Never claims predictive accuracy — this is raw per-snapshot data;
        aggregate it with evaluate_condition() before drawing conclusions,
        and only once enough samples exist.
        """
        rows = self.all_snapshots()

        def parse(ts: str) -> Optional[_dt.datetime]:
            try:
                return _dt.datetime.fromisoformat(ts)
            except ValueError:
                return None

        outcomes = []
        for i, row in enumerate(rows):
            t0 = parse(row["timestamp"])
            if t0 is None:
                continue
            result = dict(row)
            for m in minutes_list:
                target = t0 + _dt.timedelta(minutes=m)
                window = []
                for later in rows[i + 1:]:
                    tl = parse(later["timestamp"])
                    if tl is None:
                        continue
                    if tl <= target:
                        window.append(later["spot"])
                    else:
                        window.append(later["spot"])  # include the first one past target too
                        break
                if not window:
                    result[f"move_{m}m"] = None
                    result[f"mfe_{m}m"] = None
                    result[f"mae_{m}m"] = None
                    continue
                entry_spot = row["spot"]
                result[f"move_{m}m"] = window[-1] - entry_spot
                result[f"mfe_{m}m"] = max(window) - entry_spot
                result[f"mae_{m}m"] = min(window) - entry_spot
            outcomes.append(result)
        return outcomes


def evaluate_condition(
    outcomes: list[dict],
    condition_fn: Callable[[dict], bool],
    minutes_list: tuple[int, ...] = (5, 15, 30, 60),
) -> dict:
    """
    Filters outcome rows by an arbitrary condition (e.g. "regime ==
    'EXPANSION' and 'BEARISH' in confirmation") and aggregates average
    move / positive-negative-neutral split per horizon. Returns event
    count = 0 (with all stats None) rather than fabricating a result when
    no matching events exist yet.
    """
    matching = [o for o in outcomes if condition_fn(o)]
    result: dict = {"event_count": len(matching)}
    for m in minutes_list:
        key = f"move_{m}m"
        values = [o[key] for o in matching if o.get(key) is not None]
        if not values:
            result[f"avg_move_{m}m"] = None
            result[f"positive_pct_{m}m"] = None
            result[f"negative_pct_{m}m"] = None
            result[f"neutral_pct_{m}m"] = None
            continue
        n = len(values)
        result[f"avg_move_{m}m"] = sum(values) / n
        result[f"positive_pct_{m}m"] = 100 * sum(1 for v in values if v > 0) / n
        result[f"negative_pct_{m}m"] = 100 * sum(1 for v in values if v < 0) / n
        result[f"neutral_pct_{m}m"] = 100 * sum(1 for v in values if v == 0) / n
    return result


@dataclass
class FiiDiiFlow:
    fii_cash_buy: Optional[float] = None
    fii_cash_sell: Optional[float] = None
    fii_cash_net: Optional[float] = None
    dii_cash_buy: Optional[float] = None
    dii_cash_sell: Optional[float] = None
    dii_cash_net: Optional[float] = None
    fii_index_fut_buy: Optional[float] = None
    fii_index_fut_sell: Optional[float] = None
    fii_index_fut_net: Optional[float] = None
    fii_index_opt_ce_positioning: Optional[str] = None
    fii_index_opt_pe_positioning: Optional[str] = None
    cash_flow_kind: str = "daily flow"          # never mixed with open-position figures
    futures_kind: str = "open position"
    options_kind: str = "open position"
    available: bool = False
    note: str = "FII/DII participant data unavailable through current Dhan feed."


class FiiDiiProvider:
    """
    No fabricated numbers. Returns an all-unavailable FiiDiiFlow unless a
    real, licensed source is wired in via `fetch()`. If you later add one
    (e.g. NSE's participant-wise OI bulletin), keep it in a SEPARATE
    module and have `fetch()` delegate to it, per spec V2 §16 — do not
    merge cash-flow, futures-position, and options-position figures.
    """

    def fetch(self) -> FiiDiiFlow:
        return FiiDiiFlow()
