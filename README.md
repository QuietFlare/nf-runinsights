# nf-runinsights

Cross-run benchmarking for Nextflow. The plugin records per-process resource
metrics for every run into a local history store, compares each new run
against previous runs of the same pipeline, and reports what changed and why:
regressions with likely causes, over-provisioned resources, retry storms, and
pipeline-wide slowdowns.

Nextflow's built-in reports tell you everything about one run. nf-runinsights
tells you how this run compares to every run before it.

## Why not the built-in tools

| Tool | Gives you | Limit |
|---|---|---|
| `nextflow log` | Lists past runs, per-task trace fields | Per launch directory, never compares |
| `-with-report` | Per-process resource charts for one run | One run, read the charts yourself |
| `-with-trace` | Raw per-task CSV | One run, no judgment |
| nf-co2footprint | Energy and CO2 per run | One run, one dimension |
| nf-runinsights | Cross-run comparison, regression flags with causes, right-sizing evidence | Needs runs recorded with the plugin |

Built-in artifacts also disappear with `nextflow clean` or a deleted work
directory. The history store is independent and durable.

## Requirements

- Nextflow 24.10.0 or later
- Java 17+ to build

## Installation

Not yet on the plugin registry. From source:

```bash
git clone <this repo>
cd nf-runinsights
make install
```

## Usage

Enable the plugin in your config:

```groovy
plugins {
    id 'nf-runinsights@0.1.0'
}
```

Run your pipeline as usual. At the end of each run:

```
nf-runinsights: 3 prior run(s) of 'nf-core/sarek' in history
nf-runinsights: CRUNCH: 8.0s vs 2.0s median over 3 prior run(s), 4.0x slower
nf-runinsights: report: /path/to/launchdir/runinsights-report.md
```

The first run records a baseline; comparisons start on the second. The
markdown report contains the findings plus a per-process table of this run
against the historical medians.

The plugin is a pure observer. It never alters pipeline behavior, and any
failure in insight generation is swallowed so it cannot fail a run.

## Configuration

```groovy
runinsights {
    // history store directory (default: ~/.nf-runinsights/history).
    // Point it at a shared directory or s3:// prefix so a whole team
    // contributes to one history. No server required.
    history = '/shared/projects/my-team/runinsights'

    // optional AI narration of the report, off by default
    ai {
        enabled = true
        model = 'claude-opus-5'
    }
}
```

## What is recorded

One JSON file per run, containing per-process aggregates:

| Field | Meaning |
|---|---|
| `realtime_ms_median` / `_max` | Task duration |
| `peak_rss_max` | Peak memory used |
| `cpus_req` / `memory_req` | Resources requested |
| `queue_ms_median` | Time spent waiting in the executor queue |
| `cpu_eff_median` | Fraction of requested CPUs actually used |
| `read_bytes_total` | Input volume |
| `tasks` / `failed` / `retried` | Counts |
| `container` | Container image, for detecting tool changes |

Only process names and statistics are stored. No file paths, sample
identifiers, or pipeline outputs.

## Findings

All findings are computed deterministically from recorded numbers:

- Regression: a process is at least 1.5x slower than its historical median
  and at least 2 seconds slower in absolute terms. Where the evidence
  supports it, a likely cause is attached: queue wait rather than execution,
  a container change, or input growth with flat throughput.
- Improvement: the mirror of the above.
- Environment: when most processes slow down together, one pipeline-wide
  finding points at the machine, cluster load, or storage instead of blaming
  individual tools.
- Over-provisioning: peak memory never exceeded 25% of the request across
  every recorded run of a process.
- Retries and failures.

The thresholds exist to keep sub-second scheduler jitter out of the findings.

## History store

```
~/.nf-runinsights/history/
  20260817T145715-tender_mccarthy.json
  20260817T151009-admiring_kalam.json
```

A run file is created once and never appended, which makes the store safe
for concurrent runs on shared filesystems and compatible with object
storage. Paths resolve through Nextflow's filesystem layer, so
`history = 's3://bucket/prefix'` works with the run's own credentials.
A `history.jsonl` file from earlier plugin versions is still read,
never written.

The plugin decides where runs are written (`runinsights.history` in the
Nextflow config). The dashboard and MCP server decide where they read:
`--history` flag (dashboard only) > `NF_RUNINSIGHTS_HISTORY` env >
`~/.nf-runinsights/history`. For a shared team history, point all three
at the same directory.

## Dashboard

A local web UI over the store. Python standard library only:

```bash
pipx run nf-runinsights-dashboard   # http://localhost:8765
```

```bash
pipx run nf-runinsights-dashboard --history /shared/team/runinsights
```

The page header shows which store it is reading.

From a repo checkout the old door still works, no install at all:

```bash
python3 dashboard/app.py
```

Pick runs, compare them side by side with deltas against a baseline, and
optionally ask free-form questions. Ask needs the `anthropic` package
(`pipx run --spec 'nf-runinsights-dashboard[ask]' nf-runinsights-dashboard`,
or `pip install anthropic` from a checkout) and `ANTHROPIC_API_KEY`;
everything else works without them.

## MCP server

Exposes the history store to any MCP client: Claude Code, Claude Desktop,
Gemini CLI, or an OpenAI agent. The server is read-only and contains no AI;
the assistant that connects to it supplies the model.

```bash
pipx install 'nf-runinsights-dashboard[mcp]'
claude mcp add --scope user nf-runinsights -- ~/.local/bin/nf-runinsights-mcp
```

Use the absolute path: MCP clients spawn servers without your shell PATH.
(From a repo checkout, `pip install mcp` and pointing the client at an
absolute `python3` plus `mcp-server/server.py` still works.)

The server has no `--history` flag; for a non-default store, register it
with the environment variable:

```bash
claude mcp add --scope user nf-runinsights \
    --env NF_RUNINSIGHTS_HISTORY=/shared/team/runinsights \
    -- ~/.local/bin/nf-runinsights-mcp
```

Tools: `list_runs`, `get_run`, `compare_runs`, `get_process_trend`. Then ask
your assistant things like "compare my last two sarek runs" or "why was last
night's run slow".

`nf-runinsights-mcp --selftest` exercises every tool against the real store
without an MCP client.

## AI narration

With `runinsights.ai.enabled = true` and `ANTHROPIC_API_KEY` in the launch
environment, the report gains an "AI analysis" section. The division of
labor is strict: the engine computes every number and finding; the model
only explains and prioritizes them, and is instructed never to invent
values. The call is best-effort: missing credentials or any API failure
leaves the deterministic report untouched.

## Limitations

- The macOS local executor does not collect memory or CPU metrics; use
  Docker or a Linux executor for those columns.
- Tasks under a few seconds have noisy timings. The thresholds filter most
  of it, but treat sub-second deltas as noise.
- Failed or partial runs are recorded and can distort baselines. Check the
  process count before using a run as a comparison baseline.
- Local scripts are identified by filename, so two different pipelines both
  named `main.nf` share a history. Registered pipelines such as
  `nf-core/sarek` are unambiguous.

## Development

```bash
make test        # plugin unit tests
make test-py     # Python reader tests (needs: pip install pytest)
make install     # build and install into ~/.nextflow/plugins
cd test-pipeline && nextflow run main.nf          # record a baseline
cd test-pipeline && nextflow run main.nf --slow   # trigger a regression
```

The Python readers live in `nf_runinsights/` and ship to PyPI as
`nf-runinsights-dashboard` (`python3 -m build`, then twine upload). Keep
its version in `pyproject.toml` in step with the plugin version.

## License

Apache-2.0
