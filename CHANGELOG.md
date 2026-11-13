# Changelog

## 0.1.0 (2026-11-13)

Initial alpha release.

- Policy engine with dry-run by default and first-policy-wins conflict handling.
- Policies: `ttl`, `supersede` (explicit evidence only), `dedup` (exact + signature-
  blocked Jaccard), `importance`, `scope_limit`.
- Adapters: `memory`, `jsonfile`, `hindsight` (world/experience only; observations hidden).
- Audit ledger: one JSON run file plus `ledger.jsonl`; before snapshots and reasons.
- CLI: `scan`, `report`, `rollback`, `verify-forgetting`, `adapters`, `demo`.
- Forgetting verification: id or near-identical text counts as resurrection.
- 32 tests covering policies, engine, ledger, adapters and CLI.
