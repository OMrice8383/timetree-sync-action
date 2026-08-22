# TimeTree × ChatGPT Web Calendar Bridge｜実装計画 v0.10

## 0. 文書の位置づけ

本書は以下を実装へ落とす。

1. `timetree取得_要件定義_v0.12.md`
2. `TimeTree取得_基本設計_v0.12.md`
3. `TimeTree取得_詳細設計_v0.11.md`

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

実装順の正本は本書。

---

# 1. 実装運用

## ChatGPT Web

- 仕様判断
- OSS / API再確認
- GitHub Repo / diff / PRレビュー
- Codex Prompt作成
- Codex結果と正本Docsの照合
- Test追加判断
- ChatGPT Google Calendar E2E
- Notion E2E
- V1完成判定
- PROJECT_STATE更新判断

## Codex

- Fork / Clone
- Dependency導入
- Python / Node実行
- TimeTree-MCP
- SQLite
- コード実装
- Unit / Integration Test
- TimeTree Live E2E
- Windows Task Scheduler
- OpenCLI Adapter
- Git commit / push / PR

## ユーザー

- TimeTree credentialをローカル設定
- Google Cloud / Service Account設定
- 専用Google Calendar作成・共有
- Chrome TimeTree login
- Browser Bridge権限
- Conflictの最終判断

SecretをChatGPTへ貼らない。

---

# 2. 実装ルール

各Phase：

```text
調査
↓
最小実装
↓
Test
↓
git diff / 結果報告
↓
ChatGPTレビュー
↓
Checkpoint Commit
↓
次Phase
```

Live Write前にUnit + Fake IntegrationをPASSさせる。

未確認のTimeTree挙動を推測実装しない。

Phase内のFAILを次Phaseへ持ち越さない。

## Live Test Artifact Policy

Live E2Eで作るEvent / SeriesはBridge Testと識別できる名前 / Fixture IDを使う。

各Phase終了時にCleanupする。

特にBootstrap開始前は、

```text
Google専用CalendarのBridge Test Artifact = 0
TimeTree対象CalendarのBridge Test Artifact = 0
```

をGateとする。

Cleanup失敗時は次Phaseへ進まない。

---

# 3. 技術固定

- Python 3.12+を基準
- TimeTree-MCPのNode要件・VersionをP0で固定
- MCP Python SDK v2 stableを固定
- Googleは`google-api-python-client`を再利用

Transport・Google Query Contract・Hash・State等の詳細仕様は**詳細設計 v0.11を正本**とし、本書で再定義しない。

# 4. Phase一覧

```text
P0  Baseline / Dependency Freeze
P1  Live Contract Discovery
P2  Foundation
P3  Normalization Core
P4  Google Client
P5  TimeTree MCP Client
P6  Recurrence Series Core
P7  Recurrence Exception Contract / Safety Gate
P8  Bootstrap + TimeTree → Google
P9  Google → TimeTree
P10 Conflict / Delete / Crash Recovery
P11 Reconcile + Exporter Verify
P12 Scheduler / Reliability
P13 ChatGPT Web + Notion E2E
P14 OpenCLI Fallback
P15 Final Verification / V1 Release
```

Recurrence SeriesとException安全判定を**Bootstrapより前**へ完了する。

P8 Bootstrapでは、まだ未実装のP11 `reconcile / verify`を呼ばず、Bootstrap専用のRead-only Consistency Checkを使う。

# 5. P0｜Baseline / Dependency Freeze

## Codex

1. `porinpi-JAPAN/timetree-sync-action` Fork / Clone
2. upstream SHA記録
3. Python / Node / Git version
4. dependency install
5. 既存tests / lint / checks
6. Repo構造
7. Google client / TimeTree client / sync処理確認
8. TimeTree-MCP clone + SHA
9. TimeTree-MCPで`npm ci`
10. TimeTree-MCPで`npm run build`
11. `dist/index.js`等の実Runtime entrypointを確認
12. TimeTree-Exporterを正式手順でinstallし、採用Version / SHAまたはPackage Versionを記録
13. MCP Python SDK v2 Version
14. OpenCLI Version

## 完了条件

```text
baseline再実行可能
TimeTree-MCP build PASS
TimeTree-MCP runtime entrypoint確認
TimeTree-Exporter実行可能
全Version / SHA・Package Version記録
既存Test結果記録
Secret 0
```

Baseline不成立なら先へ進まない。

---

# 6. P1｜Live Contract Discovery

## TimeTree-MCP Read

実共有Calendarで：

```text
list_calendars
get_events
get_updated_events
```

確認：

```text
calendar id
event uuid
既存Fork側idとの対応
updated_at
all_day
start_at / end_at
start_timezone / end_timezone
category
type
recurrences
pagination
```

`deactivated_at`は、

> Tool出力に露出するか

を確認する。

静的調査上は通常Formatterへ露出しない想定。

削除の本契約確認はP10。

## Event Identity / Eligibility

V1 Canonical TimeTree Event IDはUUID。

確認：

```text
MCP uuid
Exporter側UID / UUID
既存Fork id
```

同一Eventの対応表を作る。

`category / type`を実確認し、詳細設計の3状態分類をP3前に確定する。

```text
SYNC
IGNORE_KNOWN
UNSUPPORTED
```

既知対象外と未知種別を同じFalseへ潰さない。

## Recurrence / Google Input Contract

Bootstrap前に以下もContractとして記録する。

```text
TimeTree-MCP:
RRULE Read / Create / Update
RDATE / EXDATE / EXRULEのRead / Write可否
Recurring SeriesのTimezone表現

Google:
eventType=default
summary空 / 欠落
timeZoneなし + RFC3339 offsetのみ
```

RRULE以外のRecurrence Featureは、安全なRound Tripを確認できない限りUnsupported。

Recurring Seriesでstart/end effective timezoneが異なる場合の扱いもP6前に確定する。

## all-day

既存Eventで、

```text
single-day
multi-day
```

の実返却を記録。

P3前にinclusive end前提を確定する。

## Timezone

最低限：

```text
start_timezone / end_timezone両方あり
片側だけ欠落
両方欠落
```

の実Payload有無を確認する。

欠落Caseが実データにない場合もFixtureで作り、Normalization時にeffective timezoneを決定する詳細設計のFallback規則を固定する。

HashとWriteで同じeffective timezoneになることを確認する。

## MCP Python v2

最小Script：

```text
StdioServerParameters(env=必要Credential)
↓
stdio_client
↓
Client
↓
list_calendars
```

確認：

```text
Protocol compatibility PASS
Connection PASS
list_calendars PASS
```

## Exporter

同CalendarをMCPと比較。

確認・記録：

```text
TimeTree-MCP calendar_id
TimeTree-Exporter calendar_code
Exporter Event UID / UUID
TimeTree UUIDとの対応
```

自動`verify`用に`[exporter].calendar_code`をConfigへ保存できることを確認する。

## Google

このPhaseはRead / credential / writer共有設定確認のみ。

Live CRUDはP4以降。

## OpenCLI Base

```text
opencli doctor
Browser Bridge
timetree-main profile
TimeTree Web login
```

## Fixture

最低限：

```text
timetree_single.json
timetree_all_day.json
timetree_recurrence.json
timetree_memo.json
timetree_unsupported_type.json
google_single.json
google_all_day.json
google_recurrence.json
```

Secret / 個人情報を除去。

## Gate

以下のどれかが未確定ならP2/P3へ進まない。

```text
TimeTree UUID identity
Event Classification
all-day contract
timezone fallback rule
Exporter identity mapping
Recurrence feature contract
Recurring timezone contract
Google empty-title / offset-only contract
```

実Payloadが設計と異なる場合はDocsを更新する。

# 7. P2｜Foundation

実装：

```text
bridge.toml
Secret loader
JSONL logger
redaction
SQLite migration
event_links
sync_state
sync_operations
conflicts
CLI skeleton
Run Lock
doctor
status
--json
--dry-run方針
```

`sync_state` Key / Operation State / Manual Recovery表現は詳細設計 v0.11をそのまま実装する。

実装計画側では同じ正式名一覧を二重管理しない。

Test：

- Config
- Secret mask
- Migration
- Repository CRUD
- Operation transition
- Lock recovery

---

# 8. P3｜Normalization Core

実装：

```text
NormalizedEvent
EventChange
Recurrence
Enums
TimeTree Adapter
Google Adapter
Event Classification
Canonical JSON / Hash
```

詳細設計 v0.11 の§7〜13および§31 Test設計を正本として実装する。

このPhaseで最低限Gateするもの：

- Timed Normalized Eventではeffective timezone確定済み
- Google offset-only Eventの安全判定
- Google無題EventのUnsupported判定
- all-day Local Calendar Date変換
- RRULE semantic canonicalization
- RDATE / EXDATE / EXRULEのSupport Gate
- Recurring Series timezone整合
- EventChange partial delete
- Event HashのTimezone意味差

**ここがPASSするまでRemote Write Coreを作らない。**

# 9. P4｜Google Client

詳細設計 v0.11 のGoogle Adapter / Query Contract / EventChange契約を実装する。

最低限：

```text
Full / Incremental pagination
same Query Contract
normal cancelled → DELETE
cancelled recurring exception → RECURRENCE_EXCEPTION_DELETE
ID-only delete payload
410 → FULL_RESYNC_REQUIRED
insert / patch / delete / get / metadata
```

Google Client単体のLive CRUDを行い、終了時にTest ArtifactをCleanupする。

Cross-system Reconcileを含む完全な410 RecoveryはP11で統合する。

# 10. P5｜TimeTree MCP Client

実装：

```text
Client(stdio_client(StdioServerParameters(...)))
```

1 `tick`で1接続。

Wrapper：

```text
list_calendars
get_events
get_updated_events
create_event
update_event
delete_event
```

Boundary：

- TimeTree Event UUIDをCanonical Identityとして返す
- Calendar ID型差
- ISO / Unix ms
- start/end timezone
- Tool error → Bridge error

Test：

Fixture + Fake Transport。

Live：

TimeTreeでCreate / Read / Update / Delete。

Create結果UUIDとRead結果UUIDが一致し、Update / Deleteで同UUIDを利用できることを確認する。

Primary CRUDが成立しない場合は先へ進まない。

# 11. P6｜Recurrence Series Core

**Bootstrap前必須。**

実装・Test：

```text
Series Read
Series Create
Rule Update
Recurrence解除
Series Delete
```

両AdapterへSeries対応を追加。

Fixture / Contract：

```text
RRULE weekly
interval
until / count
all-day recurrence
timezone
RDATE
EXDATE
EXRULE
```

RRULEはV1 Baseline Support。

RDATE / EXDATE / EXRULEはTimeTree-MCPとのRound TripをLive確認し、確認できた種類だけSupportへ追加する。

Recurring Seriesでstart/end effective timezoneが異なる場合は、安全性確認できない限りUnsupported。

このPhaseのLiveは**Bridge双方向E2Eではない**。

### TimeTree側

TimeTree-MCP Client単体でSeries CRUDをLive確認。

### Google側

Google Client単体でSeries CRUDをLive確認。

### Adapter

TimeTree / Google fixtureからNormalized SeriesへのRound Tripを確認。

Google側Live Test終了後、テストSeriesを削除し、P8 Bootstrap前に専用Google Calendarを空へ戻す。

本当のBridge E2Eは、

```text
P8 TimeTree → Google
P9 Google → TimeTree
```

で行う。

ExceptionはP7で契約確認・安全判定する。

# 12. P7｜Recurrence Exception Contract / Safety Gate

Bootstrapより前に必須。

TimeTree Seriesを作り、

```text
A. 1回だけ時刻変更
B. 1回だけ削除
```

を実行。

確認：

```text
parent ID
event UUID
updated_at
deactivated_at露出有無
recurrences
original start相当
get_events
get_updated_events
```

まずExceptionの**存在自体を確実に検出できるか**を確認する。

### A. 存在検出 + Mapping可能

`kind / parent_source_event_id / original_start`でAdapterを実装。

### B. 存在は検出できるがMapping不可能

詳細設計で定義したUnsupported診断として当該Seriesへの自動Writeを停止する。

### C. MCPで存在検出不能

Exporter等の独立Readで存在を検出できるか確認する。

独立Readでも存在検出保証を作れない場合はP7 FAIL。

**未対応Exceptionを含むCalendarを予定欠落のままBootstrapへ進めない。**

結果を詳細設計へ反映。

## P7 Bootstrap前Cross-cutting Addendum｜TimeTree Label Scope

P7 Live観測中に、BridgeのTimeTree Createが`label_id`未指定であり、Test Artifactが意図しないLabelへ入る設計不足が判明した。

P8へ進む前に次を完了する。

```text
1. get_calendar_labelsをRead-only実行
2. 大河予定 / 共通予定の実label_idを確認
3. 数値label_idをハードコードしないruntime解決を実装
4. Normalized Eventへlabelを追加
5. TimeTree read/create/updateへLabel contractを追加
6. Google Private Metadataへtimetree_label_nameを追加
7. Google新規未管理Eventのdefault Label = 大河予定
8. 既存UpdateでLabel未指定ならRemote Label保持
9. その他の実在Label = IGNORE_KNOWN / LABEL_OUT_OF_SCOPE
10. Label欠落 / 解決不能 = UNSUPPORTED
11. Hash / Fixture / Unit Testへlabelを追加
12. Live Test Artifactは大河予定で作成
13. P2-P7 regression + Ruff + compileall + diff check
```

同期対象Label：

```text
大河予定
共通予定
```

Google Calendarの色・title・descriptionからLabelを推測しない。

このAddendumはP6 Recurrence Series Contractを再実装する理由にしない。
P7 Exception write gateも、Exception contractが完了するまで引き続き閉じる。

---

# 13. P8｜Bootstrap + TimeTree → Google

前提：

```text
P3 PASS
P4 PASS
P5 PASS
P6 Series PASS
P7 Exception Contract / Safety Gate PASS
TimeTree Label Scope Addendum PASS
`大河予定` / `共通予定`をruntimeで一意解決可能
現在段階でREQUIREDなdoctor項目が全てOK
Google専用CalendarのBridge Test Artifact = 0
TimeTree対象CalendarのBridge Test Artifact = 0
```

詳細設計 v0.11 §16のBootstrap Algorithmと§20のCrash Recovery / Sync Commit Ruleをそのまま実装する。

Phase Gate：

- unmanaged Google EventがあればABORT
- Unsupported Event / RecurrenceがあればWrite前ABORT
- `bootstrap_started_ms` watermark race対策PASS
- crash-safe Create / duplicate 0
- Bootstrap Consistency Check PASS
- TimeTree → Google single / supported recurrenceをLive E2E
- 終了後Test Artifact 0

# 14. P9｜Google → TimeTree

Create：

詳細設計 v0.11のGoogle → TimeTree Crash-safe Create Flowを実装する。

Label：

```text
Bridge label metadata = 大河予定 / 共通予定
→ 対応するruntime label_idへ解決

新規未管理Google Event + label metadataなし
→ 大河予定

未知Label metadata
→ Unsupported
```

既存Mapping済みEventのUpdateでは、Label変更が無ければTimeTree既存Labelを保持する。

Update：

TimeTree Conflict Guard後にWrite。

Delete：

Google `cancelled`を、

```text
通常Delete
Recurring Exception Delete
```

へ分けて検出 / operation準備する。

Recurring Exception DeleteをSeries全体Deleteへ変換しない。

安全なDelete判定完成はP10。

Live：

```text
Google UI/API → TimeTree
single event
recurring series
```

をE2E。

---

# 15. P10｜Conflict / Delete / Crash Recovery

## Conflict

詳細設計 v0.11のConflict 9パターンと正式CLI契約を実装する。

両Snapshotを保存する。

## Google Delete Contract

Google側について、

```text
ID-only normal delete
cancelled recurring exception（id / recurringEventId / originalStartTime保持）
```

が通常Delete / Exception Deleteへ正しく分岐し、Series全削除にならないことを確認する。

## TimeTree Delete Contract

```text
1. Event作成
2. sync
3. TimeTree Delete
4. get_updated_events
5. raw / structured結果保存
```

判定：

```text
A. Toolで一意識別 → Fast Delete
B. Rawにはある → MCP最小Patch検討
C. 一意不可 → Hourly Reconcile
```

## Tombstone Lifetime

```text
normal delete → 約30日
recurring exception delete → 親Seriesが存在する間
```

をIntegration / Liveで検証する。

## Crash Injection

P8で実装したBootstrap / TT→Google Create Recoveryに加えて、

```text
Google → TimeTree remote create直後
mapping保存直前
metadata patch直前
```

も検証。

一意復旧不可時は詳細設計のManual Recovery表現へ遷移し、再Createしない。

---

# 16. P11｜Reconcile + Exporter Verify

詳細設計 v0.11 §24 Reconcile / §25 Verifyを正本として実装する。

Gate：

- Event Classificationを全経路で共通利用
- `FULL_RESYNC_REQUIRED`からFull Snapshot → Reconcile → new syncTokenまで完結
- ExporterはRaw ICSを直接比較せず、Exporter Verification AdapterでCanonical Verify Eventへ変換
- all-day / Timezone / Recurrence表現差をCanonical化して比較
- Unsupportedを無視せず報告
- Recurrence Exceptionは各Sourceが実際に表現可能なField範囲で検証
- `大河予定` / `共通予定`のLabel意味をCanonical Verifyで比較
- 対象外Labelを件数Mismatchとして誤検知しない
- verifyはRead-only

# 17. P12｜Scheduler / Reliability

Windows Task Scheduler：

```text
1分ごと
python -m bridge tick
```

App内部では詳細設計ConfigのInterval Defaultが正しくDue判定されることを確認する。

Reliability：

- tick ×10 duplicate 0
- restart
- stale lock
- MCP failure
- Google failure
- 429 / 5xx
- auth error
- 410
- pending recovery
- secret scan

---

# 18. P13｜ChatGPT Web + Notion E2E

ChatGPT Webで専用Google Calendarを使い、

```text
Read
Create
Update
Delete
```

をTimeTreeまでE2E。

Notion：

```text
TimeTree予定
+
Notion Task
↓
ChatGPT
↓
負荷を考慮したTask配置
```

Bridge問題とChatGPT App権限問題を切り分ける。

Label E2E：

```text
TimeTree 大河予定 → Google → 保持
TimeTree 共通予定 → Google → 保持
ChatGPT / Google新規でLabel信号なし → TimeTree 大河予定
```

ChatGPT Webから`共通予定`を明示指定できる安全な信号が利用可能かを実機確認する。
利用できない場合はGoogle色・title・descriptionから推測実装せず、V1では未管理Google新規Eventを`大河予定`へ既定化する契約を維持する。

---

# 19. P14｜OpenCLI Fallback

P1で準備したOpenCLI Baseを再確認し、詳細設計 v0.11 §33のFallback契約を実装する。

Read / Diagnosticを先に完成させる。

WriteはTimeTree Webの実契約をLive確認した操作だけ実装し、各操作で、

```text
contract observed
implementation
live test
cleanup
```

が揃うことをPASS条件とする。

1つでも安全に契約確認できない場合は推測実装せずP14 FAILとしてScope判断を戻す。

Primary障害時に自動Fallback Writeしない。

# 20. P15｜Final Verification / V1 Release

完成条件の正本：

```text
timetree取得_要件定義_v0.12.md
```

最終確認：

## Automated

```text
Unit
Integration
lint
secret scan
```

## Live

- TimeTree CRUD
- TimeTree UUID identity
- Event Classification / Memo・Birthday除外 / Unsupported安全停止
- Exporter calendar_code自動指定
- VERIFY_UNSUPPORTED報告
- Google通常Event Filter
- Google CRUD
- TT↔Google双方向
- all-day local-date変換
- start/end effective timezone / Hash対称性
- RRULE semantic canonicalization
- RDATE / EXDATE / EXRULE Support Gate
- Recurrence Seriesの基準Timezone
- Google offset-only timezone安全判定
- Google無題Event安全停止
- TimeTree Label `大河予定` / `共通予定` scope
- Label runtime ID解決 / round-trip / default `大河予定`
- 対象外LabelのIGNORE_KNOWN
- Recurrence Series
- Exception対応または安全停止
- Google normal delete / cancelled recurring exception分離
- TimeTree Delete契約
- Bootstrap watermark race対策
- Conflict
- Crash Recovery
- Remote Write response再Normalize後のlast_synced_hash確定
- 通常Delete / Recurring Exception DeleteのTombstone寿命
- Live Test Artifact cleanup
- 410
- Hourly Reconcile
- 3者verify
- Scheduler restart
- OpenCLI Fallback
- ChatGPT + Notion

全部PASS後のみV1 Release。

---

# 21. Codex報告Format

各Phase：

```text
1. 実施内容
2. 変更ファイル
3. 変更理由
4. 実行Command
5. Test結果
6. Live E2E結果
7. Secret非露出確認
8. 未確認 / 仮説
9. 設計との差異
10. git diff概要
11. 次Phaseへ進めるか
```

「実装済み」と「実機確認済み」を分離する。

---

# 22. 停止条件

以下は勝手に先へ進めない。

```text
TimeTree Payloadが設計と違う
TimeTree UUID Identityが確定しない
MCP / Exporter Event Identityを照合できない
Event Classificationを確定できない
MCP Protocol / Connection不成立
all-day契約が想定と違う
start/end timezone契約を保持できない
Google offset-only timezoneを安全に解釈できない
Google無題Eventを安全に処理できない
`大河予定` / `共通予定`のLabel名をruntimeで一意解決できない
TimeTree Eventのlabel_id欠落 / 未知で同期対象判定不能
Recurrence Series契約不明
RDATE / EXDATE / EXRULEをSupport/Unsupportedへ分類できない
Recurring Seriesの基準Timezoneを安全に確定できない
Recurrence Exceptionの存在検出保証を作れない
Recurrence Exceptionを安全判定できない
Google cancelled normal delete / recurring exceptionを区別できない
TimeTree Deleteを一意判定できない
Bootstrap watermarkの取りこぼし安全性を確認できない
Google Calendarに未管理Event
Bootstrap前Google Calendarを空にできない
Bridge Live Test ArtifactをCleanupできない
duplicateを一意復旧不能
OpenCLI Write契約を安全確認できない
Secret漏洩
Baseline破損
```

結果をChatGPTへ戻して正本を更新する。

# 23. PROJECT_STATE更新

最低：

```text
P0
P5
P7
P10
P13
P15
```

で更新。

必ず正本化する情報：

```text
採用Version / SHA
TimeTree-MCP実契約
TimeTree UUID / Exporter Identity契約
Event Classification規則
all-day実契約
Timezone / offset-only実契約
Google empty-title契約
TimeTree Label scope（大河予定 / 共通予定）とruntime label_id解決契約
Google新規EventのLabel default契約
Recurrence Feature（RRULE / RDATE / EXDATE / EXRULE）契約
Recurring Series timezone契約
Google cancelled Delete分類契約
TimeTree Delete検知方式
Bootstrap watermark契約
Recurrence Exception存在検出 / Mapping契約
V1完成状態
```

# 24. 実装開始点

最初にCodexへ渡すのは**P0だけ**。

P0完了結果をChatGPTでレビューするまでP1以降へ進まない。

---

# P6.1｜YEARLY Recurrence Extension

P6のWEEKLY contractを変更せず、Live確認済みの最小拡張だけを実装する。

```text
supported: all-day + RRULE:FREQ=YEARLY
unsupported: timed YEARLY and every YEARLY variant with parameters or EXDATE
```

Gateは、P6 WEEKLY回帰、YEARLY variant fail-safe、Google / TimeTree normalized
canonical一致、P8 Bootstrap classification eligible、ruff / compileall /
git diff --check、およびread-only `bootstrap --dry-run --json`とする。
Live write、P8 live bootstrap、Exception write、P9以降はこのPhaseのscope外。

Google / TimeTreeのCreate / Read / Update / Clear / Restore / Delete / Cleanup
とTimeTree UUID維持はLive Contract DiscoveryでPASS済みとして記録する。
