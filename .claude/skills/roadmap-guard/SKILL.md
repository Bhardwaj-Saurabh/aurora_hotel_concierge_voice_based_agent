---
name: roadmap-guard
description: Keep all work aligned with goal.md. Use at the START of any feature request, task, or "let's add X" to map it to a roadmap phase item and catch scope creep, and at COMPLETION to check the phase's definition of done. The compass for staying on plan.
---

# Roadmap Guard — Stay on the Plan

`goal.md` is the approved roadmap (4 phases, 11 ADRs, per-phase definitions of done). Work that
isn't on it is either scope creep or a plan change — never something to silently absorb.

## At task start

1. Read `goal.md` §4 (roadmap) if not already in context.
2. **Map the request to a phase item** (e.g. "add room service tool" → Phase 1.1). State the
   mapping explicitly: `📍 Roadmap: Phase 1.1 — get_room_service_hours tool`.
3. **Check ordering.** Phases land in order; within a phase, items are independent. If the
   request jumps ahead (e.g. Phase 3 work while Phase 1 items are open), point it out and confirm
   the user wants to jump — don't refuse, just make the skip visible.
4. **If the request maps to NO roadmap item**, stop and say so. Then offer exactly two paths:
   - **Amend the plan**: add it to the right phase (and record an ADR via the `adr` skill if it's
     an architectural decision) — requires user confirmation.
   - **Decline/defer**: note it in goal.md's out-of-scope or a future phase.
   Never quietly build off-plan work; that is how the plan and reality diverge.
5. **If the work changes agent behavior**, the `edd` skill governs the implementation order
   (eval first). Say so up front.

## At task completion

1. Check the item against its phase's **acceptance criteria** (goal.md §4) and the per-phase
   definition of done (§7). List each criterion as met/unmet.
2. Run the `gates` skill.
3. Update goal.md: mark the item done with a date, e.g. `~~1.1 …~~ ✅ 2026-07-20` (keep the text,
   strike it through). If acceptance criteria changed during the work, that's a plan amendment —
   flag it, don't silently edit.

## Scope-creep tripwires (from goal.md §6 risks)

- "While we're at it, let's also…" → separate roadmap item; map it or defer it.
- Phase 3 (worker/streaming/SIP) is the highest creep risk — its items land as **separate
  increments** (3.1 → 3.4), never as one big bang.
- Out-of-scope list (goal.md §1): full PMS integration, payments, outbound campaigns,
  multi-tenant. Requests touching these get flagged immediately.
