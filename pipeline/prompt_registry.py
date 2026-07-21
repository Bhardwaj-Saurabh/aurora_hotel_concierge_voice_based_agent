"""Temporary alias to aurora.prompt_registry (goal.md ADR-020) - see _shim_note.md."""
import sys

import aurora.prompt_registry as _real

sys.modules[__name__] = _real
