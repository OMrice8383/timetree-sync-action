# TimeTree × ChatGPT Web Calendar Bridge｜要件定義 v0.12

## 1. プロジェクト目的

TimeTreeで管理している共有カレンダーを、**ChatGPT Webから予定管理・タスク計画のContextとして利用できる仕組み**を構築する。

現在の最優先用途は以下。

- ChatGPTがTimeTree上の予定を確認する
- Notion上のProject / Taskと予定を同時に参照する
- 私生活の予定が多い日はTaskを軽くする
- 空いている日に重いTaskを配置する
- 必要に応じてChatGPTから予定を作成する
- 予定を変更する
- 予定を削除する

将来的には、同じTimeTree接続基盤をJARVIS・CLI・MCP・各種Agent / Automation等から再利用できる構造にする。
具体的な将来拡張は本書「23. 将来拡張」を正本とする。

---

# 2. 基本原則

## 2.1 TimeTreeを予定の正本とする

予定の最終的なSource of Truthは**TimeTree**とする。

Google Calendarは、

> ChatGPT WebからTimeTreeの予定へアクセスするためのBridge Calendar

として使用する。

```text
TimeTree
   ↕
Calendar Bridge
   ↕
Google Calendar
   ↕
ChatGPT Web
```

Google側から作成・変更・削除された操作も正規入力としてTimeTreeへ反映する。

つまり、

```text
最終保管先 = TimeTree
変更入口   = TimeTree / Google
```

とする。

---

# 3. 技術的制約

TimeTree公式のConnect App / Developer APIは終了しているため、一般開発者向け公式APIを前提にしない。

TimeTree側は、TimeTree Webが内部利用する非公式API等を解析したOSSへ依存する。

したがって、

> TimeTree側の仕様変更によって突然動作しなくなる可能性

を設計上の前提とする。

未確認のTimeTree挙動を推測で実装せず、実機結果を正本とする。

---

# 4. ChatGPT Webとの接続方針

V1では自作TimeTree MCPをChatGPT Webへ直接接続することを前提としない。

代わりに、

```text
TimeTree
   ↕
Calendar Bridge
   ↕
Google Calendar「TimeTree Bridge」
   ↕
ChatGPT Web
```

を使用する。

Google CalendarのRead / Create / Update / Deleteがユーザー環境のChatGPT Webから利用できることは、V1の実機E2E完成条件として確認する。

---

# 5. 採用Architecture

```text
                         TimeTree
                            ↑↓
                    TimeTree-MCP
                       Primary
                            ↑↓
                ┌────────────────────┐
                │  Calendar Bridge   │
                │                    │
                │ Normalization      │
                │ Sync / Mapping     │
                │ Conflict           │
                │ Idempotency        │
                │ Crash Recovery     │
                │ SQLite             │
                └─────────┬──────────┘
                          ↑↓
                  Google Calendar
                  「TimeTree Bridge」
                          ↑↓
                     ChatGPT Web
                          │
                          ├─ Calendar
                          └─ Notion

Independent Verification:
TimeTree → TimeTree-Exporter

Diagnostic / Final Fallback:
TimeTree Web ↔ OpenCLI / Browser Bridge
```

---

# 6. 採用Componentと役割

## 6.1 TimeTree-MCP

**Primary TimeTree Adapter**

V1で必要な役割：

```text
Calendar List
Event Read
Incremental Read
Full Snapshot
Create
Update
Delete
Recurrence
```

Calendar BridgeからMCP Clientとして呼び出す。

まず既存OSSを利用し、必要性が出るまでTimeTree API Clientを自作しない。

---

## 6.2 TimeTree-Exporter

**独立Read検証 / 参照実装**

用途：

```text
初期E2E
1日1回程度の独立検証
verify
障害調査
API変更調査
Regression確認
MCP取得結果のクロスチェック
```

通常の1時間Reconcileには使用しない。

Exporter結果だけを根拠に自動Create / Update / Delete / Mapping修復しない。

---

## 6.3 OpenCLI

**Browser Diagnostic / Final Fallback**

V1に含めるが、TimeTree専用Adapter実装はPrimary完成後の最後に行う。

先に、OpenCLI Base・Browser Bridge・TimeTreeログイン済みProfileを準備し、Browser経由でTimeTree Webを診断可能にする。
具体的なCommandは詳細設計 / 実装計画を正本とする。

V1最後に、

```text
Read / Diagnostic
必要なCreate / Update / Delete Fallback
```

を実装する。

Primary障害時に自動でOpenCLI Writeへ切り替えず、明示的にFallbackを使用する。

V1ではRead / Diagnostic Adapterを必須とし、Create / Update / Delete FallbackはそれぞれTimeTree Webの実契約を安全にLive確認した上で実装する。
いずれかのWrite契約を安全に確認できない場合、推測実装でPASS扱いにせず、P14を停止してChatGPTへScope判断を戻す。

---

## 6.4 Calendar Bridge

責務：

- TimeTree-MCP通信
- Google Calendar通信
- Normalization
- Event Mapping
- 双方向同期
- Incremental / Full Reconcile
- 重複防止
- Conflict
- Retry
- Logging
- State保存
- Crash Recovery
- Health Check

V1は1 Repo / 1 Applicationとし、過剰なMicroservice化をしない。

---

# 7. V1同期対象

最低限同期する予定情報：

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
```

削除は通常Eventの意味Fieldではなく、`EventChange`とMapping Tombstoneで扱う。

Timezoneは`Asia/Tokyo`へ固定せず、**開始と終了を別々に保持**する。

通常は同じTimezoneでも、

```text
start_timezone
end_timezone
```

を失わない。

Source側にTimezoneが欠落する場合は、Normalization時に決定的なFallback規則で**effective timezone**を確定する。

Hash判定とRemote Writeは同じeffective timezoneを使い、

> Bridge自身がFallbackしたTimezone差を次回Syncで「外部変更」と誤判定しない

ことを必須とする。

## 7.1 TimeTree Label同期範囲

V1で同期対象とするTimeTree Labelは、次の2つだけとする。

```text
大河予定
共通予定
```

Labelは予定の同期対象判定および意味情報の一部として扱う。

TimeTreeの数値`label_id`を設計上の固定値としてハードコードしない。
Calendar BridgeはTimeTree-MCPのCalendar Label取得契約を使い、起動時・`doctor`・Bootstrap前にLabel名から実際の`label_id`を解決する。

必須規則：

```text
通常Calendar Event
+
Label = 大河予定 / 共通予定
→ SYNC

通常Calendar Event
+
Calendarに実在する上記以外のLabel
→ IGNORE_KNOWN
→ reason = LABEL_OUT_OF_SCOPE

label_id欠落
Label名を一意に解決不能
同名Labelが複数あり一意に確定不能
→ UNSUPPORTED
→ 自動Writeしない
```

`大河予定`と`共通予定`のどちらかがCalendarから欠落している場合、Bootstrapおよび通常Writeを開始せず診断エラーとする。

Label変更：

```text
大河予定 ↔ 共通予定
```

は意味的変更として同期対象に含める。

TimeTree由来EventをGoogleへ同期する場合、Bridge管理用のGoogle Private MetadataへLabel名を保持する。
数値`label_id`はRemote環境依存値としてCross-systemのCanonical値にしない。

Google / ChatGPT Webから新規作成された未管理Eventについて、Bridgeが確認済みLabel Metadataを持たない場合のTimeTree作成先Labelは、

```text
大河予定
```

を既定とする。

既存Mapping済みEventの更新では、Label変更が明示されていない限りTimeTree側の既存Labelを保持する。

Google Calendarの色・予定タイトル・説明文から`大河予定` / `共通予定`を推測してはならない。
ChatGPT Webから`共通予定`を明示指定できる経路はP13で実機確認し、安全な信号を確認できた場合のみ有効化する。

## 7.2 TimeTree Event Identity

V1でTimeTree Eventを一意に識別するCanonical IDは、**TimeTreeのEvent UUID**とする。

既存Fork内の別`id`表現をCross-system Identityとして混在させない。

UUIDをNormalized Event・SQLite Mapping・Google Metadataへどのように保持するかは詳細設計を正本とする。

P1 Live Contract DiscoveryでMCP / Exporter / 既存ForkのID対応を確認する。

## 7.3 同期対象Event種別

V1で自動同期するのは**通常のCalendar Event**とする。

TimeTree側では、Eventを単純なYes/Noではなく次の3状態へ分類する。

```text
SYNC
→ 通常Calendar Event。同期する。

IGNORE_KNOWN
→ Memo / Birthdayなど、V1で意図的に同期しない既知種別。

UNSUPPORTED
→ category / typeの意味を安全に確定できない種別。
```

`IGNORE_KNOWN`は同期せず理由を記録する。

`UNSUPPORTED`は通常予定として推測同期せず、Bootstrapや自動修復では安全側へ停止する。

P1でTimeTree-MCPの`category / type`実Payloadを確認し、詳細設計の分類規則を確定する。

MCP / Exporter / Googleの`verify`でも同じ同期対象規則を適用し、既知の対象外Eventを件数差として誤検知しない。

## 7.4 Google Event種別

Google Calendar側はV1で**通常Event (`eventType = default`)**だけを同期対象とする。

特殊Eventが返った場合は通常予定としてTimeTreeへ書き込まず、既知の非対象またはUnsupportedとして扱う。

Google Full / Incremental Syncでは通常Eventだけを対象にする。具体的なQuery Parameterは詳細設計を正本とする。

Googleの通常Eventでも、TimeTree側で必須となるタイトルが空 / 欠落している場合は推測でタイトルを補わず、Unsupportedとして安全停止する。

# 8. Normalized Event要件

TimeTree形式とGoogle形式を直接相互変換し続けず、Bridge内部にNormalized Eventを持つ。

概念上必要な情報：

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

`NormalizedEvent.id`という用途不明の独自IDはV1要件に置かない。

Remote側のIDは`source_event_id`、システム間MappingはSQLiteで管理する。

削除は完全な`NormalizedEvent`へ二重表現せず、

```text
削除通知
→ EventChange

削除済みMapping
→ event_links.status = deleted
```

へ分離する。

# 9. all-day要件

Bridge内部ではendをexclusiveに統一する。

```text
start = inclusive
end   = exclusive
```

Google all-dayはexclusive endとして扱う。

TimeTree all-dayについては、現時点ではinclusive endを仕様前提とするが、実装前のLive Contract Discoveryで、

```text
single-day all-day
multi-day all-day
```

を必ず確認する。

TimeTree-MCPがall-day日時をISO timestampとして返す場合、UTC等の表示上の日付をそのまま切り出さず、**TimeTree側のCalendar Dateへ変換してからdate化**する。

実Payloadが前提と異なる場合は、**実機結果を正としてAdapter仕様・Fixture・Testを更新する。**

---

# 10. Recurrence要件

V1から繰り返し予定を同期対象にする。

## 必須

V1のSeries基準はまず**RRULE**とする。

Seriesレベル：

```text
Read
Create
Rule Update
Recurrence解除
Series Delete
```

を双方向で成立させる。

Googleが持ち得る、

```text
RDATE
EXDATE
EXRULE
```

は、Bootstrap前のRecurrence Contract GateでTimeTree-MCPとのRound Trip可否を実機確認する。

安全な双方向表現を確認できない種類を含むSeriesはUnsupportedとして自動Writeしない。
確認できた種類だけV1 Supportへ追加する。

Recurring Seriesでは繰り返し展開の基準Timezoneを1つに確定する。
`start_timezone`と`end_timezone`のeffective timezoneが異なるSeriesは、実契約で安全性を確認できない限りUnsupportedとする。

**Series対応は初回Bootstrapより前に実装・Testする。**

## Exception

「今回だけ変更」「今回だけ削除」はTimeTreeの実機契約を**Bootstrapより前**に確認する。

P7では、

```text
1. Exceptionが存在することを検出できるか
2. Parent / Original Startを含めて安全に一意表現できるか
```

の両方を確認する。

安全に表現可能ならException Adapterを利用する。

表現できないが**存在自体は確実に検出できる**場合は、当該Seriesへの自動Writeを安全停止する。

TimeTree-MCPで存在を検出できず、Exporter等の独立Readでも検出保証を作れない場合は、「安全停止できる」とみなさずP7をFAILとする。

Exception未対応を理由にSeries全体を誤更新してはいけない。

Bootstrap対象に未対応Exceptionが含まれる場合は、予定を欠落させたままBootstrap完了扱いにせず、明示的に停止してユーザー判断へ戻す。

V1完成条件は、

> Exceptionの存在検出契約が実機で確定し、対応または安全停止が成立していること

とする。

# 11. Event Mapping / State要件

SQLite等へ責務を分離して保存する。

```text
Event Mapping
Global Sync State
Write Operation State
Conflict Snapshot / Resolution
```

Event Mappingには最低限：

```text
timetree_event_id
google_event_id

last_synced_hash

last_synced_at
status
deleted_at
```

を保持する。

`last_synced_hash`は、どちら側だけが変更されたか判断する唯一の同期基準点とする。

片側ごとの現在HashはSync実行時に取得・計算する。
Debug用Cacheが本当に必要になった場合だけ、実装時に観測用Fieldとして追加してよい。

# 12. Idempotency / Crash Recovery

同じ処理を何度実行しても重複予定を増やさない。

特に、

```text
Remote Create成功
↓
Mapping保存前にProcess停止
```

しても、再起動後に同じ予定を再Createしない。

Remote Writeの途中状態を専用Operation Stateへ保存し、再起動時にRemote状態を照合して再開する。

Remote状態を一意に確認できない場合は、重複を避けるため勝手に再Createしない。

---

# 13. Conflict

Last Sync以降に両側が別々に変更された場合、自動上書きしない。

```text
status = conflict
```

として記録する。

削除も変更として扱う。

```text
片側 = deleted
もう片側 = changed
```

もConflictとし、自動削除しない。

---

# 14. Delete

Google削除はIncremental Syncから検出する。

Googleの`status=cancelled`は、次を区別する。

```text
通常Eventの削除
→ Event IDだけで届く可能性がある

Recurring Eventのcancelled Exception
→ recurringEventId + originalStartTimeを使って「その1回だけの削除」として扱う
```

Recurring Exception削除をSeries全体削除としてTimeTreeへ伝播してはいけない。

削除通知はtitle / start / end等が欠落する可能性があるため、通常の完全なNormalized Eventを要求せず、**削除専用のChange / Tombstone参照**として処理可能にする。

TimeTree削除はLive E2Eで`get_updated_events`から一意に検出できるか確認する。

## Fast Delete可能

TimeTree差分から削除を一意に識別できる場合はFast Pathで処理する。

## Fast Delete不可

1時間程度のTimeTree-MCP Full Snapshot Reconcileで、

```text
Mappingあり
+
TimeTree Full Snapshotに存在しない
```

を削除候補として判定する。

相手側が変更済みならConflict。

Mappingは即物理削除せずSoft Tombstoneとして保持する。

保持期間は削除種別で分ける。

```text
通常Event Delete
→ 約30日を既定

Recurring Exception Delete
→ 親Seriesが存在する間は保持
```

Recurring Exception Tombstoneを通常Deleteと同じ期限で削除し、削除済みInstanceを復活させてはいけない。

---

# 15. 同期周期

V1はPolling。

```text
Google → TimeTree Incremental
約1分

TimeTree → Google Incremental
約5分

TimeTree-MCP Full Reconcile
約1時間

TimeTree-Exporter Independent Verify
約24時間 / 手動verify / 障害時
```

TimeTree側は非公式API依存のため、必要以上の高頻度Pollingを行わない。

---

# 16. Incremental Sync要件

## 16.1 Google

初回：

```text
Full Sync
↓
nextSyncToken保存
```

以後：

```text
syncToken Incremental
```

V1ではRecurring Seriesを基準にするため、

```text
singleEvents=false
```

をInitial Full Syncから全Incremental Syncまで維持する。

IncrementalではInitial Full Syncと同じQuery Parameterセットを維持し、Paginationでは同じ`syncToken`と同じParameterセットに`pageToken`だけを追加する。

`syncToken`と併用不可のQuery Parameterは使用しない。

具体的なGoogle API Query Contractは詳細設計を正本とする。

`nextSyncToken`は最終Pageの成功後だけ保存する。

410 Gone時は古いtokenを破棄してFull Syncへ戻る。

cross-system mappingであるSQLite `event_links`を無条件に全削除しない。

## 16.2 TimeTree

TimeTree-MCPの`updated_after`契約を利用し、

```text
timetree_updated_after_ms
```

を保存する。

必要に応じてOverlap Windowを使い、Event ID / updated_at / Hashで重複排除する。

Initial Bootstrapでは、Full Snapshot取得**前**に`bootstrap_started_ms`を記録し、Bootstrap成功後のTimeTree watermarkをそれより後へ進めない。

これによりBootstrap処理中に発生したTimeTree変更を次回Incrementalで再取得できること。

---

# 17. 専用Google Calendar

ChatGPT Web用に専用Calendarを1つ作る。

例：

```text
TimeTree Bridge
```

V1では、

```text
1 TimeTree shared calendar
↕
1 Google Calendar
```

の1 Pairのみ扱う。

Bootstrap前のGoogle Calendarは原則空とする。

---

# 18. Operational Interface要件

Calendar Bridgeは最低限、以下の操作を持つ。

```text
bootstrap
tick / sync
doctor
status
verify
conflicts / resolve
recover
```

正式なV1 CLI表記は詳細設計で定義する。

`verify`はRead-only。

`doctor`はTimeTree-MCP / Exporter / OpenCLI / Google / SQLite等の接続・状態を確認する。

---

# 19. Security / Logging / Retry

## Credential

Gitへ保存しない：

```text
TimeTree credential
session / cookie
OAuth token
CSRF
Google Service Account private key
```

## Logging

Secretを出さない。

通常LogはID / Hash / Action中心とし、予定タイトル・説明などの個人情報は原則記録しない。

## Retry

一時的な、

```text
timeout
network error
429
5xx
```

はExponential Backoff等でRetryする。

Validation / Auth / Permission / Unsupportedは原則自動Retryしない。

---

# 20. 実行環境

V1：

```text
Windowsローカル
+
Windows Task Scheduler
```

PC停止中の同期停止は許容する。

再起動後に差分・未完了Operationから復旧できること。

Core LogicはWindows固有にせず、将来Docker / Home Server / VPS等へ移行可能にする。

---

# 21. ChatGPT × Notion

完成後は、

```text
ChatGPT Web
├─ Google Calendar → TimeTree予定
└─ Notion → Project / Task
```

を同時参照し、

> 予定の負荷を考慮したTask配置

を行えること。

例：

> 「今週のTimeTreeの予定を確認して、NotionのAI事業のタスクを無理のないように割り振って」

---

# 22. V1で作らないもの

- 独自AI Planner
- JARVIS本体
- Web UI
- Mobile App
- SaaS
- Multi-user System
- 複雑な自動スケジュール最適化
- 独自Calendar画面

AI判断はまずChatGPT Webへ任せる。

---

# 23. 将来拡張

Calendar Bridge / Calendar Coreを再利用し、

```text
JARVIS
CLI
MCP Server
別Agent
Automation
Web / Desktop App
```

からTimeTreeへアクセス可能にする。

V1では将来拡張を阻害しない構造にするだけで、これらを実装しない。

---

# 24. V1完成条件

# 25. V1完成条件

## TimeTree

- Calendar一覧取得
- target共有Calendar指定
- Read / Create / Update / Delete
- Recurrence Series CRUD
- TimeTree削除契約の実機確認
- Recurrence Exception契約の実機確認

## Sync

- TimeTree → Google Create / Update / Delete
- Google → TimeTree Create / Update / Delete
- all-dayが実機確認済みTimeTree契約とGoogle exclusive end間で正しく変換
- `start_timezone / end_timezone`が保持され、Timed Normalized Eventではeffective timezoneが確定している
- Recurrence SeriesのRRULEが双方向で成立
- RDATE / EXDATE / EXRULEはContract確認済みの種類だけSupportし、未確認種類はUnsupported
- Recurring Seriesで安全な基準Timezoneを確定できる
- Exceptionは対応または安全停止
- TimeTree Labelは`大河予定` / `共通予定`だけを同期し、それ以外の既知Labelは意図的対象外として扱う
- Label名↔`label_id`を実機で一意解決し、数値IDをハードコードしない
- TimeTree → Google → TimeTreeで`大河予定` / `共通予定`のLabel意味を保持する
- Google / ChatGPT Web由来の新規EventでLabel信号が無い場合は`大河予定`を既定とする
- Google無題Eventを推測補完せず安全停止

## Reliability

- 10回同一syncで重複0
- Live E2E用Test Artifactが本番相当Calendarへ残存しない
- Remote Write途中停止でも重複0
- 再起動後復旧
- Update / Update Conflict
- Delete / Update Conflict
- Google 410からFull Sync復旧
- Hourly MCP Full Snapshotで取りこぼし補正
- API障害時に壊れず停止
- SecretがLogへ出ない

## Verification / Fallback

- Exporterで同一Calendarの独立検証可能
- 3者Read-only verify可能
- Recurrence Exception対応時、MCP ↔ Google / SQLiteでは`kind / parent identity / original_start`を検証可能
- Exporterについては、そのOSSが実際に表現できるRecurrence情報だけを独立比較し、取得不能FieldをV1 FAIL条件にしない
- OpenCLI Base利用可能
- V1最後にTimeTree Fallback Adapter利用可能

## ChatGPT

- TimeTree由来予定をChatGPTから確認可能
- Google経由でCreate / Update / Delete可能
- Notion TaskとCalendarを同時参照可能
- Calendar予定を考慮したTask日程提案が可能
