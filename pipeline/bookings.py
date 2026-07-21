"""Temporary alias to aurora.storage.bookings (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.storage.bookings as _real

sys.modules[__name__] = _real
