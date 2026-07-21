"""Temporary alias to aurora.telemetry.otel (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.telemetry.otel as _real

sys.modules[__name__] = _real
