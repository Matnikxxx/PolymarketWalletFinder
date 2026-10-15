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

1. Scans/ranks candidate wallets from Polymarket's public leaderboard (plus
   a seed list) and scores them transparently (ROI, win rate, drawdown,
   consistency, concentration, category mix, trading-frequency cadence --
   with a written reason for every point).
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

- **Wallet-discovery candidate pool is small (~20-50), not "thousands."**
  `MAX_WALLET_CANDIDATES` is configurable, but `wallet/scanner.py` is
  bounded by the public leaderboard (50/request) plus the seed list.
  Nothing in this codebase claims to scan more than it actually does. This
  is a known limitation to be addressed in a future pass, not this one.
- Frequency/rapid-trade classification needs at least
  `MIN_TRADES_FOR_FREQUENCY_CLASSIFICATION` (default 3) timestamped trades
  to produce a real classification; below that it reports `NORMAL` with
  `insufficient_data=True` rather than guessing.
- **Wallet win-rate/ROI/drawdown/P&L show N/A** for any wallet with a
  `data_quality` of `INSUFFICIENT_DATA` or `API_UNAVAILABLE` in
  `data/wallets.json` (no activity retrieved, or the Polymarket API was
  unreachable during the last `--scan-wallets`) -- this is intentional, not
  a bug; see `CLAUDE.md`'s "Wallet analytics fix" section for the exact
  formulas and which endpoints back each number. Re-run `--scan-wallets`
  with working network access to populate real numbers.
- Wallet ROI/drawdown/consistency are reconstructed from each wallet's
  *activity feed*, not a ground-truth ledger the API provides directly --
  this is the same limitation any outside observer of a wallet has.
- Small sample sizes produce noisy stats by design -- the tools
  deliberately surface a warning rather than hide it.
