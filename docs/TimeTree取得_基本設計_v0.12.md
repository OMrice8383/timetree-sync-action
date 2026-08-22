# TimeTree × ChatGPT Web Bridge｜基本設計 v0.12

## 1. 最終目的

要件定義 v0.12を、既存OSSを最大限再利用して実現する。

最終利用形：

```text
TimeTree
   ↕
Calendar Bridge
   ↕
Google Calendar「TimeTree Bridge」
   ↕
ChatGPT Web
   +
Notion
```

TimeTreeが正本、Google CalendarがChatGPT Web Adapterである。

---

# 2. 採用OSS

## 2.1 timetree-sync-action

**採用：Bridgeの土台**

主に再利用するもの：

- TimeTree → Google既存処理
- Google Calendar Client周辺
- TimeTree ID Mappingの既存知見
- 専用Google Calendar運用
- Google Service Account
- Windows / GitHub Actions運用知見

ゼロからGoogle Clientや一方向同期を作り直さない。

---

## 2.2 TimeTree-MCP

**採用：Primary TimeTree Read / CRUD / Incremental / Full Snapshot**

重要Tool：

```text
list_calendars
get_events
get_updated_events
create_event
update_event
delete_event
```

RecurrenceもTimeTree-MCP経由で扱う。

---

## 2.3 TimeTree-Exporter

**採用：独立Full Snapshot / Verification**

通常Reconcileには使わず、

```text
初期E2E
日次程度
verify
障害調査
API変更調査
```

で使用する。

---

## 2.4 OpenCLI

**採用：Browser Diagnostic / Final Fallback**

Primary完成前はOpenCLI Base・Browser Bridge・TimeTreeログインProfileだけを準備する。

TimeTree専用AdapterはV1最後に実装。

通常同期では使用しない。

---

# 3. 採用Architecture

```text
                         TimeTree
                            ↑↓
                    ┌──────────────┐
                    │ TimeTree-MCP │
                    │   Primary    │
                    └──────┬───────┘
                           ↑↓
              ┌────────────────────────┐
              │    Calendar Bridge     │
              │                        │
              │ Normalized Event       │
              │ Sync Engine            │
              │ Mapping / Conflict     │
              │ Crash Recovery         │
              │ SQLite                 │
              └───────────┬────────────┘
                          ↑↓
                   Google Calendar
                   「TimeTree Bridge」
                          ↑↓
                     ChatGPT Web
                          │
                          └─ Notion

Hourly Reconcile:
TimeTree-MCP Full Snapshot + Google Full Snapshot + SQLite

Independent Verify:
TimeTree-MCP vs TimeTree-Exporter vs Google+SQLite

Final Fallback:
TimeTree Web ↔ OpenCLI
```

V1は1 Repo / 1 Application、1 Calendar Pairとする。

---

# 4. Runtime

```text
Windows PC
├─ Python Calendar Bridge
├─ Node.js TimeTree-MCP
├─ SQLite
├─ TimeTree-Exporter
└─ OpenCLI
```

Bridge本体はFork元に合わせPython。

TimeTree-MCPはNode.jsのまま利用し、PythonからOfficial MCP Python SDKでstdio接続する。

Windows Task Schedulerは1 Taskだけ使う。

```text
約1分ごと
python -m bridge tick
```

`tick`内部で各処理のDue判定を行う。

---

# 5. Sync Path

## 5.1 Google → TimeTree Fast Path

```text
ChatGPT / Google
↓
Google Incremental Sync
↓
Calendar Bridge
↓
TimeTree-MCP
↓
TimeTree
```

Fast PathとしてGoogle `syncToken`を利用する。

具体的な実行Intervalは要件定義 / 詳細設計Configを正本とする。

---

## 5.2 TimeTree → Google Fast Path

```text
TimeTree
↓
TimeTree-MCP get_updated_events(updated_after)
↓
Calendar Bridge
↓
Google Calendar
```

Fast PathとしてTimeTree-MCPの差分取得を利用する。

State：

```text
timetree_updated_after_ms
```

小さなOverlap Window + dedupeを使う。

---

## 5.3 Full Reconcile

```text
TimeTree-MCP Full Snapshot
+
Google Full Snapshot
+
SQLite
```

Slow Pathとして定期実行する。

目的：

- 削除取りこぼし
- Incremental取りこぼし
- Mapping崩れ
- 重複
- Sync失敗

の検出・安全な修復。

---

## 5.4 Independent Verify

```text
TimeTree-MCP
vs
TimeTree-Exporter
vs
Google + SQLite Mapping
```

通常Syncより低頻度 / 手動verify / 障害時に実行する。

Read-only。

---

# 6. Google Incremental設計

V1でRecurring Seriesを基準にするため、

Recurring Seriesを保持したまま、Googleの通常Eventだけを対象にするQuery ContractをInitial Full Syncから全Incremental Syncまで維持する。

具体的なQuery Parameterは詳細設計を正本とする。

IncrementalはInitial Full Syncと同じQuery Parameterセットを維持する。

Pagination中は、

```text
same syncToken
same query parameters
+ pageToken
```

だけにする。

最終Page成功後だけ`nextSyncToken`を保存。

410 Gone時はGoogle Clientがtoken失効を検出し、BridgeへFull Resyncが必要であることを返す。
Cross-system Reconcileを伴う完全復旧はReconcile層の責務とする。

Google APIの具体的な禁止Query Parameter・削除取得条件は詳細設計を正本とする。

cross-system mappingのSQLiteは無条件に消さない。

---

# 7. Normalized Event

Bridge内部に共通Event Modelを置く。

```text
source
source_calendar_id
source_event_id

kind
parent_source_event_id
original_start

title
all_day
start
end
start_timezone
end_timezone
description
location
label

recurrence
updated_at
```

`kind`：

```text
single
series
exception
```

独自`id`は持たず、Remote IDは`source_event_id`、システム間対応はSQLiteへ任せる。

TimeTree由来EventのCanonical IdentityはTimeTree Event UUIDへ統一する。

削除は通常Event Modelへ`deleted`を持たせず、

```text
EventChange
+
event_links Tombstone
```

で表現する。

Normalized Event / SQLite / Google Metadataでの具体的な保存場所は詳細設計を正本とする。

# 8. all-day / Timezone

## all-day

Bridge内部：

```text
start inclusive
end exclusive
```

Googleはexclusive end。

TimeTreeは現時点でinclusive endを仕様前提とするが、Live Contract Discoveryで確認後に確定する。

TimeTree-MCPのall-day日時がISO timestampの場合は、TimeTreeのCalendar Dateへ変換してからdate化する。

NormalizedEventではall-day時にTimezone差分を意味的変更として扱わない。

```text
all_day = true
→ start / end = date
→ start_timezone = None
→ end_timezone = None
```

## Timezone

Timed Eventでは単数`timezone`へ潰さず、

```text
start_timezone
end_timezone
```

を保持する。

Raw Adapter入力ではTimezone欠落を許容してもよいが、**Normalized Eventへ入った時点では両方のeffective timezoneを確定済み**とする。

TimeTree / Googleの両境界で可能な限りRound Tripする。

Google Single Eventが`timeZone`を持たずRFC3339 offsetだけを持つ場合は、offsetと採用するeffective timezoneの整合を確認する。
意味を安全に復元できない場合はUnsupportedとし、勝手に`Asia/Tokyo`等へ置換しない。

## Event Eligibility

TimeTree Eventは、

```text
SYNC
IGNORE_KNOWN
UNSUPPORTED
```

の3状態へ分類する。

Memo / Birthday等の既知非対象と、意味不明な未知種別を同じFalseへ潰さない。

Google側は`eventType=default`のみをV1同期対象とする。

同じ分類規則をSnapshot / Incremental / Bootstrap / Reconcile / Verifyで共通利用する。

### TimeTree Label Scope

V1で同期するLabelは次の2つだけ。

```text
大河予定
共通予定
```

Calendar BridgeはLabel名を意味上のCanonical値として扱い、TimeTreeの数値`label_id`を固定値として持たない。

実行時にTimeTree-MCPからCalendar Label一覧を取得し、

```text
Label名
↓
実際のlabel_id
```

を一意に解決する。

分類：

```text
通常Event + 大河予定 / 共通予定
→ SYNC

通常Event + その他の実在Label
→ IGNORE_KNOWN / LABEL_OUT_OF_SCOPE

label_id欠落・Label名解決不能・重複名で一意判定不能
→ UNSUPPORTED
```

`大河予定`または`共通予定`が存在しない状態ではBootstrap / Writeを開始しない。

TimeTree → GoogleではGoogle Private MetadataにLabel名を保持する。
Google → TimeTreeの新規未管理EventでLabel Metadataが無い場合は`大河予定`を既定とする。
既存Event更新ではLabel変更が明示されない限り元Labelを保持する。

Google Calendarの色・タイトル・説明からLabelを推測しない。

# 9. Recurrence

V1必須。

## Series

V1の基準はRRULE Series。

Bootstrap前に以下を実装・Testする。

```text
Read
Create
Rule Update
Recurrence解除
Series Delete
```

RDATE / EXDATE / EXRULEはContract GateでTimeTree-MCPとの安全なRound Tripを確認し、確認できた種類だけSupportへ追加する。

Recurring Seriesは1つの基準effective timezoneで展開できることを要求する。
開始 / 終了のeffective timezoneが異なるSeriesは安全確認できない限りUnsupported。

## Exception

Bootstrap前にTimeTree実機契約を確認する。

```text
Exceptionの存在を検出できる
+
安全にmapping可能
→ Exception Adapter実装

存在は検出できるが安全にmappingできない
→ 当該Seriesを壊さず停止

Exceptionの存在検出自体を保証できない
→ P7 FAIL
```

必要ならExporterをContract Discovery / Bootstrap安全検査の補助Readとして使うが、通常同期Primaryにはしない。

未対応Exceptionを含むCalendarを、予定欠落のままBootstrap完了扱いにしない。

# 10. SQLite

4テーブル。

```text
event_links
sync_state
sync_operations
conflicts
```

## event_links

最低限：

```text
google_event_id
timetree_event_id

event_kind
parent IDs

last_synced_hash

status
last_synced_at
deleted_at
```

Conflict判定の同期基準点は`last_synced_hash`へ一本化する。

## sync_state

Calendar全体の差分位置・実行時刻をKey/Valueで保持する。

正式Key名は詳細設計を正本とする。

## sync_operations

Remote Write途中のCrash Recoveryを担当する。

正式State / Error表現は詳細設計を正本とする。

## conflicts

TimeTree / Google両Snapshotと解決状態を保存。

# 11. Hash

同期対象の意味的FieldをCanonical JSON化してSHA-256。

正確なField / Canonicalizationは詳細設計を正本とする。

原則：

```text
Timed datetime
→ 同じ瞬間なら同じHashになる固定UTC表現

Timezone欠落
→ Normalizationで確定したeffective timezoneをHash / Write両方に使う

all-day
→ dateだけを意味的時刻として扱いTimezone差をHashへ持ち込まない

Recurrence
→ RRULEの文字列順ではなく、意味が同じなら同じCanonical表現になるよう正規化
```

Google固有FieldはHashへ入れない。

`label`は同期対象の意味FieldとしてHashへ含める。
`大河予定`と`共通予定`の変更は意味的変更として検出する。

削除は`EventChange` / Tombstoneで扱い、通常Event Hashへ`deleted`を入れない。

Conflict判定の基準点は`last_synced_hash`とする。

# 12. Conflict

基準：

```text
last_synced_hash
```

例：

```text
Last Sync = AAA
TimeTree  = BBB
Google    = AAA
→ TimeTreeだけ変更
```

```text
Last Sync = AAA
TimeTree  = BBB
Google    = CCC
→ conflict
```

Delete vs UpdateもConflict。

自動上書きしない。

---

# 13. Create / Crash Recovery

Remote Createの前後でOperation Stateを永続化し、Remote Write成功後・Mapping保存前に停止しても重複Createしない。

Google → TimeTree / TimeTree → Googleの具体的State遷移は詳細設計を正本とする。

TimeTree → Google Createでは、Google EventへTimeTree UUID由来の復旧Metadataを付与し、Crash後にRemote状態を照合できるようにする。

Remote状態を一意確認できない場合は再Createしない。

---

# 14. Google Metadata

Google Eventには、TimeTree UUID・Bridge由来・Bridge Version・TimeTree Label名を識別できる最小限のPrivate Metadataを付与する。

具体的なKey名は詳細設計を正本とする。

SQLiteがPrimary Mapping。

Google Metadataは復旧・Debug用Backup。

---

# 15. Update

Google側はFull replacementではなく、同期対象Fieldだけ`patch`する。

対象：

```text
summary
start
end
description
location
recurrence
extendedProperties.private（TimeTree UUID / Label等、必要時）
```

Google固有Fieldは触らない。

TimeTree側も変更Fieldだけ`update_event`へ渡す。

---

# 16. Delete

## Google → TimeTree

Google Incrementalの`cancelled`は2種類へ分ける。

```text
通常Eventの削除
→ SQLite MappingからTimeTree IDを解決しDelete候補

Recurring cancelled exception
→ recurringEventId / originalStartTimeを使い「その1回だけの削除」として扱う
```

Recurring cancelled exceptionをSeries全体削除へ変換しない。

削除EventはID等しか持たない可能性があるため、完全なNormalized Eventではなく削除専用Change表現でも処理できるようにする。

通常削除でTimeTreeが変更済みなら`delete_update conflict`。

## TimeTree → Google

`get_updated_events`で削除を一意検出できるかLive E2E。

可能ならFast Delete。

不可能ならHourly Full Reconcileで消失を検出。

Google変更済みならConflict。

Tombstone保持：

```text
通常Event Delete
→ 約30日

Recurring Exception Delete
→ 親Seriesが存在する間
```

Recurring Exceptionの削除状態を期限切れで忘れて復活させない。

---

# 17. Scheduler / Run Lock

Windows Task Scheduler：

```text
1分ごと
python -m bridge tick
```

Application内部でConfigの各Intervalを見てDue判定する。

具体的なDefault値は詳細設計のConfigを正本とする。

Task Scheduler側：

```text
already running → do not start
```

Application側もPID / started_atを持つRun Lockで二重起動を防止。

---

# 18. Repository方針

`porinpi-JAPAN/timetree-sync-action`をForkして拡張する。

**既存Repoを先に確認し、動いている構造を不必要に作り替えない。**

詳細設計に示すRepository treeは概念上の責務配置であり、物理File名・Folder構成を強制しない。
P0で実Forkを調査し、

```text
既存実装を再利用
+
必要最小限の追加
```

を優先する。

Calendar Bridge側で必要な責務：

```text
Normalized Event / EventChange / Adapter
Bidirectional Sync
Google Incremental
TimeTree updated_after
Hourly Reconcile
SQLite State
Crash Recovery
Hash / Conflict
Delete semantics
Recurrence
Google → TimeTree
OpenCLI Fallback Adapter連携
```

# 19. OpenCLI Fallback

OpenCLI Base・Browser Bridge・TimeTreeログインProfileを先に準備する。

AdapterはPrimary完成後。

Read / DiagnosticはV1必須。

Write Fallback：

```text
create
update
delete
```

は各操作の実Web契約をLiveで安全に確認してから実装する。

1つでも契約を安全に確認できない場合は、推測で実装せずP14を停止してScope判断へ戻す。
通常時の自動Fallback Writeは禁止。

# 20. V1設計上の完成条件

完成条件の正本は**要件定義 v0.12**とする。

本基本設計は、その完成条件を上記Architecture / Component責務で満たす。

---

# P6.1 YEARLY Recurrence Extension

P6のsupported series subsetへ、all-dayかつexact `RRULE:FREQ=YEARLY`だけを追加
する。Timed YEARLY、YEARLYの`INTERVAL` / `COUNT` / `UNTIL` / `BYDAY` /
`BYMONTH` / `BYMONTHDAY` / `EXDATE` / その他parameter、ならびにDAILY /
MONTHLY等はUnsupportedのまま維持する。

GoogleとTimeTreeでCreate / Read / Update / Clear / Restore / Delete / Cleanupを
Live確認済みで、TimeTree UUID維持も確認済みである。P8 Bootstrapの共通分類では
このexact形を通常のSYNC candidateとして扱う。Recurrence Exception write gateは
変更しない。
