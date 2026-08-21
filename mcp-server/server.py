"""Launcher kept so `python3 mcp-server/server.py` still works from a
checkout (and for MCP clients already configured with this path).

The server lives in nf_runinsights/mcp_server.py and is also on PyPI:
`pipx install 'nf-runinsights-dashboard[mcp]'` gives `nf-runinsights-mcp`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nf_runinsights.mcp_server import main

if __name__ == "__main__":
    main()
