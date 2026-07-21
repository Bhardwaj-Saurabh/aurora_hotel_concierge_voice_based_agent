"""Temporary alias to aurora.config.check (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.config.check as _real

sys.modules[__name__] = _real
