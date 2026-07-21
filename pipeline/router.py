"""Temporary alias to aurora.core.router (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.core.router as _real

sys.modules[__name__] = _real
