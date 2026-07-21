"""Temporary alias to aurora.core.spoken_text (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.core.spoken_text as _real

sys.modules[__name__] = _real
