# Contributing

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Python 3.9+ is required. The runtime dependency is only `PyYAML`; everything else is
standard library.

## Before opening a PR

- `pytest` passes.
- `ruff check .` passes (CI runs it).
- New behavior has a test. Policy tests should cover both the positive case and the
  “do nothing” case.
- No live backend is required in tests: fake the transport (`adapter._request`) or use
  the `memory` / `jsonfile` adapters.
- Update `docs/` when you add or change a policy, adapter or metric.

## Adding a policy

1. Subclass `memgov.policies.Policy`.
2. Declare `requires` if the policy needs a backend capability.
3. Evaluate the snapshot only; never call the backend from a policy.
4. Return `Action` objects with a stable `reason` and the record's `before` snapshot.
5. Register it in `build_policies`.
6. Add it to `docs/policies.md` with an example and caveats.

## Adding an adapter

See [docs/adapters.md](docs/adapters.md). The contract is deliberately four methods.
Do not hide mutations behind `list_memories`.

## Code style

- Standard library first; keep dependencies minimal.
- Prefer explicit failures over silent fallbacks.
- Comments explain *why*, not *what*.
- Keep policy evaluation deterministic: same snapshot + same config = same actions.

## Security

- Never commit credentials, tokens, private endpoints or real memory dumps.
- Example data must be synthetic.
- Write adapters must default to read-only and require an explicit write switch.

## Reporting issues

Include the policy file, the backend and version, the command, and the relevant part of
the run JSON (redact text if needed).
