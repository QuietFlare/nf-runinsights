# Releasing

Two independent channels, both tag-driven. A tag prefix picks the channel;
nothing releases without a tag (or a manual workflow_dispatch run).

| Channel | Tag | Version lives in | Workflow |
|---|---|---|---|
| PyPI (`nf-runinsights`, plus the `nf-runinsights-dashboard` shim) | `pypi-v<version>` | `pyproject.toml`, `packaging/nf-runinsights-dashboard/pyproject.toml` | `.github/workflows/release-pypi.yml` |
| Nextflow plugin registry (`nf-runinsights`) | `plugin-v<version>` | `build.gradle` | `.github/workflows/release-plugin.yml` |

Both workflows refuse to release if the tag does not match the version file.

## PyPI: dashboard + MCP server

1. Bump `version` in `pyproject.toml`, `nf_runinsights/__init__.py`, and
   the shim's `packaging/nf-runinsights-dashboard/pyproject.toml` (both its
   own version and the pinned `nf-runinsights==` dependency).
2. Commit, push `main`, wait for the `ci` workflow to go green.
3. Tag and push:

   ```bash
   git tag pypi-v0.3.0 && git push origin pypi-v0.3.0
   ```

4. The workflow builds both packages, smoke-tests the wheels
   (`nf-runinsights-mcp --selftest`), and publishes via PyPI **trusted
   publishing**, no token or secret involved.
5. Verify: https://pypi.org/project/nf-runinsights/ shows the new version,
   and `pipx run --no-cache nf-runinsights` works.

Notes:
- `pipx run` caches for ~14 days; users on the old version can
  `pipx upgrade nf-runinsights` (installs) or pass `--no-cache`.
- One-time setup: a trusted publisher on pypi.org for repository
  `QuietFlare/nf-runinsights`, workflow `release-pypi.yml`, environment
  `pypi`. Done for `nf-runinsights-dashboard`; NOT yet done for
  `nf-runinsights`, which needs a *pending* publisher with the same
  fields before the first 0.3.0 tag. Every field must match exactly or
  the token exchange fails with `invalid-publisher`.
- The shim stays until 0.4, then its pyproject and the second `build`
  line in the workflow go.
- Manual fallback: `python3 -m build` then `twine upload dist/*` with a
  PyPI API token (username `__token__`).

## Nextflow plugin registry

1. Bump `version` in `build.gradle`.
2. Commit, push `main`, wait for `ci`.
3. Tag and push:

   ```bash
   git tag plugin-v0.2.0 && git push origin plugin-v0.2.0
   ```

4. The workflow runs the Gradle unit tests, then
   `./gradlew releasePluginToRegistryIfNotExists` (idempotent: re-runs
   skip an already-published version).
5. Verify: the version appears on https://registry.nextflow.io and
   `plugins { id 'nf-runinsights@<version>' }` resolves in a pipeline.

Notes:
- One-time setup (NOT done yet): create an API key at
  registry.nextflow.io and store it as the `NPR_API_KEY` repository
  secret (Settings > Secrets and variables > Actions). Without it the
  workflow fails at the release step.
- Manual fallback: `NPR_API_KEY=... ./gradlew releasePluginToRegistry`.

## Versioning

The two channels version independently: a Python-only change (dashboard,
MCP server, store) bumps `pyproject.toml` and gets a `pypi-v*` tag; a
plugin change bumps `build.gradle` and gets a `plugin-v*` tag. A change
touching both gets both tags, which may carry different version numbers.
The history-store JSON format is the contract between them; a change to
it must keep old files readable (see the legacy `history.jsonl` handling
in `nf_runinsights/store.py` for the precedent).
