"""Temporary alias to aurora.rate_limit (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.rate_limit as _real

sys.modules[__name__] = _real
