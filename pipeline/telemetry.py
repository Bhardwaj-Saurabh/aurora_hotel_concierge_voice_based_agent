"""Temporary alias to aurora.telemetry.traces (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.telemetry.traces as _real

sys.modules[__name__] = _real
