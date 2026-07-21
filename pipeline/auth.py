"""Temporary alias to aurora.storage.auth (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.storage.auth as _real

sys.modules[__name__] = _real
