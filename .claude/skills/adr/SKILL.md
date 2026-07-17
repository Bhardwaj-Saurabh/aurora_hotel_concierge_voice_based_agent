---
name: adr
description: Record a significant technical decision as an ADR in goal.md §5. Use when a design choice is made that future work will build on — new dependency, storage/schema choice, interface boundary, protocol, or any deviation from an existing ADR. The ledger that keeps decisions and plan in sync.
---

# ADR — Architecture Decision Record

Decisions made mid-implementation and never recorded are how `goal.md` and reality drift apart.
When a choice is significant (someone would need to know *why* in six months), record it.

## When an ADR is warranted

- New dependency or external service
- Storage, schema, or interface-boundary choice (e.g. the idempotency key derivation in 2.1)
- Anything that **deviates from or supersedes an existing ADR** — never silently contradict one
- A tradeoff the user explicitly weighed in on

Not warranted: naming, formatting, internal refactors, choices fully dictated by an existing ADR.

## Format (must match goal.md §5 exactly)

Append to goal.md §5 with the next number (ADR-012 onward):

```markdown
### ADR-0NN — <decision title, stated as the choice made>

**Status:** Approved (Phase X.Y) | Accepted (existing)

**Context.** The forces: what problem, what constraints, why now. 2–4 sentences.

**Options.**
1. *Option A* — tradeoffs, honestly stated (including the rejected ones' strengths).
2. *Option B* — …
3. *Option C* — …

**Decision.** Which option and the one-sentence reason it wins here.

**Consequences.** What we now must do/maintain/accept; revisit triggers if any.
```

## Rules

- **Options must be real.** At least two genuine alternatives with honest strengths — an ADR with
  one strawman is a rubber stamp, not a record.
- **Superseding:** if a new decision replaces an old ADR, mark the old one
  `**Status:** Superseded by ADR-0NN` — never edit its Decision retroactively.
- Cross-reference the roadmap item it serves (Phase X.Y) and any related ADRs.
- Get user confirmation on the Decision before writing if the choice wasn't already theirs.
