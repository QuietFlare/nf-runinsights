# nf-runinsights-dashboard

Renamed. The dashboard and MCP server now ship as
[nf-runinsights](https://pypi.org/project/nf-runinsights/), with the same
modules and commands. This package depends on it and adds nothing. It stops
receiving releases after 0.4.

```bash
pipx uninstall nf-runinsights-dashboard
pipx install nf-runinsights
```
