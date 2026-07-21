These files are temporary aliases (goal.md ADR-020, commit 2 of the
refactor/modular-aurora branch only): the real modules moved to src/aurora/.
Each shim replaces itself in sys.modules with the real module, so state
(module-level singletons, test injection hooks) stays single-sourced.
Deleted in commit 3 together with livekit/'s bare imports.
