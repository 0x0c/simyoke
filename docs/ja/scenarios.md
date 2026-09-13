[English](../scenarios.md) · **日本語**

# シナリオ仕様（オーサリングリファレンス）

[シナリオ](glossary.md#シナリオのオーサリング)は Bajutsu が永続化する **唯一の成果物**です。プレーンな YAML で書き、git でバージョン管理し、PR でレビューできます。最初の 1 回は `record`（AI）が書き、以後は人間が所有して編集します。`run` はこの構造を AI なしで実行します。

実装: `bajutsu/scenario/`（`models/` 配下の pydantic モデル。`extra="forbid"` で未知キーを拒否）。

すべての生成規則、型、既定値、検証規則を定めた **規範的な文法**は [dsl-grammar](dsl-grammar.md) にあります。このページはオーサリングガイドであり、例を使ってシナリオの書き方を示します。

関連: [cookbook](cookbook.md)（実例集） · [dsl-grammar](dsl-grammar.md)（形式文法） · [selectors](selectors.md)（セレクタとアサーションの評価方法） · [evidence](evidence.md)（証跡） · [run-loop](run-loop.md)（実行）

---

## ファイルの形

1 ファイルは **シナリオの配列**です。ファイルレベルの説明を付けたい場合は `{ description, scenarios }` のマッピングにします。`load_scenarios()` はどちらの形式も受け付けます（どちらでもないトップレベルは拒否されます）。

```yaml
- name: ...        # scenario 1
  steps: [...]
- name: ...        # scenario 2
  steps: [...]
```

ファイルレベルの説明（および任意の per-scenario `description`）を付ける場合は次のようにします。

```yaml
description: What this file covers.
scenarios:
  - name: ...
    description: What this scenario checks.
    steps: [...]
```

ファイルの説明と各シナリオの `description` は、`report.html`（サマリーヘッダーと各シナリオカード）および `bajutsu serve` の UI に表示されます。

### スキーマバージョン

マッピング形式では、トップレベルに整数の `schema` を置いて、シナリオスキーマのバージョンを示せます。`schema` を省いたファイルはバージョン 1 として扱うため、既存のシナリオはそのままで有効です。

```yaml
schema: 1
scenarios:
  - name: ...
    steps: [...]
```

実行中の `bajutsu` が理解できるものより新しい `schema` をシナリオが宣言している場合、読み込みは「未知のフィールド」という分かりにくいエラーではなく、明快なアップグレード手順のメッセージで失敗します。これは、バージョンをまたいでシナリオツリーを読み込むとき（たとえば、固定した Git の ref から config を取得したとき）に生じる状況です。現在のバージョンは `bajutsu/scenario/models/scenario.py` の `SCHEMA_VERSION` です。版上げは読み込みを壊す変更のときだけ行います。以前必須だったフィールドの意味を取り除く変更や、古い `bajutsu` が単に拒否するのではなく誤解してしまう変更が該当します。純粋に追加的なオプションフィールドは、版上げを必要としません。

## トップレベル構造（`Scenario`）

| キー | 型 | 既定 | 説明 |
|---|---|---|---|
| `name` | str | 必須 | シナリオ名（レポート / JUnit testcase / codegen のメソッド名に使う） |
| `description` | str | なし | 任意の説明文。シナリオの report カードと serve UI に表示 |
| `from` | str | なし | **来歴（provenance）**：`record` がこのシナリオを書き起こした元の自然言語ゴール（[来歴](#from来歴)）。オーサリング用のメタデータで、`run` は読みません |
| `tags` | list[str] | `[]` | 選択ラベル。CLI の `--tag` / `--exclude` で実行対象を絞る（[再利用とデータ駆動とタグ](#再利用とデータ駆動とタグ)） |
| `data` / `dataFile` | list / str | なし | データ駆動の行。インライン `data` か `dataFile`（CSV パス）で指定する。1 行 1 run に展開し `${row.col}` を置換する。両者は排他（[再利用とデータ駆動とタグ](#再利用とデータ駆動とタグ)） |
| `preconditions` | object | `{}` | テスト前の環境準備（下記） |
| `before` | list | `[]` | `steps` の前に**独立したフェーズ**として走るセットアップのステップ列。ここで失敗するとシナリオを打ち切る（[下記](#before--afterセットアップとティアダウンのフェーズ)） |
| `steps` | list | 必須 | アクションの並び（下記） |
| `expect` | list | `[]` | 全ステップ成功後の最終アサーション（[selectors](selectors.md#アサーション評価)） |
| `after` | list | `[]` | ティアダウンのルール。各エントリは `{ on: always \| success \| error, steps }` で、判定が出たあと `steps` を抜けるすべての経路で走る（[下記](#before--afterセットアップとティアダウンのフェーズ)） |
| `capturePolicy` | list | `[]` | 繰り返し発火する証跡ルール（[evidence](evidence.md#a-capturepolicyルール方式)） |
| `network` | object | なし | `{ filter: { domains: [...] } }`。`filter.domains` は、レポートの Steps タイムラインに差し込む通信を URL ホストで絞る（親ドメインはサブドメインに一致）。未指定なら全件を表示する。Network タブは常に全件を表示する（[reporting](reporting.md#reporthtml)） |
| `mocks` | list | `[]` | 決定的なネットワークスタブ。一致する送信リクエストには、ネットワークへ行かず定型レスポンスを返す（[ネットワークモック](#ネットワークモック決定的スタブ)） |
| `redact` | object | なし | 証跡を書き出す前に適用するマスク（[evidence](evidence.md#マスキングredact)） |
| `systemAlertHandling` | bool / object | なし（ON） | リアクティブな **アラートガード**。iOS バックエンドから見えない OS プロンプトを、XCUITest ではネイティブに片付ける（モデルなし、BE-0316 を再利用）。名指しできないプロンプトには手を触れず、報告する。既定は ON。`false` で無効化し、`{ rules: [{ prompt: notifications, choice: grant }] }` なら ON のまま名指ししたプロンプトに答え、`{ pollInterval: 2 }` でネイティブのポーリング間隔を変える。CLI の `--system-alert-handling`/`--no-system-alert-handling` が上書きする（[下記](#systemalerthandlingシステムアラートガード)） |
| `iosTipKitHandling` | bool | なし（オフ） | 操作をブロックしている Apple **TipKit** の tip を閉じます。フレームワークが所有する popover であり、同じ回避策をシナリオごとに手で書かずに済ませるためのガードです。ガードは tip を、閉じるための scrim（`PopoverDismissRegion`）とその tip 自身のコンテナ（`TipView`）の両方で判定します。ごく普通の `confirmationDialog` が同一の scrim を設置するため、そちらには手を触れないようにする必要があるからです。同じ理由で、tip 向けの `interrupts` エントリを手で書く場合は `TipView` を手がかりにします（`TipView` は TipKit 自身のコンテナであり、SwiftUI と UIKit のどちらの表示でも存在を計測で確認済みです）。iOS 専用（他の環境では何もしません）で、既定は**オフ**です。tip 自体がシナリオの検証対象になる場合があるからです。CLI の `--ios-tipkit-handling`/`--no-ios-tipkit-handling` が優先します |
| `permissions` | dict | `{}` | 宣言的な OS 権限の状態（`{ <service>: grant \| revoke }`）。**アプリの起動前**に適用する（[下記](#permissions起動前の権限状態)） |
| `interrupts` | list | `[]` | **予測できない時点**で現れる差し込み画面のハンドラ。各エントリは `{ condition, steps }` で、画面が現れた場所を問わず随時判定する（[下記](#interrupts予測できない差し込み画面への対処)） |

```yaml
- name: filter narrows the catalog
  preconditions:
    launchEnv: { SHOWCASE_UITEST: "1" }
  steps:
    - tap: { label: "Search", traits: [button] }
    - wait: { for: { id: search.field }, timeout: 10 }
    - type: { text: "Horse 3", into: { id: search.field } }
    - wait: { for: { id: search.row.3 }, timeout: 5 }
  expect:
    - count: { sel: { idMatches: "search.row.*" }, equals: 1 }
    - value: { sel: { id: search.count }, equals: "1" }
```

（[`demos/showcase/scenarios/search.yaml`](../../demos/showcase/scenarios/search.yaml) 実物）

## preconditions（環境準備）

実装: `scenario/models/scenario.py` の `Preconditions`。runner の `launch_driver` がこれを読んで起動手順を組み立てます（[run-loop](run-loop.md#runner実行パイプライン)）。

| キー | 型 | 既定 | 説明 | 配線 |
|---|---|---|---|---|
| `erase` | bool | 未設定（継承。config が無ければオフ） | 各テスト前にシミュレータ全体を wipe する（`simctl erase`。アプリ、データ、設定を消去する）。既定はオフ。`reinstall` が全 wipe なしでアプリを新規状態に保つので、まっさらなデバイスが必要なテストだけ `true` にする | ✅ |
| `reinstall` | `clean` \| `overwrite` | `clean` | config が `appPath` を指定したとき、各 run の前にアプリをどう再インストールするか。`clean` は uninstall してから install する（アプリとデータを fresh にする）。`overwrite` は既存アプリに上書き install する（データコンテナは保持する） | ✅ |
| `launchArgs` | list[str] | `[]` | 起動引数（config の `launchArgs` に追記する） | ✅ |
| `launchEnv` | dict | `{}` | 起動 env（`SIMCTL_CHILD_*` で注入する。config の `launchEnv` にマージする） | ✅ |
| `deeplink` | str | なし | 起動後に `simctl openurl` で開く | ✅ |
| `locale` | str | なし | 起動時に locale/言語を強制する（`-AppleLocale`/`-AppleLanguages`）。app/config の既定を上書きする | ✅ |
| `setup` | str | なし | 再利用する前段シナリオファイル（このシナリオからの相対で解決）。その steps を本編の前に実行する | ✅ |

> `launchEnv` の解決順は **config の `launchEnv` < preconditions の `launchEnv`** です（テストに近い方が優先）。`launch_driver` は `{**eff.launch_env, **pre.launch_env}` でマージします。

> `erase` の解決順は **CLI の `--erase`/`--no-erase` > このシナリオ自身の `erase` > target config の
> `run_defaults.erase` > 組み込みのオフ**です
> （[BE-0177](../../roadmaps/BE-0177-run-behavior-target-config/BE-0177-run-behavior-target-config-ja.md)、
> [configuration](configuration.md#設定の階層defaults--targets)）。未設定のシナリオ（多くの場合はこれ）は
> target config の既定を継承し、config 側も指定がなければオフになります。`_filter_scenarios`
> （`cli/commands/run.py`）が run の開始前にこれを解決します。

> `reinstall: clean` は権限状態（`permissions`。後述）も同時にリセットします。iOS では `simctl privacy
> reset all`、Android では `pm clear` を使います。`overwrite` はどちらもリセットしません。`overwrite` は
> シナリオ間でデータを保持するために存在し、権限の許可状態もその保持対象に含まれるため、`overwrite`
> では前のシナリオが許可した権限がそのまま見え続けます。

## systemAlertHandling（システムアラートガード）

iOS バックエンドは **SpringBoard レベルのプロンプト**（通知や App Tracking Transparency のリクエスト、"Allow Paste" など）を見ることも tap することもできません。これらのプロンプトはアプリを覆って要素ツリーを潰し、ステップを静かにブロックします。**アラートガード**がこれをリアクティブに片付けます。iOS の XCUITest バックエンドでは**決定論的なネイティブ経路**をとります（BE-0315）。BE-0316 の SpringBoard 照会を再利用してアラートが提示するボタンを把握し、規則が名指しするボタンを押します。スクリーンショットもモデルへの往復も使わないため、頻出するプロンプトを 0.1 秒を大きく下回る時間で片付け、`ANTHROPIC_API_KEY` が**なくても**動作します。決定論的な経路がどれも対処できない場合（capability を持たないバックエンド、どの規則も同定しないアラート、SpringBoard の照会が列挙できず、しかもどの規則も同定しない画面。規則が同定するものは、後述のツリー内の経路が片付けます）、**ガードは何もしません**（[BE-0402](../../roadmaps/BE-0402-run-alert-guard-drop-vision-fallback/BE-0402-run-alert-guard-drop-vision-fallback-ja.md)）。ブロックされたステップまたは `wait` は、ガードを設定していないときとまったく同じように自身のタイムアウトまで進み、そのタイムアウトが**ガードの見たものを名指しします**。`wait timeout: for {'id': 'submit'} (10.0s) — an unhandled system alert is blocking the screen (buttons: Allow, Don't Allow)` の形、何も列挙できなかった場合は `… — the screen appears blocked, possibly by a system alert or another overlay outside the app's view` という曖昧な形です。BE-0402 より前は、この場合にスクリーンショットを読む AI 視覚ガードへフォールバックしていました。`run` はもうそれをしないため、**どのフラグを付けてもモデルに到達しません**。（`record` と `crawl` はオーサリング向けにそのガードを保持します。[詳細](recording.md#システムアラートの自動対処)）`wait` ステップ（`for`/`gone`/`settled`/`screenChanged`）では、ガードは **wait の途中でも**発火します。ネイティブ経路が独自の間隔（既定は 1 秒）で SpringBoard をポーリングするため、wait 自体のタイムアウトを待たず、ステップが失敗する前に回復できます（BE-0269）。

これは **既定で ON** で、**ステップ（または `expect`）がブロックされたとき、あるいはガード対象の `wait` でネイティブのポーリングがアラートを見つけたとき**に発火します。そのため、成功するシナリオは余計な処理をしません。`ANTHROPIC_API_KEY` は**不要**で、参照もしません。シナリオごとに動作を変えるには `systemAlertHandling` を使います。

**`rules` がガードの宣言のすべてです**（[BE-0406](../../roadmaps/BE-0406-system-alert-declared-prompts/BE-0406-system-alert-declared-prompts-ja.md)）。シナリオは、現れると見込むプロンプトと、それぞれに返す選択を書きます。どの応答経路も、その宣言だけを見て一致を判定します。BE-0401 が導入した順序付きの `labels` は BE-0406 で削除しました。ボタンのラベルが名指しするのはボタンであって、そのボタンが載っているアラートではありません。「Cancel」も「Close」もアプリの通常の語彙ですから、ラベルはシナリオが記述していない画面へのタップまで許してしまっていたのです。`visionInstruction` は vision フォールバックを方向づけていましたが、いまはどのコマンドにも届きません。**`run` はこれを拒否し**（後述）、`record` と `crawl` は自由記述を自身の `--alert-vision-instruction` フラグからだけ読み、シナリオからは読みません。ON と OFF は真偽値が担うので、マッピングを書けば常に ON です。

| 形 | 意味 |
|---|---|
| （省略） | ON。ただしガードは自分からはどのプロンプトにも答えず、出会ったアラートをブロックされたステップの失敗理由に書き出す |
| `systemAlertHandling: false` | このシナリオでは無効 |
| `systemAlertHandling: { rules: [{ prompt: notifications, choice: grant }] }` | ON。**名指しした対応済みプロンプト**に、他のプロンプトとどのラベルを共有していても、その規則自身の選択で答える |
| `systemAlertHandling: { visionInstruction: "tap Allow" }` | **どのコマンドにも届きません。** `run` は黙って無視せず、**どのシナリオも実行する前に失敗します**（[BE-0402](../../roadmaps/BE-0402-run-alert-guard-drop-vision-fallback/BE-0402-run-alert-guard-drop-vision-fallback-ja.md)）。`record` と `crawl` はシナリオのこのキーを読みません。スキーマが残しているのは、書いてしまったファイルにこのメッセージを返すためだけです |
| `systemAlertHandling: { pollInterval: 2 }` | ON。ネイティブの presence 照会を既定の 1 秒ではなく 2 秒間隔でポーリングする |

```yaml
- name: grant notification permission
  systemAlertHandling:
    rules:
      - prompt: notifications
        choice: grant       # accept the prompt instead of leaving it alone
  steps:
    - tap:  { id: sys.requestNotif }
    - wait: { for: { id: sys.notif.authorized }, timeout: 4 }   # the guard taps Allow, then this passes
```

許可は規則で書いてください。`visionInstruction` はシナリオからどのコマンドにも届かないため、`run` の下では実行できない答えを名指しすることになります。プロンプトをそのまま残してファイルが許可と書いた相手を**拒否**するのではなく、`run` はどのシナリオも実行する前に呼び出し全体を拒否します。BE-0401 が単一の `instruction` キーを分割してこの反転を可視化し、BE-0402 が反転に到達する最後の道筋を取り除きました。

ツリー内のタップで届くプロンプトに規則を書くと、iOS ではもう 1 つ、ツリー内の経路も有効になります。相手は SpringBoard のアラートではありません。いま対応しているのは `savePassword` の 1 つだけです。iOS はこの「パスワードを保存」アラートを**アプリ自身の**プロセスに出すので、そのボタンはラベルを持ち識別子を持たない形で要素ツリーに現れ、SpringBoard の照会には決して映りません。つまりツリー内のタップだけが、これを片付けられます。

そのタップは同じ `pollInterval` で間隔を空け、しかも直前の SpringBoard 照会が空だったポーリングでのみ撃ちます。XCUITest は要素への操作を合成する前に、割り込んでいるプロセス外のアラートを解決してしまい、アプリのツリーからはそのアラートが見えないからです。したがって 2 つのプロンプトが同時に出ているときは、まず SpringBoard のアラートに答え、そのあとでアプリ側のアラートをツリーから片付けます。

割り込んできたアラートがどのボタンを受け取るかも、利用者の方針が決めます。XCUITest は割り込まれた操作より先にそのアラートを解決し、放っておけばアラート自身の**デフォルト**ボタンで答えます。`rules` が拒否したはずの権限を許可し、しかも実行結果には何も残りません。そこで runner は、`rules` が名指ししたボタンを押す割り込み監視を入れます。判定の作法はネイティブ経路と同じで、押した結果は通常のアラートイベントとして報告されます。この監視が決して出会えない規則は、送らずに落とします。`savePassword` は別プロセスの操作に割り込むことがないからです。どの規則も同定しないアラートは、従来どおり XCUITest 自身の既定ハンドラに委ねられ、この tap 自体は変えられません。変わるのは沈黙のほうです。その割り込みに遭遇したステップや `expect` は、あなたに代わって何かが答えたかのように続けるのではなく、アラートが差し出したボタン名を添えて失敗します。
（[`demos/showcase/scenarios/save_password_browser.yaml`](../../demos/showcase/scenarios/save_password_browser.yaml) 実物）

規則が名指しするのはプロンプトです。ガードは、そのプロンプトを画面上で同定できたときにだけ押します。同定に使うラベルがすべて、それぞれちょうど 1 つずつ揃っている必要があります。どの規則も同定しなかったアラートには手を触れず、ブロックされたステップ自身の失敗理由に書き出します。当て推量のボタンを押すことはありません。

CLI の `--system-alert-handling` / `--no-system-alert-handling` フラグは**全シナリオを上書き**します。無指定ならシナリオごとの既定が使われます。その run にだけキーを与えるフラグは `--alert-poll-interval` の 1 つだけです。入る位置はシナリオとターゲット config の中間です（後述の[層の重ね方](#層の重ね方1-つのキーは-3-か所から届く)を参照）。
（[`demos/showcase/scenarios/permission.yaml`](../../demos/showcase/scenarios/permission.yaml) 実物）

### 通知バナーはタップせずスワイプで消す

フォアグラウンドの**通知バナー**は、システムアラートと同じようにステップへ割り込みます。答えるのも同じ割り込み監視です。ただし `rules` が答えることはありません（[BE-0416](../../roadmaps/BE-0416-ios-notification-banner-swipe-dismiss/BE-0416-ios-notification-banner-swipe-dismiss.md)）。

バナーはアラートではありません。規則が名指しできるボタンを 1 つも持ちません。タップできる領域は 1 つだけで、そこに載っているのは通知本文です。その領域を押すと通知元のアプリが開き、実行中のシナリオから離れてしまいます。

そこで runner は、バナー自身の実測フレームを起点に上方向へスワイプします。割り込みを引き受けたと宣言する前に、バナーが消えたことを確認します。確認が取れなかったスワイプは、割り込みを XCUITest に返します。XCUITest 自身のハンドラもバナーを片付けるので、どちらの経路でもステップの操作はシナリオが名指しした要素に着弾します。

**消えたことを確認できた**場合に限り、割り込まれたステップの記録として実行結果に残ります。種別はアラートとは別に立てます。押したボタンが存在しないので、並んで記録される文字列は通知本文そのものです。この種別があることで、読み手はその文字列をガードが選んだボタンだと読み違えずに済みます。

確認が取れなかったスワイプは、この記録を残しません。runner 自身の出力にログを書くだけです。実行結果に現れるのは、割り込まれたステップがいつもより時間を要したという事実だけです。

バナーについて宣言できることは何もありません。この経路は `systemAlertHandling` の有無にかかわらず動きます。BE-0416 より前は、ガードが有効な実行でバナーがアラート経路に入っていました。どの規則にも一致せず、ステップは失敗しました。その理由には、期待したボタンとして通知本文が並びました。

### 複数のプロンプトに違う答えを返す: `rules`

1 つのオンボーディングの流れが複数のプロンプトに出会い、それぞれに違う答えを返したい場面はよくあります。1 つの規則は、対応済みのプロンプト 1 つと選択 1 つを組にします。語彙は `handleSystemAlert` 自身の `prompt` / `choice` をそのまま再利用します。こうして、ファイルには「押されても構わない語」ではなく「現れると見込むプロンプト」が書かれます。

```yaml
- name: onboarding — accept notifications, refuse tracking
  systemAlertHandling:
    rules:
      - prompt: notifications
        choice: grant
      - prompt: tracking
        choice: deny
  steps:
    - tap:  { id: onboarding.start }
    - wait: { for: { id: home.title }, timeout: 10 }
```

ガードは、規則の順序ではなく、その規則のプロンプトから画面上のどのアラートかを同定します。そのプロンプトを同定するラベルが、実行時のロケールで解決した形でアラート上に揃っていなければなりません。同じプロンプトを指定する規則が2つあると解析時に失敗します。文脈や iOS のバージョンで見え方が変わるプロンプトは、その見え方の数だけ照合用の形を持ちます。ですから 1 つの規則のままで、そのプロンプトのどの見え方にも答えます。

どの規則も同定しなかったアラート、つまりラベル表の外にあるものや、どちらの応答経路も持たないバックエンド上のあらゆるアラートには手を触れず、ブロックされたステップ自身の失敗理由に書き出します。この沈黙こそが狙いです。ガードが答えるのは、シナリオが記述した画面だけになります。

**すべてのプロンプトがすべての経路に届くわけではありません。** ラベル表は、プロンプトごとに、3 つの面のうちどれで宣言し応答できるかを記録しています。

| `prompt` | `handleSystemAlert` ステップ | ガードのネイティブ SpringBoard 照会 | ガードのツリー内タップ |
|---|---|---|---|
| `notifications` | ✅ | ✅ | — |
| `tracking` | ✅ | ✅ | — |
| `paste` | ✅ | ✅ | — |
| `savePassword` | — | — | ✅ |

はじめの 3 つは SpringBoard が持ち主なので、ステップとネイティブ照会の両方が届き、ツリーには現れません。`savePassword` はちょうど裏返しです。iOS がアプリ自身のプロセスに出すため `springboard.alerts` は決して列挙できず、ツリー内のタップだけが片付けられます。したがって `savePassword` を名指しした `handleSystemAlert` ステップは解析時に拒否されます。このステップは SpringBoard の照会しか読まないので、空のボタン一覧を締め切りまでポーリングするしかないからです（[BE-0406](../../roadmaps/BE-0406-system-alert-declared-prompts/BE-0406-system-alert-declared-prompts-ja.md)）。

他の 3 つのプロンプトが 1 通りの見え方しか持たないのに対し、`savePassword` は 3 通りの見え方を持ちます。

| 見え方 | 英語 | 日本語 |
|---|---|---|
| Web フォーム | Save Password / Never for This Website / Not Now | パスワードを保存 / このWebサイトでは保存しない / 今はしない |
| アプリ自身の入力欄、iOS 18.6 | Save Password / Not Now | パスワードを保存 / 今はしない |
| アプリ自身の入力欄、iOS 26.5 | Save / Not Now | 保存 / 今はしない |

それでも 1 つの規則で 3 通りすべてを覆えます。`choice: grant` は動く許可側のボタンに追随し、`choice: deny` は拒否側のボタンを押します。拒否側はどの見え方でも「今はしない」です。対応している言語は、他のプロンプトと同じく英語と日本語です。

### 層の重ね方：1 つのキーは 3 か所から届く

1 つの設定は、シナリオ、コマンドライン、ターゲット config（[BE-0177](../../roadmaps/BE-0177-run-behavior-target-config/BE-0177-run-behavior-target-config-ja.md)）の 3 か所から run に届きます。キーがリストとスカラーのどちらかによって、2 つの規則がすべてを覆います。

| キー | 型 | 層の合わせ方 |
|---|---|---|
| `rules` | リスト | 内側の層から順に連結：シナリオ、次にターゲット |
| `pollInterval` | スカラー | 値を持つもっとも内側の層が勝つ：シナリオ、無ければコマンドライン、無ければターゲット |
| ON / OFF | スカラー | `--system-alert-handling` / `--no-system-alert-handling`、無ければシナリオ、無ければターゲット、無ければ ON |

リストを連結にするのは、両方の層のエントリが残るからです。シナリオの答えを先に試し、シナリオが答えなかったものにはターゲットの答えが届きます。スカラーは値を 1 つしか持てないので、もっとも内側の層が勝ちます。どの層の宣言も他の層に消されません。BE-0401 が確立しようとした性質はここにあります。`rules` が 2 層しか使えないのは、プロンプトと選択の組をフラグの値 1 つで読みやすく運べないからです。

**宣言が来た層ではなく、限定の度合いが衝突を決めます**。ターゲット config の tracking プロンプト向けの規則は、シナリオが notifications にしか規則を書いていなくても tracking に答えます。どの規則も、名指しした 1 つのプロンプトにだけ届くからです。両方の宣言が有効なままです。ターゲットの規則を上書きするには、シナリオ側に同じプロンプトの規則を書きます。上書きが及ぶ範囲は、名指しした 1 つのプロンプトだけです。自前の規則を持つシナリオにターゲットの規則が実際に答えるとき、ガードは構築時にその旨を通知します。通知は、影響を受けたシナリオとプロンプトの組ごとに 1 回だけ出ます。

### 旧来のキーからの移行

BE-0401 と BE-0406 は下記のキーをエイリアスなしで削除しました。書いた内容と違う挙動に解釈されるのではなく、置き換え先を名指しするエラーで読み込みに失敗します。

| 削除されたもの | 代わりに書くもの |
|---|---|
| `labels: ["Allow"]` | `rules: [{ prompt: notifications, choice: grant }]` — ラベルはどのプロンプトも名指ししていなかったので、現れると見込むプロンプトを書く（BE-0406） |
| `instruction: ["Allow"]` | 同じ規則の形。BE-0401 がこのキーを `labels` に改名し、BE-0406 がその `labels` を削除した |
| `instruction: "tap Allow"` | 同じ規則の形。BE-0401 の改名先は `visionInstruction` だが、BE-0402 でどのコマンドにも届かなくなった |
| `enabled: false` | `systemAlertHandling: false` |
| `enabled: true` | マッピング（または `systemAlertHandling: true`）。マッピングは常に ON |
| `alertHandling:` / `dismissAlerts:` | `systemAlertHandling:` |
| `--alert-labels "Allow,OK"` / `--alert-instruction "Allow,OK"` | シナリオまたはターゲット config の `rules` エントリ。プロンプトと選択の組をフラグの値 1 つで読みやすく運べないため、フラグは無い。serve の HTTP API に `alertLabels` を載せた `run` リクエストは、`systemAlertHandling.rules` を名指しする 400 で拒否される |
| `--alert-instruction "tap Allow"` | `--alert-vision-instruction "tap Allow"`（`record` / `crawl` 専用。`run` は BE-0402 で廃止） |
| `--alert-handling` / `--dismiss-alerts` | `--system-alert-handling` |

空の `visionInstruction` もエラーになります。以前は黙って正規化され、既定の無害な方針へ落ちていました。打ち間違いがファイルの記述と正反対の答えを返していたわけです。

このリアクティブなガードと、下記のプロアクティブな `handleSystemAlert` ステップは、SpringBoard が持つプロンプトについては**同じ**ネイティブ機構（BE-0316 の照会と tap）を共有します。違うのは*いつ*発火するかです。ガードはプロンプトが現れた場所で自動的に、ステップは作者が置いた 1 か所で発火します。

## handleSystemAlert（決定的なシステムアラートステップ）

上記の `systemAlertHandling` は**リアクティブなガード**です。プロンプトが現れた場所で自動的に発火します。`handleSystemAlert` はそのプロアクティブな対の一方です。プロンプトが現れると見込んだ地点に作者が明示的に置く**決定的なステップ**で、プロンプトのボタンをネイティブなアクセシビリティ照会で tap します。**スクリーンショットもモデル呼び出しもありません**（[BE-0316](../../roadmaps/BE-0316-ios-permission-alert-step/BE-0316-ios-permission-alert-step-ja.md)）。リクエストと許可の流れ自体をテストしたいときに使います。OS の権限リクエストを発火させ、続いて現れるプロンプトを決定的に許可または拒否します。

```yaml
- name: grant the notification prompt mid-flow
  steps:
    - tap: { id: perm.requestNotif }                              # OS の権限リクエストを発火させる
    - handleSystemAlert: { sel: { label: "Allow" }, timeout: 5 }  # プロンプトのボタンを label で tap する
    - wait: { for: { id: perm.notif.authorized }, timeout: 5 }    # 許可され、アプリの状態が更新される
```

許可ではなく拒否するには、拒否側のボタンを指定します（`handleSystemAlert: { sel: { label: "Don't Allow" }, timeout: 5 }`）。

- **`sel` は label 系のみです。** SpringBoard のアラートボタンは、アプリが割り当てた identifier も trait も value も持たず、見えているテキストしか持ちません。そのため `sel` は `label` / `labelMatches` / `index` を受け付け、`id` / `idMatches` / `traits` / `value` / `within` はパース時に拒否します。
- **run が一致させるべき label は、ターゲットの [`locale`](configuration.md#設定の階層defaults--targets) が描画するものです。** プロンプトを所有しているのは SpringBoard なので、以前は Simulator がたまたま持っていたシステム言語で描画されていました。つまり `label: "Allow"` が通るのは英語の環境だけで、日本語の環境では失敗しました。現在は、アプリを起動する前に Simulator 自身のシステム言語をその `locale` に固定するため、CI でも、同僚の Mac でも、コントリビューターの Simulator でも、`label` / `labelMatches` は同じように解決します（[BE-0320](../../roadmaps/BE-0320-ios-system-alert-locale-determinism/BE-0320-ios-system-alert-locale-determinism-ja.md)）。
- **`timeout` は必須です。** `wait` とまったく同じで、プロンプトを待つ条件待機には明示的な上限が要ります。ステップはプロンプトを待ち込んでから tap します。固定の sleep はありません。
- **待っているあいだもリアクティブなガードが動きます**（[BE-0406](../../roadmaps/BE-0406-system-alert-declared-prompts/BE-0406-system-alert-declared-prompts-ja.md)）。この待機を持っているのはバックエンドではなくランナー自身です。そのため `systemAlertHandling` は、ステップがタイムアウトして失敗したあとではなく、ステップの最中に割り込みを片付けます。これが効くのは、別のプロンプトが先に画面を取ってしまった場面です。iOS のパスワード保存アラートはアプリ自身のプロセスから出るので SpringBoard の照会からは見えず、画面を覆ったまま、このステップが待っているプロンプトを iOS に出させません。ここでガードが動かなければ、ステップは `timeout` を使い切り、一度も見ていない権限プロンプトを名指しして失敗します。ガードはこのステップ自身のプロンプトには手を触れないので、シナリオが同じプロンプトに反対の `choice` を持つ `rules` を書いていても、決めるのはステップのほうです。
- **0 件のときと複数件のときは、どちらも失敗します。** `timeout` 以内にプロンプトが現れなければステップは失敗します。label に一致するボタンを複数見つけたときは曖昧と見なし、`index` で n 番目を選んだ場合を除いてタップしません。あらゆる[セレクタ](selectors.md)が従う規則を、アラートのボタンに当てはめたものです。曖昧さは即座の失敗にはせず、期限までポーリングします。再描画で解消することがあるからです。期限の時点でも重複が残っていれば、ステップはその旨を述べて失敗します。タイムアウトは**ステップが実際に見たもの**を名指しします。何も出ていなかったのか、セレクタの名指ししないアラートだったのか、label を 2 つ持つアラートだったのかを、次のように書き分けます。

  ```text
  no system alert appeared within 5.0s: {'label': 'Allow'}
  no system alert button matching {'label': 'Allow'} appeared within 5.0s (the alert on screen offered: Save Password, Not Now)
  system alert button {'label': 'Allow'} is ambiguous and stayed ambiguous for 5.0s (the alert on screen offered: Allow, Allow) — add index to pick one
  ```

  ガードが片付けられないプロンプトに出会っていれば、ブロックされた `wait` と同じように、ガード自身の注記も添えられます。
- **iOS（XCUITest）専用です。** この能力を宣言するのは iOS バックエンドだけなので、Android や web バックエンドに対して `handleSystemAlert` を指定したシナリオは、デバイスを操作する前の **preflight** で失敗します。Android はシステムダイアログを通常の要素ツリーに出すため、そこでは素の `tap` で届きます。web バックエンドには OS レベルのプロンプト自体がありません。

`handleSystemAlert` と、隣り合う 2 つのアラート系フィールドのどちらを選ぶかの目安を次に示します。

| フィールド | 用途 | タイミング | 仕組み |
|---|---|---|---|
| `permissions` | そもそも避けられる OS 権限プロンプト | 起動前、アプリが動き出す前 | 決定的なデバイス操作 |
| `handleSystemAlert` | **既知の**、途中で tap するつもりのプロンプト | 作者が置いた明示的なステップ | 決定的（ネイティブなアクセシビリティ tap） |
| `systemAlertHandling` | **想定外**のプロンプト。プロセス外のものと、プロセス内のパスワード保存シート | ステップや wait がブロックされたときに反応 | XCUITest ではネイティブの SpringBoard 照会（モデルなし、BE-0316 を再利用）と、その照会に映らないシートへのツリー内タップ。どの規則も同定しないプロンプトは失敗理由に書き出す |

### テキストではなく意図で指定する

`permissions` では先回りできないプロンプトが 3 つあります。通知の許可、App Tracking Transparency（ATT）、そしてプロセスをまたぐペーストの同意です。通知の許可は Transparency, Consent, and Control（TCC）のサービスではなく、ATT には `simctl` の切り替え手段がまったくありません。ペーストの同意を iOS は TCC の `kTCCServicePasteboard` として記録しますが、このサービスにも `simctl` の切り替え手段はありません（[BE-0369](../../roadmaps/BE-0369-ios-paste-consent-prompt-choice/BE-0369-ios-paste-consent-prompt-choice-ja.md)）。この 3 つについては、`sel` の代わりに `prompt` と `choice` を指定できます。固定した `locale` が描画する label は run が解決します（[BE-0320](../../roadmaps/BE-0320-ios-system-alert-locale-determinism/BE-0320-ios-system-alert-locale-determinism-ja.md)）。

```yaml
- handleSystemAlert: { prompt: notifications, choice: grant, timeout: 5 }
```

`prompt` は `notifications`、`tracking`、`paste` のいずれかで、`choice` は `grant` か `deny` です。ガード自身の `rules` は、このステップが取れない 4 つ目のプロンプト `savePassword` も取ります。ステップのほうは解析時にこれを拒否します（上記の[面の一覧](#複数のプロンプトに違う答えを返す-rules)を参照）。ボタンを意味で指定するため、同じファイルが `en_US` でも `ja_JP` でもプロンプトを許可します。どちらの言語のテキストも作者が書き写す必要はありません。これは英語だけを使う場合にも役立ちます。英語の拒否ボタンのアポストロフィは、手で打った label が持つ ASCII 文字ではなく、活字体のアポストロフィ（`Don’t Allow`、`Don’t Allow Paste`）だからです。

この対応表がまだ扱っていない言語（現時点では英語と日本語のみ）の locale を指定した場合、推測した label を tap するのではなく、扱える言語を名指ししてステップが明示的に失敗します。ほかのアラートは、これまでどおり `sel` でボタンを指定します。

使う前に知っておきたい制限が 2 つあります。

- **Simulator 専用です。** 言語の固定は `simctl` の操作なので、`xcuitest.deviceType: device` の target は実機が持つシステム言語のまま動きます。この形では、画面に出ている保証のない label を解決してしまいます。実機ではボタンを `sel.label` で指定してください。
- **割り込み監視も英語を前提としていません。** `rules` の 1 エントリは、監視に届く前に固定した言語向けラベルへ解決されており、その背後にはもう組み込みのフォールバック一覧がありません。どの規則も同定しないアラートは、どの `locale` でも常に辞退されます。その割り込みに遭遇したステップや `expect` は、ボタンを推測する代わりに、差し出されたボタン名を添えて失敗します。run 自身に決めさせたいプロンプトには、規則を書いてください。

（実物は [`demos/showcase/scenarios/permission_system_alert.yaml`](../../demos/showcase/scenarios/permission_system_alert.yaml) と [`demos/showcase/scenarios/paste_system_alert.yaml`](../../demos/showcase/scenarios/paste_system_alert.yaml)）

## permissions（起動前の権限状態）

`systemAlertHandling` は権限プロンプトが**現れた後**にしか反応せず、できるのは tap だけです。権限を**取り消す**ことも、アプリが既知の状態から起動することを保証することもできません。権限があらかじめわかっている場合、`permissions` を使えば**アプリのプロセスが起動する前**にその状態を設定できるため、プロンプトはそもそも現れません。モデルを一切呼ばない、決定的でマシンチェック可能なデバイス操作です（[BE-0276](../../roadmaps/BE-0276-scenario-permission-state/BE-0276-scenario-permission-state-ja.md)）。

```yaml
- name: profile — camera already granted
  permissions:
    camera: grant
    location: grant
    contacts: revoke
  steps:
    - tap: { id: profile.avatar.upload }   # no camera-permission prompt — already granted
```

各エントリは `<service>: grant | revoke` の形を取ります。`<service>` はバックエンドに依存しない小さな語彙で、`location`、`camera`、`microphone`、`contacts`、`photos`、`calendar`、`notifications` のいずれかです。各バックエンドは、この語彙をそれぞれのネイティブな仕組みにマップします。

- **iOS** は `simctl privacy <udid> <grant|revoke> <tcc-service> <bundle>` を実行します。SpringBoard の権限プロンプトが参照するのと同じ TCC（Transparency, Consent, and Control）データベースです。
- **Android** は `pm grant` / `pm revoke` を実行し、config レベルの `grantPermissions` リスト（[drivers](drivers.md)）を支える仕組みを再利用します。シナリオの `permissions` は、この config レベルの既定の上に重なり、config が許可した権限を取り消すこともできます。

**iOS には `notifications` に対応する TCC サービスがありません**（iOS の通知許可は TCC の管轄外です）。そのため、iOS をターゲットにしたシナリオが `notifications` を指定すると、デバイスを一切操作する前の **preflight** が対応していない権限として個別に名指しして失敗します。そのプロンプト自体への対処は、引き続き `systemAlertHandling` が担います。Android の `POST_NOTIFICATIONS` は実行時権限です（API 33 以降）。そのため Android は語彙のすべてに対応します。選んだバックエンドが対応しないサービスの組み合わせも、同じように preflight が個別に名指しして失敗させます。

`permissions` に対応する XCUITest / Espresso 側のコードはないため、`codegen` はコードを生成する代わりに、サービスごとにラベル付きの `// TODO` を出力します。フィールド自体は、生成したテストの起動処理より前に bajutsu 自身が適用します。

## interrupts（予測できない差し込み画面への対処）

`if` ステップ（[下記](#条件分岐-if)）は、ステップ列の**1 か所**で条件を判定します。分岐したい画面の直前にどのステップが来るかがわかっているときは、これが適切な道具です。画面の出現がどの単一ステップにも結び付かないときは、`if` は向きません。オンボーディングの重ね表示、チュートリアル、アクセシビリティツリーに見える権限プロンプトを考えます。いずれも、アカウントの状態やネットワークの間合い、A/B の振り分け次第で、想定より数ステップ早く、あるいは遅く現れます。まったく現れないこともあります。`if` を 1 つ置いても、その置いた位置にちょうど画面が現れたときしか捕まえられません。それ以外のタイミングはすり抜け、想定していない画面に対して残りのシナリオが失敗します。

このタイミングの読めない画面を扱うのが `interrupts` です。各エントリは `condition`（`if` が使うのと同じアサーション DSL）と、画面を片付ける `steps` を指定します。ランナーは各エントリを**随時**判定し、画面がステップ列のどこに現れても評価して、条件が一致すればエントリの `steps` を実行します。この判定が追加コストなしで済むのは、そのステップのために読んだばかりのツリーに乗れる場合だけです。`wait` のポーリングの各回と、持ち越したツリーがないときに `screenChanged` ポリシー付きステップが読む `before` が、これにあたります。残りの `wait` 以外のステップは、判定のために `driver.query()` を 1 回余分に呼びます。前のステップから持ち越したツリー（BE-0234）を `before` に使う `screenChanged` ポリシー付きステップも同じです。ガードはそれを古い可能性があるものとして読み直すため、同じ 1 回を払います。ハンドラの実行後は、中断されたステップが元の位置から再開します。`wait` は元のタイムアウトに向けてポーリングを続け、操作ステップは自分の操作を試みます。そのため、`if` を置く 1 か所を作者があらかじめ言い当てる必要はありません。

```yaml
# config.yaml — アプリ全体の既定。このアプリのオンボーディング画面を全シナリオで扱う
targets:
  myapp:
    interrupts:
      - condition: { exists: { id: onboarding.skip } }
        steps:
          - tap: { id: onboarding.skip }
```

```yaml
# scenario.yaml — このシナリオ独自の追加。config レベルのリストに後続として連結される
- name: log in
  interrupts:
    - condition: { exists: { id: att.dialog } }   # App Tracking Transparency のプロンプト
      steps:
        - tap: { id: att.allow }
  steps:
    - tap:  { id: login.button }
    - wait: { for: { id: home.title }, timeout: 10 }   # 途中で差し込み画面が出ても片付けてから、この wait が通る
```

**config** レベル（`targets.<name>.interrupts`）に置いた `interrupts` リストはアプリ全体の既定です。シナリオ独自の `interrupts` はその後ろに**連結**され、config のエントリを先に判定します。`systemAlertHandling` が従うのと同じ、config が先でシナリオが後という重ね方です。エントリの `steps` は、`if` の分岐とまったく同じように、囲んでいるシナリオの `vars.*` バインディングを共有します。シナリオ側のハンドラの `steps` では、ほかのステップと同じように[コンポーネント](#再利用とデータ駆動とタグ)を `use` で呼び出せます。展開は run の前に済みます。config レベルのエントリでは `use` を使えません。ターゲットの config はコンポーネント展開を通らないため、`targets.<name>.interrupts` の下に置いた `use` は config の読み込み時に拒否されます。ステップを直接書くか、ハンドラをシナリオ側へ移してください。ハンドラ自身の `steps` が `condition` を解消しないとき（セレクタの誤りや、同じ内容で再描画され続ける画面）は、エントリはステップごとに小さな上限回数までしか発火しません。そのあとはステップが本来の結末（成功、失敗、タイムアウト）へ戻ります。設定を誤ったエントリは、実行をハングさせずに、ステップをきれいに失敗させます。

判定に使うのは決定的なアサーション DSL であり、モデル呼び出しではありません。そのため `interrupts` は `run` の判定に AI を持ち込みません。BE-0402 以降は `systemAlertHandling` も同じです。両者の違いは届く先です。アラートガードは、アクセシビリティツリーに**見えない**プロセス外のシステムプロンプトを SpringBoard の照会で扱います。一方 `interrupts` は、ツリーに**見える**画面を、マシンチェック可能な条件で扱います。どちらを選ぶかの目安を次に示します。

| フィールド | 対象 | タイミング | 仕組み |
|---|---|---|---|
| `if` | ステップ列の**わかっている**位置に出る画面 | 台本どおりの 1 回の判定 | 決定的（アサーション DSL） |
| `interrupts` | **予測できない**位置に出る、ツリーに見える画面 | 全体を通して随時判定 | 決定的（アサーション DSL） |
| `handleSystemAlert` | 途中で tap するつもりの**既知の**プロセス外プロンプト | 作者が置いた明示的なステップ | 決定的（ネイティブなアクセシビリティ tap） |
| `systemAlertHandling` | **想定外**のプロンプト。プロセス外のものと、プロセス内のパスワード保存シート | ステップや wait がブロックされたときに反応 | XCUITest ではネイティブの SpringBoard 照会（モデルなし、BE-0316 を再利用）と、その照会に映らないシートへのツリー内タップ。どの規則も同定しないプロンプトは失敗理由に書き出す |
| `permissions` | そもそも避けられる OS 権限プロンプト | 起動前、アプリが動き出す前 | 決定的なデバイス操作 |

「この条件をテスト全体を通して随時判定する」に対応するネイティブな XCUITest / Espresso / Playwright の構文はありません。そのため `codegen` はコードを生成する代わりに、フィールドと設定された各条件を名指ししたラベル付きの `// TODO` を出力します。`bajutsu run` が忠実に実行する経路です。

## `before` / `after`（セットアップとティアダウンのフェーズ）

`preconditions.setup`（[上記](#preconditions環境準備)）は前置きのシナリオファイルを指名し、ランナーはその前置きのステップを、run が始まる前にこのシナリオ自身の `steps` の先頭へ連結します。連結された前置きのステップは、シナリオ自身のステップと見分けが付きません。レポートは 1 つの連番の並びとして表示し、前置きの失敗も、セットアップ由来であるという印のない、ふつうのステップ失敗として現れます。ティアダウンにいたっては仕組みそのものがありません。後片付けを置ける場所は `steps` の末尾だけです。しかしステップループは最初の失敗で打ち切られる（[run ループ](run-loop.md)）ため、末尾の後片付けのステップが走るのは、それより前のステップがすべて成功した run に限られます。つまり、後片付けをもっとも必要としなかった run でしか走らないのです。テストユーザーを作ってから 3 ステップ先の壊れたボタンで失敗したシナリオは、そのユーザーを残したままになります。

`before` と `after` は、この 2 つの穴を塞ぎます。`before` は最初に走る順序付きのステップ列で、レポートでは独立したセクションになり、ここで失敗すると `steps` と `expect` をまったく実行せずにシナリオを打ち切ります。`after` はルールのリストで、各ルールは結末（`always`、`success`、`error`）と、その結末のときに走らせる `steps` を組にします。ランナーはシナリオの判定が出てからルールを評価し、失敗した経路も含め、`steps` を抜けるすべての経路でこのフェーズに到達します。どちらのフィールドも通常のステップ文法とアサーション DSL をそのまま使うので、フックのステップはシナリオ自身のステップと同じだけマシンチェック可能です。また、どちらも run の `${vars.*}` バインディングを共有します（[実行時変数](#実行時変数vars)）。

```yaml
- name: sign up, then release the account
  before:
    # シードのエンドポイントは、作成したユーザーの id だけをレスポンスボディで返す
    - http: { method: POST, url: "https://api.test/users", saveBody: userId }
  steps:
    - tap:  { id: login.button }
    - type: { text: "${vars.userId}", into: { id: login.username } }
  after:
    - on: always
      steps:
        - tap: { id: session.logout }
    - on: success
      steps:
        - http: { method: DELETE, url: "https://api.test/users/${vars.userId}" }
    - on: error
      steps:
        - http: { method: POST, url: "https://api.test/diagnostics", body: '{"failed":true}' }
```

（[`demos/showcase/scenarios/before_after.yaml`](../../demos/showcase/scenarios/before_after.yaml) 実物）

同じ `on` の値を持つルールは複数書けます。同じ結末を共有するルールは宣言順に走ります。1 つの `capturePolicy` トリガーを 2 つのルールが共有できるのと同じ扱いです。あるルールの `steps` が失敗しても、フェーズは止まりません。残りのルールもそのまま走ります。後片付けの残りを飛ばすことこそ、ティアダウンが避けようとしている結末だからです。その失敗が run の判定に与える影響は、run がそれまでどうなっていたかで変わります。成功していた run では、失敗したルールがそのまま run の failure（`after: step 0 (tap): …`）になります。すでに失敗していた run なら、失敗したルールは元の failure を置き換えず、その後ろへ追記されます。読み手が最初に目にする理由を、後片付けの失敗という症状ではなく元の原因のままとするためです。

キャンセルされた run（`SIGTERM`、`serve` Web UI のキャンセルボタン）もこのフェーズに到達し、`after` は `error` の結末として振り分けられます。後片付けのルールには、キャンセルの猶予時間のうち一定の割合が与えられます。その持ち時間を使い切ると残りのルールは打ち切られます。レポートを書き出すシャットダウンの後始末を、猶予時間のうちに収めるためです。

### ターゲット設定にも同じ 2 つのフィールドがある

`targets.<name>.before` と `targets.<name>.after` はシナリオと同じ形を取り、アプリ全体の既定になります。2 つの重ね方は逆向きです。

| フィールド | 重ね方 | 理由 |
|---|---|---|
| `before` | config が先、シナリオが後 | アプリ全体の前置きが用意した状態の上に、このシナリオ自身のセットアップが積み上がる。`interrupts` が従うのと同じ、config が先でシナリオが後という重ね方 |
| `after` | シナリオが先、config が後 | このシナリオが作ったものを解放してから、アプリ全体のティアダウンがその外側を閉じる。フィクスチャ方式のテストフレームワークのセットアップとティアダウンの対が与える、後から確保したものを先に解放する順序 |

`targets.<name>.before` は `targets.<name>.setup` を置き換えるものではありません。独立したレポートのフェーズになるのは `before` だけであり、`before` フェーズは `setup` が `steps` へ連結する前置きよりも先に走ります。したがって `before` のステップは、前置きが到達する画面に依存させてはいけません。

### どれを選ぶか

近い領域に 3 つのフィールドがあり、それぞれ答える問いが違います。

| フィールド | 走る位置 | レポートでの扱い | 対象 |
|---|---|---|---|
| `before` / `after` | 独立したフェーズとして、`steps` の前と判定の後 | 独立した Before / After のブロック | 検証対象のシナリオと読み手が区別できる必要のあるセットアップとティアダウン |
| `preconditions.setup` | `steps` の先頭へ連結 | 連番のステップの一部 | 複数のシナリオで共有する再利用可能な前置きで、区別が要らない場合 |
| `capturePolicy` | ステップループ全体を通してステップごとに | ステップに紐づく証跡 | ステップが失敗したときに追加の証跡を取ること。ステップを走らせることではない |

`capturePolicy` の `on: { result: error }` トリガーと `after` ルールの `on: error` は、同じ考え方に同じ語を当てたもので、粒度だけが違います。`capturePolicy` のトリガーは、run のどこで起きたかを問わず 1 つのステップの失敗ごとに発火します。`after` のルールはシナリオ全体の判定に対して一度だけ発火します。

### `codegen` が出力するもの

`before` にはフレームワークの構文が要りません。`codegen` はそのステップを、生成したテスト本体の先頭に `// before` の区切りコメントとともにそのまま展開します。最初に走り、失敗すれば後続を打ち切るという、フェーズの意味そのままだからです。`after` には構文が要り、ターゲットごとに手段が違います。Playwright と UI Automator では、テスト本体を `try` / `catch` / `finally` で包みます。どちらのターゲットもアサーションが例外を投げるので、`catch` は判定の元になったはずの失敗をそのまま捕まえられるからです。XCUITest では代わりに `addTeardownBlock` を 1 つだけ登録します。`XCTAssert` は例外を投げずに失敗を記録するだけなので、結末は `testRun?.hasSucceeded` から読みます（[codegen](codegen.md)）。

## セレクタ（要素の指定）

セレクタは、操作またはアサーションの対象となる **どの要素か** を指定します。1 つ以上のフィールドを与え、複数指定したフィールドは **AND**（すべて一致）で評価し、最低 1 つは必須です。セレクタが一意の要素にどう絞られるか、また曖昧なセレクタが最初の一致を選ばず失敗する理由は [selectors](selectors.md) を参照してください。形式的な形は [dsl-grammar](dsl-grammar.md#2-文法の全体像) にあります。

| フィールド | 型 | 説明 |
|---|---|---|
| `id` | str \| list[str] | 完全一致の `accessibilityIdentifier`。**第一候補**（安定していてローカライズされない）。リストは候補の **OR** で、要素の id が*いずれか*に一致すればよい |
| `idMatches` | str \| list[str] | id へのグロブ（例 `"list.row.*"`。複数一致を前提とする）。リストは*いずれか*のグロブに一致すればよい |
| `label` | str | 完全一致の `accessibilityLabel`（可視テキスト）。補助や曖昧性解消に使う |
| `labelMatches` | str | label への正規表現 / 部分一致（`re.search`） |
| `traits` | list[str] | アクセシビリティ trait で絞る（部分集合判定、例 `[button]`） |
| `value` | str | 完全一致の accessibility value |
| `within` | Selector | コンテナに限定する。一致要素は、ネストしたセレクタが解決する要素の内側になければならない（入れ子可） |
| `index` | int | 複数一致の k 番目を選ぶ（負数可）。最終手段であり、順序に依存する |

```yaml
- tap: { id: counter.increment }                               # by id (recommended)
- tap: { id: [stable.refresh, stable_refresh] }                # id 候補の OR（下記参照）
- tap: { label: "Delete" }                                     # by visible label (e.g. an alert button)
- tap: { id: row.action, within: { id: list.row.3 } }          # scoped to a container's subtree
- tap: { labelMatches: "^Item ", traits: [button], index: 0 }  # first matching button, fields AND-ed
```

> まず `id` を使います。要素の集合（count / 存在確認）には `idMatches` を使います。`index` は最終手段です。順序が変わると壊れます。解決の完全な意味論は [selectors](selectors.md) にあります。

### プラットフォームをまたぐ id：候補のリスト（BE-0221）

シナリオがプラットフォーム間で共有できるのは、セレクタが `id` を使う範囲までであり、その `id` をアプリ側のどの属性が満たすかはドライバ内に閉じています。ただし SPEC の id を**そのまま**再現できないプラットフォームがあります。Android の `android:id`（Views toolkit）は `.` も `-` も許さないので、`stable.refresh` は `stable_refresh`、`search.results-empty` は `search_results_empty` として現れます。**1 つ**のシナリオをどこでもそのまま走らせるため、`id` / `idMatches` に**候補のリスト**を与えると、照合はその OR になります。

```yaml
- wait: { for: { id: [stable.refresh, stable_refresh] }, timeout: 10 }
- count: { sel: { idMatches: [stable.row.*, stable_row_*] }, equals: 5 }
```

ドット形は iOS と Android Compose（どちらもそのまま再現します）に、アンダースコア形は Android Views に一致します。あるアプリの画面に現れる形は常に一方だけなので、決定的なままです。仮に両方の形が同時に画面にあれば、そのセレクタは曖昧として即座に失敗します。OR が 2 件以上の一致を暗黙に 1 つへ絞ることはありません。これにより id 規約はシナリオに**明示的に**残り、別々の id を取り違えかねないドライバ側の暗黙の `.`↔`_` 書き換えに頼りません。showcase の共有シナリオはこれを使い、`showcase-swiftui` / `showcase-compose` / `showcase-views` が同じファイルで走ります。

## ステップ文法（`steps`）

各ステップは **ちょうど 1 アクション** と、任意の修飾子（`capture:` / `name:`）からなります。1 ステップに 2 アクション以上を書くと検証エラーになります（`scenario/models/steps.py` の `_one_action`）。

| アクション | 形 | 説明 |
|---|---|---|
| `tap` | `tap: <Selector>` | 一意解決を要求する（曖昧なら失敗） |
| `tapPoint` | `tapPoint: { x: <frac>, y: <frac> }` | セレクタではなく正規化座標（0..1、左上原点）でタップします。アクセシビリティツリーが要素として公開しない操作対象（id のないタブバーのタブなど）に使う、安定性の梯子の最下段です。`record` の vision 経路がこれを出力し、`run` はそのときの画面サイズに合わせて再生します |
| `doubleTap` | `doubleTap: <Selector>` | 解決した要素を 2 回素早くタップする |
| `longPress` | `longPress: { sel: <Selector>, duration: <sec> }` | 長押し |
| `type` | `type: { text: "...", into?: <Selector>, submit?: <bool> }` | `into` 指定時は先にフォーカスする |
| `clear` | `clear: { into: <Selector> }` | フィールドをフォーカスして現在の内容をすべて削除する。web コンテキストは非対応 |
| `delete` | `delete: { into: <Selector>, count: <int> }` | フィールドをフォーカスして末尾から `count` 文字削除する（`count > 0`）。web コンテキストは非対応 |
| `select` | `select: { into: <Selector>, mode?: "all" }` | フィールドをフォーカスして内容を選択する（`mode` 既定 `all`）。web コンテキストは非対応。iOS（XCUITest）バックエンドはネイティブに対応し、codegen もネイティブの等価物を出力する |
| `copy` | `copy: {}` | 選択中の内容をクリップボードへコピーする。事前の `select` が必要。web コンテキストは非対応。iOS（XCUITest）バックエンドはネイティブに対応する |
| `selectOption` | `selectOption: { sel: <Selector>, option: "..." }` | web の `<select>` をこの value を持つ option に合わせる。web 専用（iOS / Android は失敗する） |
| `setPickerValue` | `setPickerValue: { sel: <Selector>, value: "..." }` | ホイール型のピッカー（`UIPickerView`、ホイール表示の `UIDatePicker`）をこの value を持つ行へ動かす（[後述](#setpickervalue)）。iOS（XCUITest）専用。`sel` は 1 つのホイールを指し、複数コンポーネントのピッカーは `within` / `traits` / `index` で兄弟のホイールを区別し、コンポーネントごとに 1 ステップずつ書く |
| `swipe` | `swipe: { on: <Selector>, direction: up\|down\|left\|right }` または `swipe: { from: [x,y], to: [x,y] }` | セレクタ形と座標形は混在できない。方向指定形式は**スクロール**する |
| `drag` | `drag: { on: <Selector>, direction: up\|down\|left\|right, amount?: <frac> }` | 要素そのものを**ドラッグ**する（ハンドル／仕切り／スライダー）。スクロールではない |
| `scroll` | `scroll: { to: <Selector>, direction?: up\|down\|left\|right, within?: <Selector>, amount?: <frac>, maxScrolls?: <int> }` | `to` が画面に入るまで（慣性なしに）スクロールし、上限に達したら失敗する。`direction` は**スクロール**方向（既定 `down`）で、`swipe` とは逆向き |
| `back` | `back: {}` | 1 階層戻ります。各バックエンドがプラットフォームに合った手段（Android のシステム戻るキー、iOS の OS 提供の戻るボタン、web の履歴）を使います（[BE-0210](../../roadmaps/BE-0210-android-actuation-fidelity/BE-0210-android-actuation-fidelity-ja.md)） |
| `pinch` | `pinch: { sel: <Selector>, scale: <num> }` | 2 本指の拡縮。`scale > 0`（`>1` で拡大, `<1` で縮小） |
| `rotate` | `rotate: { sel: <Selector>, radians: <num> }` | 2 本指の回転。`>0` で時計回り |
| `handleSystemAlert` | `handleSystemAlert: { sel: <Selector>, timeout: <sec> }` | iOS SpringBoard の権限プロンプトのボタンを決定的に tap する（[下記](#handlesystemalert決定的なシステムアラートステップ)）。iOS（XCUITest）専用。`sel` は `label` / `labelMatches` / `index` のみ受け付け、run が Simulator を固定するシステム言語に対して解決する。`sel` の代わりに `prompt: notifications\|tracking\|paste` と `choice: grant\|deny` を指定すると、ボタンを意味で指定でき、run がその label を解決する（BE-0320） |
| `wait` | `wait: { for\|until: ..., timeout: <sec> }` | 条件待機（下記） |
| `assert` | `assert: [ <Assertion>... ]` | ステップ途中の中間検証 |
| `relaunch` | `relaunch: { env?: {...}, args?: [...] }` | アプリを terminate + 再起動し（launch env/args を再適用し、指定分で上書き）、ready まで待つ |
| `setLocation` | `setLocation: { lat: <num>, lon: <num> }` | シミュレータの GPS 位置を上書きする（`simctl location set`） |
| `push` | `push: { payload: {...} }` | この APNs（Apple Push Notification service）ペイロードで疑似プッシュ通知を配信する（`simctl push`） |
| `http` | `http: { method?, url, headers?, body?, status?, saveBody? }` | HTTP リクエストを送る（テストデータ準備 / Webhook / API）。`status` を検証し、ボディを `${vars.<saveBody>}` に保存する |
| `totp` | `totp: { secret, into: { var } }` | RFC 6238 の時刻ベースワンタイムパスワード（2FA）をローカルで生成し `${vars.<var>}` に入れる |
| `email` | `email: { match: { to?, subject?, subjectMatches? }, extract: { var, bodyMatches }, timeout }` | 設定したメールボックスを一致するメッセージが届くまでポーリングし、コードを `${vars.<var>}` に取り出す |
| `generate` | `generate: { random\|datetime: {...}, into: { var } }` | 乱数または現在日時の値を実行時に計算し、`${vars.<var>}` に保存する（[後述](#generate実行時に計算する値)） |
| `manual` | `manual: { label: "...", bypass?: "..." }` | `record` 中に記録される人による操作の引き取り（BE-0185）。決定的な実行時の等価物がないため、`run` 時に**明示的に失敗する**——合格を偽装しない |
| `background` | `background: {}` | アプリをバックグラウンドへ送る（Home ボタン） |
| `foreground` | `foreground: {}` | バックグラウンドのアプリを前面へ復帰する（`simctl launch`。settle 用の sleep なし） |
| `clearKeychain` | `clearKeychain: {}` | Simulator のキーチェーンをリセットする（保存済みパスワード / 証明書） |
| `clearClipboard` | `clearClipboard: {}` | Simulator のペーストボードをクリアする |
| `setClipboard` | `setClipboard: { text: "..." }` | ペースト操作のため Simulator のペーストボードにテキストを投入する |
| `overrideStatusBar` | `overrideStatusBar: { time?, batteryLevel?, batteryState?, cellularBars?, wifiBars? }` | 決定的なスクリーンショットのためステータスバーを上書きする |
| `clearStatusBar` | `clearStatusBar: {}` | ステータスバーの上書きを解除する（ライブ表示に戻す） |
| `use` | `use: { component: <file>, with?: {...} }` | 再利用コンポーネントの steps を展開する。コンパイル時マクロ（[再利用](#再利用とデータ駆動とタグ)） |
| `web` | `web: { within: <Selector>, steps: [...] }` | WebView の DOM コンテキストに入ります。`within` がホストの `WKWebView` をネイティブに解決し、入れ子の `steps` はネイティブツリーではなく正規化された DOM を対象にします（[後述](#webwebview-の-dom-コンテキストに入る)） |

修飾子:

- `capture: [<token>...]`：このステップだけの証跡（[evidence](evidence.md#b-インライン証跡)）。
- `name: <str>`：ステップ ID（証跡の出力先ディレクトリ名やレポート表示に使う）。省略時は `step<i>`。
- `from: <str>`：**来歴**（[後述](#from来歴)）。このステップを記録した元のフレーズ。オーサリング用のメタデータで、`run` は読みません。

### `tap`

```yaml
- tap: { id: counter.increment }      # exact id (recommended)
- tap: { label: "Delete" }            # exact label (for an in-app alert etc. with no id)
```

### `type`

```yaml
- type: { text: "a@b.com", into: { id: auth.email } }   # focus, then type
- type: { text: "hello", submit: true }                 # submit appends a newline / confirm (uses current focus)
```

> 実装上は、`into` を指定すると内部で対象を `tap` してから `type_text` します（`orchestrator/actions/` の `_do_action`）。

### `selectOption`

```yaml
- selectOption: { sel: { id: nav.theme-picker }, option: midnight }   # value が "midnight" の option に <select> を合わせる
```

ネイティブの HTML `<select>` は、ドロップダウンがページの要素ツリーに含まれないため、座標タップでは値を決定的に切り替えられません。`selectOption` は、ほかのアクションと同じ一意解決のコアで `<select>` を解決したうえで、表示ラベルではなく option の **value** を指定して値を設定し、`change` イベントを発火します。これにより、ユーザーが選んだときと同じようにページが反応します。指定する value は `value` アサーションが `<select>` から読み取る値と一致するので、選択結果はそのまま検証できます。これは web 専用のアクションです。`<select>` は iOS や Android にネイティブの対応物がないため、これらのバックエンドは何もせずに済ませるのではなく、「サポート外のアクション」という明確な理由でステップを失敗させます。

### `setPickerValue`

```yaml
- setPickerValue:                                                # ホイールを「大学」の行へ動かす
    sel: { within: { id: form.school }, traits: [pickerWheel] }
    value: "大学"
```

ホイール型のピッカー、つまり `UIPickerView` や、ホイール表示に切り替えた `UIDatePicker` は、iOS のフォームでありふれたコントロールです。その値を設定できるステップは `setPickerValue` だけです。ホイールの各行は個別に指定できる要素になっていないため、解決したハンドルを対象とする `tap` では特定の行に到達できません。座標を扱うステップも同様です。`swipe` / `drag` / `scroll` は距離や方向を指定するドラッグであり、ホイールを目的の値のあたりまで回せても、そこで止まる保証はありません。`tapPoint` にいたっては、すでに表示されている行を叩けるだけで、表示されていない行へホイールを動かせません。いずれの場合も、結果のアサーションはドラッグの距離が行の高さとたまたま一致することに依存します。Bajutsu がほかのすべてのステップで排除している、近似的な操作そのものです。`setPickerValue` は、セレクタが解決した要素に対して XCUITest 自身の `adjust(toPickerWheelValue:)` を呼びます。`swipe` のような座標指定ではなく、`tap` と同じハンドル指定です。

`sel` が解決すべきなのはホイールそのものですが、識別子を持つ要素がホイールであることはまれです。`UIPickerView` は識別子をピッカー側に付け、ピッカーの下にあるホイールを別の子要素として公開します。そのため、識別子だけを指定したセレクタは親であるピッカーを解決し、ステップは失敗します。`adjust(toPickerWheelValue:)` は、ホイールでない要素に対しては例外を投げるからです。上の例のように識別子と `pickerWheel` トレイトを組み合わせると、セレクタは子要素のホイールに届きます。

ホイールが持たない値を指定すると、ホイールが止まった位置をそのままにするのではなく、その値を名指しして**ステップが失敗します**。したがって、後続のアサーションが検証するのはアプリであって、ジェスチャの当たり外れではありません。

複数コンポーネントのピッカー（年のホイールと月のホイールが並ぶもの）は、コンポーネントごとに独立した `pickerWheel` 要素として現れます。`sel` は常にそのうちの 1 つを指します。区別には、どのセレクタも持っている `within` / `traits` / `index` を使い、コンポーネントごとに 1 ステップずつ書きます。コンポーネントの並び順も行のラベルも、run が固定するロケールに従います。下の例は `ja_JP` を前提としており、ホイールは年 | 月 | 日の順に並びます。ロケールを指定しない場合は `en_US` となり、月 | 日 | 年の順で、同じ行のラベルは `May` や `2016` です。`demos/showcase/scenarios/picker_wheel.yaml` はロケールを指定していないため、`en_US` の並びを前提に書いてあります。

```yaml
- setPickerValue:
    sel: { within: { id: form.birthdate }, traits: [pickerWheel], index: 0 }   # 年のホイール
    value: "2016年"
- setPickerValue:
    sel: { within: { id: form.birthdate }, traits: [pickerWheel], index: 1 }   # 月のホイール
    value: "5月"
```

`within` はフレームの包含関係で範囲を絞ります。したがって `within` が名指しするコンテナは、ホイールを収めるだけの大きさを持っていなければなりません。ホイール表示の `UIDatePicker` は、単体ではその大きさに届きません。iOS は `UIDatePicker` のコンポーネントを本来の高さで配置し、ピッカーの枠に合わせて切り詰めて表示するからです。各コンポーネントは識別子を持つピッカーよりも高いフレームを報告するため、`UIDatePicker` を名指しした `within` はどの要素にも一致しません。識別子は、コンポーネントを覆う大きさを持つ外側のコンテナに付けてください。`demos/showcase/ios/swiftui/Sources/PickerView.swift` が見出しとホイールとミラーのテキストを1つの識別子でまとめているのは、そのためです。

この指定方法は、`datePicker` の分類の隙間（[セレクタ](selectors.md#正規化トレイトtrait)）も回避します。`UIDatePicker` のコンテナ要素自体は `other` に落ちますが、`setPickerValue` が指すのはその下にあるホイールの子要素であり、そちらは `pickerWheel` に分類されるためです。

`setPickerValue` は iOS（XCUITest）のアクションです。ホイール型のピッカーは Android にも web にも対応物がありません。web で同じ意図を表すのは `<select>` であり、それを設定するのは [`selectOption`](#selectoption) です。したがって Android と web のバックエンドは `pickerWheel` ケーパビリティを公開せず、シナリオはデバイス操作が始まる前の preflight で、該当ステップの位置を名指しして棄却されます。

### `web`（WebView の DOM コンテキストに入る）

```yaml
- web:
    within: { id: checkout.webview }
    steps:
      - tap: { id: pay.submit }
      - wait: { for: { id: pay.confirmation }, timeout: 10 }
```

`web` は `within` をネイティブに解決し、ちょうど 1 つの `WKWebView` ホストを指します。入れ子の `steps` は、アプリのネイティブなアクセシビリティツリーではなく、その WebView の正規化された DOM（`data-testid` → `Element.identifier`）を対象にします。web コンテンツをネイティブアプリに埋め込んだハイブリッド画面向けの構造です（[BE-0037](../../roadmaps/BE-0037-webview-hybrid-support/BE-0037-webview-hybrid-support-ja.md)）。ブロックの `steps` を終えると、制御はネイティブドライバーに戻ります。入れ子の `steps` は、`if` や `forEach` の分岐と同じく、囲むシナリオの `vars.*` を共有します。`capture` / `extract` 修飾子は `web` ステップ自体には使えません。WebView bridge の設定（`BAJUTSU_WEBVIEW_PORT`）が必要で、未設定のときは何もせず済ませるのではなくステップを明確に失敗させます。この最初の実装は、ブロック内の `tap` / `tapPoint` / `doubleTap` / `type` / `wait` / `assert` に対応しています。`longPress` / `swipe` / `drag` / `clear` / `delete` / `select` / `copy` / `selectOption` / `scroll` / `back` / `pinch` / `rotate` / `handleSystemAlert` / `setPickerValue` はそこに届かず、いずれも「web コンテキストは非対応」という明確な理由で失敗します。

### `swipe`

```yaml
- swipe: { on: { id: comp.swipearea }, direction: left }   # frame 中心 → 方向へ画面に対する割合分（既定 0.125）
- swipe: { from: [100, 400], to: [100, 200] }              # raw coordinates (last resort)
```

`{on,direction}` と `{from,to}` は、**どちらか一方だけ**でなければなりません（混在や片側の欠落は検証エラーになります）。

**方向指定**形式の意味は「スクロール」であり、各バックエンドは実際にスクロールを起こすプリミティブで実現します。iOS や Android では OS の本物のドラッグで、web ではマウスドラッグがページをスクロールしないため wheel イベント（デスクトップ）かタッチドラッグ（モバイルの [`deviceMode`](drivers.md#playwrightweb)）で実現します（BE-0227）。**座標**形式は、それ自体を目的とする素のポインタドラッグ（canvas やマップのパン、ドラッグハンドル）であり、どのバックエンドでも素のドラッグの最終手段です。

### `drag`

```yaml
- drag: { on: { id: replay.divider }, direction: right }             # 掴んだハンドルをドラッグする
- drag: { on: { id: volume.slider }, direction: up, amount: 0.3 }    # 画面に対する割合で
```

`drag` は要素アンカーの**ポインタドラッグ**です。要素そのものを掴んで方向へ動かすもので、リサイズ用の仕切り、スライダーのつまみ、並べ替えハンドルなど、スクロールではなくドラッグする操作に使います。方向指定 `swipe` と同じジオメトリを共有し（`amount` は画面に対する割合で `0 < amount ≤ 1`、省略時は小さな既定値）、方向指定 `swipe` が**スクロール**するのに対して `drag` は本物のポインタドラッグを行います。差が出るのは web だけです。web では方向指定 `swipe` が wheel スクロールになり、掴んだハンドルを動かせないため、その場合は `drag` を使います。iOS / Android では OS の本物のドラッグがスクロールもハンドル移動も兼ねるので、両者は一致します。

### `scroll`

```yaml
- scroll: { to: { id: notice.row.20 } }                 # 行が現れるまで下へスクロールし、続けて…
- tap: { id: notice.row.20 }
- scroll: { to: { label: "Log out", traits: [button] }, # 特定のコンテナをスクロールする…
            within: { id: settings.list }, maxScrolls: 25 }
- scroll: { to: { id: chart.point.7 }, amount: 0.2 }    # …既定より細かい歩幅で
```

`scroll` は画面外の要素を表示領域に入れます。1 ステップ分スクロールしてツリーを再クエリし、`to` が解決してそのフレームの**中心**が画面内に入った時点で止まります。中心は続く `tap` が狙う点なので、ビューポートより高い要素でも、中心が入りさえすれば成功します。

スクロールする領域（画面全体、または `within` が指すコンテナ）が末尾に達したと判定するには、証拠が必要です。`scroll` がコンテンツの末尾に達した（対象は領域内にない）と報告するのは、連続する 2 回の読み取りがコンテンツの静止を示したときだけです。示す形は次の 3 つです。

- ループが動くのを見た要素がまだ存在して止まっており、しかも上部のクロームではなくスクロールする領域に属していること
- 領域の境界が何も切っていないので、動きを隠せる frame がないこと
- ツリーがそのどちらも示せないとき、描画された画面もステップの前後で変化していないこと

ふつうの行が並ぶツリーは 2 つめを最初のステップで満たすので、`to` の打ち間違いはそこで即座に失敗します。画面全体を覆うウィンドウや root view を報告するツリーは満たさないので、そうしたバックエンドでは 1 ステップ遅れて、描画された画面が速い失敗を受け持ちます。どの証拠も得られないときは、`scroll` はステップを続け、`maxScrolls`（既定 15）に達した時点で、領域が動いたかどうかを観測できなかったと報告します。この失敗は、リストが終わったという主張とは別のことを述べています。両者の違いは実在します。Android は要素の bounds を見えている部分に切り取って報告するので、画面より高い行は、その裏でコンテンツがスクロールしていても同じ frame を報告します。

逆向きの誤りは、ステップの前に視野にあったものがその後どれも画面に残らなかったステップです。対象をビューポートの外へ運んでしまった可能性があるからです。一部だけ残っている場合も残ったと数えるので、一度に 1 枚のカードだけを見せる画面でのふつうのステップを取り違えることはありません。`scroll` は歩幅を半分にし、通り過ぎた範囲を読むために 1 回だけ逆向きにスクロールします。ループが取る最小の歩幅でもなお何も残らないときは、対象が見つからないと報告する代わりに、行き過ぎたことを名指して失敗します。

`amount` は 1 ステップが進む距離を、ビューポートに対する比で指定します。単位と範囲は `swipe` と `drag` の `amount` と同じで、0 より大きく 1 以下です。省略すると 1 ステップはビューポートの 0.6 を進みます。既定の歩幅が粗すぎて目的の要素をまたいでしまう画面では、`amount` を小さくします。1 ステップで現れる量が少なすぎて、`maxScrolls` が対象へ届く前に尽きる画面では大きくします。`amount` が決めるのはループの出発点だけです。行き過ぎたときの半減は、`amount` が置いた歩幅からそのまま働きます。半減が止まる下限は `amount` に連動しません。下限と同じかそれより小さい `amount` には半減する余地がないので、最初に行き過ぎたステップで、そのステップが取った歩幅を名指して失敗します。

ステップが領域を動かしたかどうかは、1 回のクエリではなく再読み取りが判定します。再読み取りが効くのは Android です。Android ではジェスチャがリストを動かした後にアクセシビリティツリーが公開されるため、1 回だけ読み取ったツリーはスクロール前の画面を表し、末尾に達した状態と同じに見えます。確認にかかるのは Android が読み取りに対して申告した猶予の分だけで、しかも `scroll` が失敗するときの最後のステップに限られます。読み取りが遅れない web と iOS では、時間がかかりません。`within` はジェスチャと、末尾および行き過ぎの判定を、1 つのスクロール可能なコンテナに限定します。省略すると画面全体をスクロールします。

対象を**現れさせる**なら `scroll`、**決まったジェスチャ**なら `swipe`、**掴んだハンドルを動かす**なら `drag` を使います。各ステップは慣性を残しません。画面に対する有限の距離を進んで止まるので、同じシナリオが高速な実機と遅い CI エミュレータのどちらでも同じように対象へ届きます。手作業で調整した `swipe` の連鎖では保証できなかった決定論です。1 ステップが進む距離は、そのステップが求めた距離になります。すべてのバックエンドで成り立ちます。ドライバー conformance スイートが 1 ステップの実移動量を測り、ジェスチャの終点より先へコンテンツが運ばれるバックエンドを不合格にするからです。それでも行き過ぎたステップは、前提として済ませずに、上の逆向きの読み取りで検出します。

> **`scroll` の `direction` は、指ではなくコンテンツが動く向きです**。`swipe` とは逆になります。`scroll: { direction: down }` は fold より下のコンテンツを現します（ドライバーは指を*上*へスワイプします）。`swipe: { direction: up }` は指が上に動きます。`scroll` に手を伸ばす書き手は「リストを下へスクロールする」と考えるので、`scroll` はその向きを名前にします。`swipe` は指を名前にします。

### `doubleTap` / `pinch` / `rotate`（ジェスチャ）

```yaml
- doubleTap: { id: gest.doubletap }                    # two quick taps
- pinch:  { sel: { id: gest.pinch },  scale: 2.0 }     # >1 zooms in, 0<scale<1 zooms out
- rotate: { sel: { id: gest.rotate }, radians: 1.57 }  # >0 clockwise (radians)
```

`scale` は **> 0** が必須です（違反は検証エラー）。`pinch` / `rotate` はマルチタッチが必要で、iOS（XCUITest）バックエンドと生成された XCUITest（`pinch(withScale:)` / `rotate(_:)`）のどちらも備えています。マルチタッチのないバックエンドは "needs multiTouch" の理由で失敗します。`doubleTap` はどこでも動作します（2 回タップ）。（実物: `doubleTap` / `longPress` は [`demos/showcase/scenarios/gestures.yaml`](../../demos/showcase/scenarios/gestures.yaml)、`pinch` / `rotate` は [`demos/showcase/scenarios/gestures_multitouch.yaml`](../../demos/showcase/scenarios/gestures_multitouch.yaml)）

### `wait`（条件待機）

固定 sleep はサポートしていません。**`timeout` は必須**です（無限待ちはできません）。

```yaml
- wait: { for: { id: home.title }, timeout: 5 }            # until an element appears
- wait: { until: { gone: { id: home.spinner } }, timeout: 15 }  # until an element disappears
- wait: { until: screenChanged, timeout: 5 }              # until query() changes
- wait: { until: settled, timeout: 3 }                    # until the screen stops changing
- wait: { until: { request: { method: GET, path: /items, status: 200 } }, timeout: 8 }  # until a matching request is observed
```

`for` と `until` は排他です（片方のみ）。`until` の値は `screenChanged` / `settled` / `{ gone: <Selector> }` / `{ request: <RequestMatch> }` のいずれかです。`request` 形式はネットワーク collector（[evidence](evidence.md)、`--network` 実行フラグ）をポーリングし、観測した通信が 1 件でも一致するまで待ちます。マッチャは [`request` アサーション](#requestネットワークアサーション)と同じで、`method` / `url` / `urlMatches` / `path` / `pathMatches` / `status` / `bodyMatches` を AND で評価し、`count` で閾値を上げられます。エンドポイントは `url`（完全一致の URL）か `urlMatches`（正規表現/部分一致）、または `path` だけで指定します。タイムアウトの扱いは種別で異なります（[run-loop](run-loop.md#待機条件待機)）。`for` / `gone` / `screenChanged` / `request` はタイムアウトするとステップ失敗になります。`settled` は安定化のヒントなので、タイムアウトしても現在の画面で続行し、失敗にはなりません。

### `assert`（中間検証）

ステップ途中での検証です。DSL（ドメイン固有言語）は `expect` と同一です（次節）。

```yaml
- assert:
    - disabled: { id: auth.submit }
```

### `setLocation` / `push`（デバイス制御）

```yaml
- setLocation: { lat: 35.681, lon: 139.767 }              # simctl location set
- push: { payload: { aps: { alert: "You have mail" } } }  # simctl push (APNs payload)
```

どちらも `simctl` 経由で Simulator を操作し、デバイスごとの制御チャネルが必要です。そのため fake ドライバや並列実行では使えず、その場合ステップはクリーンに失敗します（クラッシュはしません）。`push` は `payload` を APNs JSON として対象アプリに配信します。

### `http`（テストデータ準備用のリクエスト）

```yaml
- http: { method: POST, url: "https://api.test/seed", body: '{"n":1}', status: 200 }   # status が 200 以外なら失敗
- http: { url: "https://api.test/token", saveBody: token }   # vars.token ← レスポンスボディのテキスト
- assert:
    - exists: { id: home.title }
```

`http` はリクエストを runner から HTTP で送ります。UI ドライバは経由しません。そのため `status` の不一致はステップ失敗になり、`saveBody` はレスポンスボディのテキストを `${vars.<name>}` に保存して後続ステップで使えます。デバイスに触れない、ここで唯一のデバイス非依存アクションです。

### `totp`（二要素認証のワンタイムパスワード）

```yaml
- totp: { secret: "${secrets.TOTP_SEED}", into: { var: code } }   # vars.code ← 現在の 6 桁 OTP
- type: { text: "${vars.code}", into: { id: auth.code } }
```

`totp` は [RFC 6238](https://datatracker.ietf.org/doc/html/rfc6238) の時刻ベースワンタイムパスワードを、共有 `secret`（base32。YAML に直書きせず `${secrets.*}` に置く）と現在時刻からローカルで計算し、現在のコードを `${vars.<var>}` に保存します。後続の `type` / `assert` で使えます。スクリプトのエスケープハッチも LLM も使わずに 2FA サインインを自動化でき、値は secret と時刻の決定的な関数です（[BE-0046](../../roadmaps/BE-0046-otp-email-steps/BE-0046-otp-email-steps-ja.md)）。

### `email`（メールで届くコードをメールボックスから取得）

```yaml
- email:
    match: { to: "test@example.com", subjectMatches: "verification" }   # どのメッセージを待つか
    extract: { var: code, bodyMatches: "[0-9]{6}" }                     # vars.code ← 最初のキャプチャグループ
    timeout: 30
- type: { text: "${vars.code}", into: { id: auth.otp } }
```

`email` はメールで届く 2FA / 検証コードを待ちます。汎用 HTTP メールボックス（`targets.<name>.mailbox` で設定。[configuration](configuration.md#mailboxemail-ステップ) 参照）をポーリングし、**ステップ開始後に届いた**メッセージのうち `match` を満たすものが現れるまで待って、その本文から `bodyMatches` の正規表現（最初のキャプチャグループ、無ければマッチ全体）で値を `${vars.<var>}` に取り出します。待機は **`timeout` 必須の条件待機**です（固定 sleep なし）。タイムアウト、本文に正規表現が当たらない一致メッセージ、到達不能 / 2xx 以外のメールボックスは、いずれもクリーンなステップ失敗で、黙って誤った値を返すことはありません。対象はステップ開始より新しいメールだけ（メッセージ id で判定するので、以前の run の古いコードには一致しません）で、新着の一致が複数あれば最新を採ります。決定的で LLM 非依存、エンドポイントと認証情報は config 参照の `${secrets.*}` に置くのでシナリオはアプリ非依存のままです（[BE-0046](../../roadmaps/BE-0046-otp-email-steps/BE-0046-otp-email-steps-ja.md)）。

### `generate`（実行時に計算する値）

```yaml
- generate: { random: { string: { length: 8, charset: alnum } }, into: { var: username } }
- type: { text: "${vars.username}", into: { id: signup.username } }

- generate: { random: { uuid: {} }, into: { var: orderRef } }        # バージョン4の UUID
- generate: { random: { int: { min: 1, max: 100 } }, into: { var: quantity } }
- generate: { random: { float: { min: 0, max: 50, precision: 2 } }, into: { var: amount } }   # 例 "12.30"

- generate: { datetime: { format: "%Y-%m-%d", offsetDays: 1 }, into: { var: tomorrow } }
- type: { text: "${vars.tomorrow}", into: { id: booking.date } }
```

`generate` はランナー側で値を計算し、`${vars.<var>}` に保存します。これにより、作者がリテラルとして
書けない入力値をシナリオが供給できます。以前の run がまだ使っていないユーザー名、予約フォームに
入れる翌日の日付、他のシナリオと衝突しない参照番号などです。データ駆動の行（[再利用](#再利用とデータ駆動とタグ)）
は事前に用意した固定の表を配り、`extract` はアプリがすでに表示している値を取り込みます。どちらも
シナリオ自身が値を作り出すわけではありません（[BE-0377](../../roadmaps/BE-0377-dynamic-value-generation/BE-0377-dynamic-value-generation-ja.md)）。

値を作る生成カテゴリは、ちょうど1つ指定します。**`random`** が生成するのは、次の4種類です。

- **`string`**：`charset` から `length` 文字を引いた文字列。`charset` の既定は `alnum` で、
  ほかに `alpha`、`numeric`、`hex` を選べます。
- **`int`**：`[min, max]` の閉区間の整数。
- **`float`**：`[min, max]` の範囲の数値。任意の `precision` で小数桁数に丸めます。
- **`uuid`**：バージョン4の UUID。

**`datetime`** は現在時刻をテキストにします。`format` は `strftime` のパターンを取り、省略時は
秒までの ISO 8601 になります。符号付きの `offsetSeconds`、`offsetMinutes`、`offsetHours`、
`offsetDays` は加算されて時刻をずらします。`timezone` は `America/Los_Angeles` のような
Internet Assigned Numbers Authority（IANA）のゾーン名を取ります。既定のゾーンは UTC なので、
アプリがデバイス自身のゾーンで描画する日付に入力を一致させたいシナリオは、そのゾーンを明示します。
デバイス自身のゾーンを固定する部分は別の関心事です
（[BE-0158](../../roadmaps/BE-0158-timezone-device-primitive/BE-0158-timezone-device-primitive-ja.md)）。

値は run ごとに変わりますが、フローは決定的なままです。ローダが受理した `generate` ステップは、常に
実行され常に成功します。生成器から値を引くか、クロックを読むだけで、ネットワークとモデルのいずれにも触れません。
run ごとに違うのは生成された値だけで、これは `totp` の時刻由来のコードがすでにそうであるのと同じです。
描画できない `format` と解決できない `timezone` は、シナリオのロード時に拒否され、実行中に黙って別の
値に置き換わることはありません。生成した値は manifest とレポートに記録されるので、後で失敗したときに
その run が実際に使った値がわかります。特定の値を検証しなければならないシナリオは、その値を
`${vars.*}` に取り込んでから比較します。事前には知り得なかったリテラルと比較するわけにはいきません。
`generate` はアプリではなくランナーで動くため、どの codegen ターゲットもラベル付きの `// TODO` として
描画します。

### `manual`

`record` 中に記録される人による操作の引き取りです。

```yaml
- manual: { label: "ログインの CAPTCHA を解く" }                          # 決定的な等価物なし（本物の CAPTCHA）
- manual: { label: "Face ID を許可する", bypass: "device-control の生体認証マッチ（BE-0052）" }   # 作者が配線できる橋渡しを名指し
```

`record` は、詰まりが AI に実行できない**操作**そのもの——CAPTCHA、生体認証のプロンプト、AI が繰り返し解けないジェスチャ——であるとき `manual` ステップを出します。人が実際のデバイスを操作して制御を返し（`acted` ハンドオフ、[recording](recording.md#human-in-the-loop-ハンドオフbe-0179)）、ステップは生のジェスチャではなく観測した遷移のマーカーを記録します。`bypass` を設定すると、そのステップを再生可能にするために作者が配線できるテストビルド用のフラグ、あるいは device-control / device-state プリミティブ（BE-0035 / BE-0052）を名指しします。省略すると、そうした等価物のない引き取り（本物の CAPTCHA）であることを示します。どの codegen ターゲットもこれをラベル付きの `// TODO` として描画します。`manual` ステップは**決して黙って合格しません**。決定的な実行時の等価物がないため、`run` 時には `label` と bypass のヒントを示して `ManualStepRequired` で明示的に失敗します（原則 1・2）。名指しした `bypass` を配線し——そのうえで `manual` ステップを決定的なアクションに置き換え——ることが、作者にとって再生可能なシナリオへの道です（[BE-0185](../../roadmaps/BE-0185-record-human-takeover-step/BE-0185-record-human-takeover-step-ja.md)）。

### デバイス / システム制御（iOS）

```yaml
- background: {}                                                        # Home ボタン（SpringBoard 経由でバックグラウンド化。終了はしない）
- foreground: {}                                                        # バックグラウンドのアプリを前面へ復帰（simctl launch）
- clearKeychain: {}                                                     # 保存済みパスワード / 証明書をリセット
- clearClipboard: {}                                                    # ペーストボードをクリア
- setClipboard: { text: "COUPON123" }                                   # ペーストボードに投入（ペースト操作用）
- overrideStatusBar: { time: "9:41", batteryLevel: 100, wifiBars: 3 }   # ステータスバーを固定
- clearStatusBar: {}                                                    # ライブのステータスバーに戻す
```

`setLocation` / `push` と同様、これらは `simctl` 経由で Simulator を操作するため、デバイスごとの制御チャネルが必要で、fake ドライバや並列実行ではクリーンに失敗します。`overrideStatusBar` は、スクリーンショットや `visual` アサーションの直前に時計や電波表示を固定して画像を安定させる用途に向きます。`background` / `foreground` はバックグラウンド/フォアグラウンド遷移の対で、`foreground` は settle 用の sleep を入れずに復帰するので、必要なら直後に具体的な要素を待ってください。`setClipboard` はペースト操作のためペーストボードに値を投入します（[BE-0052](../../roadmaps/BE-0052-device-state-timezone-clipboard-shake/BE-0052-device-state-timezone-clipboard-shake-ja.md)）。

## アサーション DSL

`expect`（最終検証）と `assert`（中間検証）で共通です。リスト内はすべて **AND** で評価し、1 つでも失敗するとステップ失敗になります。評価の仕組み（要素の解決と比較）は [selectors](selectors.md#アサーション評価) にあります。

| アサーション | 意味 | 例 |
|---|---|---|
| `exists` | 一致要素が存在する（`negate: true` で不在を検証） | `exists: { id: home.title }` / `exists: { id: settings.banner, negate: true }` |
| `value` | accessibility value の一致 | `value: { sel: { id: counter.value }, equals: "2" }` |
| `label` | label の完全一致 / 部分一致 / 正規表現 | `label: { sel: { id: settings.status }, contains: "done" }` |
| `count` | 一致要素数 | `count: { sel: { idMatches: "list.row.*" }, equals: 5 }` |
| `enabled` / `disabled` | 操作可否（`notEnabled` trait） | `disabled: { id: auth.submit }` |
| `selected` | 選択 / トグル状態（`selected` trait） | `selected: { id: tab.home }` |
| `request` | 一致するネットワーク通信が観測された（`--network` が必要） | `request: { method: POST, path: /login, status: 200, count: 1 }` |
| `event` | アプリが送った分析 / テレメトリイベント。エンドポイント＋JSON ボディのフィールドを count とともに検証（`--network` が必要） | `event: { url: "https://t.example.com/track", body: { name: purchase_completed }, count: { equals: 1 } }` |
| `requestSequence` | 複数のマッチャがこの順序で観測されたか検証（`--network` が必要） | `requestSequence: [ { urlMatches: "/auth/refresh" }, { urlMatches: "/api/account" } ]` |
| `responseSchema` | 捕捉したレスポンスボディが JSON Schema に適合するか検証（`--network` が必要） | `responseSchema: { request: { urlMatches: "/api/items" }, schema: items.json }` |
| `visual` | 画面が baseline 画像に一致する（ビジュアルリグレッション） | `visual: { baseline: home.png, threshold: 0.02 }` |
| `clipboard` | デバイスのペーストボードが一致する（`simctl pbpaste` で読み戻す） | `clipboard: { equals: "COUPON123" }` / `clipboard: { matches: "\\d{6}" }` |

- `exists` はセレクタを **インラインで**書きます（`{ id: ... }` を直書き）。`negate` は任意です。
- `value` / `label` は `sel:` と、`equals` / `contains` / `matches` の **いずれか 1 つ**を指定します。
- `count` は `sel:` と、`equals` / `atLeast` / `atMost` の **いずれか 1 つ**を指定します。
- `enabled` / `disabled` / `selected` はセレクタを直書きします。
- `request` は **観測されたネットワーク通信**に一致するか検証します（[下記](#requestネットワークアサーション)）。`--network` 実行フラグが必要です。
- `event` は **アプリが送った分析 / テレメトリイベント**に一致するか検証します（[下記](#eventイベントアサーション)）。`--network` 実行フラグが必要です。
- `requestSequence` は複数の request マッチャが **順序どおりに観測された**かを検証します（[下記](#requestsequence順序付きリクエスト)）。`--network` 実行フラグが必要です。
- `responseSchema` は捕捉した **レスポンスボディが JSON Schema に適合する**かを検証します（[下記](#responseschemaレスポンスの-json-schema)）。`--network` 実行フラグが必要です。
- `visual` はスクリーンショットを baseline 画像とピクセル比較します（[下記](#visualビジュアルリグレッション)）。
- `clipboard` はデバイスのペーストボードを `simctl pbpaste` で読み戻し、`equals` / `matches`（正規表現）の **いずれか 1 つ**を検証します。`setClipboard` の読み戻し側で、「コピー」操作の検証に使います。デバイスごとの制御チャネルが必要なため、fake ドライバや並列実行では利用できず、その場合はクリーンに失敗します（[BE-0052](../../roadmaps/BE-0052-device-state-timezone-clipboard-shake/BE-0052-device-state-timezone-clipboard-shake-ja.md)）。

> **ロケール注意**: `label`/`value` の文字列比較や、可視テキストを見るアサーションは翻訳で壊れます。これらは config の固定 locale を前提に書き、セレクタ自体は `id` で書いてください。

### `request`（ネットワークアサーション）

`request` は、run のネットワーク collector が **一致する HTTP 通信を観測した**ことを表明します（`--network` 実行フラグと、アプリ内の BajutsuKit が必要）。同じマッチャを `until: { request: ... }` の wait と `mocks`（下記）で共有します。マッチフィールドは最低 1 つ必須で、列挙したフィールドは **AND** で評価します。

| フィールド | 型 | 説明 |
|---|---|---|
| `method` | str | HTTP メソッド（`GET`, `POST`, …） |
| `url` | str | 完全一致の URL（エンドポイント） |
| `urlMatches` | str | URL への正規表現 / 部分一致（クエリ文字列はここに含める） |
| `path` | str | 完全一致のパス（クエリは無視） |
| `pathMatches` | str | パスへの正規表現 |
| `status` | int | レスポンスのステータスコード |
| `bodyMatches` | str | **リクエストボディ**への正規表現 / 部分一致 |
| `count` | int | 一致した通信数。アサーションでは **厳密**、`wait` では **下限** |

```yaml
- assert:
    - request: { method: POST, path: /login, status: 200, count: 1 }
    - request: { urlMatches: "/search", bodyMatches: "apple" }   # match on the request body
```

> `count` はマッチフィールド **ではありません**。`method` / `url` / `urlMatches` / `path` /
> `pathMatches` / `status` / `bodyMatches` の少なくとも 1 つが必要です。
> （[`demos/showcase/scenarios/network_mock.yaml`](../../demos/showcase/scenarios/network_mock.yaml) 実物）

### `event`（イベントアサーション）

`event` は、画面には現れない振る舞い、すなわちアプリが**送った**分析 / テレメトリイベントを表明します
（[BE-0048](../../roadmaps/BE-0048-behavioral-protocol-assertions/BE-0048-behavioral-protocol-assertions-ja.md)）。
`request` が読むのと同じ観測済み通信に対する純粋な検査なので（`--network` 実行フラグが必要）、判定は機械のみで LLM は介在しません。
イベントの**エンドポイント**（`request` と同じ `method` / `url` / `urlMatches` / `path` / `pathMatches` マッチャ）でタイムラインを絞り、
続けて構造化した**リクエストボディのフィールド**で絞り、残った通信数を count 演算子と突き合わせます。

| フィールド | 型 | 説明 |
|---|---|---|
| `method` / `url` / `urlMatches` / `path` / `pathMatches` | str | エンドポイントのマッチャ（AND）。意味は `request` と同じ |
| `body` | map | 各 `key: value` が JSON リクエストボディに存在し、その値と等しいこと。テキストとして比較する（`amount: "300"` は JSON の数値 `300` に一致。JSON の真偽値 / null は `"true"` / `"false"` / `"null"` に一致） |
| `count` | object | 期待する多重度。`equals` / `atLeast` / `atMost` の **いずれか 1 つ**。省略時は **1 件以上** |

```yaml
expect:
  # 購入イベントが正しい金額でちょうど 1 回発火したこと
  - event:
      url: "https://t.example.com/track"
      body: { name: purchase_completed, amount: "300" }
      count: { equals: 1 }
```

> エンドポイントのフィールドか `body` の少なくとも一方が必須で、イベントは必ず何かを特定します。JSON でない、
> オブジェクトでない、あるいは存在しないリクエストボディは `body` 条件に一致しません（推測せず失敗します）。
> ボディの値は DSL の他の箇所と同じく `${vars.*}` / `${secrets.*}` トークンを使えます。

### `requestSequence`（順序付きリクエスト）

`requestSequence` は、複数のリクエストが **指定した順序で**起きたことを表明します。たとえば保護された
呼び出しの*前に*トークンリフレッシュが起きたこと、といった検証です
（[BE-0048](../../roadmaps/BE-0048-behavioral-protocol-assertions/BE-0048-behavioral-protocol-assertions-ja.md)）。
観測済みタイムラインに対する純粋な検査なので（`--network` 実行フラグが必要）、判定は機械のみです。空でない
[`request` マッチャ](#requestネットワークアサーション)のリスト（同じフィールド）を取り、**順序を保った部分列**
として照合します。各マッチャは、直前のマッチより厳密に後ろの位置にある別々の通信に一致しなければなりません。
間に無関係な通信が**挟まってもよい**のでノイズに強く、同じマッチャを2回並べれば順序を保った2件の出現を要求します。

```yaml
expect:
  - requestSequence:
      - { method: POST, urlMatches: ".*/auth/refresh" }
      - { method: GET,  urlMatches: ".*/api/account" }
```

> 各マッチャは `request` と同じフィールド（`method` / `url` / `urlMatches` / `path` / `pathMatches` /
> `status` / `bodyMatches`）を使います。マッチャ自身の `count` はここでは無視されます。シーケンスの役割は
> **順序**だからです。純粋な多重度の検査には `request` の `count` を使ってください。

### `responseSchema`（レスポンスの JSON Schema）

`responseSchema` は、捕捉した **レスポンスボディが JSON Schema に適合する**ことを表明します。画面では
表現できない契約の検査です（[BE-0048](../../roadmaps/BE-0048-behavioral-protocol-assertions/BE-0048-behavioral-protocol-assertions-ja.md)）。
観測済みタイムラインと保存済みのスキーマファイルに対する純粋で決定的な検査なので（`--network` 実行フラグが
必要）、判定は機械のみです。`request`（同じマッチャフィールド）で検証対象の交信を選び、`schema` はターゲットの
**スキーマディレクトリ**（`--schemas` フラグ、config の `targets.<name>.schemas`、またはシナリオ脇の
`schemas/`）内で解決するファイルパスです。検証には `jsonschema` ライブラリを使うので、`schema` extra を
インストールしてください（`pip install bajutsu[schema]`）。

```yaml
expect:
  - responseSchema:
      request: { method: GET, urlMatches: ".*/api/items" }
      schema: items.json        # スキーマディレクトリ内で解決
```

> 検証するのは**最初に**一致した交信のレスポンスです。一致する交信がない、スキーマファイルが無い、
> レスポンスにボディが無い、JSON でない、あるいは適合しない場合は（推測せず）失敗します。スキーマ
> ディレクトリの解決順は `visual` の `--baselines` と同じです。

### `visual`（ビジュアルリグレッション）

```yaml
- assert:
    - visual: { baseline: "home.png", threshold: 0.02, exclude: [{ x: 0, y: 0, w: 390, h: 47 }] }
    - visual: { baseline: "detail.png", compare: pixelmatch, colorTolerance: 0.1, antialiasing: true }
    - visual: { baseline: "summary-card.png", element: { id: "summary-card" } }  # 1 要素だけ比較
    - visual: { baseline: "home.png", exclude: [{ selector: { label: "last updated" } }] }  # 要素でマスク
```

`visual` はスクリーンショットを取得し、`baseline`（run の baselines ディレクトリ内の PNG。`--baselines`、またはシナリオ脇の `baselines/`）と比較します。

比較エンジンは `compare` で選択できます（BE-0165）。

| エンジン | 説明 | 既定 |
|---|---|---|
| `exact` | ピクセル完全一致。いずれかのチャネルが異なればそのピクセルは「差分」として計上されます。 | はい（後方互換） |
| `pixelmatch` | 知覚的 YIQ 色差 + アンチエイリアシング検出。サブピクセルレンダリングノイズや 1 ピクセルのエッジシフトを許容します。 | いいえ |

`compare` を省略すると、ターゲットの `visualCompare` 設定（`defaults:` または `targets.<name>` で指定）にフォールバックし、さらに未設定なら `exact` になります。

`threshold` は許容する差分ピクセルの割合（既定 `0.0` = 完全一致）で、すべてのエンジン共通です。`colorTolerance`（0–1、既定 `0.1`）は `pixelmatch` のピクセル単位の知覚的色差許容値、`antialiasing`（既定 `true`）はアンチエイリアスされたピクセルを差分から除外します。`exclude` は比較前にマスクする領域のリストで、ステータスバーや時計などに使います。各要素は、スクリーンショットのピクセル座標で表す矩形（`{ x, y, w, h }`）か、マスク対象の要素を指す `{ selector: <Selector> }`（BE-0171）のどちらかです。後者は評価時に要素のフレームへ解決されます。baseline は `approve` コマンド（[cli](cli.md#approve)）か `serve` UI で作成・更新します。baseline が無いとアサーションは失敗します。`overrideStatusBar` と併用すると時計やバッテリーを固定できます。差分は `report.html` に表示されます。`pixelmatch` では、除外されなかった（非 AA）ピクセルのみが差分画像に表示されます。

**要素スコープ比較（BE-0171）。** `visual` は既定で画面全体を比較するため、無関係な変化（バナー、行が増えたリストなど）があるたびにアサーションが失敗し、baseline が揺れます。`element: <Selector>` を指定すると、**その要素だけ**を比較します。スクリーンショットは要素のフレームにクロップされ、baseline はそのクロップ画像になるので、フレームの外側の変化は無視されます。セレクタは通常の一意解決規則で解決し、**曖昧なセレクタは最初の一致をクロップせず即座に失敗します**。何にも一致しないセレクタも失敗します。`approve` は要素スコープの baseline も画面全体の baseline と同じ手順で昇格します（baseline は単に小さい画像になるだけです）。

**セレクタによるマスク（BE-0171）。** `exclude` のピクセル矩形は、レイアウトが変わったりデバイスの解像度が変わったりした瞬間にずれます。代わりに要素を指定すると（`{ selector: { label: "last updated" } }`）、そうした変化に強くなります。要素をフレームへ解決し、矩形と同じ方法でマスクするからです。何にも一致しないマスクセレクタは何もしません（画面上に隠すものが無いため）。曖昧なセレクタは、決定論の原則どおり失敗します。セレクタと矩形は一つの `exclude` リストに混在でき、どちらも要素スコープ比較と併用できます（クロップした要素の内側のマスクは、クロップの座標系へ変換されます）。

## ネットワークモック（決定的スタブ）

`mocks` はテストをライブサーバから独立させます。送信リクエストが一致すると、BajutsuKit はネットワークへ行かずに定型レスポンスを返します。各モックは `{ match, respond }` です。

- **`match`** は[リクエストマッチャ](#requestネットワークアサーション)の **リクエスト側**フィールドを再利用します（`method` / `url` / `urlMatches` / `path` / `pathMatches` / `bodyMatches`）。`status` / `count` はモックの `match` には **適用されません**。
- **`respond`** は定型の返答です。`status`（既定 `200`）、`headers`（既定 `{}`）、`body`（文字列）、`delayMs`（人工的な遅延）を指定します。`respond` を省くと空の `200` を返します。

```yaml
- name: GET answered by a mock stub
  mocks:
    - match: { method: GET, urlMatches: "example.com" }
      respond:
        status: 418                       # real example.com returns 200; 418 proves the stub served it
        headers: { Content-Type: text/plain }
        body: "stubbed by bajutsu"
  steps:
    - tap:  { id: net.fetch }
    - wait: { until: { request: { method: GET, urlMatches: "example.com", status: 418 } }, timeout: 6 }
  expect:
    - request: { method: GET, urlMatches: "example.com", status: 418 }
```

モックは `BAJUTSU_MOCKS` env で BajutsuKit に渡されます（`dump_mocks`, `scenario/serialize.py`）。形式的な形は [dsl-grammar](dsl-grammar.md#2-文法の全体像) にあります。

## 再利用とデータ駆動とタグ

コア文法の周りには、小さなテンプレートとマクロの層があります。これはロード時、決定的 run の **前**に実行されるため、ランナーは常に展開済みのプレーンなシナリオだけを見ます。展開順、`${ns.key}` 補間、深さ制限といった規範的な規則は [dsl-grammar](dsl-grammar.md#6-テンプレートとマクロ層) にあります。ここではオーサリングの視点から説明します。

### コンポーネント（`use` → 再利用ステップ）

**コンポーネント**は別ファイルで、`params` のリストと、それを `${params.<name>}` で参照する `steps` のリストからなります。`use` ステップが `with` で params を束縛して呼び出します。`use` は **コンパイル時マクロ**であり、`expand_components`（`scenario/expand.py`）が run の前に、コンポーネントの置換済みステップへ置き換えます。展開は再帰的で、コンポーネントが別のコンポーネントを `use` でき、深さは 25 までです。params 不足、未知の params、未宣言を指す残留 `${params.*}`、循環参照ではエラーになります。`use` は run に残らないため、決定性には影響しません。展開の対象は、シナリオ自身の `steps` と、[`interrupts`](#interrupts予測できない差し込み画面への対処) の各エントリの回復用 `steps` です。

```yaml
# login.component.yaml: コンポーネントファイル（単一マッピング。別ファイルとして読み込む）
params: [user, pass]
steps:
  - type: { text: "${params.user}", into: { id: auth.user } }
  - type: { text: "${params.pass}", into: { id: auth.pass } }
  - tap:  { id: auth.submit }
```

```yaml
# シナリオ側: 上の 3 ステップに params を置換して展開される
steps:
  - use: { component: login.component.yaml, with: { user: alice, pass: hunter2 } }
  - tap: { id: home.tab }
```

### データ駆動シナリオ（`data` / `dataFile`）

`data`（インライン行）か `dataFile`（CSV パス。両者は **排他**）を持つシナリオは、`${row.<column>}` を置換して **1 行 1 シナリオ**に展開されます（`expand_data`, `scenario/expand.py`）。派生シナリオは `"<name> [row N: col=val, …]"` に改名され、元の preconditions を保ちます。そのため各行ともアプリを fresh に再インストールし、テンプレートの `erase` / `reinstall` を継承します。

```yaml
- name: search returns a result
  data:
    - { q: dog, expect: "1 result" }
    - { q: cat, expect: "2 results" }
  steps:
    - type: { text: "${row.q}", into: { id: search.field }, submit: true }
  expect:
    - label: { sel: { id: home.status }, equals: "${row.expect}" }
```

> **ちょうど 1 トークン**だけの文字列（`"${row.qty}"`）は **生**の値になります（数値は数値のまま）。大きな
> 文字列に **埋め込まれた**トークンは、テキストとして差し込まれます（`"item-${row.id}"`）。

CSV の `dataFile` は、列名を与えるヘッダ行を持ち、以降の各行が 1 シナリオになります。

> **参照はスイートの中にとどまります。** `use` コンポーネントと `dataFile` のパスは、シナリオファイルを
> 起点に解決します。解決後のファイルは、読み込みを始めた scenarios ディレクトリ（スイートのルート）の中に
> とどまらなければなりません。ルートの外へ出る参照、すなわち絶対パス、ルートを抜ける `../` の連鎖、外を
> 指すシンボリックリンクは、明確なエラーで拒まれ、読み込まれることはありません。そのため、シナリオが
> ローダに自分の木の外のファイルを開かせることはできません（[BE-0174](../../roadmaps/BE-0174-scenario-ref-path-containment/BE-0174-scenario-ref-path-containment-ja.md)）。
> ルートの中にとどまる相対参照は、今までどおり動きます。たとえば同じ階層の `components/shared.yaml` や、
> サブディレクトリにあるシナリオからルートより上に出ない `../shared.yaml` です。

### タグと選択

`tags` はシナリオにラベルを付けます。CLI の `--tag` / `--exclude` フラグで実行対象を絞ります。シナリオは、少なくとも 1 つの `--tag` を持ち（または `--tag` 未指定で）、**かつ** `--exclude` のタグを 1 つも持たない場合に実行対象として残ります。`--exclude` が `--tag` より優先されます（`select_scenarios`, `scenario/select.py`）。両フラグともカンマ区切りで複数指定できます。

```yaml
- name: checkout smoke
  tags: [smoke, checkout]
  steps:
    - tap: { id: cart.checkout }
```

```bash
uv run bajutsu run --target showcase-swiftui --tag smoke --exclude wip   # run @smoke, skip anything @wip (across the app's scenarios dir)
```

### シークレット（`${secrets.X}`）

シークレットの環境変数名を config で宣言します（`secrets: [API_TOKEN, ...]`）。宣言した各名 `X` は環境から解決され、**アクション時**に実行ステップへ `${secrets.X}` として置換されます。シナリオファイルは **トークン**を保持し、実値は持ちません。さらにリテラル値は証跡で **自動マスク**されるため、シークレットはコミットしてもレビューしても安全です。`${params.*}` / `${row.*}`（ロード時の展開）と異なり、この名前空間は run ループが解決します。

```yaml
# config が宣言: secrets: [API_TOKEN]
steps:
  - type: { text: "${secrets.API_TOKEN}", into: { id: auth.token } }   # real value typed; token kept in the report
```

### ランタイム変数 (`${vars.*}`)

ステップの `extract` 修飾子は、ステップ実行後に UI 要素のプロパティを `vars.*` に取り込みます。後続のステップやシナリオレベルの `expect` で、`${vars.<name>}` として参照できます。

```yaml
steps:
  - tap: { id: counter.inc }
    extract:
      count: { sel: { id: counter.value } }          # vars.count ← element's value (default)
      heading: { sel: { id: header }, prop: label }   # vars.heading ← element's label
  - assert:
      - value: { sel: { id: other.field }, equals: "${vars.count}" }
```

各 `extract` エントリは、`sel`（セレクタ、`resolve_unique` で一意解決）と、省略可能な `prop`（`value` | `label` | `identifier`、既定 `value`）を指定します。セレクタが一意に解決できない場合や、プロパティが `None` の場合、ステップは失敗します。

### 条件分岐 (`if`)

ステップでアサーション DSL と同じ条件を評価し、分岐できます。

```yaml
steps:
  - if:
      condition: { exists: { id: dialog.alert } }
      then:
        - tap: { id: dialog.dismiss }
      else:
        - tap: { id: home.start }
```

条件は現在の要素ツリーに対して評価されます（`${...}` 補間あり）。条件が成立すれば `then` のステップ群が、そうでなければ `else` のステップ群が実行されます（`else` 省略時は何もしません）。ネストしたステップは、外側のシナリオと同じ `vars.*` バインディングを共有します。`capture` / `extract` 修飾子は `if` ステップでは使えません。

### 要素のイテレーション (`forEach`)

セレクタに一致する全要素に対して、ステップを繰り返し実行できます。

```yaml
steps:
  - forEach:
      sel: { idMatches: "item.*" }
      as: current
      steps:
        - tap: { id: "${vars.current}" }
```

要素リストはループ開始時に 1 回スナップショットされます。各要素の `identifier` が `vars.<as>` に格納され、ネストしたステップで参照できます。`identifier` のない要素はステップを失敗させます。0 件一致は no-op（成功）です。セレクタは `${...}` 補間に対応しています。`capture` / `extract` 修飾子は `forEach` ステップでは使えません。

## capture トークン文法

`capture:`（ステップ単体）と `capturePolicy[].capture`（ルール）で共通です。形は `<kind>[.<modifier>]` です。

- **種別**: `screenshot` / `elements` / `actionLog` / `deviceLog` / `network` / `video` / `appTrace` / `rawTree`
- **修飾子**: `before` / `after` / `around` / `onError`

検証は、種別と修飾子の集合に対して行われます（`scenario/models/_base.py` の `_validate_capture`）。種別ごとの取得タイミングと、どれが取得されるかは [evidence](evidence.md#証跡種別と取得タイミング) にあります。

## YAML の注意点

PyYAML（YAML 1.1）は `on`/`off`/`yes`/`no` を真偽値に解決します。`capturePolicy` のトリガーキー `on:` が `True` になるのを防ぐため、Bajutsu の YAML ローダ（`common/_yaml.py`）は **`true`/`false` だけを真偽値**として扱い、`on`/`off`/`yes`/`no` は文字列のまま読みます。

## `from`（来歴）

`from:` は、**ある構成要素がどの自然言語フレーズから記録されたか**を残します（BE-0044）。任意の文字列で、シナリオ（元のゴール）、各ステップ、各 `expect` アサーション、各 `capturePolicy` ルールという 4 つのレベルに付きます。これにより、レビュアーは各部分が*なぜ*存在するのかを見て、`record` が意図を忠実に正規化できているかを判断できます。

```yaml
- name: 設定を開いて再生成する
  from: "設定を開いて、再インデックスして、正規化設定が消えていることを確認して"   # 元のゴール
  steps:
    - tap: { id: settings.open }
      from: "設定を開く"
  expect:
    - exists: { label: "正規化設定が変更されています", negate: true }
      from: "正規化設定が消えていること"
  capturePolicy:
    - on: { action: tap, idMatches: "*.submit" }
      capture: [screenshot.after, network]
      from: "送信を押すたびにスクショとネットワークログを残して"
```

- **書き込むのは `record`（Tier 1、AI）だけです。** ゴールを構造化シナリオへ正規化する際に `from:` を埋めます。手書きのシナリオは単に省略でき、書き出した YAML も汚れません（未設定の `from:` は間引かれます）。
- **`run`（Tier 2）は一切読みません。** 来歴はオーサリング用のメタデータで、オーケストレータは参照しないので、ゲートに AI を加えず、pass/fail にも影響しません。
- **グルーピングは創発的です。** 1 つの発話が複数ステップを生むとき、それらは**同じ** `from:` 文字列を持ちます。範囲（span）構文はありません。`lint` は来歴カバレッジ（`from:` を持つステップ数）を advisory として報告しますが、run を落とすことはありません。
- **`trace` とレポートに表示します。** [`bajutsu trace`](cli.md#trace) は各ステップのフレーズを `← "<フレーズ>"` としてインライン表示し、`report.html` はステップの下に表示します。どちらも同じフレーズの連続を 1 つのラベルにまとめ、タイムラインを「自然言語 ↔ 操作」の対応図にします。
- フレーズは、著者が書いた言語のまま**逐語的**に保ちます（翻訳しません）。

## ラウンドトリップ（読込 ⇄ 書出）

- `load_scenarios(text) -> list[Scenario]`: YAML 文字列 → 検証済みモデル。
- `dump_scenarios(scenarios) -> str`: モデル → YAML。著者が設定しなかったフィールドだけを落とします。

`record` の出力はこの `dump_scenarios` を通ります。生成された YAML は `load_scenarios` でそのまま読み戻せます。
