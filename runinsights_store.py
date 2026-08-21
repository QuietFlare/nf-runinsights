"""Compatibility shim: the store moved to nf_runinsights/store.py.

`import runinsights_store` hands back the real module, so set_history()
and HISTORY_DIR keep behaving as one shared state.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nf_runinsights import store as _store

sys.modules[__name__] = _store
