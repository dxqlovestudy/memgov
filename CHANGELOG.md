# Changelog

## 0.2.0 (2026-11-13)

- Full-auto AI governance: `memgov ai-govern` with candidate blocking, OpenAI-compatible
  judge, strict JSON/evidence validation, confidence and keep-newer gates, per-run caps.
- Post-apply forgetting verification with automatic rollback when the failure rate
  exceeds a threshold.
- Prompt `conflict-v1` and content-addressed decision cache.
- Candidate extraction by anchors (URL/IP/table/file/MR/quoted terms) + bigram blocking.
- 55 tests; real-gateway probe: 1 supersede pair -> 0.97-0.98 confidence, 839-849 tokens.

## 0.1.0 (2026-11-13)

Initial alpha release.

- Policy engine with dry-run by default and first-policy-wins conflict handling.
- Policies: `ttl`, `supersede` (explicit evidence only), `dedup` (exact + signature-
  blocked Jaccard), `importance`, `scope_limit`.
- Adapters: `memory`, `jsonfile`, `hindsight` (world/experience only; observations hidden).
- Audit ledger: one JSON run file plus `ledger.jsonl`; before snapshots and reasons.
- CLI: `scan`, `report`, `rollback`, `verify-forgetting`, `adapters`, `demo`.
- Forgetting verification: id or near-identical text counts as resurrection.
- 33 tests covering policies, engine, ledger, adapters and CLI.
