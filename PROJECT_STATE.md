# PROJECT_STATE

Last Updated: 2026-08-21

## Current Phase

P0 Baseline / Dependency Freeze: COMPLETE
P1 Live Contract Discovery: COMPLETE
P2 Foundation: COMPLETE
P3 Normalization Core: COMPLETE
P4 Google Calendar Client: COMPLETE + LIVE E2E
P5 TimeTree MCP Client: COMPLETE + LIVE CRUD
P6 Recurrence Series Core: NEXT
P7 Recurrence Exception Gate: NOT STARTED
P8-P15: NOT STARTED

## Canonical Design

Priority:

Requirements
→ Basic Design
→ Detailed Design
→ Implementation Plan
→ Implementation

Current implementation order follows:

`TimeTree取得_実装計画_v0.9.md`

V1 architecture:

TimeTree
↕
TimeTree-MCP (Primary)
↕
Python Calendar Bridge
↕
Dedicated Google Calendar `TimeTree Bridge`
↕
ChatGPT Web / Notion

TimeTree is the Source of Truth.

TimeTree-Exporter remains an independent read / verification path.

OpenCLI is final diagnostic / fallback tooling and is not the normal sync path.

## Development Policy

- Do as much development and review as practical in ChatGPT Web.
- Use local execution / editing / testing only where required.
- Do not store TimeTree or Google credentials in the repository.
- Do not log secrets.
- Do not enable recurrence series writes before P6.
- Do not enable generic recurrence exception writes before P7.
- Do not treat live verification as equivalent to product integration.
- Unknown or unverified external contracts fail safe.

## Repository

Main fork:

`OMrice8383/timetree-sync-action`

Branch:

`main`

Latest checkpoints:

- `ba25c5c` — `fix: clean invalid tests package marker`
- `80db1fe` — `feat: add P5 TimeTree MCP client`
- `c8a380e` — `feat: add P4 Google Calendar client`
- `3182597` — `feat: add P3 normalization core`
- `7442f7a` — `feat: add P2 bridge foundation`

Current worktree after `ba25c5c`:

clean

Upstream baseline SHA:

`6beddee66b4d70ad970de029e2440852f9c85de0`

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

## TimeTree-MCP

Repository:

`ehs208/TimeTree-MCP`

Version:

`0.3.0`

P0 upstream SHA:

`64c8188eac103c0d8e8726b5fc5b2c31f961f3a5`

Local P1 checkpoint:

`87c2690 fix: complete P1 TimeTree read contracts`

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
- TimeTree calendar ID and TimeTree-Exporter calendar code are distinct identifiers and must not be conflated.

Target TimeTree calendar ID:

`1016563008`

TimeTree-Exporter calendar code:

`2EeVifbGQLAx`

P1 verification:

- typecheck: PASS
- tests: 16 / 16 PASS
- build: PASS
- diff check: PASS

## Core Event Semantics

### Identity

TimeTree canonical identity:

UUID

### Timezone

- Start and end timezone fallback are independent.
- Never copy a resolved timezone from one side to the other.
- Timed events use timezone-aware datetimes internally.
- Effective start/end timezone changes are semantic and affect the event hash.

### All-day

TimeTree:

inclusive end

Internal / Google:

exclusive end

Conversion must remain explicit in both directions.

### Classification

TimeTree:

- `category=1,type=0` → `SYNC`
- `category=2` → `IGNORE_KNOWN` / Memo
- `type=1` → `IGNORE_KNOWN` / Birthday
- unknown category/type → `UNSUPPORTED`

Google:

- empty-title events are unsupported
- unsupported special event types fail safe

### Recurrence

Confirmed supported core so far:

- RRULE normalization
- EXDATE basic confirmed contract / round-trip

Not enabled generically:

- RDATE
- EXRULE
- unknown recurrence lines

Series writes remain gated until P6.

Exception writes remain gated until P7.

## P2 Foundation — COMPLETE

Implemented foundation includes:

- configuration loading
- secret loading / recursive redaction
- JSONL logging
- SQLite migration
- event link / sync state repository
- operation journal state transitions
- conflict repository
- daily run lock
- safe CLI skeletons

Windows-specific lock behavior was corrected so PID liveness checking does not use unsafe `os.kill(pid, 0)` semantics.

Final P2 regression at P5 gate:

14 / 14 PASS

## P3 Normalization Core — COMPLETE

Implemented:

- normalized event model
- event kind: single / series / exception
- event classification
- TimeTree normalization
- Google normalization
- all-day inclusive/exclusive conversion
- independent timezone handling
- recurrence canonicalization
- semantic event hash
- partial delete change model
- recurrence exception identity model

Final P3 regression at P5 gate:

35 / 35 PASS

## P4 Google Calendar Client — COMPLETE

Checkpoint:

`c8a380e feat: add P4 Google Calendar client`

Implemented:

- fixed Google list query contract
- `singleEvents=false` for recurrence-preserving reads
- pagination
- full-sync `nextSyncToken`
- incremental sync token flow
- HTTP 410 → full-resync signal
- cancelled event parsing
- recurring master / exception identity preservation
- insert
- get
- patch
- delete
- private bridge metadata
- timed / all-day write bodies
- recurrence write gate

Google delete observability uses:

`showDeleted=true`

Updates use:

PATCH

P4 tests at P5 final gate:

17 / 17 PASS

P4 live E2E:

COMPLETE

## P5 TimeTree MCP Client — COMPLETE

Checkpoint:

`80db1fe feat: add P5 TimeTree MCP client`

Implemented in:

`bridge/timetree_client.py`

MCP SDK:

`mcp==2.0.0`

### Connection Boundary

- one MCP stdio session is reused per client context
- MCP child receives only explicit:
  - `TIMETREE_EMAIL`
  - `TIMETREE_PASSWORD`
- unexpected environment variables are rejected
- MCP transport, tool, protocol, configuration, and write-gate failures are separated
- malformed payloads fail safe

### Read Boundary

Implemented wrappers:

- `list_calendars`
- `get_events`
- `get_updated_events`

P1-required fields are enforced.

Read timestamps accept TimeTree-MCP ISO 8601 or Unix ms and are converted to Unix ms at the P5 → P3 boundary.

`type=0` regression is explicitly guarded.

UUID duplicate handling:

- same UUID with usable `updated_at` → newer payload wins
- conflicting duplicate without usable update metadata → fail safe

### Write Boundary

Implemented:

- create
- update
- delete

Calendar ID is a canonical string internally and converted to numeric form only at the TimeTree-MCP write boundary.

Timed events:

- Unix ms timestamps
- independent start/end effective timezones

All-day events:

- internal exclusive end
- TimeTree inclusive end on write

Update writes only explicitly requested semantic fields.

Create / Update / Delete UUID consistency is enforced.

### Recurrence Gates

P5 deliberately blocks:

- recurrence series create/update/delete until P6
- recurrence exception writes/deletes until P7

Opening the P6 series gate must not open the P7 exception gate.

### P5 Unit Tests

14 / 14 PASS

Coverage includes:

- calendar listing / client reuse
- sanitized P1 fixture → P3 normalization
- ISO → Unix ms conversion
- `type=0` preservation
- incremental UUID dedupe
- malformed payload fail-safe
- tool error mapping
- transport error mapping
- create/update/delete UUID consistency
- all-day inclusive TimeTree write
- semantic partial update body
- P6 series gate
- P7 exception gate
- explicit credential-only MCP environment

### P5 Live E2E / CRUD

Live probe:

`tests/p5/live_timetree_client_probe.py`

Confirmed TRUE:

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

Live test artifact was removed.

P5 Technical Gate:

PASS

## Repository Hygiene Fix

Checkpoint:

`ba25c5c fix: clean invalid tests package marker`

The inherited `tests/__init__.py` contained non-Python text.

It was emptied and revalidated.

Post-fix:

- `python -m compileall -q bridge tests`: PASS
- Ruff over bridge + P2-P5: PASS
- `git diff --check`: PASS
- worktree: clean

## Final Verification at P5 Completion

- P2: 14 / 14 PASS
- P3: 35 / 35 PASS
- P4: 17 / 17 PASS
- P5: 14 / 14 PASS
- Ruff: PASS
- Python compile: PASS
- diff check: PASS
- P5 TimeTree live CRUD: PASS
- worktree after checkpoint: clean
- pushed to `origin/main`

## Security / Secrets

- TimeTree password is not stored in the repository.
- Google private credentials are not stored in the repository.
- Secret values must not be logged.
- Local environment variables may be kept in the active shell intentionally for live development, but must never be committed or copied into fixtures / logs / PROJECT_STATE.

Known dependency debt from P0:

TimeTree-MCP `npm audit` reported:

- 2 low
- 2 moderate
- 2 high

No automatic audit fix has been applied.

Review remains required before final operation / release.

## P6 — NEXT: Recurrence Series Core

Goal:

Safely enable recurrence SERIES handling without opening generic recurrence EXCEPTION writes.

P6 scope:

1. Series Read
2. Series Create
3. Series Rule Update
4. Recurrence Removal
5. Series Delete

Contracts to verify / implement:

- weekly RRULE
- INTERVAL
- UNTIL
- COUNT
- all-day recurrence
- recurrence timezone semantics
- EXDATE
- RDATE
- EXRULE

Rules:

- Do not infer unsupported recurrence syntax.
- Only contracts confirmed by normalization + live round-trip may become writable.
- RDATE / EXRULE remain unsupported unless P6 evidence proves the exact TimeTree ↔ Google contract.
- EXDATE may use the already confirmed basic contract, but P6 must verify series write behavior end-to-end.
- Series and exception identities must remain separate.
- P7 exception gate remains closed throughout P6.

## P6 Entry Gate

Before writing recurrence series:

- inspect current P3 recurrence canonicalization
- inspect current P4 Google recurrence write gate
- inspect current P5 TimeTree recurrence write gate
- define exact supported recurrence subset
- add fixture/unit tests first
- run recurrence live tests only with disposable test events
- guarantee cleanup
- do not test generic recurrence exceptions yet

## Next Action

Start P6 Recurrence Series Core.

Recommended order:

1. Re-read Requirements / Basic Design / Detailed Design / Implementation Plan recurrence sections.
2. Inspect current P3/P4/P5 recurrence implementation.
3. Define P6 supported-series contract and fail-safe unsupported contract.
4. Add P6 unit fixtures/tests.
5. Implement series write enablement.
6. Run P2-P6 regression.
7. Run disposable live recurrence series E2E.
8. Cleanup.
9. Mark P6 COMPLETE only if the full gate passes.
