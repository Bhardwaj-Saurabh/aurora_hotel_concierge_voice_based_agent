"""Temporary alias to aurora.core.knowledge (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.core.knowledge as _real

sys.modules[__name__] = _real
