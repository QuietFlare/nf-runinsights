"""URL-store behavior through fsspec's memory:// backend: everything here
also holds for s3:// (which test_store_s3.py checks against a fake server)."""

import json
import sys

import pytest

fsspec = pytest.importorskip("fsspec")

from nf_runinsights import store

from conftest import proc


@pytest.fixture
def memfs():
    fs = fsspec.filesystem("memory")
    fs.store.clear()
    orig_dir, orig_legacy = store.HISTORY_DIR, store.LEGACY_FILE
    store.set_history("memory://history")
    yield fs
    store.HISTORY_DIR, store.LEGACY_FILE = orig_dir, orig_legacy
    fs.store.clear()


def put(fs, name, entry):
    fs.pipe(f"/history/{name}", json.dumps(entry).encode())


def entry(run_name, ts, processes=None):
    return {"run_name": run_name, "ts": ts, "pipeline": "main.nf",
            "processes": processes or {}}


def test_url_store_stays_string():
    orig_dir, orig_legacy = store.HISTORY_DIR, store.LEGACY_FILE
    try:
        store.set_history("s3://bucket/prefix")
        assert store.HISTORY_DIR == "s3://bucket/prefix"   # no Path-mangling
        assert store.LEGACY_FILE == "s3://bucket/history.jsonl"
    finally:
        store.HISTORY_DIR, store.LEGACY_FILE = orig_dir, orig_legacy


def test_remote_runs_sorted_and_summarized(memfs):
    put(memfs, "b.json", entry("newer", "2026-01-02T10:00:00",
                               {"FOO": proc(3000)}))
    put(memfs, "a.json", entry("older", "2026-01-01T10:00:00",
                               {"FOO": proc(1000)}))
    assert [r["run_name"] for r in store.runs_summary()] == ["older", "newer"]
    result = store.compare(["older", "newer"])
    assert result["processes"][0]["runs"][1]["median_ms"] == 3000


def test_remote_corrupt_files_skipped(memfs):
    put(memfs, "good.json", entry("good", "2026-01-01T10:00:00"))
    memfs.pipe("/history/bad.json", b"{not json")
    assert [e["run_name"] for e in store.load_history()] == ["good"]


def test_remote_legacy_jsonl(memfs):
    memfs.pipe("/history.jsonl",
               (json.dumps(entry("legacy", "2026-01-01T09:00:00")) + "\n").encode())
    put(memfs, "a.json", entry("newer", "2026-01-02T10:00:00"))
    assert [e["run_name"] for e in store.load_history()] == ["legacy", "newer"]


def test_remote_empty_prefix_is_empty_not_error(memfs):
    store.set_history("memory://nothing-here")
    assert store.load_history() == []


def test_missing_fsspec_explains_the_extra(memfs, monkeypatch):
    monkeypatch.setitem(sys.modules, "fsspec.core", None)
    with pytest.raises(RuntimeError, match=r"\[s3\]"):
        store.load_history()
