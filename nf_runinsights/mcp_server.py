"""
nf-runinsights MCP server, lets an AI assistant (Claude Code, Claude
Desktop, Gemini CLI, or any MCP client) query your pipeline run history.

All data logic lives in the shared module store.py (also used by
dashboard.py), this file is only the MCP door.

Usage:
    pipx install 'nf-runinsights-dashboard[mcp]'
    claude mcp add --scope user nf-runinsights -- ~/.local/bin/nf-runinsights-mcp
    (use the absolute path: MCP clients spawn without your shell PATH)

    nf-runinsights-mcp --selftest   # exercise every tool, no MCP client needed
    python3 mcp-server/server.py --selftest    # same, from a repo checkout
"""

from __future__ import annotations

import json
import sys

from nf_runinsights import store


def build_server():
    try:
        from mcp.server import MCPServer          # mcp SDK >= 2.0
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as MCPServer  # mcp SDK 1.x
        except ImportError:
            sys.exit(
                "the MCP server needs the mcp package: pip install mcp, or "
                "pipx install 'nf-runinsights-dashboard[mcp]' "
                "(--selftest works without it)"
            )

    mcp = MCPServer("nf-runinsights")

    @mcp.tool()
    def list_runs(pipeline: str | None = None) -> str:
        """List every recorded pipeline run (name, timestamp, pipeline),
        oldest first. Optionally filter by pipeline name, e.g. 'nf-core/sarek'."""
        return json.dumps(store.runs_summary(pipeline), indent=1)

    @mcp.tool()
    def get_run(run_name: str) -> str:
        """Full per-process metrics for one recorded run: median/max task
        time, peak memory, queue wait, CPU efficiency, retries, container."""
        detail = store.run_detail(run_name)
        return json.dumps(
            detail if detail else {"error": f"run '{run_name}' not found"}, indent=1
        )

    @mcp.tool()
    def compare_runs(run_names: list[str]) -> str:
        """Side-by-side comparison of two or more runs of the same pipeline.
        Returns per-process median times, memory, and queue wait for each run,
        slowest process first. The first named run is the natural baseline."""
        return json.dumps(store.compare(run_names), indent=1)

    @mcp.tool()
    def get_process_trend(process: str, pipeline: str | None = None) -> str:
        """History of one process across all recorded runs, how its median
        task time, memory, and queue wait evolved. Accepts short names
        ('FASTQC') or full names ('NFCORE_SAREK:SAREK:FASTQC')."""
        return json.dumps(store.process_trend(process, pipeline), indent=1)

    return mcp


def selftest() -> None:
    d = store.HISTORY_DIR
    note = f"exists: {d.is_dir()}" if hasattr(d, "is_dir") else "remote"
    print(f"history dir: {d} ({note})")
    runs = store.runs_summary()
    print(f"\nlist_runs → {len(runs)} run(s)")
    for r in runs:
        print(f"  {r['run_name']}  {r['pipeline']}  {r['ts'][:16] if r['ts'] else '?'}")
    if len(runs) >= 2:
        same = [r for r in runs if r["pipeline"] == runs[-1]["pipeline"]][-2:]
        names = [r["run_name"] for r in same]
        print(f"\ncompare_runs({names}) →")
        result = store.compare(names)
        for row in result.get("processes", [])[:5]:
            cells = ["-" if c is None else f"{c['median_ms']}ms" for c in row["runs"]]
            print(f"  {row['process'].rsplit(':', 1)[-1]:30s} {' | '.join(cells)}")
        proc = result["processes"][0]["process"].rsplit(":", 1)[-1]
        trend = store.process_trend(proc)
        print(f"\nget_process_trend('{proc}') → {len(trend.get('points', []))} points, "
              f"overall median {trend.get('overall_median_ms')}ms")
    print("\nselftest OK")


def main() -> None:
    if "--selftest" in sys.argv:
        selftest()
    else:
        build_server().run()


if __name__ == "__main__":
    main()
