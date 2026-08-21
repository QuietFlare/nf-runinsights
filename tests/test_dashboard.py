import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from nf_runinsights.dashboard import Handler

from conftest import proc


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get_json(url):
    status, body = get(url)
    return status, json.loads(body)


def post_json(url, payload):
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_index_page(server, history):
    status, body = get(server + "/")
    assert status == 200
    assert b"nf-runinsights" in body


def test_unknown_path_404(server, history):
    status, body = get_json(server + "/api/nope")
    assert status == 404


def test_api_runs(server, make_run):
    make_run("a", "2026-01-01T10:00:00", processes={"FOO": proc(1000)})
    status, data = get_json(server + "/api/runs")
    assert status == 200
    assert [r["run_name"] for r in data["runs"]] == ["a"]
    assert data["store"]  # the resolved history path is reported


def test_api_compare_needs_two_runs(server, make_run):
    make_run("a", "2026-01-01T10:00:00")
    status, data = get_json(server + "/api/compare?runs=a")
    assert status == 400
    assert "error" in data


def test_api_compare(server, make_run):
    make_run("a", "2026-01-01T10:00:00", processes={"FOO": proc(1000)})
    make_run("b", "2026-01-02T10:00:00", processes={"FOO": proc(3000)})
    status, data = get_json(server + "/api/compare?runs=a,b")
    assert status == 200
    assert data["pipeline"] == "main.nf"
    assert data["processes"][0]["runs"][1]["median_ms"] == 3000


def test_api_ask_rejects_bad_json(server, history):
    status, data = post_json(server + "/api/ask", b"{broken")
    assert status == 400
    assert "error" in data


def test_api_ask_rejects_empty_question(server, history):
    status, data = post_json(server + "/api/ask", b'{"question": "  "}')
    assert status == 400
    assert "error" in data


def test_port_in_use_is_a_sentence_not_a_traceback(history, monkeypatch):
    import socket
    import sys as _sys
    from nf_runinsights.dashboard import main
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    port = blocker.getsockname()[1]
    blocker.listen(1)
    try:
        monkeypatch.setattr(_sys, "argv", ["nf-runinsights-dashboard",
                                           "--port", str(port)])
        with pytest.raises(SystemExit) as exc:
            main()
        assert "already in use" in str(exc.value)
    finally:
        blocker.close()


def test_api_ask_without_credentials_is_5xx_error(server, make_run, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    make_run("a", "2026-01-01T10:00:00")
    status, data = post_json(server + "/api/ask", b'{"question": "why?"}')
    assert status == 503
    assert "error" in data
