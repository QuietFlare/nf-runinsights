import json

import pytest

from nf_runinsights import store


@pytest.fixture
def history(tmp_path):
    """Point the store at a fresh temp directory, restore afterwards."""
    d = tmp_path / "history"
    d.mkdir()
    orig_dir, orig_legacy = store.HISTORY_DIR, store.LEGACY_FILE
    store.set_history(str(d))
    yield d
    store.HISTORY_DIR, store.LEGACY_FILE = orig_dir, orig_legacy


@pytest.fixture
def make_run(history):
    """Write one run file into the store and return the entry dict."""

    def _make(run_name, ts, pipeline="main.nf", processes=None):
        entry = {
            "run_name": run_name,
            "ts": ts,
            "pipeline": pipeline,
            "processes": processes if processes is not None else {},
        }
        (history / f"{ts.replace(':', '')}-{run_name}.json").write_text(
            json.dumps(entry)
        )
        return entry

    return _make


def proc(median_ms, peak_rss=None, queue_ms=None, tasks=1, retried=0):
    return {
        "realtime_ms_median": median_ms,
        "peak_rss_max": peak_rss,
        "queue_ms_median": queue_ms,
        "tasks": tasks,
        "retried": retried,
    }
