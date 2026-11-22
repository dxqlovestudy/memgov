from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import yaml

from . import __version__
from .adapters import HindsightAdapter, JsonFileAdapter, MemoryAdapter
from .adapters.base import Adapter
from .ai import AIConfig
from .ai_governor import AIGovernor
from .ai_supersede import SupersedeGovernor, SupersedeLedger, summarize_supersede
from .ai_transition import TransitionGovernor, summarize_transition
from .engine import Engine
from .ledger import Ledger, verify_forgetting
from .models import MemoryRecord
from .policies import build_policies
from .report import render_markdown, summary


def require_write_env(args: argparse.Namespace) -> None:
    """Hindsight writes need a second, process-level switch besides --apply."""
    if (
        getattr(args, "apply", False)
        and getattr(args, "adapter", None) == "hindsight"
        and os.environ.get("MEMGOV_ALLOW_WRITE") != "1"
    ):
        raise SystemExit(
            "refusing to mutate Hindsight: set MEMGOV_ALLOW_WRITE=1 in addition to --apply"
        )


def build_adapter(
    args: argparse.Namespace,
    write: bool = False,
    supersede_ledger: Optional[Dict[str, List[str]]] = None,
) -> Adapter:
    if args.adapter == "memory":
        return MemoryAdapter(demo_records())
    if args.adapter == "jsonfile":
        if not args.jsonfile:
            raise SystemExit("--jsonfile is required for the jsonfile adapter")
        return JsonFileAdapter(args.jsonfile)
    if args.adapter == "hindsight":
        return HindsightAdapter(
            base_url=args.base_url or os.environ.get("HINDSIGHT_API_URL", "http://127.0.0.1:8888"),
            bank=args.bank or os.environ.get("HINDSIGHT_BANK_ID", "default"),
            timeout=args.timeout,
            allow_write=write,
            supersede_ledger=supersede_ledger,
        )
    raise SystemExit(f"unknown adapter: {args.adapter}")


def load_policy(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise SystemExit("policy file must be a mapping")
    return data


def demo_records() -> List[MemoryRecord]:
    now = datetime.now(timezone.utc)
    return [
        MemoryRecord(
            id="m-pref-1",
            text="任何 git push 必须逐次征得用户同意。",
            kind="world",
            tags=["preference", "git"],
            created_at=now,
            importance=0.9,
        ),
        MemoryRecord(
            id="m-addr-old",
            text="Hindsight 实例地址为 http://192.0.2.10:8888。",
            kind="world",
            tags=["env"],
            created_at=now,
            importance=0.6,
        ),
        MemoryRecord(
            id="m-addr-new",
            text="Hindsight 实例地址为 http://192.0.2.20:8888。",
            kind="world",
            tags=["env"],
            created_at=now,
            importance=0.9,
            metadata={"supersedes": ["m-addr-old"]},
        ),
        MemoryRecord(
            id="m-dup-a",
            text="交付前必须跑全量 mvn -o test 并附原始输出。",
            kind="world",
            tags=["delivery"],
            created_at=now,
            importance=0.7,
        ),
        MemoryRecord(
            id="m-dup-b",
            text="交付前必须跑全量 mvn -o test，并附原始输出",
            kind="world",
            tags=["delivery"],
            created_at=now,
            importance=0.5,
        ),
        MemoryRecord(
            id="m-task-old",
            text="正在等 CI 跑完再合并 PR。",
            kind="experience",
            tags=["task", "ephemeral"],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            importance=0.3,
        ),
    ]


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--runs-dir", default="runs")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")


def add_adapter_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--adapter", choices=["memory", "jsonfile", "hindsight"], default="memory")
    p.add_argument("--jsonfile")
    p.add_argument("--base-url")
    p.add_argument("--bank")
    p.add_argument("--timeout", type=int, default=30)


def cmd_scan(args: argparse.Namespace) -> int:
    require_write_env(args)
    cfg = load_policy(args.policy)
    policies = build_policies(cfg)
    if not policies:
        raise SystemExit("no enabled policies in policy file")
    ledger = Ledger(args.runs_dir)
    adapter = build_adapter(args, write=args.apply)
    engine = Engine(
        adapter=adapter,
        policies=policies,
        ledger=ledger,
        dry_run=not args.apply,
        max_records=args.limit,
    )
    report = engine.run()
    path = report.run_file
    if args.json:
        print(json.dumps({"summary": summary(report), "run_file": str(path)}, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))
        print(f"run file: {path}")
        if not args.apply and report.actions:
            print("dry-run only; re-run with --apply to mutate the backend")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    run = Ledger.load_run(args.run)
    if args.json:
        print(json.dumps(run, ensure_ascii=False, indent=2))
        return 0
    print(f"# memgov run {run.get('run_id')}\n")
    print(f"- adapter: `{run.get('adapter')}`")
    print(f"- mode: **{'dry-run' if run.get('dry_run') else 'apply'}**")
    print(f"- records: {run.get('records_scanned')}")
    print(f"- counts: {json.dumps(run.get('counts') or {}, ensure_ascii=False)}")
    actions = run.get("actions") or []
    print(f"- actions: {len(actions)}, applied: {sum(1 for a in actions if a.get('applied'))}")
    if run.get("mode") == "ai":
        print(f"- ai: {json.dumps(run.get('ai') or {}, ensure_ascii=False)}")
    if run.get("verification"):
        print(f"- verification: {json.dumps(run['verification'], ensure_ascii=False)}")
    if run.get("rolled_back"):
        print("- rolled_back: yes")
    if run.get("warnings"):
        print("\n## Warnings\n")
        for w in run["warnings"]:
            print(f"- {w}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    require_write_env(args)
    run = Ledger.load_run(args.run)
    adapter = build_adapter(args, write=args.apply)
    todo = [
        a
        for a in (run.get("actions") or [])
        if a.get("applied") and a.get("action") in ("invalidate", "archive")
    ]
    if not todo:
        print("nothing to roll back")
        return 0
    if not args.apply:
        print(f"would restore {len(todo)} memories (dry-run; pass --apply)")
        for a in todo:
            print(f"  {a.get('memory_id')}  {a.get('reason')}")
        return 0
    ok = 0
    for a in todo:
        try:
            adapter.restore(a["memory_id"])
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"restore failed for {a.get('memory_id')}: {exc}", file=sys.stderr)
    print(f"restored {ok}/{len(todo)}")
    return 0 if ok == len(todo) else 1


def cmd_verify(args: argparse.Namespace) -> int:
    run = Ledger.load_run(args.run)
    adapter = build_adapter(args)
    result = verify_forgetting(
        run, lambda q, n: adapter.recall(q, n), min_similarity=args.min_similarity, top=args.top
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["failures"] == 0 else 1


def cmd_ai_govern(args: argparse.Namespace) -> int:
    require_write_env(args)
    base_url = args.ai_base_url or os.environ.get("MEMGOV_AI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("MEMGOV_AI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    model = args.ai_model or os.environ.get("MEMGOV_AI_MODEL") or ""
    if not args.candidates_only:
        if not base_url or not model:
            raise SystemExit("--ai-base-url and --ai-model (or env) are required")
        if not api_key:
            raise SystemExit("MEMGOV_AI_API_KEY or OPENAI_API_KEY is required")

    cache_path = None
    if not args.no_cache:
        cache_path = args.ai_cache or os.path.join(args.runs_dir, "ai-cache.json")

    config = AIConfig(
        base_url=base_url or "http://127.0.0.1:1",
        api_key=api_key,
        model=model or "none",
        confidence_threshold=args.confidence,
        max_pairs=args.max_pairs,
        max_invalidations=args.max_invalidations,
        min_candidate_score=args.min_candidate_score,
        auto_rollback=args.auto_rollback,
        verify_failure_threshold=args.verify_threshold,
        cache_path=cache_path,
    )
    ledger = Ledger(args.runs_dir)
    adapter = build_adapter(args, write=args.apply)
    governor = AIGovernor(
        adapter=adapter,
        config=config,
        ledger=ledger,
        dry_run=not args.apply,
        max_records=args.limit,
        candidates_only=args.candidates_only,
    )
    report = governor.run()
    if args.candidates_only:
        from .ai_governor import summarize_candidates

        print(summarize_candidates(report))
    elif args.json:
        print(json.dumps({"summary": summary(report), "run_file": report.run_file,
                          "ai": report.ai, "verification": report.verification,
                          "rolled_back": report.rolled_back}, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))
        print(f"run file: {report.run_file}")
        if not args.apply and report.actions:
            print("dry-run only; re-run with --apply for full-auto governance")
    return 0


def cmd_ai_transition(args: argparse.Namespace) -> int:
    require_write_env(args)
    base_url = args.ai_base_url or os.environ.get("MEMGOV_AI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("MEMGOV_AI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    model = args.ai_model or os.environ.get("MEMGOV_AI_MODEL") or ""
    if not args.candidates_only:
        if not base_url or not model:
            raise SystemExit("--ai-base-url and --ai-model (or env) are required")
        if not api_key:
            raise SystemExit("MEMGOV_AI_API_KEY or OPENAI_API_KEY is required")

    cache_path = None
    if not args.no_cache:
        cache_path = args.ai_cache or os.path.join(args.runs_dir, "ai-cache.json")
    config = AIConfig(
        base_url=base_url or "http://127.0.0.1:1",
        api_key=api_key,
        model=model or "none",
        confidence_threshold=args.confidence,
        auto_rollback=args.auto_rollback,
        verify_failure_threshold=args.verify_threshold,
        cache_path=cache_path,
    )
    ledger = Ledger(args.runs_dir)
    adapter = build_adapter(args, write=args.apply)
    governor = TransitionGovernor(
        adapter=adapter,
        config=config,
        ledger=ledger,
        dry_run=not args.apply,
        max_records=args.limit,
        candidates_only=args.candidates_only,
        aliases_path=args.aliases,
        max_transitions=args.max_transitions,
        max_candidates=args.max_candidates,
        max_judgements=args.max_judgements,
        max_invalidations=args.max_invalidations,
        only_transition=[x for x in (args.only_transition or "").split(",") if x.strip()],
    )
    report = governor.run()
    if args.candidates_only:
        print(summarize_transition(report))
    elif args.json:
        print(json.dumps({"summary": summary(report), "run_file": report.run_file,
                          "ai": report.ai, "verification": report.verification,
                          "rolled_back": report.rolled_back}, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))
        print(f"run file: {report.run_file}")
        if not args.apply and report.actions:
            print("dry-run only; re-run with --apply and MEMGOV_ALLOW_WRITE=1 to mutate")
    return 0


def cmd_ai_supersede(args: argparse.Namespace) -> int:
    """Certify supersessions with three judge framings; write the local ledger only.

    No ``require_write_env``: ``--apply`` writes the local supersede ledger, never the
    Hindsight backend. The backend mutation happens later via ``scan --apply``
    (SupersedePolicy), which is the command that needs ``MEMGOV_ALLOW_WRITE=1``.
    """
    base_url = args.ai_base_url or os.environ.get("MEMGOV_AI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("MEMGOV_AI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    model = args.ai_model or os.environ.get("MEMGOV_AI_MODEL") or ""
    if not base_url or not model:
        raise SystemExit("--ai-base-url and --ai-model (or env) are required")
    if not api_key:
        raise SystemExit("MEMGOV_AI_API_KEY or OPENAI_API_KEY is required")

    cache_path = None
    if not args.no_cache:
        cache_path = args.ai_cache or os.path.join(args.runs_dir, "ai-cache.json")
    config = AIConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        confidence_threshold=args.confidence,
        cache_path=cache_path,
        prompt_version="supersede-v1",
    )
    ledger = Ledger(args.runs_dir)
    supersede_ledger = SupersedeLedger(
        args.supersede_ledger or os.path.join(args.runs_dir, "supersede-ledger.jsonl")
    )
    adapter = build_adapter(args, write=False)
    governor = SupersedeGovernor(
        adapter=adapter,
        config=config,
        ledger=ledger,
        supersede_ledger=supersede_ledger,
        dry_run=not args.apply,
        max_records=args.limit,
        max_pairs=args.max_pairs,
        max_accepts=args.max_accepts,
        min_score=args.min_score,
        min_similarity=args.min_similarity,
        vote_threshold=args.vote_threshold,
        min_confidence=args.confidence,
        seeded_overlap=args.seeded_overlap,
        seeded_top_k=args.seeded_top_k,
        use_seeded=args.use_seeded,
        use_duplicate=args.use_duplicate,
        duplicate_confidence=args.duplicate_confidence,
    )
    report = governor.run()
    if args.json:
        print(
            json.dumps(
                {"summary": summary(report), "run_file": report.run_file, "ai": report.ai},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(summarize_supersede(report))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    policies = build_policies(
        {
            "policies": [
                {"name": "ttl", "enabled": True, "rules": [{"tag": "ephemeral", "max_age_days": 7}]},
                {"name": "supersede", "enabled": True},
                {"name": "dedup", "enabled": True, "similarity": 0.92},
            ]
        }
    )
    adapter = MemoryAdapter(demo_records())
    ledger = Ledger(args.runs_dir)
    report = Engine(adapter, policies, ledger, dry_run=True).run()
    print(render_markdown(report))
    print(f"run file: {report.run_file}")
    return 0


def cmd_adapters(_: argparse.Namespace) -> int:
    for adapter in (MemoryAdapter(),):
        print(json.dumps(adapter.describe(), ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "name": "hindsight",
                "capabilities": HindsightAdapter("http://127.0.0.1:8888", "default").describe()[
                    "capabilities"
                ],
                "note": "writes require --apply plus MEMGOV_ALLOW_WRITE=1",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="memgov", description="Memory governance engine")
    parser.add_argument("--version", action="version", version=f"memgov {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="evaluate policies and optionally apply them")
    add_common(p_scan)
    add_adapter_args(p_scan)
    p_scan.add_argument("--policy", default="examples/policies/default.yaml")
    p_scan.add_argument("--limit", type=int, default=None)
    p_scan.add_argument("--apply", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    p_report = sub.add_parser("report", help="render a previous run")
    p_report.add_argument("run")
    p_report.add_argument("--json", action="store_true")
    p_report.set_defaults(func=cmd_report)

    p_rollback = sub.add_parser("rollback", help="restore memories invalidated by a run")
    p_rollback.add_argument("run")
    add_adapter_args(p_rollback)
    p_rollback.add_argument("--apply", action="store_true")
    p_rollback.set_defaults(func=cmd_rollback)

    p_verify = sub.add_parser("verify-forgetting", help="check invalidated memories stay forgotten")
    p_verify.add_argument("run")
    add_adapter_args(p_verify)
    p_verify.add_argument("--top", type=int, default=5)
    p_verify.add_argument("--min-similarity", type=float, default=0.9)
    p_verify.set_defaults(func=cmd_verify)

    p_ai = sub.add_parser("ai-govern", help="full-AI governance: candidates -> model -> actions")
    add_common(p_ai)
    add_adapter_args(p_ai)
    p_ai.add_argument("--limit", type=int, default=None)
    p_ai.add_argument("--apply", action="store_true")
    p_ai.add_argument("--ai-base-url")
    p_ai.add_argument("--ai-model")
    p_ai.add_argument("--ai-cache")
    p_ai.add_argument("--no-cache", action="store_true")
    p_ai.add_argument("--confidence", type=float, default=0.85)
    p_ai.add_argument("--max-pairs", type=int, default=100)
    p_ai.add_argument("--max-invalidations", type=int, default=10)
    p_ai.add_argument("--min-candidate-score", type=float, default=0.22)
    p_ai.add_argument("--auto-rollback", dest="auto_rollback", action="store_true", default=True)
    p_ai.add_argument("--no-auto-rollback", dest="auto_rollback", action="store_false")
    p_ai.add_argument("--verify-threshold", type=float, default=0.1)
    p_ai.add_argument("--candidates-only", action="store_true")
    p_ai.set_defaults(func=cmd_ai_govern)

    p_tr = sub.add_parser("ai-transition", help="state-transition governance: T -> old current-state assertions")
    add_common(p_tr)
    add_adapter_args(p_tr)
    p_tr.add_argument("--limit", type=int, default=None)
    p_tr.add_argument("--apply", action="store_true")
    p_tr.add_argument("--aliases", help="YAML alias file (default built-in aliases)")
    p_tr.add_argument("--max-transitions", type=int, default=10)
    p_tr.add_argument("--max-candidates", type=int, default=5)
    p_tr.add_argument("--max-judgements", type=int, default=25)
    p_tr.add_argument("--max-invalidations", type=int, default=3)
    p_tr.add_argument("--confidence", type=float, default=0.90)
    p_tr.add_argument("--ai-base-url")
    p_tr.add_argument("--ai-model")
    p_tr.add_argument("--ai-cache")
    p_tr.add_argument("--no-cache", action="store_true")
    p_tr.add_argument("--auto-rollback", dest="auto_rollback", action="store_true", default=True)
    p_tr.add_argument("--no-auto-rollback", dest="auto_rollback", action="store_false")
    p_tr.add_argument("--verify-threshold", type=float, default=0.1)
    p_tr.add_argument("--candidates-only", action="store_true")
    p_tr.add_argument("--only-transition", default="", help="comma-separated transition id prefixes")
    p_tr.set_defaults(func=cmd_ai_transition)

    p_sup = sub.add_parser(
        "ai-supersede",
        help="certify supersessions (MEMOIR-style 3 framings) and write the local supersede ledger",
    )
    add_common(p_sup)
    add_adapter_args(p_sup)
    p_sup.add_argument("--limit", type=int, default=None)
    p_sup.add_argument("--apply", action="store_true", help="write accepted pairs to the supersede ledger")
    p_sup.add_argument("--supersede-ledger", help="path of the JSONL supersede ledger")
    p_sup.add_argument("--max-pairs", type=int, default=100)
    p_sup.add_argument("--max-accepts", type=int, default=10)
    p_sup.add_argument("--min-score", type=float, default=0.30, help="candidate score floor (blocking only)")
    p_sup.add_argument("--min-similarity", type=float, default=0.0)
    p_sup.add_argument("--vote-threshold", type=float, default=0.6, help="SUPERSEDES vote ratio (MEMOIR tau_s)")
    p_sup.add_argument("--seeded-overlap", type=float, default=0.22, help="text-overlap floor for conflict-seeded candidates")
    p_sup.add_argument("--seeded-top-k", type=int, default=3)
    p_sup.add_argument("--seeded-quota-ratio", type=float, default=0.7, help="share of candidate slots reserved for the seeded channel")
    p_sup.add_argument("--no-seeded", dest="use_seeded", action="store_false", help="disable the conflict-seeded channel")
    p_sup.add_argument("--no-duplicate", dest="use_duplicate", action="store_false", help="disable DUPLICATE verdicts")
    p_sup.add_argument(
        "--duplicate-confidence",
        type=float,
        default=None,
        help="confidence gate for DUPLICATE (default: --confidence minus 0.05)",
    )
    p_sup.add_argument("--confidence", type=float, default=0.85, help="mean confidence gate (calibrated on deepseek-flash: 0.77-0.88 band)")
    p_sup.add_argument("--ai-base-url")
    p_sup.add_argument("--ai-model")
    p_sup.add_argument("--ai-cache")
    p_sup.add_argument("--no-cache", action="store_true")
    p_sup.set_defaults(func=cmd_ai_supersede)

    p_demo = sub.add_parser("demo", help="run the built-in demo")
    p_demo.add_argument("--runs-dir", default="runs")
    p_demo.set_defaults(func=cmd_demo)

    p_adapters = sub.add_parser("adapters", help="show adapter capabilities")
    p_adapters.set_defaults(func=cmd_adapters)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
