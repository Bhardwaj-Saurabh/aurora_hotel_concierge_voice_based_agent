# PII Retention Policy — Draft (goal.md Phase 4.4)

> **Status: DRAFT, not approved.** This is a starting point written from the code's actual
> current behavior, with sensible proposed defaults and clear reasoning. The retention periods
> below are **not** a legal or compliance determination — they need real legal/privacy review
> before this document governs a system handling real guests. Until this document is promoted
> from Draft to Approved, `TELEMETRY_INCLUDE_CONTENT=true` must never be set outside local,
> non-production debugging with non-sensitive data (see §5, the explicit gate).

---

## 1. What personal data this system touches

| Data | Where | Sensitivity |
|---|---|---|
| Guest name, contact (phone/email) | Booking records (`aurora/storage/bookings.py`) | Direct PII |
| Check-in/out dates, guest count, room type | Booking records | Low sensitivity alone; identifying combined with the above |
| Caller transcript / agent reply text | `TurnTrace` events, **omitted by default** | Potentially sensitive (could contain anything a caller says) |
| Session/turn/trace IDs | Every trace | Not PII themselves, but correlate turns to one call |
| Confirmation ID | Booking records | Not PII; non-guessable by design (ADR-014) |

## 2. What's already technically enforced (in code today)

- **Redaction by default** (`aurora/telemetry/traces.py`, `_SENSITIVE_KEYS`): `guest_name`, `contact`,
  `phone`, `phone_number`, `email` are replaced with `[REDACTED]` in every trace, unconditionally.
- **Content omission by default** (`_CONTENT_KEYS`): `transcript`, `query`, `result`, `text`,
  `message` fields are replaced with `[OMITTED:<length>]` unless `TELEMETRY_INCLUDE_CONTENT=true`
  is explicitly set.
- **Booking PII lives only in the booking store** (SQLite file or Postgres, per ADR-007/013),
  never in the JSONL/OTel telemetry stream — the two data stores are already separated by design.
- **OTel export inherits redaction** (`aurora/telemetry/otel.py`) — it maps an *already-redacted*
  trace payload to spans; there is no path for raw content to reach an export backend that
  wasn't already blocked at the source.

## 3. Proposed retention periods (draft — needs legal/business sign-off)

| Data | Proposed retention | Reasoning |
|---|---|---|
| Telemetry JSONL / OTel traces | **30 days** | Long enough for operational debugging and SLO trend analysis (goal.md 4.2); short enough that a leak or breach exposes a bounded window. Purely operational data with no business need to keep indefinitely. |
| Booking records | **Length of stay + 1 year** | Common hospitality-industry norm for dispute resolution, chargebacks, and basic tax/accounting needs. **This number is a placeholder** — actual requirements depend on jurisdiction, payment processor obligations, and the business's own policy; a real deployment must get this from legal/finance, not from this document. |
| Confirmation IDs / idempotency keys | Same as their booking record | They have no independent value once the booking they belong to is purged. |

## 4. Deletion procedure (not yet automated)

No automated purge job exists today. Before this policy is approved for production:

1. A scheduled job (cron, or a Postgres extension like `pg_cron`) that deletes booking rows past
   the retention window, and rotates/deletes JSONL files past 30 days.
2. A documented manual process for an early-deletion request (e.g. a guest asking to be
   forgotten), including how to find and remove their booking row(s) and any log lines
   referencing their session ID.
3. Verification that deleting a booking row doesn't break anything relying on its existence
   (nothing currently does — bookings are read-mostly, never referenced by ID elsewhere).

## 5. The explicit gate on `TELEMETRY_INCLUDE_CONTENT=true`

`TELEMETRY_INCLUDE_CONTENT=true` turns off both redaction protections in §2 — full transcripts,
full guest names and contact details land in the trace stream (and, if `TELEMETRY_OTLP_ENDPOINT`
is also set, in whatever backend that feeds). This must **never** be enabled outside local,
non-production debugging with non-sensitive test data, until **all** of the following are true:

- [ ] This document is reviewed and approved by whoever owns privacy/legal decisions for the
      real deployment (not just accepted as-is from this draft).
- [ ] §3's retention periods are replaced with actual, sourced figures.
- [ ] §4's deletion mechanism is built and tested, not just described.
- [ ] There's a documented lawful basis / consent story for recording caller speech content
      (this varies by jurisdiction — e.g. call-recording consent laws differ by US state and by
      country).

None of these boxes are checked today. `config_check.py` and the RUNBOOK both already state
`TELEMETRY_INCLUDE_CONTENT` is for local debugging only; this document is the detailed reasoning
behind that rule, and the checklist a real production rollout needs to clear before that rule
can change.
