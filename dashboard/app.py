"""Launcher kept so `python3 dashboard/app.py` still works from a checkout.

The dashboard lives in nf_runinsights/dashboard.py and is also on PyPI:
`pipx run nf-runinsights-dashboard`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nf_runinsights.dashboard import main

if __name__ == "__main__":
    main()
