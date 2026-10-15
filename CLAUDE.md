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
- Do not commit `.env`, credentials, local logs, or user data.

## Change workflow
- Inspect relevant code and tests before editing.
- Keep changes focused and explain which files change.
- Do not change ranking weights, ROI thresholds, frequency thresholds, or
  recommendation cutoffs unless the user explicitly asks.
- Preserve existing tests; add targeted tests for new behavior.
- Run `python -m pytest -q` after code changes and report the exact result.
- Never claim a test passed unless it was actually run.
