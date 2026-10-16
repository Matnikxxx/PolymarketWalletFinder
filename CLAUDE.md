# Polymarket Wallet Finder / Tracker

## Purpose
This project discovers candidate Polymarket wallets, analyzes their public activity,
filters and ranks wallets, verifies available data, and records new activity from
wallets the user explicitly chooses to track.

## Safety boundaries
- Read-only public data only.
- Never place, simulate, copy, or execute trades.
- Never request, read, log, or store private keys, seed phrases, or signing credentials.
- Never enable tracking automatically. New candidates default to `tracked: false`.
- Do not add bankroll, position-management, copy-sizing, or order-execution features.
- Keep the dashboard read-only; do not add mutation routes.
- Do not make live API requests in tests. Use mocks/fixtures.

## Main flow
1. `wallet/scanner.py` discovers a bounded candidate list.
2. `analytics/wallet_stats.py` calculates wallet statistics.
3. `filters/wallet_filter.py` applies wallet eligibility filters.
4. `wallet/ranking.py` scores candidates and assigns recommendations.
5. `wallet/verifier.py` records verification/data-quality results.
6. `wallet/monitor.py` polls selected wallets and records newly observed activity.
7. `analytics/dashboard.py` prepares read-only dashboard data; `dashboard/app.py`
   renders it.

## Configuration and persistence
- Central settings are in `config/settings.py`; environment examples are in `.env.example`.
- Wallet configuration is stored in `data/wallets.json`.
- Use `tracked: true` to select a wallet for ongoing tracking.
- For compatibility, old configuration entries with `enabled` are still read when
  `tracked` is absent. New writes should use `tracked`.
- Do not discard existing user selections or history during migrations.
- `--scan-wallets` merges into the stored config (case-insensitive address
  matching, custom fields and `tracked` preserved, undiscovered wallets kept);
  it must never rebuild the file from scratch.
- Discovery pagination: `LEADERBOARD_PAGE_SIZE` (max 50) and
  `MAX_LEADERBOARD_PAGES` bound the leaderboard walk; `MAX_WALLET_CANDIDATES`
  caps the combined leaderboard + seed pool.
- Stale-data rule: a failed refresh must never overwrite stored metrics or
  scores. It sets `data_quality="STALE"`, `hard_filter_status="STALE"`, updates
  `last_refresh_attempt`, leaves `last_successful_refresh` untouched, and bars
  the wallet from ranking. A *successful empty* response is not stale.
- Discovery must keep the seed list reachable: the leaderboard walk is bounded
  by `MAX_WALLET_CANDIDATES - len(seeds)` whenever the cap can hold the whole
  seed list, so a scan never rediscovers a leaderboard-only pool.
- Every scanned candidate records `first_seen_at`/`last_seen_at`/`times_seen`/
  `discovery_source`; a wallet that is only rediscovered keeps its metrics.
- `monitor/scan_report.py` aggregates diagnostics read-only: it must never
  recompute metrics, re-run filters, re-rank, or alter thresholds, and it must
  state that frequency penalties affect the score, not ranking eligibility.
- Do not lower hard-filter thresholds or add leaderboard rotation to raise the
  PASS count. Report the rejection-reason counts and leave the decision to the
  user.
- A short leaderboard page must never end discovery (it usually means the
  server clipped the page, not that the data ran out). Empty page, repeated
  page, page cap, budget and API errors are the authoritative stops.
- Config values must never fail silently: unparseable values fall back to the
  default AND are recorded in `config.settings.CONFIG_ISSUES`, which the scan
  diagnostics print. An exported variable overriding `.env` is reported too
  (python-dotenv's `override=False` is deliberate; do not flip it without
  saying so).
- Do not commit `.env`, credentials, local logs, or user data.

## Change workflow
- Inspect relevant code and tests before editing.
- Keep changes focused and explain which files change.
- Do not change ranking weights, ROI thresholds, frequency thresholds, or
  recommendation cutoffs unless the user explicitly asks.
- Preserve existing tests; add targeted tests for new behavior.
- Run `python -m pytest -q` after code changes and report the exact result.
- Never claim a test passed unless it was actually run.
