# TimeTree × ChatGPT Web Calendar Bridge｜詳細設計 v0.11

## 0. 文書の位置づけ

本書は以下を実装可能な粒度へ具体化する。

- `timetree取得_要件定義_v0.12.md`
- `TimeTree取得_基本設計_v0.12.md`

優先順位：

```text
要件定義
↓
基本設計
↓
詳細設計
↓
実装計画
↓
実装
```

実装順の正本は実装計画とし、本書ではAlgorithm / Data / Interfaceを定義する。

---

# 1. V1基本原則

## 1.1 Source of Truth

```text
最終保管先 = TimeTree
変更入口   = TimeTree / Google
```

## 1.2 Calendar Pair

V1：

```text
1 TimeTree shared calendar
↕
1 Google Calendar「TimeTree Bridge」
```

## 1.3 Bootstrap前提

Google専用Calendarは原則空。

未管理Eventが存在する場合はBootstrapを停止する。

---

# 2. Runtime

V1は短命Process。

```text
Windows Task Scheduler
↓
python -m bridge tick
```

常駐Daemonにしない。

Task Scheduler側とApplication側Run Lockの二重防止を行う。

Run Lock：

```text
state/bridge.lock

pid
started_at
```

Processが存在しない古いLockは回収可能にする。

---

# 3. Repository方針

物理Folder / File構造はP0の実Fork調査で確定する。

詳細設計として固定するのは責務だけとする。

```text
Config / Logging
Normalized Event / EventChange
TimeTree Adapter / MCP Client
Google Adapter / Incremental Client
Sync Engine / Conflict / Reconcile / Recovery
SQLite Repository / Migration
Exporter Verification Adapter
OpenCLI Diagnostic Adapter
CLI
Tests / Fixtures
```

既存`timetree-sync-action`の動作構造を不必要に全面移行せず、既存I/Oを再利用して必要な責務だけ最小追加する。

OpenCLI TimeTree Adapter本体はOpenCLI側の適切なAdapter配置へ実装する。

# 4. Config

`config/bridge.toml`

```toml
[bridge]
version = "0.1"
default_timezone = "Asia/Tokyo"

[timetree]
calendar_id = "..."
incremental_interval_seconds = 300
overlap_seconds = 30

[labels]
sync_names = ["大河予定", "共通予定"]
google_new_default = "大河予定"
test_artifact_label = "大河予定"

[google]
calendar_id = "..."
incremental_interval_seconds = 60

[reconcile]
interval_seconds = 3600

[verify]
interval_seconds = 86400

[exporter]
calendar_code = "..."

[state]
database = "state/calendar.db"

[logging]
path = "logs/bridge.jsonl"
```

`[timetree].calendar_id`と`[exporter].calendar_code`は別概念として保持する。

P1で同じCalendarについて、

```text
TimeTree-MCP calendar_id
TimeTree-Exporter calendar_code
```

の対応を実機確認して保存する。

SecretはRepo外。

例：

```text
TIMETREE_EMAIL
TIMETREE_PASSWORD
GOOGLE_SERVICE_ACCOUNT_FILE
```

# 5. MCP Python SDK v2 Client

## 5.1 正式なstdio構成

`StdioServerParameters`を直接`Client`へ渡さない。

```text
StdioServerParameters
↓
stdio_client(...)
↓
Client(...)
↓
TimeTree-MCP subprocess
```

概念：

```python
server = StdioServerParameters(
    command="node",
    args=[...],
    env={
        "TIMETREE_EMAIL": "...",
        "TIMETREE_PASSWORD": "..."
    }
)

async with Client(stdio_client(server)) as client:
    ...
```

`env=`は必須のCredentialだけを明示的に渡す。

子Processが親ProcessのSecret環境変数をすべて暗黙継承する前提にしない。

1 `tick`内では同じClient接続を使い回し、EventごとにMCP Serverを再起動しない。

# 6. TimeTree MCP Client Boundary

# 6. TimeTree MCP Client Boundary

使用Tool：

```text
list_calendars
get_calendar_labels
get_events
get_updated_events
create_event
update_event
delete_event
```

## Calendar ID

Bridge内部は文字列Canonical。

Tool仕様上Writeで数値が必要な場合だけBoundaryで変換する。

数値化不能ならConfiguration Error。

## 日時

Read：

```text
ISO 8601
↓
aware datetime / date
```

Write：

```text
aware datetime / date
↓
Unix ms等、Toolが要求する形式
```

CoreへTimeTree固有表現を漏らさない。

## `deactivated_at`

P1では、

> `deactivated_at`が整形済みTool出力へ露出するか

を確認する。

静的調査上は`get_updated_events`の通常出力へ露出しない想定とする。

削除契約の本調査はDelete Live E2Eで行う。

---

# 7. NormalizedEvent / Change Model

通常Eventの完全な意味表現は`NormalizedEvent`を使う。

```python
NormalizedEvent
├─ source
├─ source_calendar_id
├─ source_event_id
│
├─ kind
├─ parent_source_event_id
├─ original_start
│
├─ title
├─ all_day
├─ start
├─ end
├─ start_timezone
├─ end_timezone
├─ description
├─ location
├─ label
│
├─ recurrence
└─ updated_at
```

独自`id`は持たない。

`created_at`はV1 Sync / Conflict / Hash判定で利用しないためCanonical Modelへ含めない。

Incremental APIから届く削除通知はtitle / start / end等が欠落し得るため、完全な`NormalizedEvent`を必須にしない。

差分取得層では概念的に次の`EventChange`を使う。

```text
EventChange
├─ change_type
│   ├─ UPSERT
│   ├─ DELETE
│   └─ RECURRENCE_EXCEPTION_DELETE
├─ source_event_id
├─ parent_source_event_id?
├─ original_start?
└─ event?  # UPSERT時のNormalizedEvent
```

通常削除は最小IDだけでも扱える。

Recurring cancelled exceptionは、

```text
source_event_id
parent_source_event_id
original_start
```

をすべて保持し、Series全体削除と区別する。

削除状態は、

```text
変更通知
→ EventChange

永続状態
→ event_links.status = deleted
```

へ分離し、`NormalizedEvent.deleted`という第二の削除表現を持たない。

## Identity

TimeTree由来EventのCanonical Identity：

```text
source_event_id = TimeTree Event UUID
```

同じUUIDを、

```text
event_links.timetree_event_id
Google extendedProperties.private.timetree_id
```

にも保持する。

TimeTree内部の別`id`が存在しても、V1のCross-system Identityへ混在させない。

P1で、

```text
TimeTree-MCP uuid
TimeTree-Exporter側の対応UID / UUID
既存Forkのid
```

の対応を記録し、`verify`で同一Eventを正しく照合できることを確認する。

## kind

```text
single
series
exception
```

## 時刻型

Raw Adapter入力ではTimezone Fieldが欠落する可能性がある。

Normalization完了後は、

```text
all_day=false
→ start/end = timezone-aware datetime
→ start_timezone/end_timezone = str
→ effective timezone確定済み

all_day=true
→ start/end = date
→ start_timezone = None
→ end_timezone = None
```

とする。

all-dayではTimezone差を意味的変更として扱わない。

# 8. Canonical Time Semantics

## timed

```text
start = inclusive
end   = exclusive
```

## all-day

```text
start_date = inclusive
end_date   = exclusive
```

Bridge内部はend exclusiveへ統一。

---

# 9. TimeTree Adapter

## 9.1 Read対象

```text
uuid
title
start_at
end_at
all_day
start_timezone
end_timezone
note
location
label_id
recurrences
updated_at
category
type
```

削除関連Fieldは存在・露出状況をLiveで確認する。

## 9.2 Event Identity

TimeTree AdapterはMCP `uuid`を、

```text
NormalizedEvent.source_event_id
```

へ入れる。

既存Forkが別の内部`id`を使っている場合も、Adapter境界でUUIDへ統一する。

## 9.3 Event Classification

V1の自動同期対象は通常Calendar Eventだけ。

Boolではなく3状態へ分類する。

```text
classify_event(event)

SYNC
→ 通常Calendar Event

IGNORE_KNOWN
→ Memo / Birthday等、既知の意図的対象外

UNSUPPORTED
→ 未知 / 意味未確定category・type
```

`IGNORE_KNOWN`は理由を記録してSKIPする。

`UNSUPPORTED`は通常予定としてGoogleへ作成せず、Bootstrap / Reconcileの自動修復では安全側へ停止する。

同じ分類規則を、

```text
Incremental
Full Snapshot
Bootstrap
Reconcile
Verify
```

で共通利用する。

Google側も`eventType=default`をSYNCとし、特殊Eventは通常予定として同期しない。

### TimeTree Label Resolution / Scope

V1で同期対象にするLabel名：

```text
大河予定
共通予定
```

`label_id`はTimeTree Calendar固有のRemote値なので、Source of Truthとしてハードコードしない。

Calendar Bridgeは`get_calendar_labels`を使い、起動時・`doctor`・Bootstrap前に、

```text
大河予定 → runtime label_id
共通予定 → runtime label_id
```

を一意に解決する。

必須条件：

```text
各Label名がちょうど1件存在
→ PASS

対象Label欠落
同名Label複数
Eventのlabel_idがLabel一覧に存在しない
label_id欠落
→ UNSUPPORTED_TIMETREE_LABEL
→ 自動Write停止
```

Event Classificationはcategory/type判定後にLabel Scopeを適用する。

```text
SYNC可能な通常Event
+
label in {大河予定, 共通予定}
→ SYNC

SYNC可能な通常Event
+
その他の実在Label
→ IGNORE_KNOWN
→ LABEL_OUT_OF_SCOPE
```

Calendar Label Catalogに存在するLabel IDが、runtimeで一意解決済みの
`大河予定` / `共通予定`のどちらのIDにも一致しない場合は、Label名が空または
`null`であっても、Label名を推測せず`LABEL_OUT_OF_SCOPE`として
`IGNORE_KNOWN`に分類する。

`label_id`欠落、Catalogに存在しないID、または`大河予定` / `共通予定`自体を
一意に解決できない場合は`UNSUPPORTED`としてsafe-stopする。

Normalized Eventの`label`には数値IDではなくLabel名を入れる。

TimeTreeへのCreate / Label変更Write時だけ、Normalized `label`をruntime `label_id`へ変換する。
既存TimeTree EventのUpdateで`label`が変更対象に含まれない場合、`label_id`を送信せずRemote既存値を保持する。

Live Test Artifactは原則`大河予定`で作成する。

これによりMCP / Exporter / Googleの対象Event差を`verify`の誤Mismatchにしない。

## 9.4 all-day

現時点の仕様前提：

```text
TimeTree end = inclusive
```

P1でsingle-day / multi-dayを実確認する。

TimeTree-MCPがall-dayをISO timestampで返す場合は、単純にUTC表示の`.date()`を取らない。

```text
ISO timestamp
+
TimeTree側timezone（有効値。欠落時はdefault）
↓
TimeTreeのLocal Calendar Dateへ変換
↓
date化
```

その上で、契約がinclusive endなら、

```text
normalized_end = timetree_end_date + 1 day
```

逆方向：

```text
timetree_end = normalized_end - 1 day
```

とする。

異なる実契約なら、本節・Fixture・Testを実機結果へ更新する。

## 9.5 Timezone

Timed Eventでは、

```text
start_timezone
end_timezone
```

をそれぞれ保持する。

**NormalizedEventにはHash / Writeで使うeffective timezoneを入れる。**

規則：

```text
sourceに有効なIANA timezoneあり
→ その値

欠落 / 不正
→ 欠落側だけconfig.default_timezoneを決定的Fallbackとして採用
→ WARN記録
```

片側欠落時に、もう片側Timezoneを無条件コピーしない。

このFallbackはWrite直前だけで行わずNormalization時に確定する。

これにより、

```text
Hashに使うTimezone
=
Remote Writeに使うTimezone
```

を保証し、Bridge自身のFallbackを次回Syncで外部変更と誤認しない。

datetime自身のoffset / instantは失わない。

all-dayでは、

```text
start_timezone = None
end_timezone = None
```

へ正規化する。

# 10. Google Adapter

Read：

```text
id
eventType
summary
start
end
description
location
recurrence
status
updated
recurringEventId
originalStartTime
extendedProperties.private
```

V1は`eventType=default`のみ同期対象とする。

特殊Eventが返った場合は通常EventとしてTimeTreeへ書かず、分類結果に従ってignore / unsupportedへ回す。

Google all-day endはそのままNormalizedへ入れる。

Timed eventは`start.timeZone / end.timeZone`をそれぞれ取得し、

```text
start_timezone
end_timezone
```

へ正規化する。

### Google offset-only timed event

Single Eventでは`timeZone`が無く、`dateTime`自身のRFC3339 offsetだけが存在し得る。

```text
timeZoneあり
→ そのIANA timezoneを利用

timeZoneなし + offsetあり
→ config.default_timezoneを候補にし、そのEvent時刻でoffsetが一致するか確認

offset一致
→ default_timezoneをeffective timezoneとして採用

offset不一致 / 意味を一意復元できない
→ UNSUPPORTED_GOOGLE_TIMEZONE
```

offsetを無視して`default_timezone`へ置換してはいけない。

### Google Label Metadata

Bridge管理Eventでは、

```text
extendedProperties.private.timetree_label_name
```

へTimeTree Label名を保持する。

許可値：

```text
大河予定
共通予定
```

Google → TimeTree Normalizationでは、

```text
Bridge管理 / Mapping済みEvent
+
timetree_label_nameあり
→ そのLabelを使用

新規未管理Google Event
+
Label Metadataなし
→ config [labels].google_new_default
→ 大河予定

未知のtimetree_label_name
→ UNSUPPORTED_GOOGLE_LABEL
```

Google Calendar `colorId`、予定タイトル、descriptionからLabelを推測しない。

Mapping済みBridge EventからLabel Metadataが消失した場合は、無条件に`大河予定`へFallbackせず不整合として診断する。
Metadata修復はMapping / TimeTree current stateを安全に照合できる経路でのみ行う。

### Google title validation

`eventType=default`でも`summary`が空 / 欠落なら、TimeTree-MCPのtitle必須契約を満たせない。

```text
summary空 / 欠落
→ UNSUPPORTED_GOOGLE_EMPTY_TITLE
```

としてTimeTreeへ推測タイトルを書き込まない。

---

# 11. Google Sync Query Contract

V1：

```text
singleEvents=false
eventTypes=default
```

をInitial Full Syncから全Incremental Syncまで固定。

Incremental SyncではInitial Full Syncと同じQuery Parameterセットを維持する。

Pagination：

```text
same syncToken
same query parameters
+ pageToken
```

以下は`syncToken`と併用しない：

```text
iCalUID
orderBy
privateExtendedProperty
q
sharedExtendedProperty
timeMin
timeMax
updatedMin
```

削除Eventは`syncToken`利用時に自動的に結果へ含まれるため、

```text
showDeleted=false
```

をIncremental requestへ明示しない。

Initial Full Syncでも、後続Incrementalと矛盾する`showDeleted=false`固定を同期Query Contractへ入れない。

最終Pageで得た`nextSyncToken`だけを保存。

Query Parameterの意味を変更する場合は既存tokenを再利用せずFull Syncから開始する。

# 12. Recurrence

## 12.1 Series Model

V1のBaseline SupportはRRULE。

Normalized：

```text
recurrence = [
  "RRULE:..."
]
```

Google `recurrence[]`はRRULE以外に、

```text
RDATE
EXDATE
EXRULE
```

を含み得る。

Contract DiscoveryではTimeTree-MCP Read / Create / UpdateのRound Tripを種類ごとに確認する。

```text
安全なRound Trip確認済み
→ Supportへ追加しCanonicalizer / Testも追加

未確認 / 欠落 / 意味変換が必要
→ UNSUPPORTED_RECURRENCE_FEATURE
→ 自動Write停止
```

Seriesレベル：

```text
read
create
rule update
recurrence removal
series delete
```

を実装する。

### Recurring Series Timezone

Recurring Seriesは繰り返し展開の基準Timezoneを1つに確定する。

```text
start effective timezone == end effective timezone
→ Support

異なる
→ 実契約で安全性を確認できない限り
   UNSUPPORTED_RECURRENCE_TIMEZONE
```

GoogleへWriteするRecurring Eventでは、この基準Timezoneをstart / endの両方へ明示する。

## 12.2 Exception Contract Gate

Bootstrapより前にTimeTree側の、

```text
one occurrence update
one occurrence delete
```

をLive確認する。

最初に、

> Exceptionが存在すること自体を、Primaryまたは独立Read経路で確実に検出できるか

を確認する。

Google：

```text
recurringEventId
originalStartTime
```

TimeTreeは実Payloadで確認。

### A. 存在検出 + 一意Mapping可能

```text
kind = exception
parent_source_event_id
original_start
```

を使用。

### B. 存在は検出可能だが安全にMapping不可

```text
event / series status = unsupported
diagnostic error = UNSUPPORTED_RECURRENCE_EXCEPTION
```

として当該Seriesへの自動Writeを停止する。

### C. MCPでは存在検出不可

TimeTree-Exporter等の独立ReadでException存在を検出可能か確認する。

独立Readで存在検出できるなら、**Contract Discovery / Bootstrap安全検査だけ**の補助に使える。

MCP / Exporter等の双方で存在検出保証を作れない場合は、

```text
P7 FAIL
```

とし、「安全停止できる」とはみなさない。

未対応Exceptionを含むCalendarを予定欠落のままBootstrap完了扱いにしない。

Series全体へ誤反映しない。

# 13. Hash

## 13.1 対象

```text
title
start
end
start_timezone
end_timezone
all_day
description
location
label
recurrence
kind
parent_source_event_id（exception時）
original_start（exception時）
```

削除は`EventChange` / Tombstoneで表現し、通常Event Hashへ`deleted`を入れない。

all-dayでは、

```text
start_timezone = ""
end_timezone = ""
```

としてHashし、Timezone差を意味的変更にしない。

## 13.2 Canonicalization

Timed datetime**成分**は、同じ瞬間なら同じCanonical値になるようUTCの固定表現へ変換する。

ただしEvent全体Hashにはeffective timezone Fieldも別に含めるため、

```text
同じ瞬間 + 同じeffective timezone
→ 同じEvent Hash

同じ瞬間 + effective timezone変更
→ Event Hashは変更
```

となる。

推奨：

```text
aware datetime
↓
UTC
↓
Unix epoch milliseconds
```

または同等の固定精度UTC表現を使用する。

Canonicalization：

```text
None → ""
timed datetime → UTC epoch milliseconds
all-day date → YYYY-MM-DD
timed timezone → Normalizationで確定済みeffective IANA string
all-day timezone → ""
label → approved exact name (`大河予定` / `共通予定`)
newline → \n
```

### Recurrence Canonicalization

RRULEは単に行順をsortするだけではなく、parseして意味的にCanonical化する。

RDATE / EXDATE / EXRULEをV1 Supportへ追加する場合も、同じく意味的Canonicalization規則とTestを追加してから有効化する。

最低限：

```text
property名 → uppercase
RRULE key → stable order
BYDAY等の順序非依存list → stable order
不要な空白除去
意味が同じdefault表現は同じCanonical結果
複数recurrence line → canonicalized lineをstable order
```

例：

```text
RRULE:FREQ=WEEKLY;BYDAY=MO;INTERVAL=1

RRULE:INTERVAL=1;BYDAY=MO;FREQ=WEEKLY
```

が同一意味なら同一Hashになること。

Canonical JSON：

```text
sort_keys=true
UTF-8
```

SHA-256。

Google固有非同期Fieldは入れない。

`last_synced_hash`をConflict判定の唯一の同期基準点とする。

# 14. SQLite

## 14.1 event_links

```sql
CREATE TABLE event_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    timetree_event_id TEXT UNIQUE,
    google_event_id TEXT UNIQUE,

    timetree_parent_event_id TEXT,
    google_parent_event_id TEXT,

    event_kind TEXT NOT NULL DEFAULT 'single',

    last_synced_hash TEXT,

    status TEXT NOT NULL,

    last_synced_at TEXT,
    deleted_at TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

status：

```text
synced
conflict
deleted
error
unsupported
```

Write途中状態は`sync_operations`だけで管理し、`event_links.status=pending`という第二の途中状態を作らない。

片側ごとの現在Hashは同期時に計算する。
`last_timetree_hash / last_google_hash`はV1必須Schemaから外し、同期判定を`last_synced_hash`へ一本化する。

---

## 14.2 sync_state

```sql
CREATE TABLE sync_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
```

正式Key：

```text
google_sync_token
timetree_updated_after_ms

last_google_sync_at
last_timetree_sync_at
last_mcp_reconcile_at
last_exporter_verify_at

bridge_bootstrapped_at
```

---

## 14.3 sync_operations

```sql
CREATE TABLE sync_operations (
    operation_id TEXT PRIMARY KEY,

    direction TEXT NOT NULL,
    action TEXT NOT NULL,

    source_event_id TEXT,
    target_event_id TEXT,

    source_hash TEXT,
    payload_hash TEXT,

    state TEXT NOT NULL,

    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

state：

```text
prepared
remote_applied
mapping_saved
done
failed
```

`source_hash`：

```text
Operationを開始したSource側Normalized EventのHash
```

`payload_hash`：

```text
Remoteへ送る同期対象PayloadをCanonical化したHash
```

Crash Recovery時の「同じ操作か」「送信内容が変わっていないか」の照合に使う。

一意復旧不能：

```text
state = failed
last_error = NEEDS_MANUAL_RECOVERY
```

`needs_manual_recovery`という未定義Stateは作らない。

---

## 14.4 conflicts

```sql
CREATE TABLE conflicts (
    conflict_id TEXT PRIMARY KEY,

    event_link_id INTEGER,
    conflict_type TEXT NOT NULL,

    timetree_snapshot_json TEXT,
    google_snapshot_json TEXT,

    status TEXT NOT NULL,
    resolution TEXT,

    created_at TEXT NOT NULL,
    resolved_at TEXT
);
```

# 15. Google Metadata

```text
extendedProperties.private

sync_source = "timetree-chatgpt-bridge"
timetree_id
timetree_label_name
bridge_version
```

`timetree_id`には**TimeTree Event UUID**を保存する。

`timetree_label_name`には`大河予定`または`共通予定`を保存する。
TimeTreeの数値`label_id`はCross-system Canonical Metadataとして保存しない。

必要に応じてException親IDも追加。

SQLiteがPrimary Mapping。

Google Metadataは復旧・Debug用Backup。

# 16. Bootstrap

正式CLI：

```text
python -m bridge bootstrap
```

前提：

- P1でTimeTree Event UUID / Event種別 / all-day / Timezone契約確認済み
- Recurrence Series Adapter / Unit Test完了
- Recurrence Exception契約確認済み
- Exception未対応時の安全停止判定が実装済み
- TimeTree Label契約確認済み
- `大河予定` / `共通予定`をruntimeで一意解決可能
- Google専用Calendarが空
- doctorの必須項目PASS
- Bootstrap Create PathのCrash Recovery Test PASS

処理：

```text
1. doctor
2. target Calendar確認
3. Google空確認
4. bootstrap_started_ms = now を記録
5. TimeTree Full Snapshot
6. Calendar Label一覧取得・`大河予定` / `共通予定`を一意解決
7. Event Classification + Label Scope Classification
8. Unsupported / 未検出保証Exception検査
9. 問題があればWrite前にABORT
10. Normalize
11. crash-safe Google Create
12. Mapping保存
13. Google Full List再取得
14. Bootstrap Consistency Check
15. nextSyncToken保存
16. timetree_updated_after_ms = bootstrap_started_ms を保存
17. bootstrapped_at保存
```

TimeTree watermarkをBootstrap終了時刻へ進めない。

Bootstrap中にTimeTreeで発生した変更は、次回Incrementalで再取得される。

`Bootstrap Consistency Check`はRead-onlyで、

```text
eligible event count
TimeTree UUID
Label（大河予定 / 共通予定）
Google metadata timetree_id
Google metadata timetree_label_name
Mapping presence
Normalized Hash
```

を確認する。

この時点ではP11で実装する本格的な`reconcile` / 3者`verify`を呼ばない。

Bootstrapで部分的にGoogleへCreateした後に失敗しても重複しないよう、Createは`sync_operations`を利用してRecovery可能にする。

# 17. `tick`

```text
python -m bridge tick
↓
run lock
↓
config / DB
↓
recover pending operations
↓
Google incremental
↓
必要なら TimeTree incremental
↓
必要なら MCP Full Reconcile
↓
必要なら Exporter Verify
↓
status更新
↓
unlock
```

失敗した工程に依存する後続Writeを無条件で続けない。

---

# 18. Google → TimeTree Incremental

## Read

```text
events.list(
    syncToken=...,
    singleEvents=false,
    ...same allowed query parameters
)
```

最終Page成功後だけtoken更新。

## 410

Google Client層は410を検出したら、

```text
tokenをinvalid扱い
↓
FULL_RESYNC_REQUIREDをSync Engineへ返す
```

までを責務とする。

完全復旧はReconcile層で、

```text
Google Full Snapshot
↓
SQLite Mapping / TimeTree SnapshotとReconcile
↓
new nextSyncToken
```

を行う。

`event_links`は無条件全削除しない。

## New Google Event

```text
Google new
↓
Normalize
↓
operation prepared
↓
TimeTree create
↓
remote_applied
↓
event_links
↓
mapping_saved
↓
Google metadata patch
↓
hash確定
↓
done
```

## Google Update Conflict Guard

TimeTree側の直近変更をWrite直前に確認。

対象Batchについて、

```text
oldest last_synced_at - overlap
```

から`get_updated_events`を1回実行。

TimeTree Hashも変化していればConflict。

---

# 19. TimeTree → Google Incremental

State：

```text
timetree_updated_after_ms
```

Query：

```text
saved watermark - overlap
```

Run開始時刻を記録し、全処理成功時だけwatermarkを進める。

Overlap再取得はHashでSKIP。

TimeTree変更時はGoogle currentを取得：

```text
Google current hash == last_synced_hash
→ Google patch

!=
→ conflict
```

---

# 20. Create Crash Recovery

Remote Write前に必ずOperationを保存。

## Google → TimeTree

Crash後：

```text
pending operation
↓
Remote照合
↓
一意Eventあり
→ Mapping復旧 → metadata → done

一意確認不可
→ state=failed
→ last_error=NEEDS_MANUAL_RECOVERY
```

## TimeTree → Google

Create Payloadへ最初から、

```text
timetree_id
sync_source
timetree_label_name
```

を付与し、Crash後にGoogle側から検索しやすくする。

## Sync Commit Rule

Remote Write成功だけで`last_synced_hash`を確定しない。

```text
Remote Write
↓
Write ResponseまたはGETしたTarget Event
↓
Target Adapterで再Normalize
↓
Target Hash計算
↓
Sourceの意図したCanonical Hashと一致確認
```

一致した場合だけ、

```text
event_links.last_synced_hash = Target Hash
status = synced
```

へ進める。

Server側の正規化等によりHashが一致しない場合は、同期済み扱いにせず再読込 / 診断へ回す。
これにより「送ったPayload」ではなく「Remoteに実際に保存された意味」を同期基準点にする。

---

# 21. Update

Google：

```text
events.patch()
```

同期対象Fieldだけ更新。

TimeTree：

```text
update_event
```

へ変更Fieldだけ渡す。

`label`変更時はNormalized Label名をruntime `label_id`へ解決して送る。
`label`が変更対象でないUpdateでは`label_id`を送らず既存Labelを保持する。

---

# 22. Delete

## Google → TimeTree

Googleの`status=cancelled`はEvent種別で分岐する。

### A. Cancelled Recurrence Exception

```text
status = cancelled
recurringEventIdあり
originalStartTimeあり
```

の場合、

```text
EventChange.change_type = RECURRENCE_EXCEPTION_DELETE
source_event_id = id
parent_source_event_id = recurringEventId
original_start = originalStartTime
```

として扱う。

Series全体をTimeTreeから削除してはいけない。

TimeTree側Exception contractが対応可能ならその1回だけ削除する。
未対応なら当該SeriesをUnsupportedとして安全停止する。

### B. 通常Event Delete

`recurringEventId`を持たない通常cancelled Eventは、最終的に`id`しか保証されない前提で扱う。

```text
EventChange.change_type = DELETE
source_event_id = Google Event ID
```

SQLite MappingでTimeTree IDを解決する。

TimeTree未変更：

```text
delete_event
↓
status=deleted
↓
deleted_at
```

TimeTree変更済み：

```text
delete_update conflict
```

## TimeTree → Google

Fast Delete契約をLive E2E。

P1では`deactivated_at`の露出有無だけ確認し、削除契約の結論はここで出す。

判定：

```text
A. Tool出力で一意検出
→ Fast Delete

B. Raw APIにあるがformatterが落としている
→ MCP最小Patch検討

C. 一意検出不可
→ Hourly Full Reconcile
```

Tombstone保持規則：

```text
通常Event Delete
→ 30日程度

Recurring Exception Delete
→ 親Seriesが存在する間
```

親Seriesが削除され、そのSeriesに属するException Tombstoneが不要になった時点でまとめてCleanup可能。

Recurring Exception Tombstoneを30日で消して削除済みInstanceを復活させてはいけない。

---

# 23. Conflict

判定：

| TimeTree | Google | Action |
|---|---|---|
| unchanged | unchanged | skip |
| changed | unchanged | TT → Google |
| unchanged | changed | Google → TT |
| changed | changed | conflict |
| deleted | unchanged | delete propagate |
| unchanged | deleted | delete propagate |
| deleted | changed | conflict |
| changed | deleted | conflict |
| deleted | deleted | deleted収束 |

Conflict時：

```text
event_links.status=conflict
conflictsへ両Snapshot保存
```

解決CLI：

```text
python -m bridge conflicts
python -m bridge resolve <id> --winner timetree
python -m bridge resolve <id> --winner google
```

---

# 24. Hourly Reconcile

比較：

```text
TimeTree-MCP Full Snapshot
Google Full Snapshot
SQLite event_links
```

安全に修復可能：

- TimeTree新規 → Google Create
- Google新規 → TimeTree Create
- Google metadataあり / Mapping欠落 → Mapping復旧
- Mappingあり / Metadata欠落 → 条件付きMetadata復旧

自動修復しない：

- 両側変更
- 片側消失 + 相手変更
- duplicate
- unknown recurrence exception

---

# 25. `verify`

正式CLI：

```text
python -m bridge verify
```

Read-only。

Raw形式を直接比較しない。

```text
TimeTree-MCP
↓ TimeTree Adapter

TimeTree-Exporter ICS
↓ Exporter Verification Adapter

Google
↓ Google Adapter

Canonical Verify Event
↓
比較
```

all-dayのinclusive/exclusive差、Timezone表現、RRULE property順などをCanonical化した後で比較する。

比較対象：

```text
TimeTree-MCP
vs
TimeTree-Exporter
vs
Google + SQLite Mapping
```

Event Classificationの扱い：

```text
SYNC
→ 通常比較

IGNORE_KNOWN
→ 意図的対象外として件数比較から除外し、必要ならskip件数を表示

UNSUPPORTED
→ 無視せずVERIFY_UNSUPPORTEDとして報告
→ V1の通常運用では要調査 / FAIL判定対象
```

Event Identity：

```text
TimeTree-MCP UUID
↔ TimeTree-Exporter側の対応UID / UUID
↔ SQLite timetree_event_id
↔ Google metadata timetree_id
```

P1でExporterのUID/UUID対応と`calendar_code`を実機確認し、同一Eventを安定して照合できることをGateとする。

SYNC Eventの最低比較：

```text
count
canonical event identity
title
start
end
all_day
label
mapping presence
metadata presence
recurrence（取得可能な範囲）
```

Recurrence Exception対応時：

```text
MCP ↔ Google / SQLite
→ kind
→ parent identity
→ original_start
```

を検証する。

TimeTree-Exporterは、現行OSSが実際に表現できるRecurrence情報だけを独立比較する。
Exporterが表現しない`original_start`等をV1 FAIL条件にしない。

修復しない。

# 26. Retry

Retry：

```text
timeout
network
429
5xx
temporary subprocess failure
```

原則Retryしない：

```text
400 validation
401/403 auth
404 mapping inconsistency
unsupported recurrence
```

Backoff：

```text
1s + jitter
2s + jitter
4s + jitter
```

`Retry-After`優先。

---

# 27. Logging

JSONL。

通常は、

```text
timestamp
run_id
component
direction
action
event IDs
hash
result
```

中心。

title / descriptionは原則記録しない。

Secret Redaction必須。

---

# 28. `doctor`

正式CLI：

```text
python -m bridge doctor
```

各Checkは、

```text
REQUIRED
WARN
NOT_IMPLEMENTED
```

の性質を持てるようにする。

## Core動作を停止するREQUIRED

```text
Config
Python / Node
MCP Client transport
TimeTree-MCP connection / protocol compatibility
TimeTree auth / target calendar
TimeTree labels: `大河予定` / `共通予定`が各1件存在し一意解決可能
Google auth / target calendar / writer permission
SQLite
```

## 実装成熟度によりWARN / REQUIREDが変わるもの

```text
Exporter
OpenCLI Base
Browser Bridge
TimeTree Browser login
TimeTree OpenCLI Adapter
```

Bootstrap時点ではExporterのContract確認は済んでいる必要があるが、日次`verify`機能そのものが未実装でも、それだけを理由にBootstrapを止めない。

OpenCLI Adapter実装前は、

```text
OpenCLI Base = OK
TimeTree Adapter = NOT_IMPLEMENTED
```

を正常な途中状態として扱う。

`doctor`全体を単純な1個のPASS/FAILにせず、**現在の実装段階で必須なREQUIREDが全てOKか**をGate判定に使う。

# 29. `status`

```text
python -m bridge status
```

表示：

```text
last Google sync
last TimeTree sync
last MCP reconcile
last Exporter verify

open conflicts
pending / failed operations

mapped events
deleted tombstones

Google sync token present?
TimeTree watermark
```

`--json`対応。

---

# 30. CLI正式表記

V1の正本：

```text
python -m bridge bootstrap

python -m bridge tick
python -m bridge sync

python -m bridge sync-google
python -m bridge sync-timetree
python -m bridge reconcile
python -m bridge verify

python -m bridge doctor
python -m bridge status
python -m bridge conflicts
python -m bridge resolve <id> --winner <timetree|google>
python -m bridge recover
```

`sync`は`tick`の手動alias。

将来console scriptを追加してもよいが、V1文書では`python -m bridge`へ統一する。

---

# 31. Test設計

## Unit

### Normalizer

```text
timed TT → Normalized
timed Google → Normalized
single-day all-day
multi-day all-day
inclusive/exclusive
TimeTree local-date extraction
Asia/Tokyo
America/Los_Angeles
DST境界
Raw timezone missing → effective timezone確定
Google offset-only + matching default timezone
Google offset-only + mismatching default → unsupported
Google empty summary → unsupported
TimeTree label 大河予定 → SYNC
TimeTree label 共通予定 → SYNC
TimeTree その他の実在Label → IGNORE_KNOWN
TimeTree label欠落 / 解決不能 → unsupported
Google new event label metadataなし → 大河予定
Google unknown label metadata → unsupported
Recurring start/end timezone mismatch → unsupported
```

### Hash

```text
same instant different UTC offset → same time hash
title change
time change
start timezone change（timed）→ hash change
end timezone change（timed）→ hash change
all-day timezone difference → unchanged hash
recurrence semantic change → hash change
same RRULE meaning / different property order → same hash
RDATE / EXDATE / EXRULE supported/unsupported gate
same instant + same timezone → same event hash
same instant + timezone change → event hash change
kind change
exception parent/original_start change
Google-only field → unchanged hash
```

### Conflict

9パターン。

### Event Classification / Identity

```text
TimeTree uuid → source_event_id
Memo → IGNORE_KNOWN
Birthday → IGNORE_KNOWN
unknown category/type → UNSUPPORTED
Google eventType=default → SYNC
Google special eventType → ignore / unsupported
MCP / Exporter identity match
```

### State

```text
operation transitions
failed + NEEDS_MANUAL_RECOVERY
tombstone
sync token
watermark
```

## Integration

Fake TimeTree + Fake Google：

```text
CRUD
conflict
retry
crash recovery
Remote Write response再Normalize → last_synced_hash確定
410
reconcile
recurrence RRULE series
RDATE / EXDATE / EXRULE contract gate
unsupported exception safe stop
label scope / default / preservation
normal delete tombstone expiry
recurring exception tombstone retained with parent
```

---

# 32. Live E2E

## Contract Discovery

- MCP protocol compatibility
- TimeTree Read payload
- `deactivated_at`露出有無
- TimeTree Event UUID / 既存id対応
- `category / type`とsync対象判定
- Calendar Label一覧と`大河予定` / `共通予定`のruntime `label_id`
- 対象Label / 対象外Labelの分類
- all-day single/multi
- start/end Timezone（片側欠落Case含む）
- Recurrence Series payload
- Exporter UID / UUID対応
- Exporter compare

## Basic CRUD

両方向：

```text
Create
Update title
Update time
Update location
Update description
Update label（大河予定 ↔ 共通予定）
Delete
```

## Timezone

最低：

```text
Asia/Tokyo
```

Fixtureでは`start_timezone != end_timezone`も確認する。

## Recurrence

Series CRUDを両方向。

Exception：

```text
one occurrence update
one occurrence delete
```

をTimeTreeで実観測。

## Delete delta

TimeTree Delete後の`get_updated_events`を確認。

## Crash

Remote Create成功後 / Mapping前で停止。

## Google 410

Mock / Integrationで確認。Live強制不要。

## Idempotency

```text
tick × 10
→ Create/Update/Delete/Duplicate/Conflict = 0
```

## Test Artifact Cleanup

Live E2Eで作るEvent / Seriesは識別可能なTest Prefixまたは専用Fixture IDを持たせる。

TimeTree側Live Test Artifactは`[labels].test_artifact_label = "大河予定"`を使う。

各Live Phase終了時に削除し、Bootstrap開始前には対象Google Calendar / TimeTree CalendarにBridge用Test Artifactが残っていないことを確認する。

Test Artifactが残存している場合はBootstrapへ進まない。

---

# 33. OpenCLI Fallback

Primary完成後に実装。

Base：

```text
opencli doctor
Browser Bridge
timetree-main profile
login
```

Read / Diagnostic：

```text
status
calendars
events
event <id>
```

JSON必須。

Write Fallback：

```text
create
update
delete
```

は各操作のTimeTree Web実契約をLive確認できた場合だけ実装する。

ただしV1のP14完了条件としては3操作すべての確認を試みる。
1つでも安全に確認できない場合、推測で作らずP14をFAILとしてChatGPTへScope判断を戻す。

Primary障害時に自動Fallback Writeしない。

# 34. V1完成判定の参照

完成条件の正本は`要件定義 v0.12`。

本詳細設計で定義したTest / E2Eがその完成条件を満たすことを実装計画P15で最終検証する。

---

# P6.1 YEARLY Recurrence Extension

Validatorはraw RRULEのparameter集合をcanonicalization前にも確認し、次だけを
受理する。

```text
all_day = true
exactly one line = RRULE:FREQ=YEARLY
```

従って、canonicalizerが`INTERVAL=1`等を省略できる場合でも、YEARLYに追加
parameterがあればUnsupportedとする。Timed YEARLY、YEARLY + `INTERVAL` /
`COUNT` / `UNTIL` / `BYDAY` / `BYMONTH` / `BYMONTHDAY` / `EXDATE`、その他の
未確認variantは`UNSUPPORTED_RECURRENCE_FEATURE`とする。Google / TimeTreeの
Normalized recurrenceは同じcanonical lineになる。

Live evidenceはGoogle / TimeTreeともCreate / Read / Update / Clear / Restore /
Delete / Cleanup = PASS、TimeTree UUID維持 = PASS、cleanup後artifact = 0。
P8 Bootstrapのread-only classificationではexact YEARLYをeligibleとして扱い、
generic recurrence exception writeは引き続き拒否する。
