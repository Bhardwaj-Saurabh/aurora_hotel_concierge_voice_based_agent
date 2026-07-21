"""Aurora hotel voice agent (goal.md ADR-020).

Keep this module import-light: subpackages (core, server, worker, ...) have
optional heavy dependencies and are imported explicitly by their consumers.
"""

__version__ = "1.0.0"
