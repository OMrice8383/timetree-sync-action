# PROJECT_STATE

Last Updated: 2026-08-20

## Current Phase

P0 Baseline / Dependency Freeze: COMPLETE
P1 Live Contract Discovery: NOT STARTED

## Canonical Design

Priority:

Requirements
→ Basic Design
→ Detailed Design
→ Implementation Plan
→ Implementation

Current implementation order follows:
TimeTree取得_実装計画_v0.9.md

## P0 Baseline

### Main Repository

Upstream:
porinpi-JAPAN/timetree-sync-action

Fork:
OMrice8383/timetree-sync-action

Upstream baseline SHA:
6beddee66b4d70ad970de029e2440852f9c85de0

Baseline worktree at P0 verification:
clean

### Environment

Python:
3.12.13

uv:
0.11.8

Node.js:
24.14.1

npm:
11.11.0

Git:
2.54.0.windows.1

GitHub CLI:
2.97.0

OpenCLI:
1.8.6

### Python Dependencies

MCP Python SDK:
mcp==2.0.0

TimeTree-Exporter:
timetree-exporter==0.8.0

google-api-python-client:
2.198.0

google-auth:
2.56.3

requests:
2.34.2

ruff:
0.16.3

### TimeTree-MCP

Repository:
ehs208/TimeTree-MCP

Version:
0.3.0

Pinned SHA:
64c8188eac103c0d8e8726b5fc5b2c31f961f3a5

Runtime entrypoint:
dist/index.js

Node requirement:
>=18.0.0

P0 verification:
- npm ci: PASS
- npm run build: PASS
- npm run typecheck: PASS
- npm test: PASS
- tests: 11 passed / 0 failed
- dist/index.js: confirmed

Security baseline:
npm audit reports 6 vulnerabilities:
- 2 low
- 2 moderate
- 2 high

High findings are currently transitive dependencies.
No audit fix has been applied.
This is recorded security debt and must be reviewed before final operation/release.

### Original Repository Checks

Official CI equivalent:
ruff check src
PASS

Python compile:
PASS

Repository-wide ruff check:
FAIL due to upstream tests/__init__.py containing non-Python text.

The upstream GitHub Actions lint target is src only.

Existing automated test coverage in the original repository:
effectively none.

### Existing Architecture Findings

Current Google client:
- google-api-python-client
- Service Account authentication
- list_events uses singleEvents=True
- update uses events.update()

Current TimeTree path:
- TimeTree-Exporter internal API is used directly

Current sync:
- primarily TimeTree -> Google
- no V1 SQLite state model
- no V1 bidirectional incremental sync
- no V1 conflict model
- no V1 crash recovery model
- no V1 three-way verify

V1 direction remains:

TimeTree
↕
TimeTree-MCP (Primary)
↕
Python Calendar Bridge
↕
Dedicated Google Calendar
↕
ChatGPT Web

TimeTree-Exporter remains an independent read verification path.

## P0 Live Verification

No TimeTree or Google live E2E was performed in P0.

Live TimeTree contract discovery begins in P1.

## Security

No TimeTree credentials were stored in the repository.

No Google service-account secret was stored in the repository.

No TimeTree password, cookies, CSRF token, or Google private key was pasted into ChatGPT.

## Unverified / Deferred to P1+

Not yet verified:

- MCP protocol connection with real TimeTree credentials
- list_calendars real payload
- get_events real payload
- get_updated_events real payload
- TimeTree Event UUID identity
- Exporter identity correspondence
- event classification
- all-day contract
- start/end timezone contract
- recurrence contract
- recurrence exception contract
- TimeTree deletion contract
- Google live contract

These must not be guessed during implementation.

## Next

P1 Live Contract Discovery.

Start read-only.

Initial TimeTree-MCP tools:

1. list_calendars
2. get_events
3. get_updated_events

Secrets stay only on the local PC.


