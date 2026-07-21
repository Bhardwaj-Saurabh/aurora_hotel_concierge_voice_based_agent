"""Temporary alias to aurora.core.agent (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.core.agent as _real

sys.modules[__name__] = _real
