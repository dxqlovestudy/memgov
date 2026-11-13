# Design

## Problem

Memory backends are optimized for **recall**. Governance is usually an afterthought:
a manual delete endpoint, a soft-delete flag, or nothing at all. The result is a store
that grows and silently degrades:

- stale facts remain valid and keep matching;
- multiple versions of the same fact coexist with equal weight;
- temporary task state is indistinguishable from durable rules;
- a “deleted” fact is still reachable through a derived summary;
- no one can reconstruct why a record disappeared.

`memgov` is a thin, backend-agnostic layer that makes those decisions explicit,
repeatable and auditable.

## Gap analysis (measured on Hindsight 1.2.0)

| Capability | Hindsight 1.2.0 | memgov |
|---|---|---|
| Manual soft invalidation | yes — `PATCH /memories/{id}` `{state: invalidated}` | adapter action |
| Invalidate reason | yes — `reason` field | recorded in ledger |
| Bulk delete by type | yes — `DELETE /memories?type=` | optional `delete` action |
| Near-duplicate merge | automatic consolidation (semantic, not policy-driven) | `dedup` policy (exact + Jaccard) |
| TTL / expiration | **no** — no `expires_at`, no ttl config | `ttl` policy |
| Supersede / version chain | **no** — only `valid` / `invalidated` | `supersede` policy (explicit evidence) |
| Importance / decay | **no** — no importance score | `importance` policy (if backend exposes it) |
| Usage signals | **no** — no `use_count` / `last_used_at` | `importance`/`scope_limit` if available |
| Capacity limit | **no** — `max_observations_per_scope=-1` | `scope_limit` policy |
| Active forgetting check | **no** | `verify-forgetting` |

Observation records are derived and rejected by curation (`400 only world/experience
facts can be curated`), so the Hindsight adapter never exposes them as actionable.

## Lifecycle model

```
valid ──invalidate──▶ invalidated ──restore──▶ valid
  │                        │
  └──delete (opt-in)──▶ gone (not reversible)
```

- **invalidate** is the default: soft, reversible, excluded from recall by the backend.
- **archive** is expressed as invalidate with an `archive:` reason when the backend has
  no separate archive state.
- **delete** is opt-in and marked irreversible in the ledger.

## Engine semantics

1. Read one snapshot of live records from the adapter.
2. Run policies in configured order over the snapshot.
3. A record claimed by an earlier policy is never touched by a later one; the conflict
   is reported as a warning rather than silently resolved.
4. Normalize actions against adapter capabilities (archive→invalidate; drop delete when
   unsupported).
5. In dry-run, stop here and write the report.
6. In apply mode, call the adapter per action, record `applied` / `error`, write the
   ledger.

Policies never call the backend; this keeps evaluation deterministic and makes dry-run
equivalent to apply minus the mutation.

## Safety invariants

- `dry_run=True` is the default in the engine, CLI and tests.
- Hindsight writes require both `--apply` and `MEMGOV_ALLOW_WRITE=1`.
- Every action keeps the record's full `before` snapshot and a human-readable `reason`.
- Invalidation is reversible; rollback is a first-class CLI command.
- No similarity-based supersede: if the replacement is not explicit, nothing happens.
- Unsupported capabilities produce warnings, never a pretend action.
- Observations and other derived records are not curated directly.

## Non-goals

- Not a memory store or vector database.
- Not a recall ranker or reranker.
- Not an automatic truth arbiter: semantic conflict detection is explicitly out of scope
  for v0.1.
- Not a hosted service (yet).
