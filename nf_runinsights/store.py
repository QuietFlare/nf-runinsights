"""
Shared data layer for nf-runinsights readers.

One brain, two doors: dashboard.py (browser) and mcp_server.py (AI
assistants) both import this module, so load/compare/trend/ask behave
identically everywhere and are maintained in one place.

Store resolution: set_history() (from a --history flag) >
NF_RUNINSIGHTS_HISTORY env > ~/.nf-runinsights/history (the plugin's default).

The store may also be a URL (s3://bucket/prefix, or anything fsspec
understands); that needs the fsspec package, installed by the [s3] extra.
Local paths never touch fsspec, so the base install stays stdlib-only.

Parsed run files are cached by path and stamp (mtime and size locally,
ETag or size remotely). Run files never change once written, so a store
of hundreds of runs costs one listing per call, not one read per file.
"""

from __future__ import annotations

import glob
import json
import os
import re
import threading
from pathlib import Path
from statistics import median


def _resolve(path: str):
    """Local paths become Path; URLs stay strings (Path mangles '//')."""
    return path if "://" in path else Path(path)


HISTORY_DIR = _resolve(
    os.environ.get(
        "NF_RUNINSIGHTS_HISTORY", str(Path.home() / ".nf-runinsights" / "history")
    )
)

ASK_MODEL = os.environ.get("NF_RUNINSIGHTS_ASK_MODEL", "claude-haiku-4-5-20251001")


def _legacy_file(hist):
    """history.jsonl (pre-0.1 plugin) lives next to the history dir."""
    if isinstance(hist, Path):
        return hist.parent / "history.jsonl"
    return hist.rstrip("/").rsplit("/", 1)[0] + "/history.jsonl"


LEGACY_FILE = _legacy_file(HISTORY_DIR)


def set_history(path: str) -> None:
    """Point the store somewhere else (e.g. from a --history flag)."""
    global HISTORY_DIR, LEGACY_FILE
    HISTORY_DIR = _resolve(path)
    LEGACY_FILE = _legacy_file(HISTORY_DIR)
    _cache.clear()


def _url_fs():
    """fsspec filesystem + root path for a URL store."""
    try:
        from fsspec.core import url_to_fs
    except ImportError:
        raise RuntimeError(
            f"reading {HISTORY_DIR} needs the fsspec package: "
            "pip install 'nf-runinsights[s3]' "
            "(local directories work without it)"
        )
    return url_to_fs(str(HISTORY_DIR))


def _parse_legacy(text: str) -> list[dict]:
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


_lock = threading.Lock()
_cache: dict[str, tuple[object, object]] = {}   # key -> (stamp, parsed or None)


def _cached(key, stamp, read):
    """Parse through the cache. A file is read again only when its stamp moves."""
    hit = _cache.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        value = read()
    except (ValueError, OSError):
        value = None    # corrupt or unreadable: skipped, and not retried until it changes
    _cache[key] = (stamp, value)
    return value


def _remote_stamp(info: dict):
    return info.get("ETag") or (
        info.get("size"),
        str(info.get("mtime") or info.get("LastModified") or info.get("created")),
    )


def _legacy():
    """(key, stamp, reader) for history.jsonl, or None when there is none."""
    if isinstance(HISTORY_DIR, Path):
        if not LEGACY_FILE.exists():
            return None
        st = LEGACY_FILE.stat()
        return (str(LEGACY_FILE), (st.st_mtime_ns, st.st_size),
                lambda: _parse_legacy(LEGACY_FILE.read_text()))
    fs, _ = _url_fs()
    legacy = LEGACY_FILE.split("://", 1)[-1]
    if not fs.exists(legacy):
        return None
    return (legacy, _remote_stamp(fs.info(legacy)),
            lambda: _parse_legacy(fs.cat_file(legacy).decode()))


def _run_files(pattern: str = "*.json"):
    """(key, stamp, reader) for each run file matching pattern, in name order."""
    if isinstance(HISTORY_DIR, Path):
        if not HISTORY_DIR.is_dir():
            return
        for f in sorted(HISTORY_DIR.glob(pattern)):
            try:
                st = f.stat()
            except OSError:
                continue
            yield str(f), (st.st_mtime_ns, st.st_size), (lambda f=f: json.loads(f.read_text()))
        return
    fs, root = _url_fs()
    fs.invalidate_cache()   # a long-running dashboard must see runs finished since its last call
    found = fs.glob(root.rstrip("/") + "/" + pattern, detail=True)
    for name in sorted(found):
        if found[name].get("type") == "directory":
            continue
        yield name, _remote_stamp(found[name]), (lambda n=name: json.loads(fs.cat_file(n)))


def load_history() -> list[dict]:
    """All recorded runs, oldest first. Corrupt entries are skipped."""
    entries: list[dict] = []
    with _lock:
        seen = set()
        legacy = _legacy()
        if legacy:
            seen.add(legacy[0])
            entries.extend(_cached(*legacy) or [])
        for key, stamp, read in _run_files():
            seen.add(key)
            e = _cached(key, stamp, read)
            if isinstance(e, dict):
                entries.append(e)
        for key in [k for k in _cache if k not in seen]:
            del _cache[key]
    entries.sort(key=lambda e: e.get("ts") or "")
    return entries


def migrate_legacy() -> int:
    """Split history.jsonl into per-run files and rename it. Local stores only."""
    if not isinstance(HISTORY_DIR, Path):
        raise RuntimeError(
            f"{HISTORY_DIR} is remote: migrate the local copy, then sync it"
        )
    if not LEGACY_FILE.exists():
        raise FileNotFoundError(f"no {LEGACY_FILE} to migrate")
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for e in _parse_legacy(LEGACY_FILE.read_text()):
        # same file name the plugin writes, so old and new runs sort together
        stamp = re.sub(r"[^0-9T]", "", str(e.get("ts") or ""))[:15]
        target = HISTORY_DIR / f"{stamp}-{e.get('run_name') or 'run'}.json"
        if target.exists():
            continue
        target.write_text(json.dumps(e))
        written += 1
    LEGACY_FILE.rename(LEGACY_FILE.with_name("history.jsonl.migrated"))
    return written


def is_failed(e: dict) -> bool:
    """Runs recorded before plugin 0.2 carry no status and count as completed."""
    return e.get("status") == "failed"


def runs_summary(pipeline: str | None = None) -> list[dict]:
    return [
        {
            "run_name": e.get("run_name"),
            "ts": e.get("ts"),
            "pipeline": e.get("pipeline"),
            "status": e.get("status") or "completed",
            "process_count": len(e.get("processes") or {}),
        }
        for e in load_history()
        if pipeline is None or e.get("pipeline") == pipeline
    ]


def run_detail(run_name: str) -> dict | None:
    """One run by name. Run files carry the name, so those are tried first;
    legacy runs have no file of their own and fall back to a full load."""
    with _lock:
        for key, stamp, read in _run_files(f"*-{glob.escape(run_name)}.json"):
            e = _cached(key, stamp, read)
            if isinstance(e, dict) and e.get("run_name") == run_name:
                return e
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
        "runs": [
            {"run_name": e.get("run_name"), "ts": e.get("ts"),
             "status": e.get("status") or "completed"}
            for e in selected
        ],
        "pipeline": selected[0].get("pipeline"),
        "processes": rows,
    }


def process_trend(
    process: str, pipeline: str | None = None, include_failed: bool = False
) -> dict:
    """One process across runs. Failed runs are left out unless asked for."""
    points = []
    skipped = 0
    for e in load_history():
        if pipeline is not None and e.get("pipeline") != pipeline:
            continue
        for name, rec in (e.get("processes") or {}).items():
            # match short names too, users say "FASTQC", history says
            # "NFCORE_SAREK:SAREK:FASTQC"
            if name == process or name.rsplit(":", 1)[-1] == process:
                if is_failed(e) and not include_failed:
                    skipped += 1
                    continue
                points.append(
                    {
                        "run_name": e.get("run_name"),
                        "ts": e.get("ts"),
                        "pipeline": e.get("pipeline"),
                        "status": e.get("status") or "completed",
                        "median_ms": rec.get("realtime_ms_median"),
                        "peak_rss": rec.get("peak_rss_max"),
                        "queue_ms": rec.get("queue_ms_median"),
                        "retried": rec.get("retried"),
                    }
                )
    if not points:
        hint = f", only in {skipped} failed run(s)" if skipped else ""
        return {"error": f"no history for process '{process}'{hint}"}
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
            "or reinstall as 'nf-runinsights[ask]' "
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
