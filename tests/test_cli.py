from __future__ import annotations

from memgov.cli import main


def test_demo_runs(tmp_path, capsys):
    rc = main(["demo", "--runs-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "memgov scan report" in out
    assert "supersede" in out
    assert "ttl" in out
    assert list(tmp_path.glob("*.json"))


def test_scan_jsonfile_dry_run(tmp_path, capsys):
    policy = tmp_path / "p.yaml"
    policy.write_text(
        "policies:\n  - name: ttl\n    rules:\n      - tag: ephemeral\n        max_age_days: 7\n",
        encoding="utf-8",
    )
    data = tmp_path / "m.json"
    data.write_text(
        '{"records":[{"id":"old","text":"task","kind":"experience",'
        '"tags":["ephemeral"],"created_at":"2026-01-01T00:00:00Z"}]}',
        encoding="utf-8",
    )
    rc = main(
        [
            "scan", "--adapter", "jsonfile", "--jsonfile", str(data),
            "--policy", str(policy), "--runs-dir", str(tmp_path / "runs"), "--json",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert '"dry_run": true' in out
    # dry-run must not mutate the source file
    assert "2026-01-01T00:00:00" in data.read_text(encoding="utf-8")
