# Polymarket Wallet Finder / Tracker

A Polymarket wallet **discovery and tracking** tool. It scans/ranks candidate
wallets, verifies them against a checklist, and tracks selected wallets'
activity over time (new BUY/SELL fills, cadence diagnostics). **It never
executes or simulates any trade, paper or live.** There is no private key
anywhere in this codebase and no order-placement code path.

> This project was formerly a paper/copy-trading bot ("PolymarketCopyBot").
> All copy-trading/paper-trading functionality has been removed in an
> architecture cleanup pass -- see `CLAUDE.md` for what changed and why.
> Every wallet-discovery/filtering/ranking/verification threshold below is
> unchanged by that cleanup.

---

## 1. What this tool does

1. Scans/ranks candidate wallets from Polymarket's public leaderboard (walked
   with bounded `limit`/`offset` pagination, plus a seed list) and scores them
   transparently (ROI, win rate, drawdown, consistency, concentration,
   category mix, trading-frequency cadence -- with a written reason for every
   point).
2. Lets you **manually** mark specific wallets as tracked in
   `data/wallets.json`. Nothing is tracked automatically just because it
   was scanned.
3. Polls tracked wallets for new activity, established against a
   **baseline** so it never retroactively reports old history as new.
4. Records every new BUY and SELL fill it observes: timestamp, market,
   price, size, side, transaction id -- plus a rapid-trading diagnostic
   flag when a wallet's current cadence looks like a burst.
5. Persists everything atomically so a restart never loses state or
   double-reports the same activity.
6. Produces wallet reports and a read-only dashboard for browsing
   discovered/tracked wallets and their recent activity.

---

## 2. Install (Windows PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
```

(macOS/Linux: `python3 -m venv .venv && source .venv/bin/activate`.)

---

## 3. Configure wallets

`data/wallets.json` starts empty (or with whatever candidates you've
scanned before) and is written by `--scan-wallets`. Each entry looks like:

```json
{ "wallet": "0x...", "tracked": true }
```

`"tracked": true` means this wallet is selected for ongoing tracking.
Run a scan to (re-)rank candidates and write scores back into this file,
then set `tracked` to `true` for the wallets you want to follow:

```powershell
python main.py --scan-wallets
python main.py --verify-wallets
```

`--verify-wallets` runs the full verification checklist and prints
`VERIFIED` / `PARTIALLY_VERIFIED` / `FAILED` / `UNKNOWN` for each wallet in
`data/wallets.json`, with the specific checks that failed.

---

## 4. Track wallet activity

```powershell
python main.py --track-wallets
```

This polls every selected (`"tracked": true`) wallet on `CHECK_INTERVAL`
seconds and records any new BUY/SELL activity to
`data/wallet_activity_log.json`. Stop with `Ctrl+C` -- state is saved
automatically. This never places, simulates, or copies a trade -- it only
observes and records.

---

## 5. View results

```powershell
python main.py --status         # quick discovery/tracking status (console)
python main.py --report         # full per-wallet report (console)
python main.py --dashboard-cli  # read-only combined view, console text version
```

## 5a. Web dashboard

```powershell
python main.py --dashboard
```

Starts a local, read-only web dashboard at **http://127.0.0.1:5000**
(override with `--host` / `--port`). Opens in any browser. It does **not**
start wallet tracking -- run `python main.py --track-wallets` separately in
another terminal for that; the dashboard only reads existing state.

Sections: top wallet details, tracked wallets, the full scanned/ranked
wallet table (clearly separate from "tracked" -- scanning never
auto-tracks anything), and recent wallet activity (new BUY/SELL fills, with
a rapid-trade badge where relevant).

It reuses the exact same `analytics/dashboard.py::build_dashboard_data()`
function as the console version above -- there is one source of truth for
what's shown, not two. Auto-refreshes every 20s; there's also a manual
Refresh link. No trading controls exist anywhere in it.

## 5b. Trading-frequency / rapid-trade diagnostics

Every wallet is classified by trading cadence: `LOW_FREQUENCY`, `NORMAL`,
`HIGH_FREQUENCY`, `RAPID_TRADER`, or `BOT_LIKE`. This feeds two separate
things:

- **Ranking**: a `RAPID_TRADER` or `BOT_LIKE` wallet is penalized in
  `--scan-wallets` scoring, and `BOT_LIKE` can never be `RECOMMENDED` no
  matter how good its ROI/win-rate looks.
- **Live tracking**: if a tracked wallet suddenly starts firing BUYs in a
  tight burst during `--track-wallets`, the recorded activity is flagged
  `is_rapid: true` with a `rapid_reason` -- purely informational. Nothing
  is ever rejected, delayed, or resized: the tracker only observes. SELLs
  are always recorded plainly, without this flag.

See `RAPID_TRADE_*`, `*_FREQUENCY_*`, and `BOT_LIKE_*` in `.env.example`.

## 5c. Core Statistics & Population Accounting (Phase 6)

The core statistics engine (`analytics/eligibility.py`, `analytics/performance.py`, `analytics/models.py`) implements deterministic position partitioning and mathematical performance accounting:

- **Priority Cascade Eligibility**: Partitions positions through an exhaustive priority waterfall:
  `COMBO_EXCLUDED` &rarr; `UNRELIABLE_DATA` &rarr; `REPEAT_EXPOSED` &rarr; `UNRESOLVED_OPEN` &rarr; `INCOMPLETE` &rarr; `ELIGIBLE`.
  Guarantees mutually exclusive buckets where every position belongs to exactly one classification.
- **Economic Win/Loss Classification**:
  - Breakeven band: $\pm\$0.01$ (`BREAKEVEN_TOLERANCE_USDC`) around $\$0.00$ net result.
  - Strict 6-decimal rounding on monetary boundaries.
  - Win rate calculated over decisive positions: $\frac{\text{Wins}}{\text{Wins} + \text{Losses}}$ (breakeven and partial payouts excluded from the win-rate denominator).
- **Comprehensive Risk & Performance Metrics**:
  - **ROI**: Net-result over total eligible cost, fee-exclusive.
  - **Drawdown**: Peak-to-trough drawdown computed in USD, cost-percentage, and peak-percentage.
  - **Concentration**: Top-1 and Top-3 profit concentration percentages.
  - **Consistency**: Standard deviation of return-to-cost ratios ($\sigma$ of $\rho = \frac{r}{c}$).
  - **Sensitivity Analysis**: Generates both clean-data and repeat-exposure sensitivity views.

## 5d. Descriptive Frequency Classification (Phase 7)

Phase 7 frequency analysis (`analytics/frequency_models.py`, `analytics/frequency.py`) provides pure analytical insights into trading cadence and order-book interaction:

- **Logical Trade Consolidation (`consolidate_logical_trades`)**:
  - Groups partial fills sharing an on-chain transaction hash into single logical trades.
  - Excludes non-trade operations: `REDEEM`, `SPLIT`, `MERGE`, `DEPOSIT`, `WITHDRAWAL`, `REWARD`, `YIELD`, `REBATE`.
  - Isolates repeat-exposed duplicate fills while preserving singleton unhashed fills.
- **Precision Frequency Metrics (`compute_frequency_metrics`)**:
  - Exact gap intervals: min, median, mean seconds, and zero-second boundary safety.
  - Threshold percentiles: percentage of gaps $\le 10$s, $\le 30$s, $\le 60$s.
  - Rolling window peaks: 1-minute ($60$s), 5-minute ($300$s), 15-minute ($900$s).
  - Burst episode clustering: groups contiguous chains of gaps $\le 10$s.
  - Cross-market intensity: maximum distinct markets traded within a rolling 60-second window.
  - Fill expansion factor: $\frac{\text{raw\_fill\_count}}{\text{total\_logical\_trades}}$.
- **Descriptive Profile Pipeline (`compute_frequency_profile`)**:
  - Classification tiers: `NORMAL`, `ACTIVE`, `RAPID_TRADER`, `BOT_LIKE`, `INSUFFICIENT_DATA`.
  - **Strict AND Logic for BOT_LIKE**: Requires both `peak_trades_in_1m >= 10` **AND** `pct_gaps_le_10s >= 50.0%`.
  - **Incomplete History Safety**: Truncated feeds return `INSUFFICIENT_DATA` (`data_quality="PARTIAL"`) preventing rate distortion.
  - **Architectural Purity**: Produces descriptive analytical facts; does NOT reject wallets, does NOT alter scores, and does NOT issue recommendations.

---

## 5e. Wallet discovery: pagination, validation and merge behaviour

`--scan-wallets` builds its candidate pool like this:

- The public leaderboard (`GET /v1/leaderboard`) is walked with
  `limit`/`offset` pages: `LEADERBOARD_PAGE_SIZE` rows per page (hard ceiling
  50, enforced by the scanner regardless of configuration) for at most
  `MAX_LEADERBOARD_PAGES` requests.
- Pagination stops as soon as: the page cap is reached, an empty page comes
  back, a page repeats a page already seen (the API ignoring `offset`), an API
  request fails, or the candidate budget is filled. Attempted requests are
  counted, including the one that failed, and the loop is bounded by the page
  cap so it can never spin.
- A **short page does not end the walk.** A server that clips a 50-row request
  to 20 rows is not the same signal as the leaderboard running out, and
  treating it as the end silently truncated the pool (raising
  `MAX_WALLET_CANDIDATES` appeared to do nothing). Short pages are walked
  through and counted; the empty page / repeated page / page cap / budget are
  the authoritative stops.
- Every address is validated against `^0x[a-fA-F0-9]{40}$` and normalized to
  lowercase. Rows that fail validation are skipped individually (and counted)
  -- one malformed row never aborts a scan.
- Candidate addresses are de-duplicated globally across pages and against the
  seed list, leaderboard ordering is preserved, and valid seeds are appended
  afterwards. The overall `MAX_WALLET_CANDIDATES` cap covers the union.
- **Seed capacity is reserved.** The leaderboard is PnL-ordered, so its top
  rows change slowly; if it were allowed to consume the whole candidate budget,
  every scan would rediscover an almost identical pool and the seed list would
  never be scanned at all (exactly what happened before this was fixed). When
  the cap can hold the whole seed list plus at least one leaderboard wallet,
  the leaderboard walk is bounded by `MAX_WALLET_CANDIDATES - seeds` and the
  seeds fill the rest. If the cap is smaller than the seed list, the
  leaderboard takes the whole cap and seeds get no slots.
- Offsets advance by the rows the leaderboard actually returned, and each
  request is capped by the remaining budget, so pages never overlap, skip, or
  ask for more rows than needed.
- Repeated candidates are often legitimate: if the same wallets really are the
  top PnL traders this month, the same wallets come back. `--scan-wallets` now
  records `first_seen_at`, `last_seen_at`, `times_seen` and `discovery_source`
  per wallet so repeats are measurable, and prints a diagnostics block that
  lists pages attempted, rows returned, invalid/duplicate addresses, seeds
  added, known-vs-new candidates, and why each candidate passed or failed.
- Seed wallets that were already stored in `data/wallets.json` are never lost
  either way -- see below.

### Scan diagnostics

Every `--scan-wallets` run ends with a `SCAN DIAGNOSTICS` block reporting:

- leaderboard pages attempted/succeeded and why pagination stopped;
- rows returned, addresses rejected as invalid, duplicate addresses dropped;
- candidates from the leaderboard vs seeds (and seeds already on the board);
- candidates this scan, previously known, new, and previously stored wallets
  that were not rediscovered;
- counts of `PASS` / `FAIL` / `INSUFFICIENT_DATA` / `STALE` plus the resulting
  recommendations;
- hard-filter rejection reasons grouped by cause, with the concrete values
  behind each group (e.g. "Only N positions, below minimum N" -> "Only 4
  positions, below minimum 40");
- `STALE` refreshes with their real cause (metrics kept);
- **how far off each rejected wallet was**: the distribution of "missed by 1 /
  2 / 3+ criteria" and an explicit "closest to passing" list of the wallets
  that missed by a *single* criterion (these are the ones worth reviewing
  first -- loosening nothing, just telling you where the line fell);
- **outcome by discovery source** (leaderboard vs seed: scanned / PASS / FAIL /
  STALE and median ROI per source);
- the acceptance band in force (`MIN_ROI` .. `REJECT_ROI_ABOVE`, positions,
  active days, consistency, drawdown, politics) and a one-line explanation of
  how many wallets passed and why the others did not.

Diagnostics are read-only aggregation of results the pipeline already
computed: they never recompute a metric, re-run a filter, re-rank, or change a
threshold. Frequency classification is reported as a **score penalty**, not an
exclusion -- `wallet/ranking.py` never removes a wallet from ranking because of
its cadence; a `BOT_LIKE` wallet simply cannot be `RECOMMENDED` (and only
`PASS` wallets reach ranking at all).

`--scan-wallets` **merges** into `data/wallets.json`, it never rebuilds it:

- Matching is case-insensitive on the normalized address, so casing can never
  create a duplicate record.
- Fields a user added by hand are preserved; fresh scan values overwrite
  stale ones.
- The stored `tracked` selection always wins (the legacy `enabled` field is
  honoured when `tracked` is absent and rewritten as `tracked`).
- Wallets that were not rediscovered -- including manually added ones -- are
  retained unchanged, in their original order. **A scan never deletes a
  wallet because it dropped off the leaderboard.**

### Data freshness in `data/wallets.json`

Each scanned record carries:

| Field | Meaning |
| --- | --- |
| `last_refresh_attempt` | When this scan tried to refresh the wallet (always updated, success or failure). |
| `last_successful_refresh` | When the last *successful* closed-positions refresh happened (unchanged by a failed request). |
| `data_quality` | `SUFFICIENT`, `PARTIAL`, `INSUFFICIENT`, `ACTIVITY_ONLY` or `STALE` -- see below. |
| `data_quality_reason` | Human-readable explanation of the value above. |
| `pnl_source` | `closed_positions_api`, `activity_reconstruction` or `none` -- where the stored P&L numbers came from. |
| `first_seen_at` / `last_seen_at` | When this wallet was first discovered, and when a scan last rediscovered it. |
| `times_seen` | How many scans have rediscovered it (a scan that fails to refresh it still counts -- it was seen). |
| `discovery_source` | `leaderboard` or `seed` -- which input produced it. Kept from the last known source if unknown. |

`data_quality` values:

- **`SUFFICIENT`** / **`PARTIAL`** / **`INSUFFICIENT`** -- the closed-positions
  request succeeded; these are the Phase 6 retrieval statuses describing how
  complete/eligible the retrieved history was.
- **`ACTIVITY_ONLY`** -- the request succeeded but the history was empty, and
  the numbers present were reconstructed from `/activity` fills. Never
  presented as authoritative closed-position accounting.
- **`STALE`** -- the refresh failed. This is "we could not look", not "the
  wallet has no history": the previous metrics, scores and recommendation are
  kept exactly as they were, `hard_filter_status` becomes `STALE`, and the
  wallet is excluded from ranking. Stale data can never produce a fresh PASS.

A *successful but empty* history is **not** an error: it is reported as an
empty/insufficient result (never `STALE`), and no ROI/win-rate/P&L is
published from it.

## 6. Run tests

```powershell
python -m pytest
```

---

## 7. Reset tracking data

```powershell
python main.py --reset
```

Deletes `seen_events.json` and `wallet_activity_log.json` (the tracking
baseline and activity log). Your wallet configuration (`data/wallets.json`)
and verification results are preserved.

---

## 8. Stop the tracker

`Ctrl+C` during `--track-wallets`. State is flushed to disk before exit.

---

## 9. Extending this later

This build is intentionally analysis/tracking-only. If you ever want to add
any form of order execution, it must be built as a clearly separate,
explicitly selected module -- see `CLAUDE.md` for the ground rules future
work on this repo should follow.

---

## Known limitations (read before trusting the numbers)

- **Wallet-discovery candidate pool is bounded by the page cap.**
  `MAX_WALLET_CANDIDATES` (default 30) caps the pool per scan and
  `wallet/scanner.py` pages the public leaderboard at
  `LEADERBOARD_PAGE_SIZE` rows per request (hard ceiling 50) for at most
  `MAX_LEADERBOARD_PAGES` requests (default 10) -- i.e. at most 500
  leaderboard rows per scan even with a raised cap. Nothing in this codebase
  claims to scan more than it actually does, and each scan's pool size is
  printed at the end of `--scan-wallets`.
- Frequency/rapid-trade classification needs at least
  `MIN_TRADES_FOR_FREQUENCY_CLASSIFICATION` (default 3) timestamped trades
  to produce a real classification; below that it reports `NORMAL` with
  `insufficient_data=True` rather than guessing.
- **Wallet win-rate/ROI/drawdown/P&L show N/A** for any wallet with no
  eligible closed positions (e.g. a `data_quality` of `INSUFFICIENT`,
  `INSUFFICIENT_DATA` or `API_UNAVAILABLE` in `data/wallets.json`) -- this is
  intentional, not a bug; see `CLAUDE.md`'s "Wallet analytics fix" section
  for the exact formulas and which endpoints back each number. Re-run
  `--scan-wallets` with working network access to populate real numbers.
- **A `data_quality` of `STALE` means the last refresh failed** (for example
  the closed-positions API was unreachable). The stored ROI/win-rate/P&L and
  score are the previous successful refresh's numbers, not fresh ones, and
  the wallet is skipped by ranking until a refresh succeeds. Check
  `last_refresh_attempt` vs `last_successful_refresh` and
  `data_quality_reason` in `data/wallets.json`.
- Wallet ROI/drawdown/consistency are reconstructed from each wallet's
  *activity feed*, not a ground-truth ledger the API provides directly --
  this is the same limitation any outside observer of a wallet has.
- Small sample sizes produce noisy stats by design -- the tools
  deliberately surface a warning rather than hide it.
- **A low PASS rate is expected with the default thresholds.** The accepted
  ROI band is `[MIN_ROI, REJECT_ROI_ABOVE]` = `[15%, 80%]` -- a *higher* ROI
  above the cap is a rejection reason, not a better candidate -- combined with
  `MIN_POSITIONS=40`, `MIN_ACTIVE_DAYS=30`, `MIN_CONSISTENCY=4.4`,
  `MAX_DRAWDOWN=20%`, the resolution-only exclusion and the politics share
  rule. On a 30-wallet pool it is normal for only one or two wallets to clear
  every threshold. If you want more candidates, widen the market universe
  before loosening the thresholds -- and note that loosening them changes what
  "qualified" means. Check the `SCAN DIAGNOSTICS` rejection-reason counts
  before assuming the filter is broken.
- **Repeated candidates across scans are normal** when the top PnL traders on
  the leaderboard genuinely do not change. Use `times_seen` /
  `first_seen_at` in `data/wallets.json` and the "new this scan" line in the
  diagnostics block to tell repetition from a discovery problem. There is
  deliberately no random rotation of leaderboard offsets: that would trade the
  best-known wallets for lower-ranked ones without any evidence they are
  better candidates.
- **To actually widen the candidate universe**, raise `MAX_WALLET_CANDIDATES`
  (for example 100), which now really does walk the leaderboard across
  multiple pages at `LEADERBOARD_PAGE_SIZE` rows per page. Budget for it: each
  scanned wallet costs up to `MAX_HISTORY_PAGES` activity pages plus up to
  `MAX_HISTORY_PAGES` closed-position pages (both stop early on a short page),
  so a larger pool means proportionally more API requests per scan. Watch the
  diagnostics' "pages attempted", "rows per page" and `stop reason` to see what
  the pool size is actually buying you.
- **If a changed setting appears to have no effect**, the diagnostics say why.
  `SCAN DIAGNOSTICS` prints a "Configuration in force" block with each
  discovery setting's *effective value and source* (`.env`, `process
  environment`, or `default`), plus any `CONFIG ISSUE`. The three most common
  causes are reported explicitly:
  1. a variable exported in your shell/IDE wins over `.env` (python-dotenv's
     `override=False` default -- deliberate, so CI variables win; the report
     names the conflict),
  2. a value that cannot be parsed (`"100"` with quotes, `100 # comment`,
     `abc`) falls back to the default -- now with a loud `CONFIG ISSUE` instead
     of silently,
  3. the configured limit exceeding what the page cap can reach (e.g.
     `MAX_WALLET_CANDIDATES=1000` with `MAX_LEADERBOARD_PAGES=10` and
     `LEADERBOARD_PAGE_SIZE=50` tops out at 500 rows + seeds) -- reported with
     the exact arithmetic and which knob to raise.
  Values written as `"100"`, `100 # comment` or `100.0` are now parsed
  correctly; only genuine garbage falls back to the default.
