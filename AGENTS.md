# Repository Map

## Project

Project: TimeTree × ChatGPT Web Calendar Bridge
Repository: OMRice8383/timetree-sync-action

Primary architecture:

```text
TimeTree
↕
TimeTree-MCP
↕
Python Calendar Bridge
↕
Dedicated Google Calendar "TimeTree Bridge"
↕
ChatGPT Web / Notion
```

Source of Truth: TimeTree

## Mandatory reading order

Before coding or design changes, read:

1. `PROJECT_STATE.md`
2. `timetree取得_要件定義_v0.11.md`
3. `TimeTree取得_基本設計_v0.11.md`
4. `TimeTree取得_詳細設計_v0.10.md`
5. `TimeTree取得_実装計画_v0.9.md`
6. Relevant implementation and tests

The four canonical design documents are not currently present in this
repository. Do not invent them or infer their contents; use
`PROJECT_STATE.md`, the current task prompt, and the implementation/tests.

Authority order:

```text
Requirements > Basic Design > Detailed Design > Implementation Plan > Implementation
```

If implementation and design appear to disagree, re-check the authoritative
source and do not silently make the implementation the specification.

## Current development rule

Current phase / checkpoint: see `PROJECT_STATE.md`.

At this bootstrap point, P0-P6 are complete, P7 is next/in progress, and the
P6 implementation checkpoint is `c3bf348 feat: add P6 recurrence series core`.
Do not redo P6. Generic recurrence exception writes remain CLOSED.

`allow_recurrence_write` authorizes only the confirmed P6 series subset; it does
not authorize recurrence exception writes.

## Critical safety and contract rules

- Unknown or unverified external behavior fails safe.
- Do not infer TimeTree API contracts.
- Live-observed behavior and implemented behavior are separate facts.
- TimeTree canonical event identity is UUID.
- Series identity and exception identity must never be conflated.
- A moved recurrence occurrence must not be identified only by its new start time.
- Generic recurrence exception writes stay closed until the P7 contract is live-confirmed.
- TimeTree raw `parent_id` or `recurring_uuid` currently fails with
  `UNSUPPORTED_RECURRENCE_EXCEPTION`.
- Google and TimeTree exception create/update/delete remain rejected even when
  `allow_recurrence_write=True`.
- Do not silently broaden recurrence support.
- Unsupported recurrence shapes fail safe.
- Live tests use disposable artifacts and guarantee cleanup.
- Do not map `parent_id` to the canonical parent UUID without contract evidence.

Confirmed P6 recurrence subset:

- exactly one `RRULE`
- `FREQ=WEEKLY`
- optional plain `BYDAY`, positive `INTERVAL`, positive `COUNT`, or `UNTIL`
- confirmed `EXDATE` forms only
- `RDATE`, `EXRULE`, and unsupported recurrence shapes fail safe

Confirmed date/time contracts include TimeTree all-day writes as UTC-midnight
dates and semantic equivalence of Google timed UTC/TZID `EXDATE` forms.
See `PROJECT_STATE.md` for the full confirmed contract and evidence.

## Development workflow

```text
Inspect → Contract/design reasoning → Minimal implementation
→ Unit/fixture tests → Static checks → Live test only when gate permits
→ Cleanup → Review → Commit only when explicitly requested
```

Use the existing phase-specific `unittest` commands. Do not treat a manual
probe as product integration or declare a phase complete without its gate.

## Commands and environment

Local repository: `C:\Users\white\dev\timetree-sync-action`
Python: 3.12+
TimeTree-MCP runtime: `C:\Users\white\dev\TimeTree-MCP\dist\index.js`

Before live work, use disposable test data and inspect the exact probe output.
Do not run a live probe automatically when it requires a human TimeTree UI
action.

## Secrets

Never commit, print, or place in fixtures, logs, docs, `PROJECT_STATE.md`, or
`AGENTS.md`:

- TimeTree credentials
- Google service-account private credentials
- OAuth, session, cookie, or CSRF secrets

## Git and agent behavior

- Inspect `git status` before editing.
- Preserve unrelated user changes.
- Do not commit or push unless explicitly requested.
- Do not rewrite completed P0-P6 work without a confirmed regression or design conflict.
- When uncertain, inspect canonical sources, distinguish evidence from hypothesis,
  fail safe, and report the unresolved contract.
- Always run cleanup for disposable live artifacts and report cleanup status.


<claude-mem-context>
# Memory Context

# [timetree-sync-action] recent context, 2026-08-21 12:23pm GMT+9

No previous sessions found.
</claude-mem-context>