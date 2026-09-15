# NIFTY Dealer Intelligence V2

Estimates **modeled** NIFTY dealer exposure and hedging pressure from
live Dhan option-chain data, and checks whether actual market behaviour
is confirming that read.

# NIFTY Dealer Intelligence V2.1.3 — the actual live bug, found and fixed

## What the diagnostics found

The V2.1.2 diagnostics did their job: a real Dhan session reported

> `6 candidate row(s) matched symbol+instrument+exchange, but none had a
> parseable, non-expired expiry in column 'SEM_EXPIRY_DATE'.`

That's precise — symbol/instrument/exchange matching was correct (6
real NIFTY FUTIDX rows found), and the *only* problem was the expiry
date format. The original parser only tried `%Y-%m-%d`, `%d-%b-%Y`,
`%d/%m/%Y` — Dhan's live instrument master evidently uses a format with
a time component (e.g. `2026-09-30 00:00:00`) that none of those
matched.

## The fix

- `futures_data._parse_expiry_date()`: a dedicated parser tried against
  12 explicit formats (covering `%Y-%m-%d %H:%M:%S`, `T`-separated
  ISO-with-microseconds, `%d-%b-%Y %H:%M:%S`, etc.), plus a fallback
  that takes the leading 10 characters as `%Y-%m-%d` when they look like
  a plain ISO date — this catches essentially any `YYYY-MM-DD<anything>`
  variant without needing to enumerate every possible time-component
  format explicitly.
- **`InstrumentMasterDiagnostics` now distinguishes "couldn't parse the
  expiry at all" from "parsed fine but is in the past"** — the earlier
  version conflated these into one message, which would have been
  actively misleading here (it would have said "raw values: (column was
  empty)" even though the column had real, valid-looking data). Now:
  `parsed_but_expired` is tracked separately, and **`sample_expiry_values`**
  captures up to 3 actual raw strings that failed to parse — the exact
  evidence that led to this fix, and what future format mismatches will
  now surface immediately without another round-trip.
- **OHLC + a few more quote fields added** (`open_price`, `high_price`,
  `low_price`, `close_price`, `average_price`, `last_trade_time`),
  parsed defensively from both nested (`ohlc: {open, high, low, close}`)
  and flat key shapes, since Dhan's exact quote schema for this field
  set wasn't independently verified. Any field Dhan doesn't actually
  return stays `None` — nothing here is fabricated. Shown in both the
  Futures card and the Live Diagnostic Panel.

**Tests: 138 → 147, all passing.** Nine new tests, including one that
reproduces the exact live failure verbatim (6 candidate rows,
`YYYY-MM-DD HH:MM:SS`-format expiry) and confirms it now resolves
correctly, plus coverage for the corrected diagnostic-note branching and
the new OHLC fields defaulting to `None` (never 0) when absent.

**Honest note:** I still don't have a live Dhan connection to confirm
the OHLC field names against a real response — same caveat as the
buy/sell-quantity proxy fields. If `open_price`/`high_price`/etc. show
`N/A` in the next live run, that's the next diagnostic to extend the
same way this expiry fix was: capture what's actually there and adjust.

---

# NIFTY Dealer Intelligence V2.1.2 — futures resolution diagnostics

## Live test result and the fix

A live run against a real, market-closed Dhan session confirmed the
whole V2.1.1 pipeline is genuinely wired: spot ticking live, dealer
regime computed from real option-chain Greeks/OI (`EXPANSION`, GEX
−195,834,850L, Gamma Flip ₹23,488, spot 90pts below it), confirmation
engine honestly reporting `NO CONFIRMATION`/`UNAVAILABLE` for every
input that genuinely wasn't wired (no fabrication anywhere) — but
**futures resolution itself failed**: `Futures: UNAVAILABLE — Could not
resolve the current NIFTY futures contract from the instrument master.`

That's the correct failure behavior (never guess an ID), but it gave no
way to tell *why* it failed. V2.1.2 adds `futures_data.
InstrumentMasterDiagnostics`: every parsing step now records what it
actually saw — the real column headers in the fetched CSV, which
candidate names matched, how many rows contained "NIFTY", how many of
those passed the instrument/exchange filter, how many had a valid
unexpired expiry — and a plain-language diagnosis of exactly which step
came up empty. This is surfaced in two places:

- **Data Health issues** now include a one-line summary pointing to the
  diagnostic panel when futures is unavailable.
- **The Live Diagnostic Panel** gets a new "Futures Resolution
  Diagnostics" section with the full detail — including the first 15
  column headers Dhan's instrument master actually returned, which is
  exactly what's needed to fix the column-name guesses in
  `futures_data.py`'s `_EXCHANGE_COLS`/`_SYMBOL_COLS`/etc. lists if
  Dhan's real schema differs from the candidates currently tried.

A short (30s) failure-cache TTL was also added so a column-name fix
takes effect on the next refresh instead of being stuck behind the
6-hour success-cache TTL, without hammering Dhan's server with a fresh
multi-row CSV fetch on every Streamlit rerun while broken.

**Tests: 132 → 138, all passing.** Six new tests exercise the
diagnostics against synthetic CSVs: missing columns, zero NIFTY rows,
NIFTY rows found but all expired, and the success case — plus an
end-to-end test that `fetch_futures_snapshot` actually surfaces
`.diagnostics` on a real resolution failure, not just a generic message.

**Practical next step for the person running this against a live Dhan
account:** open the Live Diagnostic Panel → "Futures Resolution
Diagnostics" → "Column headers seen (first 15)" after the next refresh.
That list is the actual live schema Dhan is returning; if it doesn't
contain any of `SEM_TRADING_SYMBOL`/`SEM_SMST_SECURITY_ID`/etc., that's
the fix needed in `futures_data.py`'s candidate-column lists — and the
diagnostics will say exactly which of the two (symbol vs. security-ID
column) is the blocker.

---

# NIFTY Dealer Intelligence V2.1.1 (+ Cash Market integration)

## Cash Market — now a first-class, explicit layer

The cash market (NIFTY spot) previously fed confirmation only implicitly
through a generically-named "Price Structure" family. It's now a named,
visible layer with parity to the Futures layer:

- **`confirmation_engine`'s family key is now literally `"Cash Market"`**
  (renamed from `"Price Structure"`), scored by two functions —
  `_score_cash_market_price` (spot price direction vs. previous snapshot)
  and `_score_cash_market_levels` (spot vs. support/resistance/VWAP) —
  using the **same, single spot pipeline** (`inp.spot` / `inp.price_change`)
  that already existed. No duplicate cash-data fetch was created, per the
  explicit instruction not to build a second spot engine.
- **`ConfirmationInputs.price_change_pct`** — a new, real, displayed field
  (spot price change as a percentage), computed in `app.py` from the same
  real price data. Added because it's actually used (shown in the new
  Cash Market card and diagnostic panel), not as an inert placeholder.
- **A dedicated "🏦 Cash Market" card** in the Live Market Confirmation
  section (`ui.dashboard.render_live_market_confirmation`), parallel to
  the "📈 Futures" card: spot LTP, change, change %, and — shown
  honestly, not hidden — Cash Volume and Cash Flow/CVD as
  **`UNAVAILABLE`**, with the reason inline (NIFTY spot is an index
  value, not a traded instrument; it structurally has no order book or
  volume of its own). This is kept strictly distinct from the Futures
  order-book flow proxy so the two metrics — one real, one structurally
  absent — are never visually or logically conflated.
- **`signal_engine.classify_confirmation_strength()`'s `"Cash"`
  component** now explicitly documented as reading from the `"Cash
  Market"` family (the lookup key changed with the rename; the logic
  and its FULL/PARTIAL/CONFLICT/INSUFFICIENT-DATA behavior are
  unchanged).

**Tests: 126 → 132, all passing.** Six new tests lock in: the family key
is literally `"Cash Market"` (and `"Price Structure"` no longer exists
anywhere), `price_change_pct` is accepted and doesn't alter verdict
math, Cash Market votes bullish/bearish correctly on its own, the
Confirmation Score's `"Cash"` component sources from it correctly, and
Cash Volume/Cash Flow-CVD remain distinct, always-`UNAVAILABLE` keys in
Data Health separate from the Futures flow proxy.

**Files touched:** `confirmation_engine.py`, `signal_engine.py`,
`ui/dashboard.py`, `app.py`, `tests/test_calculations.py`. No files
added — this was a rename-and-extend of existing structure, not new
architecture.

---

# NIFTY Dealer Intelligence V2.1.1

## V2.1.1 — response to code review (two review passes)

Two review messages arrived after the first V2.1 delivery. Before making
any change I re-checked the actual shipped code against each specific
claim, because some were accurate and some weren't — here's the honest
breakdown, followed by what was actually fixed.

**Claims checked and found FALSE against the delivered V2.1 code**
(verified by `grep` before touching anything, shown in the implementation
report below): `futures_available=False` was NOT hard-coded — it was
already set from `futures_snapshot.available` (a real fetch result).
`futures_price_change`/`futures_oi_change` were NOT left unpopulated —
`ConfirmationInputs` was already receiving them from a real
`futures_data.fetch_futures_snapshot()` call. The same-day-expiry TTE
bug was NOT present in `app.py` — it was already using
`utils.time_to_expiry_years()` with a configurable IST expiry time, not
`strptime(...).` at midnight.

**Claims checked and found TRUE — these were real gaps, now fixed:**
1. **CVD had no real data path at all.** V2.1 left it permanently
   `CVDStatus.UNAVAILABLE`, which was honest but incomplete — Dhan's
   futures quote endpoint DOES expose aggregate order-book
   `buy_quantity`/`sell_quantity`, which V2.1 wasn't using. V2.1.1 adds
   `futures_data.compute_flow_proxy()`: a real, documented **order-book
   imbalance proxy** (`buy_quantity − sell_quantity`, and its change
   vs. the previous stored snapshot), always labeled
   `CVDStatus.PROXY` — never presented as true aggressor-side CVD.
2. **Naive `datetime.now()` calls remained in `futures_data.py`**
   (contract-expiry filtering and snapshot timestamps), inconsistent
   with the rest of the app's IST-aware timestamps. Fixed to use
   `utils.now_ist()`.
3. **The "cash market" wasn't being treated as its own confirmation
   layer** with visible componentry — it was folded silently into
   "Price Structure." V2.1.1 doesn't duplicate the spot pipeline (the
   review explicitly forbids that) but does make it a named, visible
   component (`Cash`) in the new cross-layer confirmation score below.
4. **No FULL/PARTIAL/CONFLICT/INSUFFICIENT-DATA confirmation
   taxonomy** combining the dealer model with cash+futures+flow existed.
   Added `signal_engine.classify_confirmation_strength()`.
5. **No live diagnostic panel** for verifying the pipeline by inspection
   existed. Added `ui.dashboard.render_diagnostic_panel`.

### What "Cash Volume" / "Cash CVD" honestly are

NIFTY spot is an **index value**, not a traded instrument — it has no
order book, no traded volume, and no aggressor-side flow of its own
(that's a structural fact about what an index is, not a Dhan API gap).
V2.1.1 reports `Cash Volume` and `Cash Flow/CVD` as `UNAVAILABLE` in
Data Health with that reasoning attached, and keeps them **strictly
separate** from `Futures Flow (Proxy)` (which IS real, computed data)
so the two are never confused — the review explicitly bans exactly that
kind of substitution (e.g. "cash_cvd_change = futures_price_change").

### Confirmation Score — every component visible

`signal_engine.classify_confirmation_strength()` combines four
independently-sourced components (never fabricated, never duplicated
pipelines) into one of `FULL_BULLISH` / `FULL_BEARISH` /
`PARTIAL_BULLISH` / `PARTIAL_BEARISH` / `CONFLICT` / `INSUFFICIENT_DATA`:

| Component | Source |
|---|---|
| Dealer | `dex_engine.ModeledDeltaBalance.lean` (existing Modeled Delta Balance — not duplicated) |
| Cash | `confirmation_engine`'s "Price Structure" family (real spot price change + levels) |
| Futures | `confirmation_engine`'s "Futures OI" family (real futures price+OI, spec V2.1) |
| Flow | `confirmation_engine`'s "CVD" family (real order-book flow proxy, or honestly UNAVAILABLE) |

`FULL` requires all four available AND agreeing. `PARTIAL` requires at
least one agreeing with at least one other unavailable/neutral —
**never called FULL on partial evidence**, matching the review's
explicit example. `CONFLICT` is any real disagreement among available
components. This lives in `signal_engine.py`, not
`confirmation_engine.py`, on purpose: `confirmation_engine.py` still
never imports `dealer_regime`/`gex_engine` (a boundary a dedicated test
asserts directly), so folding in the dealer-model component happens
where both sides are already legitimately in scope.

### Price+Flow divergence (never an independent signal)

`confirmation_engine._score_cvd` now applies the full
price-vs-flow matrix from the review: agreement votes bull/bear
("Bullish/Bearish Flow Confirmation"); disagreement is recorded as a
**caution** (`ConfirmationResult.cautions`), e.g. "price up but flow
down → bearish divergence" — explicitly NOT an independent bull/bear
vote, per the review's "CVD must not independently generate BUY/SELL."

### Implementation report (both review docs' §20/§23 requirements)

**Files modified:** `futures_data.py`, `storage.py`,
`confirmation_engine.py`, `signal_engine.py`, `market_data.py`,
`app.py`, `ui/dashboard.py`, `tests/test_calculations.py`.
**Files added:** none (extended existing modules only, per "do not
create placeholder classes that are never called" / "do not
rebuild").

**Futures implementation:** `futures_data.fetch_futures_snapshot()` →
`dhan_client.get_quote()` against the dynamically-resolved current-month
contract (unchanged mechanism from V2.1) → LTP/OI/ΔOI into
`FuturesSnapshot` → `ConfirmationInputs.futures_price_change` /
`futures_oi_change` in `app.py` (STEP 6) → `confirmation_engine.
_score_futures()` → `ConfirmationResult.family_status["Futures OI"]`.

**CVD/Flow implementation:** same `get_quote()` response →
`buy_quantity`/`sell_quantity` → `futures_data.compute_flow_proxy()` →
`CVDStatus.PROXY` + `cvd_change` in `ConfirmationInputs` (app.py STEP 1)
→ `confirmation_engine._score_cvd()` (now price-vs-flow aware) →
`ConfirmationResult.family_status["CVD"]` + `.cautions`. Falls back to
`CVDStatus.UNAVAILABLE` automatically if the quote payload doesn't
include buy/sell quantity fields — never fabricated.

**Confirmation flow (full path, both review docs' requested diagram):**
```
Dhan quote (futures)  ──┐
Dhan option chain      ─┼─► ConfirmationInputs ─► confirmation_engine
Dhan spot LTP (cash)   ─┘         │                       │
                                   │                       ▼
dex_engine (dealer model) ────────┼──────────► ConfirmationResult
                                   │                       │
                                   ▼                       ▼
                      signal_engine.classify_confirmation_strength()
                                   │
                                   ▼
                         ConfirmationScore (FULL/PARTIAL/CONFLICT/
                                   INSUFFICIENT, all 4 components shown)
                                   │
                                   ▼
                signal_engine.combine_final_state() → Final State
```

**TTE:** unchanged from V2.1 (already correct — see the false-claim note
above); `futures_data.py`'s unrelated naive-datetime calls fixed for
consistency.

**Tests: 68 (V2 baseline) → 106 (V2.1) → 126 (V2.1.1), all passing.**
The 20 new tests cover: the flow proxy formula (including a test that it
can never equal price change by construction), price/flow divergence
producing cautions rather than votes, all four `ConfirmationStrength`
branches with every example from the review reproduced exactly (full
bearish, conflict, partial, insufficient), Cash Volume/CVD being
structurally distinct from the Futures flow proxy in Data Health, an
instrument-master parsing regression test for the naive/aware datetime
fix, and a schema-migration test seeding an old-style V2.1
`futures_snapshots` table without the new columns.

**Honest self-assessment:** I still have no live Dhan account or network
access in this environment. "Buy_quantity"/"sell_quantity" as Dhan's
exact field names for resting order-book quantity are my best documented
guess (with fallback names tried) based on how Indian broker quote APIs
are typically shaped — if Dhan's actual field names differ, the proxy
will report `flow_proxy_available=False` (visible immediately in Data
Health as `Futures Flow (Proxy): UNAVAILABLE`) rather than silently
computing garbage, but I cannot claim to have verified the exact field
name against a live response.

---

# NIFTY Dealer Intelligence V2.1

## What changed in V2.1 (live market confirmation upgrade)

V2.1 does not touch GEX/DEX/Gamma Flip math, storage architecture, or
the UI structure — it connects REAL observed market data to the
confirmation engine that V2 had built the scaffolding for but never
actually wired up, and fixes two real bugs found along the way.

- **New `futures_data.py` module**: live NIFTY futures LTP/OI/volume,
  via a dynamically-resolved current-month contract (Dhan's instrument
  master CSV — never a hard-coded Security ID that goes stale at
  rollover). A `classify_futures_positioning()` heuristic labels
  price+OI combinations as "Possible long buildup" etc. — always
  "Futures Positioning Interpretation," never "Confirmed Institutional
  Position."
- **Fixed the option-flow confirmation bug**: V2's confirmation engine
  scored raw call/put ΔOI sign directly as bullish/bearish — exactly the
  "ΔOI alone proves nothing" mistake the app's own docstrings warned
  against elsewhere. `positioning_engine.aggregate_for_confirmation()`
  now feeds the confirmation engine from the same price+OI+volume
  classifications used everywhere else, excludes low-confidence/OI-only
  and "unclear" classifications from voting, and reports "MIXED /
  UNCLEAR" when the evidence doesn't clear that bar.
- **Fixed the same-day-expiry TTE bug**: `datetime.strptime(expiry,
  "%Y-%m-%d")` implicitly anchors expiry at midnight, which made
  same-day options look like they had negative time value for most of
  expiry day. `utils.time_to_expiry_years()` now anchors expiry at a
  configurable IST time (`config.EXPIRY_HOUR_IST`/`EXPIRY_MINUTE_IST`,
  default 15:30) and floors at `config.MIN_TTE_SECONDS` as a documented
  numerical-stability floor, never silently presenting a floored value
  as an exact calculation.
- **CVD is honestly UNAVAILABLE, not approximated.** Dhan's
  option-chain/marketfeed endpoints (the ones this app is built on)
  don't expose directional buy/sell tick volume, which true CVD
  requires. `confirmation_engine.CVDStatus` makes this explicit
  (OBSERVED / PROXY / UNAVAILABLE) rather than silently computing and
  labeling an approximation as real CVD.
- **Expanded Data Health**: per-field LIVE/STALE/UNAVAILABLE/ERROR
  status (`market_data.FieldStatus`) for futures, futures OI, option
  LTP/OI/ΔOI, volume, CVD, VWAP, and levels — additive to the existing
  V2 health booleans, never converting `None` into `0`.
- **New "LIVE MARKET CONFIRMATION" UI section** (`ui.dashboard.
  render_live_market_confirmation`) showing observed spot/futures/
  option-flow/CVD-status/level-structure data, separate from and above
  the existing "Dealer Structure" card — preserving the OBSERVED vs.
  MODELED vs. INTERPRETED separation the spec requires.
- **New alert kinds**: RESISTANCE_BROKEN, FUTURES_POSITIONING_CHANGE,
  ALIGNMENT_CHANGE, MARKET_OPEN, MARKET_CLOSE, DATA_STALE, DATA_RECOVERY
  (added to the existing V2 alert set — nothing removed). The
  GAMMA_FLIP_CROSS alert now includes the observed confirmation
  direction, matching the spec's example.
- **Storage schema migrated in place**: `snapshots` gains
  `futures_ltp`/`futures_oi`/`futures_oi_change`/`futures_price_change`
  via `ALTER TABLE ADD COLUMN` on existing databases (never a
  drop/recreate), plus a new `futures_snapshots` table mirroring the
  `option_leg_snapshots` previous-value pattern.

### Implementation report (spec V2.1 §33)

**Files added:** `futures_data.py`

**Files changed:** `config.py`, `utils.py`, `dhan_client.py`,
`market_data.py`, `positioning_engine.py`, `confirmation_engine.py`,
`signal_engine.py`, `storage.py`, `app.py`, `ui/dashboard.py`,
`.env.example`, `.streamlit/secrets.toml.example`,
`tests/test_calculations.py`. Untouched: `option_chain.py`, `greeks.py`,
`gex_engine.py`, `dex_engine.py`, `dealer_regime.py`, `cache.py`,
`ui/dealer_card.py`, `ui/gex_chart.py`, `ui/dex_chart.py`,
`ui/option_chain.py`, `ui/positioning.py`.

**What was fixed:** Futures LTP/OI/ΔOI (newly wired, previously absent
entirely), the option-flow-confirmation ΔOI-sign bug, the same-day
expiry TTE bug, CVD's availability being surfaced honestly instead of
silently defaulting to "missing" with no explanation, Data Health going
from a handful of booleans to per-field LIVE/STALE/UNAVAILABLE/ERROR
status.

**Tests: before 68 passed → after 106 passed.** All pre-existing V2
tests pass unmodified except two whose underlying `ConfirmationInputs`
fields were renamed (`net_call_delta_oi_change`/`net_put_delta_oi_change`
→ `option_flow`) — no V2 test actually exercised those fields directly,
so nothing needed updating there; the 36 new tests cover futures
positioning (all 4 price/OI combinations + missing LTP/missing OI),
confirmation wiring (bullish/bearish agreement, dealer-vs-market
disagreement, no/partial confirmation, CVD honesty), the option-flow
aggregate (all label combinations + the low-confidence exclusion), the
TTE fix (same-day before/after expiry time, future expiry), Gamma Flip
proximity at an exact match, a Gamma-Flip-cross alert, Data Health field
statuses, and a schema-migration test that seeds an old-style V2
database and confirms the upgrade doesn't lose data.

**Data availability (self-reported, not verified against a live Dhan
account — no network access in this environment):**
- NIFTY Spot: wired to `/v2/marketfeed/ltp` (unchanged from V2) — LIVE
  when credentials are valid and the market is open.
- NIFTY Futures: wired to a newly-added `/v2/marketfeed/quote` call
  against a dynamically-resolved Security ID — LIVE if instrument-master
  resolution succeeds, else UNAVAILABLE with an explicit error, never a
  guessed ID or a fabricated price.
- Futures OI: same call as above, same availability.
- CVD: UNAVAILABLE by design — Dhan's documented option-chain/marketfeed
  endpoints don't expose the buy/sell tick data true CVD requires.
- Option LTP / OI: wired to `/v2/optionchain` (unchanged from V2) — LIVE
  when credentials are valid.
- FII/DII: still UNAVAILABLE (unchanged from V1/V2 — no licensed source
  wired in).

I have not been able to execute this against a live Dhan account in this
environment, so "LIVE" above means "the code path is wired to make a
real, correctly-shaped API call," not "I personally watched it return
live numbers." The instrument-master column names in `futures_data.py`
are my best documented guess at Dhan's current CSV schema (with several
fallback column names tried) — if resolution fails against the real
file, that's the first place to check, and the module fails to
`available=False` rather than guessing, so it will be visible immediately
in Data Health rather than silently wrong.

---

## What changed in V2 (upgrade from V1 — see full list at the end)

- **Gamma Flip is now genuinely spot-dependent.** V1 found where the
  cumulative sum of strike-level GEX (at the current spot) changed sign
  — a snapshot artifact. V2 sweeps hypothetical spot values, recomputes
  Black-Scholes gamma at each one, and finds where AGGREGATE modeled GEX
  actually crosses zero (see `gex_engine.gamma_flip_v2`). Multiple
  crossings are detected and the nearest to spot is reported as primary.
- **New MIXED regime** when the model disagrees with itself (aggregate
  GEX sign vs. local gamma at the nearest strike to spot).
- **DEX now reports two explicitly separate units**: Delta Exposure
  (contract-equivalent, NOT rupees) and Rupee Delta Notional (an actual
  currency estimate, = Delta Exposure × spot).
- **Fixed the positioning-engine "previous LTP" bug**: V1 never actually
  passed a previous-snapshot map to `positioning_engine`, so every
  classification silently fell back to the low-confidence OI-only path.
  V2 persists per-strike/leg LTP+OI to SQLite each cycle
  (`storage.SnapshotStore.save_option_legs` /
  `get_previous_option_legs`) so "previous LTP" survives Streamlit
  reruns and app restarts.
- **Fixed the snapshot-sequencing bug**: V1 saved the snapshot BEFORE
  confirmation/final_state were computed, so those fields were always
  blank in storage. V2's `app.py` follows the exact 9-step order from
  the spec and saves exactly once, at the end, fully populated.
- **Final State is now an explicit, transparent matrix**
  (`signal_engine.combine_final_state`) — 7 named branches, never a
  bare buy/sell call.
- **Alignment is now categorical (LOW/MEDIUM/HIGH)**, not a percentage —
  false precision like "72% chance" is explicitly removed until enough
  historical outcome data exists to validate a number statistically.
- **New Backtest screen** (`ui.dashboard.render_backtest_screen`,
  `storage.evaluate_condition`): filter your own accumulated snapshot
  history by regime/confirmation and see average/positive/negative move
  outcomes at 5/15/30/60 minutes.
- **New "How This Works" transparency panel** — mandatory per spec,
  explains observed vs. modeled vs. assumed vs. unobservable.
- **Terminology throughout**: "Modeled Dealer GEX/DEX", "Estimated
  Hedging Pressure", "Observed Market Confirmation" — never bare
  "Dealer GEX" implying a fact.
- **Data Health** now shows LIVE/DELAYED/STALE age bands (not just a
  binary ok/not-ok).

**This is an analysis / advisory application only.** It never places,
modifies, or executes any order. There is no trading/order code anywhere
in this repository.

## What it answers

1. What is estimated dealer positioning?
2. How might dealers hedge that positioning?
3. Is the market being pinned/chopped, or allowed to expand into a trend?
4. Is actual NIFTY price/flow confirming that read?

The app always keeps **positioning**, **hedging pressure**, and **market
confirmation** visually and logically separate — it never claims dealers
"control" the market, and it never produces a directional call from
dealer positioning alone (see `signal_engine.combine_final_state` — a
missing or conflicting confirmation always resolves to a WAIT state).

## Project structure

```
dealer_intelligence/
├── app.py                 # Streamlit entry point — wires everything together
├── config.py               # Constants, thresholds, credential loading
├── dhan_client.py           # Thin Dhan v2 HTTP API wrapper (read-only)
├── option_chain.py          # Parses raw Dhan payload into typed rows
├── market_data.py           # Spot + data-quality/staleness tracking
├── greeks.py                # Black-Scholes fallback when Dhan omits Greeks
├── gex_engine.py            # Gamma Exposure + Gamma Flip
├── dex_engine.py             # Delta Exposure (dealer delta lean)
├── dealer_regime.py         # Gamma regime, pin zone, gamma walls
├── positioning_engine.py    # Probable call/put writing/buying/unwinding/covering
├── confirmation_engine.py   # Market Confirmation layer (Layer B)
├── levels_engine.py          # Support/resistance/VWAP
├── signal_engine.py          # Hedging model, final-state combination, alerts, confidence
├── storage.py                # SQLite (+ optional Supabase) snapshots, event log, FII/DII
├── cache.py                  # In-process TTL cache to avoid hammering the API
├── utils.py                  # IST time, market hours, lakh/crore formatting
├── ui/                        # Streamlit rendering only — no calculation logic
└── tests/                     # pytest unit tests for the calculation engines
```

Every calculation lives in its own module and is unit-tested independently
of Streamlit/Dhan, so the math can be verified without live credentials.

## Setup

1. **Python 3.11+**, then:
   ```bash
   cd dealer_intelligence
   pip install -r requirements.txt
   ```

2. **Dhan API credentials.** Get your Client ID and Access Token from the
   Dhan web app (My Profile → DhanHQ Trading APIs). Choose ONE of:

   - **Local development:** copy `.env.example` to `.env` and fill in
     `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN`.
   - **Streamlit Cloud / secrets-based deployment:** copy
     `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and
     fill in the same two values.

   Credentials are never hard-coded and never committed — both example
   files are templates only.

3. **(Optional) Supabase**, for historical snapshots beyond local SQLite:
   set `SUPABASE_URL` / `SUPABASE_KEY` the same way. If left blank, the
   app uses local SQLite only (`data/snapshots.db`) — no setup required.

## Run

```bash
streamlit run app.py
```

## Run tests

```bash
pytest tests/ -v
```

## Calculation documentation (summary — full detail is in each module's
docstring and in the "How is this calculated?" expanders in the app)

**GEX** (`gex_engine.py`): `Gamma × OI × Lot Size × Spot² × 0.01`, summed
per strike as `CE_GEX − PE_GEX` under the convention that dealers are
modeled as net long calls / net short puts. This is a standard, widely
used convention for public dealer-gamma models — it is a **model**, not a
measurement of any real dealer's book.

**Gamma Flip (V2 — spot-dependent)**: sweeps hypothetical spot values in
`[spot − flip_search_range_points, spot + flip_search_range_points]`,
recomputing Black-Scholes gamma at each hypothetical spot (each option's
own IV/strike/time-to-expiry held fixed), aggregating modeled GEX(S) the
same way as the current-spot calculation, and finding where that curve
crosses zero via linear interpolation (`gex_engine.find_zero_crossings`).
Multiple crossings can exist; the one nearest current spot is reported
as primary, and all crossings are shown in the Gamma Flip chart. This is
explicitly a model of "what gamma would be if spot were here," not a
forecast of how OI/IV would actually respond if spot moved.

**DEX** (`dex_engine.py`): two separate, explicitly labeled figures —
**Delta Exposure** (`Delta × OI × Lot Size`, a contract-equivalent count,
NOT rupees) and **Rupee Delta Notional** (`Delta × OI × Lot Size × Spot`,
an actual currency estimate). Positive Net DEX = call-delta heavy /
bullish lean; negative = put-delta heavy / bearish lean, under the
heuristic that dealers are the primary counterparty to visible OI —
labeled "Modeled Delta Balance," never presented as an observed fact.

**Regime classification** (`dealer_regime.py`): PIN/CHOP, EXPANSION,
TRANSITION, or **MIXED** (new in V2 — when aggregate modeled GEX sign
disagrees with local gamma at the strike nearest spot). Uses spot's
distance from the (now spot-swept) Gamma Flip AND total GEX magnitude
vs. a configurable transition band — never a single hardcoded cutoff —
and every result carries an explicit list of `reasons`.

**Market Confirmation** (`confirmation_engine.py`): looks only at actual
observed market behaviour (price, levels, CVD, futures, option ΔOI flow)
— it never looks at GEX/DEX, so it can genuinely agree or disagree with
the dealer-positioning read. Each family (Price Structure / CVD /
Futures OI / Option Flow) reports its own 🟢/🔴/🟡/⚪ status.

**Final State** (`signal_engine.combine_final_state`): an explicit
7-branch matrix (PIN+none/bearish/bullish, EXPANSION+bearish/bullish/
conflict, TRANSITION, MIXED) — dealer environment alone never produces a
directional call; missing or conflicting confirmation always resolves to
a WAIT state ("NO FOMO" rule).

**Alignment** (`signal_engine.alignment_score`): **categorical —
LOW / MEDIUM / HIGH**, not a percentage. Counts how many independent
signal families (gamma regime direction, modeled delta balance, market
confirmation) agree. False precision like "72% chance" is explicitly
removed until enough historical outcome data exists to validate a number
statistically (see the Backtest screen).

## Fixed bugs (V1 → V2)

- **Previous-LTP bug**: `positioning_engine.py` always accepted a
  previous-snapshot map, but V1's `app.py` never actually supplied one —
  every classification silently used the low-confidence OI-only
  fallback. V2 persists per-strike/leg LTP+OI in SQLite
  (`storage.SnapshotStore.save_option_legs` /
  `get_previous_option_legs`), independent of Streamlit session state,
  so "previous LTP" is reliable across reruns. Covered by
  `test_storage_previous_ltp_fix_survives_across_calls`.
- **Snapshot-sequencing bug**: V1's `app.py` called `save_snapshot()`
  with `confirmation=""` and `final_state=""` placeholders, before those
  values were computed later in the function. V2 follows the exact
  9-step pipeline order from the spec and saves exactly once, at the
  end. Covered by `test_snapshot_only_saved_with_complete_fields`.
- **Gamma Flip artifact**: V1's "flip" was a cumulative-sum-over-strikes
  read at the current spot — not spot-dependent at all. V2 replaces it
  with the hypothetical-spot sweep described above.

## Known limitations / honesty notes

- **FII/DII data**: Dhan does not publish official FII/DII participant-wise
  flow through its documented market-data APIs. `storage.FiiDiiProvider`
  returns "Data unavailable" for every field by default rather than
  inventing numbers. Wire in a licensed feed (e.g. NSE's participant-wise
  OI bulletin) in `FiiDiiProvider.fetch()` if you have one.
- **Futures price/OI and CVD** are optional inputs to the confirmation
  engine and are not wired to a live source in this initial build (Dhan's
  futures/market-depth endpoints can be added in `market_data.py` — the
  confirmation engine already accepts them and will say so explicitly
  ("Confirmation inputs not wired in yet: ...") until you do).
- **Exchange holiday calendar**: `utils.is_market_open()` checks
  weekday + trading-hours only; it does not know NSE holidays. Wire in a
  holiday list if you need that precision.
- The **backtest/outcome module** (`storage.SnapshotStore.outcomes_after`)
  computes point-move outcomes from your own accumulated snapshot
  history — it has no predictive claim and needs weeks/months of
  snapshots before the aggregated outcomes are meaningful. No
  accuracy number is fabricated ahead of that data existing.

## Database schema (SQLite, `data/snapshots.db`)

**`snapshots`** — one row per complete pipeline run (see `storage.Snapshot`):
`id, timestamp, spot, total_gex, net_dex, net_rupee_notional, gamma_flip,
distance_from_flip, regime, hedging_environment, confirmation,
final_state, alignment, positioning_summary (JSON), data_health_summary
(JSON), ce_oi_total, pe_oi_total, ce_change_oi_total, pe_change_oi_total,
volume_total`.

**`event_log`** — one row per alert (`id, timestamp, kind, message`).

**`option_leg_snapshots`** — one row per (strike, leg) per refresh cycle
(`id, timestamp, strike, leg, ltp, oi`), indexed on `timestamp`. This is
the table that fixes the previous-LTP bug; `prune_option_legs()` keeps
only the most recent N distinct timestamps since it grows fast (2 rows
per strike per refresh).

## Files changed in V2 (vs. the original delivery)

Every module was touched at least for terminology; the ones with real
logic changes:
- `config.py` — new flip-sweep and data-age thresholds
- `gex_engine.py` — rewritten: spot-dependent Gamma Flip sweep,
  `find_zero_crossings` factored out for testability
- `dex_engine.py` — rewritten: added Rupee Delta Notional,
  `ModeledDeltaBalance` with explicit disclaimers
- `dealer_regime.py` — rewritten: added MIXED regime, `reasons` list,
  `FlipProximity` (ABOVE/BELOW/NEAR/UNKNOWN)
- `confirmation_engine.py` — rewritten: added per-family `FamilyStatus`
- `signal_engine.py` — rewritten: explicit Final State matrix,
  categorical `Alignment`, richer hedging-model reasoning
- `storage.py` — rewritten: per-leg snapshot persistence (previous-LTP
  fix), `evaluate_condition` for the Backtest screen, expanded `Snapshot`
  schema
- `market_data.py` — added `DataAge` (LIVE/DELAYED/STALE)
- `positioning_engine.py` — added `summarize()` for snapshot storage
  (classification logic itself was already correct — the bug was in how
  `app.py` called it, now fixed)
- `app.py` — rewritten: corrected 9-step pipeline order, wires the
  previous-LTP store, new Backtest and How-This-Works tabs
- `ui/*.py` — updated for new data shapes and V2 terminology; added
  `render_gamma_flip_curve`, `render_confirmation_status`,
  `render_alignment`, `render_transparency_panel`, `render_backtest_screen`
- `tests/test_calculations.py` — rewritten/expanded (68 tests): spot-swept
  Gamma Flip, multiple crossings, MIXED regime, Final State matrix
  (all 7 branches), categorical alignment, previous-LTP fix, snapshot
  sequencing, backtest aggregation, market-hours, and scenarios A–E
- `greeks.py`, `option_chain.py`, `levels_engine.py`, `dhan_client.py`,
  `cache.py`, `utils.py` — unchanged in logic (already matched the
  corrected architecture)

## Build stages (as implemented)

1. Dhan connection, spot, option chain, Data Health — `dhan_client.py`,
   `market_data.py`, `option_chain.py`
2. Greeks, GEX, Gamma Flip — `greeks.py`, `gex_engine.py`
3. DEX, dealer positioning, pin zones — `dex_engine.py`,
   `positioning_engine.py`, `dealer_regime.py`
4. Market confirmation — `confirmation_engine.py`, `levels_engine.py`
5. Dealer regime + hedging model + final state — `signal_engine.py`
6. Charts — `ui/gex_chart.py`, `ui/dex_chart.py`, `ui/dashboard.py`
   (Price + Dealer Map)
7. Event log — `storage.py` (`log_event` / `recent_events`),
   `ui/dashboard.render_event_log`
8. Historical snapshots — `storage.SnapshotStore` (SQLite + optional
   Supabase)
9. Backtest/outcomes — `storage.SnapshotStore.outcomes_after`
