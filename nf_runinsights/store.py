"""
Shared data layer for nf-runinsights readers.

One brain, two doors: dashboard.py (browser) and mcp_server.py (AI
assistants) both import this module, so load/compare/trend/ask behave
identically everywhere and are maintained in one place.

Store resolution: set_history() (from a --history flag) >
NF_RUNINSIGHTS_HISTORY env > ~/.nf-runinsights/history (the plugin's default).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from statistics import median

HISTORY_DIR = Path(
    os.environ.get(
        "NF_RUNINSIGHTS_HISTORY", str(Path.home() / ".nf-runinsights" / "history")
    )
)
LEGACY_FILE = HISTORY_DIR.parent / "history.jsonl"

ASK_MODEL = os.environ.get("NF_RUNINSIGHTS_ASK_MODEL", "claude-haiku-4-5-20251001")


def set_history(path: str) -> None:
    """Point the store somewhere else (e.g. from a --history flag)."""
    global HISTORY_DIR, LEGACY_FILE
    HISTORY_DIR = Path(path)
    LEGACY_FILE = HISTORY_DIR.parent / "history.jsonl"


def load_history() -> list[dict]:
    """All recorded runs, oldest first. Corrupt entries are skipped."""
    entries = []
    if LEGACY_FILE.exists():
        for line in LEGACY_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if HISTORY_DIR.is_dir():
        for f in sorted(HISTORY_DIR.glob("*.json")):
            try:
                entries.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
    entries.sort(key=lambda e: e.get("ts") or "")
    return entries


def runs_summary(pipeline: str | None = None) -> list[dict]:
    return [
        {
            "run_name": e.get("run_name"),
            "ts": e.get("ts"),
            "pipeline": e.get("pipeline"),
            "process_count": len(e.get("processes") or {}),
        }
        for e in load_history()
        if pipeline is None or e.get("pipeline") == pipeline
    ]


def run_detail(run_name: str) -> dict | None:
    for e in load_history():
        if e.get("run_name") == run_name:
            return e
    return None


def compare(run_names: list[str]) -> dict:
    by_name = {e.get("run_name"): e for e in load_history()}
    missing = [n for n in run_names if n not in by_name]
    if missing:
        return {"error": f"run(s) not in history: {', '.join(missing)}"}
    selected = [by_name[n] for n in run_names]
    if len({e.get("pipeline") for e in selected}) > 1:
        return {"error": "compare runs of the same pipeline"}

    names: list[str] = []
    for e in selected:
        for p in e.get("processes") or {}:
            if p not in names:
                names.append(p)

    rows = []
    for proc in names:
        cells = []
        for e in selected:
            rec = (e.get("processes") or {}).get(proc)
            cells.append(
                None
                if rec is None
                else {
                    "median_ms": rec.get("realtime_ms_median"),
                    "peak_rss": rec.get("peak_rss_max"),
                    "queue_ms": rec.get("queue_ms_median"),
                    "tasks": rec.get("tasks"),
                }
            )
        rows.append({"process": proc, "runs": cells})
    rows.sort(key=lambda r: (r["runs"][0] or {}).get("median_ms") or -1, reverse=True)
    return {
        "runs": [{"run_name": e.get("run_name"), "ts": e.get("ts")} for e in selected],
        "pipeline": selected[0].get("pipeline"),
        "processes": rows,
    }


def process_trend(process: str, pipeline: str | None = None) -> dict:
    points = []
    for e in load_history():
        if pipeline is not None and e.get("pipeline") != pipeline:
            continue
        for name, rec in (e.get("processes") or {}).items():
            # match short names too, users say "FASTQC", history says
            # "NFCORE_SAREK:SAREK:FASTQC"
            if name == process or name.rsplit(":", 1)[-1] == process:
                points.append(
                    {
                        "run_name": e.get("run_name"),
                        "ts": e.get("ts"),
                        "pipeline": e.get("pipeline"),
                        "median_ms": rec.get("realtime_ms_median"),
                        "peak_rss": rec.get("peak_rss_max"),
                        "queue_ms": rec.get("queue_ms_median"),
                        "retried": rec.get("retried"),
                    }
                )
    if not points:
        return {"error": f"no history for process '{process}'"}
    med = [p["median_ms"] for p in points if p["median_ms"] is not None]
    return {
        "process": process,
        "points": points,
        "overall_median_ms": median(med) if med else None,
    }


def ask(question: str, pipeline: str | None = None, run_names: list[str] | None = None) -> dict:
    """AI answer over the history. Optional: needs the anthropic package and
    ANTHROPIC_API_KEY; returns a clear error dict (never raises) otherwise."""
    try:
        import anthropic
    except ImportError:
        return {
            "error": "Ask needs the anthropic package: pip install anthropic, "
            "or reinstall as 'nf-runinsights-dashboard[ask]' "
            "(everything else works without it)"
        }
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "Set ANTHROPIC_API_KEY in the environment to use Ask."}

    entries = load_history()
    if pipeline:
        entries = [e for e in entries if e.get("pipeline") == pipeline]
    if run_names:
        entries = [e for e in entries if e.get("run_name") in run_names]
    entries = entries[-10:]
    if not entries:
        return {"error": "no matching runs in history"}

    context = json.dumps(entries, separators=(",", ":"))[:40_000]
    prompt = (
        "You are answering a question about Nextflow pipeline run performance, "
        "using history recorded by the nf-runinsights plugin. Per-process "
        "metrics: realtime_ms_median/max (task duration), peak_rss_max (bytes), "
        "queue_ms_median (queue wait), cpu_eff_median (fraction of requested "
        "CPUs used), read_bytes_total, retried, failed, container, requested "
        "resources.\n\n"
        "Rules: use ONLY numbers present in the data, never invent values. "
        "Never declare one run better or worse overall unless task durations "
        "support it, richer recorded metadata is NOT better performance; if "
        "some runs lack fields newer runs have, say so plainly. Low "
        "cpu_eff_median means the process used less CPU than requested (an "
        "over-provisioning signal), not parallelism. Format times and bytes "
        "readably. Under 200 words.\n\n"
        f"RUN HISTORY (oldest first):\n{context}\n\nQUESTION: {question}"
    )
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=ASK_MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return {"answer": msg.content[0].text}
    except Exception as e:
        return {"error": f"ask failed: {e}"}
