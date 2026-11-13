# Adapters

An adapter turns a backend into the small contract `memgov` needs.

```python
class Adapter:
    name: str
    capabilities: Capabilities

    def list_memories(self, limit=None) -> list[MemoryRecord]: ...
    def apply(self, action: Action) -> bool: ...
    def restore(self, memory_id: str) -> bool: ...
    def recall(self, query: str, limit: int = 5) -> list[dict]: ...
```

## Capabilities

| Capability | Meaning |
|---|---|
| `invalidate` | soft-retire a record, reversible |
| `delete` | hard delete, irreversible |
| `archive` | a separate archive state (otherwise archive→invalidate) |
| `importance` | records expose a meaningful importance score |
| `usage_signals` | `use_count` / `last_used_at` are maintained |
| `recall` | can run a query for forgetting verification |

Policies declare `requires`; the engine skips a policy with a warning when a requirement
is missing. This is how a backend-neutral policy set stays honest.

## `memory`

In-memory list of `MemoryRecord`. Intended for tests, demos and policy development.

## `jsonfile`

A JSON snapshot:

```json
{"records": [{"id": "a", "text": "...", "kind": "world", "tags": ["ephemeral"],
              "created_at": "2026-01-01T00:00:00Z", "importance": 0.7}]}
```

Mutations are persisted back to the same file. Useful for offline evaluation and CI.

## `hindsight`

REST adapter for Hindsight 1.x.

| memgov | Hindsight |
|---|---|
| `kind` | `fact_type` (`world`, `experience`) |
| `created_at` | `date` or `mentioned_at` |
| `updated_at` | `updated_at` |
| `supersedes` | `metadata.supersedes` |
| `invalidate` | `PATCH /v1/default/banks/{bank}/memories/{id}` `{state: invalidated, reason}` |
| `restore` | same endpoint, `{state: valid}` |
| `recall` | `POST /v1/default/banks/{bank}/memories/recall` |

Important limits:

- **Observations are hidden.** Hindsight rejects curation of derived observations with
  `400 only world/experience facts can be curated`; the adapter lists only `world` and
  `experience`. Invalidating a source fact prunes its derived observations server-side.
- **No TTL, supersede, importance or usage fields.** Those policies warn and skip unless
  you write the corresponding metadata yourself.
- **Delete is disabled by default.** `allow_delete=True` is required, and the action is
  recorded as irreversible.
- **Two write switches.** `--apply` turns off dry-run; `MEMGOV_ALLOW_WRITE=1` (or
  `allow_write=True`) unlocks the adapter. Both must agree.
- **Pagination** is `limit`/`offset` with a 500-record page cap; scans of ~5k records
  take ~20s in the reference environment.

## Adding an adapter

1. Implement `list_memories` returning **actionable** records only. Hide derived,
   read-only or index-only rows.
2. Set `capabilities` truthfully. Do not claim `importance` if the backend has no such
   signal.
3. Implement `apply` with the smallest mutation that expresses the action; prefer soft
   invalidation over delete.
4. Implement `restore` for every reversible action, and `recall` if forgetting
   verification should work.
5. Add a test with a fake transport (no live backend) covering mapping, capability
   guards and one apply/restore round-trip.
6. Document the mapping and limits in this file.
