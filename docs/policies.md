# Policies

A policy is a pure function over a list of `MemoryRecord`. It returns proposed
`Action`s. Policies run top-down; the first policy to claim a record wins.

```yaml
version: 1
policies:
  - name: ttl
    enabled: true
    rules: [...]
```

Common fields: `name`, `enabled` (default `true`).

## `ttl`

Invalidate a record when its age exceeds the first matching rule.

```yaml
- name: ttl
  rules:
    - tag: ephemeral          # exact tag match
      max_age_days: 7
    - tag_prefix: "task:"     # any tag starting with the prefix
      max_age_days: 14
    - kind: experience        # backend-neutral kind
      max_age_days: 90
    - text_regex: "MR !\\d+"  # last resort; Chinese/ASCII both work
      max_age_days: 2
```

- Age uses `updated_at`, falling back to `created_at`, then `last_used_at`.
- Records without any timestamp are skipped (never guessed).
- Already-invalidated records are skipped.
- Rule order matters: the first matching rule decides the limit.

## `supersede`

Invalidate a record that a newer record **explicitly** claims to replace.

Supported evidence:

```yaml
metadata:
  supersedes: [old-id]        # list
  supersedes: "old1,old2"     # comma string
  replaces: [old-id]          # alias
tags: ["supersedes:old-id"]   # tag form
```

Not supported by design: semantic similarity, same entity + same slot guessing, LLM
judgement. If the replacement is ambiguous, the policy does nothing. Silent guessing is
worse than a stale record because it destroys data without a trace.

The replacement id is recorded in `Action.replacement_id` and in the ledger.

## `dedup`

Collapse duplicates inside one `kind`.

```yaml
- name: dedup
  similarity: 0.95       # Jaccard over character bigrams; 1.0 = exact only
  signature_size: 16     # blocking keys per record
  max_block_size: 2000   # skip pathological buckets (common bigrams)
  max_candidates: 300    # hard cap on comparisons per record
```

How it works:

1. Exact normalized text matches are always collapsed.
2. Near-duplicates use character-bigram signature blocking: each record indexes its
   `signature_size` smallest-hash bigrams; candidates come from shared buckets.
3. A cheap length-ratio guard rejects obvious non-matches before Jaccard.
4. The newest record (then highest importance) is kept.

Threshold guidance:

- `0.95` is conservative; it catches punctuation/whitespace/typo variants.
- `0.90` catches single-character or short-phrase paraphrases but starts merging
  distinct rules in dense Chinese text.
- `0.85` should be evaluated offline against a labelled sample before use.

Calibrate on your own corpus; thresholds are corpus-specific and not portable.

## `importance`

Archive records whose decayed score falls below a threshold. Requires a backend that
exposes `importance` (the Hindsight adapter does not, so this policy warns and skips).

```yaml
- name: importance
  enabled: false
  half_life_days: 90
  archive_below: 0.15
  kinds: [world, experience]
```

Score: `importance × 0.5^(age/half_life) × (1 + 0.1·ln(1+use_count))`. It is an ordering
device inside a policy, not a truth score.

## `scope_limit`

Keep at most N records per scope, evicting the lowest score. Scopes come from a tag
prefix (default `scope:`).

```yaml
- name: scope_limit
  enabled: false
  max_per_scope: 500
  tag_prefix: "scope:"
  half_life_days: 90
```

Requires `importance`; without it the policy is skipped with a warning.

## Ordering advice

1. `ttl` — cheap, intuitive, removes obviously dead state.
2. `supersede` — explicit replacements before duplicate detection, so the surviving
   version is the one that owns the chain.
3. `dedup` — collapse what is left.
4. `importance` / `scope_limit` — only when the backend exposes the needed signals.

Putting `dedup` before `supersede` can invalidate the replacement target as a duplicate
of the old record; keep the order above unless you have a reason.
