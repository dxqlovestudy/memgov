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


def test_ai_govern_candidates_only(tmp_path, capsys):
    data = tmp_path / "m.json"
    data.write_text(
        '{"records":['
        '{"id":"old","text":"实例地址是 http://192.0.2.10:8888","kind":"world",'
        '"created_at":"2026-11-01T00:00:00Z"},'
        '{"id":"new","text":"实例地址改为 http://192.0.2.10:9999","kind":"world",'
        '"created_at":"2026-11-02T00:00:00Z"}]}',
        encoding="utf-8",
    )
    rc = main([
        "ai-govern", "--adapter", "jsonfile", "--jsonfile", str(data),
        "--candidates-only", "--runs-dir", str(tmp_path / "runs"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "candidate pairs" in out
    assert "192.0.2.10" in out


def test_hindsight_apply_requires_env(tmp_path, monkeypatch):
    import pytest

    monkeypatch.delenv("MEMGOV_ALLOW_WRITE", raising=False)
    with pytest.raises(SystemExit):
        main([
            "scan", "--adapter", "hindsight", "--base-url", "http://127.0.0.1:1",
            "--bank", "x", "--policy", str(tmp_path / "missing.yaml"), "--apply",
            "--runs-dir", str(tmp_path),
        ])
