# PROJECT_STATE

Last Updated: 2026-08-21

## Current Phase

P0 Baseline / Dependency Freeze: COMPLETE  
P1 Live Contract Discovery: COMPLETE  
P2 Foundation: COMPLETE  
P3 Normalization Core: COMPLETE  
P4 Google Calendar Client: COMPLETE + LIVE E2E  
P5 TimeTree MCP Client: COMPLETE + LIVE CRUD  
P6 Recurrence Series Core: COMPLETE + LIVE SERIES E2E  
P7 Recurrence Exception Contract / Safety Gate: COMPLETE + SAFE-STOP
P8: IN PROGRESS
P9-P15: NOT STARTED

---

## Canonical Authority

This file is the current project handoff / state checkpoint.

Canonical design priority:

1. `docs/timetree取得_要件定義_v0.12.md`
2. `docs/TimeTree取得_基本設計_v0.12.md`
3. `docs/TimeTree取得_詳細設計_v0.11.md`
4. `docs/TimeTree取得_実装計画_v0.10.md`
5. Implementation

If implementation and design appear to disagree:

- do not silently reinterpret the contract
- re-read the canonical design documents
- distinguish confirmed live behavior from assumptions
- unknown / unverified external behavior must fail safe

Current implementation order follows:

`docs/TimeTree取得_実装計画_v0.10.md`

---

## Project Goal / V1 Architecture

V1 architecture:

```text
TimeTree
   ↕
TimeTree-MCP (Primary)
   ↕
Python Calendar Bridge
   ↕
Dedicated Google Calendar "TimeTree Bridge"
   ↕
ChatGPT Web / Notion
```

TimeTree is the Source of Truth.

TimeTree-Exporter remains an independent read / verification path.

OpenCLI is final diagnostic / fallback tooling and is not the normal sync path.

---

## Development Policy

- Do as much development, review, reasoning, and artifact preparation as practical in ChatGPT Web.
- Use local execution / editing / testing only where required.
- Explicit PowerShell commands for local work are acceptable.
- Prefer small gated phases over large speculative implementations.
- Add unit / fixture coverage before live write tests where practical.
- Live write tests must use disposable test events and guarantee cleanup.
- Do not store TimeTree or Google credentials in the repository.
- Do not log secrets.
- Do not copy live secrets into fixtures, docs, chat summaries, or `PROJECT_STATE.md`.
- Do not treat a manual/live probe as equivalent to product sync-engine integration.
- Unknown or unverified external contracts fail safe.
- Generic recurrence exception writes remain closed until P7 is explicitly completed.

---

## Repository

Main repository:

`OMrice8383/timetree-sync-action`

Branch:

`main`

Local path:

`C:\Users\white\dev\timetree-sync-action`

Latest P6 checkpoint:

`c3bf348 feat: add P6 recurrence series core`

Previous checkpoints:

- `88e1fb4 docs: clean project state formatting`
- `dca965f docs: update project state through P5`
- `ba25c5c fix: clean invalid tests package marker`
- `80db1fe feat: add P5 TimeTree MCP client`
- `c8a380e feat: add P4 Google Calendar client`
- `3182597 feat: add P3 normalization core`
- `7442f7a feat: add P2 bridge foundation`

P6 push:

```text
88e1fb4..c3bf348  main -> main
```

Worktree after P6 commit / push:

```text
clean
```

`git status --short` returned no output.

Upstream baseline SHA:

`6beddee66b4d70ad970de029e2440852f9c85de0`

---

## Environment Baseline

Python:

`3.12.13`

uv:

`0.11.8`

Node.js:

`24.14.1`

npm:

`11.11.0`

Git:

`2.54.0.windows.1`

GitHub CLI:

`2.97.0`

OpenCLI:

`1.8.6`

### Python Dependencies

MCP Python SDK:

`mcp==2.0.0`

TimeTree-Exporter:

`timetree-exporter==0.8.0`

google-api-python-client:

`2.198.0`

google-auth:

`2.56.3`

requests:

`2.34.2`

ruff:

`0.16.3`

---

## TimeTree-MCP

Upstream repository:

`ehs208/TimeTree-MCP`

Version:

`0.3.0`

P0 upstream SHA:

`64c8188eac103c0d8e8726b5fc5b2c31f961f3a5`

Local P1 checkpoint:

`87c2690 fix: complete P1 TimeTree read contracts`

Local runtime path:

`C:\Users\white\dev\TimeTree-MCP`

Runtime entrypoint:

`dist/index.js`

### P1 Confirmed / Patched Contracts

- `CalendarUser.name` may be null.
- `get_updated_events` pagination fixed.
- Inclusive-boundary UUID dedupe implemented.
- `deactivated_at` exposed.
- `parent_id` exposed.
- `recurring_uuid` exposed.
- `type=0` is preserved.
- TimeTree event canonical identity is UUID.
- TimeTree calendar ID and TimeTree-Exporter calendar code are distinct concepts and must not be conflated.

Target TimeTree calendar ID:

`1016563008`

TimeTree-Exporter calendar code:

`2EeVifbGQLAx`

P1 verification:

- typecheck: PASS
- tests: 16 / 16 PASS
- build: PASS
- diff check: PASS

---

## Core Event Semantics

### Identity

TimeTree canonical identity:

`UUID`

Google identity:

Google event ID, with recurrence master / exception identity preserved separately.

Series and exception identity must never be conflated.

### Timezone

- Start and end timezone fallback are independent.
- Never copy a resolved timezone from one side to the other.
- Timed events use timezone-aware datetimes internally.
- Effective start/end timezone changes are semantic and affect the event hash.
- Timed recurrence series require matching effective start/end timezone in the currently enabled P6 contract.

### All-day

Internal / Google:

exclusive end

TimeTree read semantics:

inclusive end

Conversion must remain explicit in both directions.

Important P6 live finding:

TimeTree all-day writes must serialize calendar dates as UTC midnight with timezone `UTC`.

Do not serialize an all-day calendar date as `Asia/Tokyo 00:00` and then send its UTC instant.

Why:

```text
2026-10-10 00:00 Asia/Tokyo
→ 2026-10-09 15:00 UTC
→ TimeTree preserves timestamp but normalizes timezone to UTC
→ read-back calendar date becomes 2026-10-09
```

Confirmed safe write representation:

```text
start_at       = target calendar date at UTC 00:00
start_timezone = UTC

end_at         = TimeTree inclusive end date at UTC 00:00
end_timezone   = UTC
```

This was live-verified in P6.

### Classification

TimeTree:

- `category=1,type=0` → `SYNC`
- `category=2` → `IGNORE_KNOWN` / Memo
- `type=1` → `IGNORE_KNOWN` / Birthday
- unknown category/type → `UNSUPPORTED`

Google:

- empty-title events are unsupported
- unsupported special event types fail safe

---

# Recurrence Contract

## P6 Confirmed Writable Series Subset

P6 intentionally enables only a conservative, live-confirmed series subset.

Required:

- exactly one `RRULE`
- `FREQ=WEEKLY`

Optional confirmed RRULE fields:

- `BYDAY`
- `INTERVAL`
- `COUNT`
- `UNTIL`

Rules:

- `BYDAY` supports plain weekdays only:
  - `MO`
  - `TU`
  - `WE`
  - `TH`
  - `FR`
  - `SA`
  - `SU`
- `INTERVAL` must be positive.
- `COUNT` must be positive.
- `COUNT` and `UNTIL` must not both be present.
- Timed `UNTIL` uses UTC compact datetime.
- All-day `UNTIL` uses compact date.
- Unknown RRULE keys fail safe.
- The P6.1 exception to the non-weekly rule is exact all-day `RRULE:FREQ=YEARLY`.
- Timed YEARLY and every parameterized / EXDATE YEARLY variant fail safe.
- DAILY, MONTHLY, and other non-weekly recurrence shapes fail safe.
- Do not broaden this subset without explicit contract evidence.

## P6.1 Confirmed Writable Series Extension

Live-confirmed and enabled:

```text
all_day = true
RRULE:FREQ=YEARLY
```

The YEARLY rule must contain no additional parameter and must be the only
recurrence line. This includes rejecting a raw `INTERVAL=1` that a generic
canonicalizer might otherwise omit. Timed YEARLY, YEARLY with `INTERVAL`,
`COUNT`, `UNTIL`, `BYDAY`, `BYMONTH`, `BYMONTHDAY`, `EXDATE`, or any other
parameter remain unsupported.

Live evidence supplied by the completed Contract Discovery:

- Google Create / Read / Update / Clear / Restore / Delete / Cleanup: PASS
- TimeTree Create / Read / Update / Clear / Restore / Delete / Cleanup: PASS
- TimeTree UUID preserved through title update: PASS
- Cleanup zero: PASS

## EXDATE

Confirmed:

### Timed UTC form

```text
EXDATE:YYYYMMDDTHHMMSSZ
```

### Timed TZID form

```text
EXDATE;TZID=<effective series timezone>:YYYYMMDDTHHMMSS
```

TZID must match the effective series timezone.

Google live behavior confirmed that it may normalize:

```text
EXDATE:20260918T010000Z
```

to:

```text
EXDATE;TZID=Asia/Tokyo:20260918T100000
```

These are the same instant.

Canonicalization therefore converts equivalent timed UTC / TZID EXDATE forms to the same UTC semantic representation.

Important safety rule:

Raw EXDATE context must be validated before canonicalization so a mismatched TZID cannot be erased by UTC conversion.

### All-day EXDATE

Confirmed:

```text
EXDATE;VALUE=DATE:YYYYMMDD
```

All-day EXDATE remains a date semantic, not an instant semantic.

## Unsupported Recurrence Features

Still unsupported / fail-safe:

- `RDATE`
- `EXRULE`
- timed YEARLY RRULE
- parameterized or EXDATE YEARLY RRULE
- DAILY, MONTHLY, and other non-weekly RRULE
- unknown RRULE keys
- unconfirmed recurrence syntax

Do not infer compatibility because Google or TimeTree happens to accept a syntax.

## Exception Gate

Generic recurrence exception writes remain CLOSED until P7.

Opening P6 series writes must never open the P7 exception write path.

P6 explicitly protects this at both Google and TimeTree write boundaries.

---

# P2 Foundation — COMPLETE

Implemented:

- configuration loading
- secret loading / recursive redaction
- JSONL logging
- SQLite migration
- event link / sync state repository
- operation journal state transitions
- conflict repository
- daily run lock
- safe CLI skeletons

Windows-specific lock behavior:

Do not use `os.kill(pid, 0)` for PID liveness on Windows.

This previously caused `KeyboardInterrupt` / terminal instability because Windows does not provide the same safe POSIX existence-probe semantics.

Current P2 regression:

14 / 14 PASS

---

# P3 Normalization Core — COMPLETE

Implemented:

- normalized event model
- event kind:
  - single
  - series
  - exception
- event classification
- TimeTree normalization
- Google normalization
- all-day inclusive / exclusive conversion
- independent timezone handling
- recurrence canonicalization
- semantic event hash
- partial delete change model
- recurrence exception identity model

P6 addition:

Timed EXDATE UTC and TZID forms now canonicalize by instant.

Example:

```text
EXDATE:20260918T010000Z
==
EXDATE;TZID=Asia/Tokyo:20260918T100000
```

Current P3 regression:

36 / 36 PASS

---

# P4 Google Calendar Client — COMPLETE + LIVE E2E

Checkpoint:

`c8a380e feat: add P4 Google Calendar client`

Implemented:

- fixed Google list query contract
- `singleEvents=false`
- `showDeleted=true`
- pagination
- full-sync `nextSyncToken`
- incremental sync token flow
- HTTP 410 → full-resync signal
- cancelled event parsing
- recurring master / exception identity preservation
- insert
- get
- PATCH update
- delete
- private bridge metadata
- timed / all-day write bodies
- recurrence write gate

Google updates use:

`PATCH`

P4 tests:

17 / 17 PASS

P4 standalone live E2E:

COMPLETE

---

# P5 TimeTree MCP Client — COMPLETE + LIVE CRUD

Checkpoint:

`80db1fe feat: add P5 TimeTree MCP client`

Implemented in:

`bridge/timetree_client.py`

### Connection Boundary

- one MCP stdio session reused per client context
- MCP child receives only explicit TimeTree credentials
- unexpected environment variables rejected
- transport / tool / protocol / configuration / write-gate failures separated
- malformed payloads fail safe

### Read Boundary

Implemented wrappers:

- `list_calendars`
- `get_events`
- `get_updated_events`

P1-required fields enforced.

Read timestamps:

- TimeTree-MCP ISO 8601 or Unix ms accepted
- normalized to Unix ms at P5 → P3 boundary

UUID duplicate handling:

- same UUID with usable `updated_at` → newer payload wins
- conflicting duplicate without usable update metadata → fail safe

### Write Boundary

Implemented:

- create
- update
- delete

Calendar ID:

canonical string internally, numeric only at TimeTree-MCP write boundary.

Timed events:

- Unix ms timestamps
- independent start/end effective timezones

All-day events:

- internal exclusive end
- TimeTree inclusive end
- P6 corrected live write serialization to UTC-midnight date representation

Update writes only explicitly requested semantic fields.

Create / Update / Delete UUID consistency enforced.

### P5 Tests

14 / 14 PASS

### P5 Live CRUD

Confirmed:

- connection
- target calendar found
- create UUID returned
- created UUID found by full read
- incremental read found same UUID
- update preserved UUID
- updated event readable
- delete returned same UUID
- deleted UUID absent from full read
- cleanup successful

---

# P6 Recurrence Series Core — COMPLETE + LIVE SERIES E2E

Checkpoint:

`c3bf348 feat: add P6 recurrence series core`

Changed / added:

- `bridge/adapters.py`
- `bridge/canonical.py`
- `bridge/google_client.py`
- `bridge/timetree_client.py`
- `bridge/recurrence.py`
- `tests/p3/test_hash.py`
- `tests/p5/test_timetree_client.py`
- `tests/p6/test_recurrence_series.py`
- `tests/p6/live_timetree_series_probe.py`
- `tests/p6/live_google_series_probe.py`

Commit summary:

```text
10 files changed
1480 insertions
24 deletions
```

## P6 Implemented

### Series Read

Supported series syntax is validated during normalization.

Unsupported recurrence syntax fails safe on read rather than silently entering the normalized model.

### Series Create

Google and TimeTree series create are enabled only with explicit recurrence-write gate opening.

### Series Rule Update

Confirmed live:

- `INTERVAL`
- `UNTIL`
- existing supported RRULE semantics

TimeTree recurrence update uses semantic update field:

`recurrence`

### Recurrence Removal

Confirmed live on both systems.

TimeTree:

`recurrences: []`

Google:

explicit recurrence clear only.

Google recurrence removal must use explicit:

`clear_recurrence=True`

A normal single-event PATCH must not accidentally emit recurrence clear.

### Series Delete

Series delete is enabled with the P6 series gate.

Generic exception delete remains blocked until P7.

## P6 Google Safety Boundary

Even when:

`allow_recurrence_write=True`

an `EventKind.EXCEPTION` write is still rejected.

This prevents the P6 flag from bypassing P7.

## P6 TimeTree Safety Boundary

Exception create/update/delete remains blocked even when the P6 series flag is open.

## P6 Parameterized EXDATE Fix

Adapter recurrence property parsing now handles parameters correctly.

For example:

```text
EXDATE;VALUE=DATE:20261017
```

is recognized as property:

`EXDATE`

rather than invalid property:

`EXDATE;VALUE=DATE`

## P6 Live Finding 1 — TimeTree All-day Date Serialization

Initial all-day series live round-trip:

```text
recurrence_match = true
date_match       = false
```

Root cause:

TimeTree normalized the timezone to UTC while preserving the instant.

The bridge was sending local midnight instant instead of serializing the calendar date as UTC midnight.

Fix:

TimeTree all-day write uses UTC midnight date representation.

After fix:

TimeTree all-day series round-trip PASS.

## P6 Live Finding 2 — Google Timed EXDATE Normalization

Initial Google timed series round-trip:

```text
time_match       = true
recurrence_match = false
```

Observed:

```text
write:
EXDATE:20260918T010000Z

Google read:
EXDATE;TZID=Asia/Tokyo:20260918T100000
```

These are the same instant.

Fix:

Timed EXDATE canonicalization became timezone-aware.

After fix:

Google timed series round-trip PASS.

## P6 Unit / Regression Verification

Final unit regressions:

- P2: 14 / 14 PASS
- P3: 36 / 36 PASS
- P4: 17 / 17 PASS
- P5: 14 / 14 PASS
- P6: 15 / 15 PASS

Total:

96 / 96 PASS

Static verification:

- Ruff: PASS
- `python -m compileall -q bridge tests`: PASS
- `git diff --check`: PASS

## P6 TimeTree Live Series Probe

Probe:

`tests/p6/live_timetree_series_probe.py`

Final result:

```text
active_series_delete          true
all_day_series_delete         true
all_day_series_roundtrip      true
cleanup                       true
connection                    true
recurrence_removal            true
removed_series_delete         true
timed_series_create           true
timed_series_read_roundtrip   true
timed_series_rule_update      true
```

Result:

10 / 10 TRUE

## P6 Google Live Series Probe

Probe:

`tests/p6/live_google_series_probe.py`

Final result:

```text
active_series_delete_incremental       true
all_day_series_delete_incremental      true
all_day_series_roundtrip               true
cleanup                                true
recurrence_removal                     true
removed_series_delete_incremental      true
timed_series_create_incremental        true
timed_series_read_roundtrip            true
timed_series_rule_update               true
```

Result:

9 / 9 TRUE

Both live probes confirmed cleanup.

No disposable P6 test events were intentionally left behind.

## P6 Completion Gate

PASS

P6 is complete.

Do not reopen the series contract casually.

Any expansion beyond the confirmed P6 subset requires new evidence and tests.

---

# Repository Hygiene

Inherited invalid `tests/__init__.py` was previously fixed in:

`ba25c5c fix: clean invalid tests package marker`

Post-P6:

- full P2-P6 regression: PASS
- Ruff: PASS
- compileall: PASS
- diff check: PASS
- live TimeTree series: PASS
- live Google series: PASS
- cleanup: PASS
- commit: `c3bf348`
- pushed to `origin/main`
- worktree: clean

Windows Git may print:

```text
LF will be replaced by CRLF the next time Git touches it
```

This is a line-ending warning, not a `git diff --check` failure.

---

# Security / Secrets

- TimeTree password is not stored in the repository.
- Google service-account private credentials are not stored in the repository.
- Secret values must not be logged.
- Local environment variables may intentionally remain in the active PowerShell session during live development.
- Never commit environment secrets.
- Never place secret values into fixtures, probes, logs, docs, or `PROJECT_STATE.md`.

Google live probe expects one of:

- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `GOOGLE_SERVICE_ACCOUNT_FILE`

The last live session used a local service-account file via environment variable.

Do not record private credential contents or tokens.

---

# Known Dependency Debt

From P0 TimeTree-MCP `npm audit`:

- 2 low
- 2 moderate
- 2 high

No automatic audit fix has been applied.

Review remains required before final operation / release.

This is not a P7 blocker unless a dependency issue directly affects P7 safety or runtime behavior.

---

# P7 — COMPLETE: Recurrence Exception Contract / Safety Gate

## P7 Closure Status — Safe-stop V1

P7 is COMPLETE using a safe-stop contract. V1 does not implement generic
TimeTree recurrence-exception `original_start` mapping or exception writes.

The following evidence is sufficient to detect recurrence-exception evidence,
but not to perform a safe generic exception mapping:

- a Series master with exception evidence in its observed RRULE / EXDATE state
- a related child exposing `parent_id` or `recurring_uuid`

When such evidence is observed, the affected Series is UNSUPPORTED and must
safe-stop. A child `start_at` is never promoted to `original_start`. EXDATE
and detached children are never guessed into a one-to-one mapping. The
confirmed P6 series EXDATE syntax remains unchanged; P7 safe-stop applies to
exception evidence observed during the live contract check.

### Confirmed TimeTree live evidence

For “この予定のみ編集”:

- the master retained its RRULE and gained an EXDATE for the original slot
- a distinct detached child was observed
- the child exposed `parent_id` and `recurring_uuid` related to the master
- the child `start_at` represented the edited actual start
- `get_updated_events` returned the master and child

For “この予定のみ削除”:

- the master retained its RRULE and gained an EXDATE for the deleted slot
- no child was returned
- `get_updated_events` returned the master

An edited detached child was also observed to survive Series master deletion
with the same UUID while becoming a standalone Event with:

```text
parent_id = null
recurring_uuid = null
recurrences = []
```

Master deletion must not assume that a related edited child is deleted.
Cleanup must track recorded child UUIDs and must not delete an ambiguous
standalone event by title alone.

### P7 write boundary

Generic TimeTree recurrence-exception create / update / delete remains CLOSED.
`allow_recurrence_write=True` authorizes only the confirmed P6 Series subset
and never opens an exception write path. No separate exception-write flag exists.

### P7 Label Contract closure

V1 synchronizes only the exact Label names `大河予定` and `共通予定`.
TimeTree Label names are resolved at runtime with `get_calendar_labels`;
numeric `label_id` values are not hardcoded or used as cross-system canonical
metadata. Other existing Labels are `IGNORE_KNOWN / LABEL_OUT_OF_SCOPE`.
Missing, unknown, duplicate, or otherwise ambiguous Label resolution is a
safe-stop. Normalized Event `label` is a semantic name and is included in
canonical hashing. Google metadata uses
`extendedProperties.private.timetree_label_name`; unmanaged Google events
without Label metadata default to `大河予定`, while managed metadata loss does
not silently fall back. Static tests and the live Label write verification
passed. Live Test Artifacts were cleaned up.

P7 completion evidence consists of the read-only/live observations above,
safe-stop implementation and tests, Label Contract implementation, full
P2-P7 regression, static checks, and final `[P7 TEST]` zero-artifact Read.

## Goal

Define and verify the exact recurrence EXCEPTION contract before enabling any generic exception write path.

P7 must not assume that series support implies exception support.

P7 is a separate safety boundary.

## Already Available Identity Signals

TimeTree read-side fields confirmed in P1:

- `parent_id`
- `recurring_uuid`

Google recurrence exception identity already modeled / parsed using:

- recurring master identity
- `recurringEventId`
- `originalStartTime`

Internal model already distinguishes:

- `EventKind.SERIES`
- `EventKind.EXCEPTION`

Partial recurrence exception delete model already requires parent + original start identity.

These existing structures are prerequisites, not proof that writes are safe.

## P7 Core Questions

Before enabling exception writes, determine with evidence:

1. Exact TimeTree exception identity.
2. Relationship between:
   - exception UUID
   - parent series UUID / ID
   - `parent_id`
   - `recurring_uuid`
   - original occurrence start.
3. Exact Google exception identity and mutation contract.
4. Whether moved occurrences preserve original occurrence identity.
5. Whether edited occurrence timestamps differ from original-start identity.
6. Cancellation / deletion semantics for one occurrence.
7. Read behavior after:
   - edit one occurrence
   - move one occurrence
   - delete one occurrence
8. Whether TimeTree-MCP can safely express the required exception mutations.
9. Whether generic create/update/delete can accidentally mutate the whole series.
10. Whether both systems can round-trip the same exception without duplicate creation.

## P7 Safety Rules

- Keep all generic exception write gates CLOSED after P7; V1 provides
  safe-stop, not generic exception writes.
- Do not reuse P6 `allow_recurrence_write` as permission for exception writes.
- Do not infer exception write parameters from read payloads.
- Do not test against important real events.
- Use disposable series only.
- Cleanup must include master and any generated exception / detached occurrence.
- Preserve master identity and original occurrence identity separately.
- Unknown exception shapes fail safe.
- A moved occurrence must not be matched only by its new start time.
- P7 completion is based on the combined contract, implementation, tests,
  live evidence, and cleanup; manual UI observation alone is insufficient.
- Live observation and product implementation are separate completion conditions.

## P7 Entry Sequence (Completed)

1. Re-read recurrence-exception sections of:
   - Requirements
   - Basic Design
   - Detailed Design
   - Implementation Plan
2. Inspect current:
   - `bridge/models.py`
   - `bridge/adapters.py`
   - `bridge/google_client.py`
   - `bridge/timetree_client.py`
   - P1 TimeTree-MCP recurrence fields
   - P3 exception identity / delete models
3. Write down the exact current exception gates and every possible bypass path.
4. Define a read-only P7 exception identity contract first.
5. Add sanitized fixtures / unit tests for:
   - normal exception
   - moved occurrence
   - cancelled / deleted occurrence
   - incomplete identity fail-safe
6. Verify live READ behavior using a disposable recurring master and one edited occurrence.
7. Only after read identity is proven, inspect TimeTree-MCP write capability.
8. Define explicit exception write API separate from P6 series permission.
9. Add unit safety tests before any live exception mutation.
10. Run disposable live exception E2E with guaranteed cleanup.
11. Run P2-P7 full regression.
12. Mark P7 COMPLETE only after:
    - contract proven
    - implementation complete
    - safety gate verified
    - live E2E passed
    - cleanup passed

---

# Handoff After P7

P7 is complete with the safe-stop contract above. P8 is now in progress under
the explicit P8-A through P8-B.1 scope below.

The next phase may begin only from an explicit new request after reviewing:

```text
Read PROJECT_STATE.md as the canonical current-state handoff.
P6 is COMPLETE and committed as c3bf348.
Do not redo P6.
P7 is COMPLETE. Keep generic recurrence exception writes CLOSED.
```

Historical P7 entry sequence:

1. Verify GitHub `main` contains `c3bf348`.
2. Re-read canonical design docs for recurrence exceptions.
3. Inspect current exception identity code and all write gates.
4. Produce a P7 contract matrix:
   - TimeTree field
   - Google field
   - internal field
   - observed meaning
   - writeability
   - confidence / evidence
5. Design P7 read-only fixtures/tests first.
6. Do not perform generic recurrence exception live writes yet.

---

# P8 Current Status — IN PROGRESS

```text
P8-A   Bootstrap Core / Fake Integration: COMPLETE
P8-A.1 Deterministic Google Event ID Recovery: COMPLETE
P8-B   CLI / Doctor / Read-only Gate: IMPLEMENTED
P8-B.1 Canonical classification / exception scope review: COMPLETE
P8-B.2 Unnamed existing out-of-scope Label contract: COMPLETE
P8-B.3 Recurrence Diagnostic: COMPLETE
P6.1 exact all-day YEARLY extension: IMPLEMENTED
Live Write: 0
P8-C   Live Bootstrap CLI / Recovery Gate: COMPLETE; Live Bootstrap NOT EXECUTED
```

P8-B.1 aligns TimeTree classification with the canonical order:

```text
category/type classification
→ Label Scope for normal Calendar Events
→ P7 exception evidence check for in-scope SYNC candidates only
```

Birthday, Memo, and out-of-scope normal Events are ignored before the
exception gate. Generic recurrence exception writes remain CLOSED.

Current read-only gate blockers:

- none

Latest authoritative live read-only result:

- Doctor: PASS
- Bootstrap dry-run: PASS
- `ready_for_live_bootstrap = true`
- `remote_writes = 0`

The current live TimeTree snapshot has no in-scope exception evidence after
classification. Existing unnamed out-of-scope Label IDs are ignored without
inferring a Label name. No Google or TimeTree remote write has been executed.

P8-B.3 previously identified one in-scope normal Calendar Event recurrence
shape before P6.1:

```text
event_kind: series
all_day: true
effective_timezone_relation: same
property_names: RRULE
recurrence_lines: RRULE:FREQ=YEARLY
reason_code: UNSUPPORTED_RECURRENCE_FEATURE (historical pre-P6.1 result)
reason: P6 writable RRULE requires FREQ=WEEKLY (historical pre-P6.1 result)
```

P6.1 now enables this exact all-day YEARLY shape. Parameterized, timed, or
otherwise unconfirmed YEARLY variants remain unsupported and generic
recurrence exception writes remain closed.

---

# Do Not Redo

Do not redo these unless a regression or design conflict is discovered:

- P0 baseline work
- P1 TimeTree read-contract discovery
- P2 foundation
- P3 normalization core
- P4 Google standalone client
- P5 TimeTree standalone client
- P6 series contract discovery
- P6 TimeTree live series probe
- P6 Google live series probe
- TimeTree all-day UTC-midnight live contract
- Google UTC/TZID timed EXDATE equivalence

---

# Current Checkpoint Summary

```text
Repo:
OMrice8383/timetree-sync-action

Branch:
main

Latest implementation checkpoint:
d57387c feat: add P8-C live bootstrap recovery gate

Worktree:
clean; main synced with origin/main

Completed:
P0
P1
P2
P3
P4 + Google Live E2E
P5 + TimeTree Live CRUD
P6 + TimeTree/Google Live Series E2E
P7 safe-stop contract + Label Contract
P8-A Bootstrap Core / Fake Integration
P8-A.1 deterministic Google Event ID Recovery
P8-B CLI / Doctor / Read-only Gate
P8-B.1 canonical classification / exception scope review
P8-B.2 unnamed existing out-of-scope Label contract
P8-B.3 recurrence diagnostic
P8-C Live Bootstrap CLI / Recovery Gate

Final regression:
P2 14/14
P3 37/37
P4 17/17
P5 14/14
P6 18/18
P7 19/19
P8 62/62
Total 181/181

Live:
TimeTree P6 10/10 true
Google P6 9/9 true
cleanup true
TimeTree P7 exception evidence read: safe-stop contract confirmed
TimeTree Label Contract live write: PASS
P7 Test Artifact final full read: 0
P8-B.2/P6.1 Live Read-only Gate: PASS
P8-B.3 Recurrence Diagnostic after P6.1: unsupported_count = 0
P8-C final Live read-only Doctor: PASS
P8-C final Bootstrap dry-run: PASS
P8-C ready_for_live_bootstrap: true
P8-C remote_writes: 0
P8-C recovery.authorized: false (expected clean-start state)
Google credentials: configured; credential file existence PASS
TimeTree raw events: 360
TimeTree eligible: 90
TimeTree ignored: 270
TimeTree unsupported: 0
TimeTree exception evidence after classification: 0
TimeTree unnamed out-of-scope Label events: 20
TimeTree unresolved Label events: 0
TimeTree recurrence diagnostics: 0
Google live events: 0; tombstones: 20; unmanaged: 0
SQLite bootstrap state / links / sync operations / failed operations / conflicts: empty

Next:
P8 Live Bootstrap — final pre-write Gate, then first persistent Bootstrap execution
P8-C implementation is checkpointed; Live Bootstrap intentionally not executed yet

Critical guard:
P7 generic recurrence exception writes are still CLOSED.
```
