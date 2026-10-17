# Phase D — Diagnostic report & implementation plan

Scope: the six questions raised after Phase C. **Nothing in this report changes a
threshold, a ranking weight, a Phase 6 formula, or the read-only guarantee.**
Baseline: commit `f908931`, `python -m pytest -q` → 350 passed.

> **Status: implemented.** P1–P6 were approved and landed as working-tree
> changes (P6 is documentation only). The findings below are preserved as the
> original diagnosis; each "Verdict" describes the code **before** the fix.
> Test count after P1–P5: `python -m pytest -q` → 453 passed. See README
> section **5f. Historical data completeness and the page cap** for the
> user-facing documentation of the implemented behaviour.

---

## 1. Why does the scanner repeatedly discover similar wallets?

**Verdict: expected behaviour, not a bug.**

Discovery is a deterministic function of inputs that deliberately change slowly:

- the public leaderboard queried as `category=OVERALL`, `timePeriod=MONTH`,
  `orderBy=PNL` — the top PnL traders of the current month;
- the fixed 10-wallet `SEED_CANDIDATE_WALLETS` list.

The same inputs therefore produce the same candidates. Evidence from the user's
own `data/wallets.json`: exactly 20 leaderboard + 10 seed wallets, and the
single PASS is a seed. Candidate history (`first_seen_at`, `last_seen_at`,
`times_seen`, `discovery_source`) plus the diagnostics' "previously known / new
this scan" counters make repetition measurable; tests confirm `times_seen`
increments and `first_seen_at` is never rewritten.

No change proposed. Rotating leaderboard offsets would trade the best-known
wallets for lower-ranked ones with no evidence they are better candidates.

## 2. Why do so many wallets fail the hard filters (especially resolution-only)?

**Verdict: the filters are strict by design — but the resolution-only check has
one verified false-positive path.**

Rejection counts from the stored FAIL records (29 of 30):

| count | reason |
|---|---|
| 20 | consistency below `MIN_CONSISTENCY` 4.4 |
| 16 | active days below `MIN_ACTIVE_DAYS` 30 |
| 12 | resolution-only (no active exits) |
| 8 | ROI below `MIN_ROI` 15% |
| 8 | ROI above `REJECT_ROI_ABOVE` 80% |
| 5 | positions below `MIN_POSITIONS` 40 |
| 2 | drawdown above `MAX_DRAWDOWN` 20% |

The ROI band is `[15%, 80%]`, so a *higher* ROI is a rejection reason. This is
configuration, not a defect.

**Verified defect (D2):** `is_resolution_only` is derived from the presence of
a `TRADING_EXIT` position, which `build_position_records()` infers from SELL
fills in the `/activity` feed. When the wallet's SELL fills fall **outside the
fetched activity window**, no `TRADING_EXIT` is found and the wallet is
declared resolution-only. Reproduced: 50 closed positions closed 60 days ago +
a recent activity window containing only BUYs → `is_resolution_only=True` →
FAIL, although the wallet did sell. Absence of evidence is being reported as
evidence of absence.

## 3. Are trade counts, closed positions, active days, ROI, win rate and consistency correct?

**Verdict: the arithmetic is correct; the input window can be incomplete.**

Hand-checked fixture: 2 wins (+10), 1 loss (-50), 1 breakeven, all cost 100 →
wins 2, losses 1, breakeven 1, win rate 66.6667%, net -30, cost 400, ROI -7.5%.
The code returns exactly that (my initial "-10%" expectation wrongly excluded
the breakeven position's cost from the denominator).

Definitions in use:

- `total_trades` = raw fills (logical trades are reported separately);
- `num_positions` = **eligible** Phase 6 positions, not every closed row;
- `active_days` = distinct UTC dates across `opened_at`/`closed_at`/`resolved_at`;
- `win_rate` = wins / (wins + losses), partials and breakevens excluded;
- `ROI` = settled net / settled cost;
- `consistency` = `10 / (1 + |σ/μ|)` of per-position return ratios.

All are correct *for the data supplied*. Under truncation, `active_days` is a
lower bound and consistency/ROI describe a recent slice, not lifetime history.

## 4. Does the 3,000-event limit truncate history and distort statistics?

**Verdict: yes — and the truncation was invisible at the time of diagnosis
(D1).** *(Fixed in P1/P4: see the status note above.)*

`MAX_HISTORY_PAGES=30` gives 30 × 100 = **3,000 activity fills** and
30 × 50 = **1,500 closed positions**, both fetched newest-first. Reproduced: a
wallet whose 1,500 retrievable closed positions all fall inside a 5-day window
yields `active_days=6`, `retrieval_status=SUFFICIENT`,
`is_complete_history=True` — and is then rejected for "Only 6 active days".

Root cause: `main.py` calls `compute_wallet_statistics(w, positions)` without
passing `is_complete_history`, so the analytics engine cannot know the history
was capped. The paginated helpers return a bare `list`, discarding whether the
final page was full at the page cap.

Impact: one stored wallet has exactly 1,500 eligible positions and another has
1,548 raw fills, so the cap is reached in practice. At diagnosis, truncated
wallets were labelled `SUFFICIENT` and their metrics were presented as
complete. (*After P1 they are `PARTIAL`, the stop reason is persisted, and the
scan diagnostics/dashboard show COMPLETE / PARTIAL / UNKNOWN.*)

## 5. Do candidate discovery and deduplication work across repeated scans?

**Verdict: yes.**

Addresses are validated (`^0x[a-fA-F0-9]{40}$`), lowercased and de-duplicated
globally per scan (leaderboard pages + seeds); `merge_wallet_records()` keys on
the normalized address, collapses casing duplicates, preserves `tracked` and
custom fields, and retains wallets that were not rediscovered. `times_seen`
increments per rediscovery and `first_seen_at` is immutable. Covered by
`test_scanner_pagination.py`, `test_wallet_persistence.py`,
`test_scan_diagnostics.py`.

## 6. Dashboard, tracking and configuration review

- **Dashboard** reads `data/wallets.json`. Measured on the 29 FAIL records:
  `roi_pct` and `win_rate_pct` are present on 29/29, but `num_positions`,
  `consistency_score`, `drawdown_pct` and `total_trades` on only **1/29**, and
  `active_days` on **0/30 records**. So a rejection shows "N/A" for exactly the
  numbers that produced it. **Verified gap (D3).**
- **Dashboard** has no way to show that a wallet's history was truncated.
- **Tracking** (`WalletMonitor`) is sound: baseline before first report,
  `event_id` de-duplication, atomic state writes, read-only, never places
  orders. No change proposed.
- **Configuration**: no secrets are read; values are env-driven and parsing was
  hardened in the previous pass. At diagnosis `MAX_HISTORY_PAGES` was not
  surfaced anywhere in scan output, so the truncation it caused was invisible.
  (*After P4 the per-wallet page count, cap, stop reason and record count are
  printed, and README 5f documents the knob and its trade-offs.*)
- **Latent robustness (D5):** `analytics/performance.py::_parse_utc_date` raises
  `AttributeError` on a non-string timestamp. Not reachable through the current
  pipeline (the client normalizes timestamps), but a future API shape change
  would crash an entire scan. Type guard only — no math change.

---

## Prioritized plan

| # | Fix | Impact | Risk | Status |
|---|---|---|---|---|
| **P1** | Propagate pagination metadata (`PagedRecords`) from both paginated fetchers; pass `is_complete_history` into `compute_wallet_statistics`; persist `history_truncated`, `activity_truncated`, `closed_positions_truncated`, retrieval counts/pages | Truncated wallets become `PARTIAL`/honest instead of claiming `SUFFICIENT`. **No PASS/FAIL change** (the filter never reads retrieval status) | Low — additive metadata, defaults preserved | **Done** |
| **P2** | `exit_history_complete` parameter (defaults to `is_complete_history`); when the exit history is truncated and no trading exit was seen, `is_resolution_only=None` instead of `True` | Removes false "resolution-only" rejections; those wallets become `INSUFFICIENT_DATA`, never a fresh PASS | Low — no threshold change, no new passes | **Done** |
| **P3** | Persist diagnostic metrics for FAIL/INSUFFICIENT wallets (positions, active days, consistency, drawdown, realized P&L, avg size); single shared consistency-score helper (`analytics/consistency.py`) | Rejections become explainable from `wallets.json`, dashboard and `--report` | Low — write-only fields, one pure refactor | **Done** |
| **P4** | Diagnostics: truncation section, per-candidate History column, annotation on active-day rejections; dashboard badge + active-days column; same fields persisted for PASS records | Users can tell a truncated-window rejection from a genuine one | Low | **Done** |
| **P5** | Timestamp type guards in `_parse_utc_date`, the activity window and `calculate_drawdown`/`effective_timestamp` | Prevents a whole-scan crash on unexpected timestamp types (three crash paths were found) | Very low | **Done** |
| **P6** | README 5f, `.env.example`, `CLAUDE.md` invariants, this report | Prevents future misdiagnosis | None | **Done** |

Explicitly **not** changed: `MIN_ROI`, `REJECT_ROI_ABOVE`, `MIN_POSITIONS`,
`MIN_CONSISTENCY`, `MAX_DRAWDOWN`, `MIN_ACTIVE_DAYS`, ranking weights,
tie-breaking, Phase 6 formulas, and any trading/signing capability.
