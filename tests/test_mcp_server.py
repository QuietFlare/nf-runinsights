import pytest

from nf_runinsights import mcp_server

from conftest import proc


def test_selftest_covers_all_tools(make_run, capsys):
    make_run("a", "2026-01-01T10:00:00", processes={"FOO": proc(1000)})
    make_run("b", "2026-01-02T10:00:00", processes={"FOO": proc(3000)})
    mcp_server.selftest()
    out = capsys.readouterr().out
    assert "2 run(s)" in out
    assert "selftest OK" in out


def test_selftest_on_empty_store(history, capsys):
    mcp_server.selftest()
    assert "0 run(s)" in capsys.readouterr().out


def test_build_server_registers_tools(history):
    pytest.importorskip("mcp")
    server = mcp_server.build_server()
    assert server is not None
