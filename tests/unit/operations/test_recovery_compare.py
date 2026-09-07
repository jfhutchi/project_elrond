"""Prove capture and comparison expose real divergence without promoting a winner."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from quantbot.operations.recovery_compare import compare_reports, render_comparison
from quantbot.operations.store_inventory import inspect_store

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/recovery_evidence.py"


def ledger(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER, store_identity TEXT);
            INSERT INTO schema_version VALUES (12, 'copied-lineage');
            CREATE TABLE runs (run_id TEXT, status TEXT, started_at TEXT, finished_at TEXT);
            INSERT INTO runs VALUES ('run-a', 'HALTED', '2026-08-19T20:05:00Z', NULL);
            CREATE TABLE account_snapshots (snapshot_id TEXT, run_id TEXT, account_id TEXT,
                captured_at TEXT, cash TEXT, buying_power TEXT, equity TEXT, currency TEXT);
            INSERT INTO account_snapshots VALUES ('snapshot-a', 'run-a', 'private-account',
                '2026-08-20T14:05:44Z', '100', '100', '100', 'USD');
            CREATE TABLE kill_switch_state (engaged INTEGER, updated_at TEXT);
            INSERT INTO kill_switch_state VALUES (1, '2026-08-20T14:05:44Z');
        """)
        conn.commit()
    finally:
        conn.close()


def capture(path: Path, host: str = "measured-host") -> dict:
    return {
        "report_version": 1,
        "host": {"hostname": host},
        "as_of": "2026-09-05T12:00:00Z",
        "stores": [inspect_store(path)],
    }


def test_same_account_lineage_latest_dates_and_uuid_do_not_hide_changed_snapshot(tmp_path):
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    ledger(first)
    shutil.copyfile(first, second)
    conn = sqlite3.connect(second)
    conn.execute("UPDATE account_snapshots SET equity='90'")
    conn.commit()
    conn.close()
    a, b = capture(first), capture(second)
    assert a["stores"][0]["latest_account_snapshot"] == b["stores"][0]["latest_account_snapshot"]
    report = compare_reports({"alienware": a, "pi": b}, expected_hosts=["omen", "alienware", "pi"])
    assert report["store_authority"] == "AMBIGUOUS"
    assert report["selected_store"] is None and report["startup_allowed"] is False
    assert report["missing_host_captures"] == ["omen"]
    pair = report["comparisons"][0]
    assert pair["differing_fields"] == ["account_snapshot_content", "content_sha256"]
    assert "runtime_observations" in pair["unavailable_fields"]
    markdown = render_comparison(report)
    assert "account_snapshot_content" in markdown and "Recorded evidence differs" in markdown
    assert "private-account" not in markdown
    assert (
        compare_reports({"pi": b, "alienware": a}, expected_hosts=["pi", "omen", "alienware"])
        == report
    )


def test_matching_file_ids_and_bytes_across_captures_never_collapse_hosts(tmp_path):
    path = tmp_path / "state.db"
    ledger(path)
    a, b = capture(path), capture(path)
    # Identical hostname can arise from cloned OS images; labels still bound separate captures.
    report = compare_reports({"alienware": a, "pi": b}, expected_hosts=["alienware", "pi"])
    assert len(report["candidates"]) == 2
    assert report["store_authority"] == "AMBIGUOUS"
    assert report["comparisons"][0]["differing_fields"] == []


def test_same_capture_alias_requires_matching_content(tmp_path):
    path = tmp_path / "state.db"
    ledger(path)
    a = capture(path)
    alias = copy.deepcopy(a["stores"][0])
    alias["path"] = str(tmp_path / "alias.db")
    a["stores"].append(alias)
    report = compare_reports({"omen": a}, expected_hosts=["omen", "pi"])
    assert len(report["candidates"]) == 1
    assert report["store_authority"] == "UNKNOWN"
    assert report["candidates"][0]["aliases"] == [alias["path"]]
    alias["content_sha256"] = "changed-between-path-inspections"
    report = compare_reports({"omen": a}, expected_hosts=[])
    assert report["store_authority"] == "AMBIGUOUS"


def test_hidden_history_change_is_detected_even_with_identical_latest_rows(tmp_path):
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    ledger(first)
    conn = sqlite3.connect(first)
    conn.execute("INSERT INTO runs VALUES ('older', 'FAILED', '2020-01-01', NULL)")
    conn.commit()
    conn.close()
    shutil.copyfile(first, second)
    conn = sqlite3.connect(second)
    conn.execute("UPDATE runs SET status='HALTED' WHERE run_id='older'")
    conn.commit()
    conn.close()
    report = compare_reports({"a": capture(first), "b": capture(second)}, expected_hosts=[])
    assert report["comparisons"][0]["differing_fields"] == ["content_sha256"]
    assert "SQLite layout differences" in render_comparison(report)


def test_latest_rows_with_equal_timestamps_have_deterministic_tie_order(tmp_path):
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    for path, names in ((first, ["b", "a"]), (second, ["a", "b"])):
        ledger(path)
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM runs")
        conn.executemany(
            "INSERT INTO runs VALUES (?, 'HALTED', '2026-09-01', NULL)", [(name,) for name in names]
        )
        conn.commit()
        conn.close()
    assert inspect_store(first)["latest_run"] == inspect_store(second)["latest_run"]


def test_portable_capture_without_site_packages_preserves_db_and_redacts(tmp_path):
    path = tmp_path / "state.db"
    ledger(path)
    before = path.read_bytes(), path.stat().st_mtime_ns, set(tmp_path.iterdir())
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(SCRIPT),
            "capture",
            "--repo",
            str(tmp_path),
            "--scan-root",
            str(tmp_path),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "QUANTBOT_DB_PATH": str(path), "ALPACA_PAPER_API_KEY": "do-not-print"},
        timeout=60,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["stores"][0]["account_snapshot_content"][0]["sha256"]
    assert report["startup_preflight"]["allowed"] is False
    assert "do-not-print" not in result.stdout and "private-account" not in result.stdout
    assert before == (path.read_bytes(), path.stat().st_mtime_ns, set(tmp_path.iterdir()))


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_portable_shared_reader_refuses_sidecars_without_base_hash(tmp_path, suffix):
    path = tmp_path / "state.db"
    ledger(path)
    sidecar = Path(str(path) + suffix)
    sidecar.write_bytes(b"unmerged state")
    before = path.read_bytes(), sidecar.read_bytes()
    report = capture(path)
    assert report["stores"][0]["evidence_state"] == "SIDECARS_REQUIRE_CONSISTENT_SNAPSHOT"
    assert "content_sha256" not in report["stores"][0]
    assert before == (path.read_bytes(), sidecar.read_bytes())
    comparison = compare_reports({"pi": report}, expected_hosts=["pi"])
    assert comparison["store_authority"] == "UNKNOWN"
    assert "SIDECARS_REQUIRE_CONSISTENT_SNAPSHOT" in render_comparison(comparison)


def test_offline_cli_reads_only_reports_not_paths_named_inside_them(tmp_path):
    path = tmp_path / "state.db"
    ledger(path)
    report = capture(path)
    report_file = tmp_path / "capture.json"
    report_file.write_text(json.dumps(report), encoding="utf-8-sig")
    path.unlink()  # The comparison must work without access to the source host/store.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(SCRIPT),
            "compare",
            "--report",
            f"alienware={report_file}",
            "--expect",
            "pi",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1, result.stderr
    compared = json.loads(result.stdout)
    assert compared["candidates"][0]["evidence"]["latest_run"]
    assert compared["missing_host_captures"] == ["pi"]
    assert not path.exists()


def test_unknown_report_version_refuses_interpretation(tmp_path):
    path = tmp_path / "state.db"
    ledger(path)
    report = capture(path)
    report["report_version"] = 999
    with pytest.raises(ValueError, match="UNSUPPORTED_OR_INVALID_REPORT"):
        compare_reports({"alienware": report}, expected_hosts=[])


def test_research_only_store_is_not_silently_excluded_from_comparison(tmp_path):
    path = tmp_path / "stocks.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE closes (symbol TEXT, close TEXT)")
    conn.execute("INSERT INTO closes VALUES ('SPY', '100')")
    conn.commit()
    conn.close()
    report = compare_reports({"alienware": capture(path)}, expected_hosts=["pi"])
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["evidence"]["history_counts"]["closes"] == 1
    assert report["store_authority"] == "UNKNOWN"


def test_linux_startup_scan_reports_wrapper_reference_without_exposing_content(
    tmp_path, monkeypatch
):
    from quantbot.operations import host_inventory as module

    mapped = tmp_path / "etc/systemd/system"
    mapped.mkdir(parents=True)
    unit = mapped / "custom-wrapper.service"
    unit.write_text("ExecStart=/opt/project_elrond/run.py --token=private-value")
    original_path = Path

    def local_path(value):
        candidate = original_path(value)
        if candidate.is_relative_to(tmp_path):
            return candidate
        return original_path(tmp_path / str(value).lstrip("/"))

    local_path.home = lambda: tmp_path / "home"
    monkeypatch.setattr(module, "Path", local_path)
    result = module._linux_startup_files()
    record = next(item for item in result["files"] if item["path"] == str(unit))
    assert record["elrond_reference"] is True
    assert record["sha256"]
    assert "private-value" not in json.dumps(result)
    assert result["unreadable_or_absent"]
    assert unit.read_text().endswith("--token=private-value")
