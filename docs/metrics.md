# Governance metrics

`memgov` reports what it changed and whether the change held. It deliberately does not
report a single “memory quality score”: quality depends on the task, and a composite
score hides the trade-offs.

## Per-run metrics

| Metric | Definition | Notes |
|---|---|---|
| `records_scanned` | live records returned by the adapter | excludes derived/read-only rows |
| actions by policy | counts for `ttl`, `supersede`, `dedup`, `importance`, `scope_limit` | a record is claimed by at most one policy |
| `applied` / `errors` | mutations succeeded / failed | errors are per action, with the backend message |
| `warnings` | unsupported capabilities, claimed-record conflicts | warnings must be reviewed, not suppressed |

## Forgetting failure rate (F)

After applying invalidations, `memgov verify-forgetting` re-queries the backend with each
invalidated record's text and checks whether it still comes back.

```
F = records still recalled / invalidated records checked
```

A hit by **id** or by **near-identical text** counts as a failure, because backends may
resurface a derived copy of the same fact. `F > 0` means the deletion contract is broken
at the retrieval layer; this is the failure mode that a simple “DELETE returned 200”
check cannot see.

This metric follows the *active forgetting* dimension of
[GateMem](https://github.com/rzhub/GateMem) (MGS = U × (1 − A) × (1 − F) for shared-memory
agents). `memgov` only implements the F part; utility and access control are task-specific.

## Coverage and audit metrics

- **supersede coverage**: share of records whose metadata declares a replacement chain.
  Zero coverage means the `supersede` policy is inert — usually because the writer never
  records `supersedes`.
- **dedup yield**: duplicates found / records scanned. A sudden jump indicates a write
  path that stopped deduplicating upstream.
- **audit completeness**: every action has `before`, `reason`, `policy`, `applied`.
  `memgov` writes one JSON run file plus `ledger.jsonl`; a rollback needs the run file.

## What not to use

- **Total record count** is not a quality metric; a growing bank can be healthier than a
  shrinking one.
- **Action count** is not impact: 500 TTL invalidations of dead task state may matter less
  than one wrong supersede.
- **Forgetting rate without a denominator**: always report `checked` alongside `F`.

## Reference environment

A full dry-run over 5,326 live Hindsight records (world + experience) takes about 20
seconds on an Apple-silicon laptop with `signature_size=16`, `max_candidates=300` and
`similarity=0.95`. Dedup dominates the runtime; TTL and supersede are effectively free.
