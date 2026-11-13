from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memgov.adapters import MemoryAdapter
from memgov.engine import Engine
from memgov.ledger import Ledger, verify_forgetting
from memgov.models import MemoryRecord
from memgov.policies import TTLPolicy


def make_records():
    now = datetime.now(timezone.utc)
    return [
        MemoryRecord(id="old", text="旧的临时任务", kind="experience", tags=["ephemeral"],
                     created_at=now - timedelta(days=30)),
        MemoryRecord(id="keep", text="长期偏好", kind="world", created_at=now),
    ]


def test_dry_run_does_not_mutate(tmp_path):
    adapter = MemoryAdapter(make_records())
    ledger = Ledger(str(tmp_path))
    report = Engine(adapter, [TTLPolicy([{"tag": "ephemeral", "max_age_days": 7}])],
                    ledger, dry_run=True).run()
    assert len(report.actions) == 1
    assert report.actions[0].applied is False
    assert [r.id for r in adapter.list_memories()] == ["old", "keep"]
    assert (tmp_path / "ledger.jsonl").exists()


def test_apply_mutates_and_is_rollbackable(tmp_path):
    adapter = MemoryAdapter(make_records())
    ledger = Ledger(str(tmp_path))
    report = Engine(adapter, [TTLPolicy([{"tag": "ephemeral", "max_age_days": 7}])],
                    ledger, dry_run=False).run()
    assert report.actions[0].applied is True
    assert [r.id for r in adapter.list_memories()] == ["keep"]

    run_file = ledger.latest_run()
    run = Ledger.load_run(str(run_file))
    action = run["actions"][0]
    assert action["before"]["text"] == "旧的临时任务"
    adapter.restore(action["memory_id"])
    assert [r.id for r in adapter.list_memories()] == ["old", "keep"]


def test_verify_forgetting_detects_resurrection(tmp_path):
    adapter = MemoryAdapter(make_records())
    ledger = Ledger(str(tmp_path))
    report = Engine(adapter, [TTLPolicy([{"tag": "ephemeral", "max_age_days": 7}])],
                    ledger, dry_run=True).run()
    run = report.to_dict()
    # memory still exists -> recall finds it -> memory governance failure
    result = verify_forgetting(run, lambda q, n: adapter.recall(q, n), top=5)
    assert result["checked"] == 1
    assert result["failures"] == 1
    assert result["failure_rate"] == 1.0

    # after removal, forgetting passes
    adapter.records = [r for r in adapter.records if r.id != "old"]
    result2 = verify_forgetting(run, lambda q, n: adapter.recall(q, n), top=5)
    assert result2["failures"] == 0


def test_unsupported_policy_warns(tmp_path):
    from memgov.policies import Capabilities, ImportancePolicy

    adapter = MemoryAdapter(make_records())
    adapter.capabilities = Capabilities(importance=False)
    ledger = Ledger(str(tmp_path))
    report = Engine(adapter, [ImportancePolicy()], ledger, dry_run=True).run()
    assert report.actions == []
    assert any("lacks importance" in w for w in report.warnings)


def test_report_file_is_json(tmp_path):
    adapter = MemoryAdapter(make_records())
    ledger = Ledger(str(tmp_path))
    Engine(adapter, [TTLPolicy([{"tag": "ephemeral", "max_age_days": 7}])],
           ledger, dry_run=True).run()
    run_file = ledger.latest_run()
    assert run_file is not None
    data = json.loads(Path(run_file).read_text(encoding="utf-8"))
    assert data["adapter"] == "memory"
    assert data["records_scanned"] == 2
