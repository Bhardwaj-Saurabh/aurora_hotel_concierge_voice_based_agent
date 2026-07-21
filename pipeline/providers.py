"""Temporary alias to aurora.core.providers (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.core.providers as _real

sys.modules[__name__] = _real
