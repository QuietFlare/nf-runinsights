"""Integration: real s3:// URLs against a fake S3 (moto server).
Needs the [s3] extra plus moto[server]; skipped when either is absent.
The final proof against real AWS stays a manual step: upload the store
with `aws s3 cp` and run the dashboard with --history s3://..."""

import json

import pytest

fsspec = pytest.importorskip("fsspec")
s3fs = pytest.importorskip("s3fs")
moto_server = pytest.importorskip("moto.server")

from nf_runinsights import store

from conftest import proc

BUCKET = "nf-runinsights-test"


@pytest.fixture(scope="module")
def s3_endpoint():
    server = moto_server.ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def s3_history(s3_endpoint, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # route the store's own url_to_fs() call at the fake server
    monkeypatch.setitem(
        fsspec.config.conf, "s3", {"client_kwargs": {"endpoint_url": s3_endpoint}}
    )
    s3fs.S3FileSystem.clear_instance_cache()
    fs = fsspec.filesystem("s3")
    fs.mkdir(BUCKET)
    orig_dir, orig_legacy = store.HISTORY_DIR, store.LEGACY_FILE
    store.set_history(f"s3://{BUCKET}/history")
    yield fs
    store.HISTORY_DIR, store.LEGACY_FILE = orig_dir, orig_legacy
    s3fs.S3FileSystem.clear_instance_cache()


def entry(run_name, ts, processes=None):
    return {"run_name": run_name, "ts": ts, "pipeline": "main.nf",
            "processes": processes or {}}


def test_s3_store_end_to_end(s3_history):
    fs = s3_history
    fs.pipe(f"{BUCKET}/history/a.json",
            json.dumps(entry("older", "2026-01-01T10:00:00",
                             {"FOO": proc(1000)})).encode())
    fs.pipe(f"{BUCKET}/history/b.json",
            json.dumps(entry("newer", "2026-01-02T10:00:00",
                             {"FOO": proc(3000)})).encode())
    fs.pipe(f"{BUCKET}/history/junk.json", b"{broken")

    assert [r["run_name"] for r in store.runs_summary()] == ["older", "newer"]
    result = store.compare(["older", "newer"])
    assert result["pipeline"] == "main.nf"
    assert result["processes"][0]["runs"][1]["median_ms"] == 3000
    trend = store.process_trend("FOO")
    assert trend["overall_median_ms"] == 2000
