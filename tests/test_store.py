import json

import pytest

from nf_runinsights import store

from conftest import proc


# --- load_history -----------------------------------------------------------

def test_missing_dir_yields_empty(tmp_path):
    orig_dir, orig_legacy = store.HISTORY_DIR, store.LEGACY_FILE
    try:
        store.set_history(str(tmp_path / "nope"))
        assert store.load_history() == []
    finally:
        store.HISTORY_DIR, store.LEGACY_FILE = orig_dir, orig_legacy


def test_corrupt_files_are_skipped(history, make_run):
    make_run("good", "2026-01-02T10:00:00")
    (history / "bad.json").write_text("{not json")
    entries = store.load_history()
    assert [e["run_name"] for e in entries] == ["good"]


def test_sorted_by_timestamp_not_filename(history, make_run):
    make_run("newer", "2026-01-02T10:00:00")
    make_run("older", "2026-01-01T10:00:00")
    assert [e["run_name"] for e in store.load_history()] == ["older", "newer"]


def test_legacy_jsonl_still_read(history, make_run):
    make_run("from_dir", "2026-01-02T10:00:00")
    legacy = history.parent / "history.jsonl"
    legacy.write_text(
        json.dumps({"run_name": "from_legacy", "ts": "2026-01-01T09:00:00",
                    "pipeline": "main.nf", "processes": {}})
        + "\n\nnot json\n"
    )
    store.set_history(str(history))  # re-resolve LEGACY_FILE next to the dir
    assert [e["run_name"] for e in store.load_history()] == [
        "from_legacy", "from_dir"
    ]


def test_migrate_legacy_writes_run_files_and_renames(history):
    legacy = history.parent / "history.jsonl"
    legacy.write_text(
        json.dumps({"run_name": "a", "ts": "2026-01-01T09:00:00+02:00",
                    "pipeline": "main.nf", "processes": {}}) + "\n"
        + "not json\n"
        + json.dumps({"run_name": "b", "ts": "2026-01-02T10:00:00+02:00",
                      "pipeline": "main.nf", "processes": {}}) + "\n"
    )
    store.set_history(str(history))
    assert store.migrate_legacy() == 2
    assert not legacy.exists()
    assert (history.parent / "history.jsonl.migrated").exists()
    assert sorted(f.name for f in history.glob("*.json")) == [
        "20260101T090000-a.json", "20260102T100000-b.json"
    ]
    assert [e["run_name"] for e in store.load_history()] == ["a", "b"]
    with pytest.raises(FileNotFoundError):
        store.migrate_legacy()


# --- cache ------------------------------------------------------------------

def count_parses(monkeypatch):
    """Count json.loads calls, one per file read from disk."""
    calls = []
    real = store.json.loads
    monkeypatch.setattr(store.json, "loads",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    return calls


def test_second_load_does_not_reparse_unchanged_files(make_run, monkeypatch):
    make_run("a", "2026-01-01T10:00:00")
    make_run("b", "2026-01-02T10:00:00")
    parses = count_parses(monkeypatch)
    store.load_history()
    store.load_history()
    assert len(parses) == 2
    make_run("c", "2026-01-03T10:00:00")
    assert [e["run_name"] for e in store.load_history()] == ["a", "b", "c"]
    assert len(parses) == 3


def test_changed_file_is_read_again(history, make_run, monkeypatch):
    import os
    make_run("a", "2026-01-01T10:00:00")
    store.load_history()
    parses = count_parses(monkeypatch)
    f = next(history.glob("*.json"))
    f.write_text(json.dumps({"run_name": "a", "ts": "2026-01-01T10:00:00",
                             "pipeline": "changed", "processes": {}}))
    later = f.stat().st_mtime_ns + 2_000_000_000
    os.utime(f, ns=(later, later))
    assert store.load_history()[0]["pipeline"] == "changed"
    assert len(parses) == 1


def test_deleted_file_drops_out(history, make_run):
    make_run("a", "2026-01-01T10:00:00")
    make_run("b", "2026-01-02T10:00:00")
    store.load_history()
    next(history.glob("*-a.json")).unlink()
    assert [e["run_name"] for e in store.load_history()] == ["b"]


def test_run_detail_reads_only_its_own_file(make_run, monkeypatch):
    for i in range(5):
        make_run(f"r{i}", f"2026-01-0{i + 1}T10:00:00")
    parses = count_parses(monkeypatch)
    assert store.run_detail("r3")["run_name"] == "r3"
    assert len(parses) == 1


# --- runs_summary / run_detail ---------------------------------------------

def test_runs_summary_counts_and_filters(make_run):
    make_run("a", "2026-01-01T10:00:00", pipeline="p1",
             processes={"FOO": proc(1000)})
    make_run("b", "2026-01-02T10:00:00", pipeline="p2")
    all_runs = store.runs_summary()
    assert [(r["run_name"], r["process_count"]) for r in all_runs] == [
        ("a", 1), ("b", 0)
    ]
    assert [r["run_name"] for r in store.runs_summary("p2")] == ["b"]


def test_run_detail(make_run):
    make_run("a", "2026-01-01T10:00:00")
    assert store.run_detail("a")["run_name"] == "a"
    assert store.run_detail("ghost") is None


# --- compare ----------------------------------------------------------------

def test_compare_unknown_run_is_error(make_run):
    make_run("a", "2026-01-01T10:00:00")
    result = store.compare(["a", "ghost"])
    assert "ghost" in result["error"]


def test_compare_mixed_pipelines_is_error(make_run):
    make_run("a", "2026-01-01T10:00:00", pipeline="p1")
    make_run("b", "2026-01-02T10:00:00", pipeline="p2")
    assert "same pipeline" in store.compare(["a", "b"])["error"]


def test_compare_rows_slowest_first_with_gaps(make_run):
    make_run("base", "2026-01-01T10:00:00",
             processes={"FAST": proc(1000), "SLOW": proc(9000)})
    make_run("next", "2026-01-02T10:00:00",
             processes={"SLOW": proc(4000), "ONLY_NEW": proc(2000)})
    result = store.compare(["base", "next"])
    assert result["pipeline"] == "main.nf"
    rows = {r["process"]: r["runs"] for r in result["processes"]}
    # ordered by baseline median, missing-in-baseline last
    assert [r["process"] for r in result["processes"]] == [
        "SLOW", "FAST", "ONLY_NEW"
    ]
    assert rows["SLOW"][0]["median_ms"] == 9000
    assert rows["SLOW"][1]["median_ms"] == 4000
    assert rows["FAST"][1] is None          # absent from second run
    assert rows["ONLY_NEW"][0] is None      # absent from baseline


def test_compare_respects_requested_order(make_run):
    make_run("a", "2026-01-01T10:00:00", processes={"P": proc(1000)})
    make_run("b", "2026-01-02T10:00:00", processes={"P": proc(2000)})
    result = store.compare(["b", "a"])   # newer first, as baseline
    assert [r["run_name"] for r in result["runs"]] == ["b", "a"]


# --- process_trend ----------------------------------------------------------

def test_trend_matches_short_and_full_names(make_run):
    make_run("a", "2026-01-01T10:00:00",
             processes={"NFCORE_SAREK:SAREK:FASTQC": proc(1000)})
    make_run("b", "2026-01-02T10:00:00",
             processes={"NFCORE_SAREK:SAREK:FASTQC": proc(3000)})
    short = store.process_trend("FASTQC")
    full = store.process_trend("NFCORE_SAREK:SAREK:FASTQC")
    assert len(short["points"]) == len(full["points"]) == 2
    assert short["overall_median_ms"] == 2000


def test_trend_pipeline_filter(make_run):
    make_run("a", "2026-01-01T10:00:00", pipeline="p1",
             processes={"FOO": proc(1000)})
    make_run("b", "2026-01-02T10:00:00", pipeline="p2",
             processes={"FOO": proc(5000)})
    assert len(store.process_trend("FOO", pipeline="p1")["points"]) == 1


def test_trend_unknown_process_is_error(make_run):
    make_run("a", "2026-01-01T10:00:00")
    assert "error" in store.process_trend("GHOST")


def test_trend_leaves_failed_runs_out_unless_asked(make_run):
    make_run("a", "2026-01-01T10:00:00", processes={"FOO": proc(1000)})
    make_run("b", "2026-01-02T10:00:00", processes={"FOO": proc(9000)},
             status="failed")
    make_run("c", "2026-01-03T10:00:00", processes={"FOO": proc(3000)},
             status="completed")
    assert store.process_trend("FOO")["overall_median_ms"] == 2000
    assert store.process_trend("FOO", include_failed=True)["overall_median_ms"] == 3000


def test_trend_only_in_failed_runs_says_so(make_run):
    make_run("b", "2026-01-02T10:00:00", processes={"FOO": proc(9000)},
             status="failed")
    assert "failed" in store.process_trend("FOO")["error"]


def test_runs_summary_reports_status(make_run):
    make_run("a", "2026-01-01T10:00:00")
    make_run("b", "2026-01-02T10:00:00", status="failed")
    assert [r["status"] for r in store.runs_summary()] == ["completed", "failed"]


# --- ask --------------------------------------------------------------------

def test_ask_never_raises_without_credentials(make_run, monkeypatch):
    """Whether or not the anthropic package is installed, ask() must return
    an error dict (not raise) when no API key is configured."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    make_run("a", "2026-01-01T10:00:00")
    result = store.ask("why slow?")
    assert "error" in result and "answer" not in result
