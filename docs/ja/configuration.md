[English](../configuration.md) · **日本語**

# 設定、ターゲットのオンボーディング、doctor

ツール本体はアプリ非依存です。アプリ固有の差分はすべて config に置くので、同じバイナリと同じドライバで複数のターゲットを実行できます。ターゲットを追加するときは `targets.<name>` を 1 つ加えるだけです。

実装: `bajutsu/common/config/resolve.py`（解決） · `bajutsu/common/doctor.py`（規約充足度スコア）。config はリポジトリのルートには同梱されていません。`--config`（既定のファイル名は `bajutsu.config.yaml`）で渡します。デモにはすぐ動くものが同梱されています（例: [`demos/showcase/showcase.config.yaml`](../../demos/showcase/showcase.config.yaml)（iOS）、[`demos/web/demo.config.yaml`](../../demos/web/demo.config.yaml)（web））。

関連: [concepts のアプリ非依存](concepts.md#6-アプリ非依存差分は-config-に寄せる) · [drivers](drivers.md) · [scenarios](scenarios.md)

---

## 設定の階層（defaults × targets）

`bajutsu.config.yaml` は 2 層で構成されています。値の解決順は **既定 < ターゲット < シナリオ** で、テストに近い方が優先されます。

```yaml
defaults:                       # 全ターゲット共通の既定
  platform: ios                 # チーム共通の既定プラットフォーム(ios/android/web)。省略すると各ターゲットの backend から導出
  backend: [ios]                # 順序付きリスト。プラットフォーム(ios/android/web/fake)か actuator(xcuitest)。単一文字列も可
  device:  "iPhone 15"
  locale:  en_US
  capture: [screenshot.after, elements, actionLog]
  redact:  { headers: [Authorization, Cookie], fields: [token, password] }
  secrets: [LOGIN_PASSWORD]         # ${secrets.X} に使える環境変数名（実値は証跡でマスク）
  ai:      { provider: api-key, keyEnv: ANTHROPIC_API_KEY }   # AI 経路のプロバイダ / モデル / エンドポイント / キー（下記）
  reservedNamespaces: [auth, nav]   # 共有フロー / コンポーネントの id 契約（情報用）

targets:
  showcase-swiftui:             # ← --target showcase-swiftui で選択
    bundleId:       com.bajutsu.showcase.ios.swiftui     # iOS のターゲット（web で baseUrl を設定する場合を除き必須）
    deeplinkScheme: showcaseswiftui
    idNamespaces:   [stable, horse, search, log, notice, perm, sys, net]
    launchEnv:      { SHOWCASE_UITEST: "1" }
    scenarios:      demos/showcase/scenarios   # このターゲットのシナリオディレクトリ（run が読み、record が書く）
    systemAlertHandling:  { rules: [{ prompt: notifications, choice: grant }] }   # アラートガード（後述）のアプリ既定。--system-alert-handling が実行ごとに上書き
    iosTipKitHandling:    true                       # TipKit の tip ガードのアプリ既定（iOS 専用、ビルトイン既定はオフ）
    # 任意: erase / network / backend / device / locale / launchArgs / setup / redact / secrets / mockServer / appPath / build

  web:                          # web ターゲット（Playwright backend）は URL で指定する
    platform:  web                                  # 任意: 通常は backend や baseUrl から導出されるが、明示すると最も明確
    baseUrl:   "http://127.0.0.1:8787/index.html"   # web では必須（bundleId の代わり）
    backend:   [web]
    headless:  true                                 # web のみ: false でブラウザを画面に表示（--headed が実行ごとに上書き）
    browser:   chromium                             # web のみ: 描画エンジン。chromium / firefox / webkit（--browser が実行ごとに上書き）
    deviceMode: desktop                             # web のみ: "desktop"（既定）か Playwright のデバイスプリセット名（例 "iPhone 13"）。ターゲットをモバイル端末として駆動する
    scenarios: demos/web/scenarios
```

各プラットフォームはターゲットを自分のハンドルで指定します。**iOS** は `bundleId`、**web** は `baseUrl`、**Android** は `package` です。ターゲットの `platform` がどのハンドルを必須にするかを選びます。`platform` は**任意**で、未指定なら `backend` から導かれるプラットフォームに既定化されます（この項目を追加する前の config はそのまま動きます）。曖昧さを避けたいときは明示してください。プラットフォームに対して誤ったハンドルを持つ（あるいは 1 つも持たない）ターゲットは読み込み時に拒否されます。[drivers → Playwright](drivers.md#playwrightweb) と `demos/web` を参照してください。

### 解決（`resolve` → `Effective`）

`resolve(config, target)` が 1 ターゲット分の有効値 `Effective`（frozen dataclass）を構築します。ターゲットが未定義の場合は `KeyError` となり、CLI は終了コード 2 で終了します。下記の一部のフィールド――各プラットフォーム自身の識別子、および web 限定の `headless` / `browser` / `device_mode`――は `Effective` の平坦なフィールドではありません。`Effective.platform_config` が、解決済みの `platform` に応じて、互いに排他的なプラットフォームごとの 3 つの config（`IosConfig` / `WebConfig` / `AndroidConfig`）のいずれかに絞り込みます（[BE-0126](../../roadmaps/BE-0126-per-platform-effective-config/BE-0126-per-platform-effective-config-ja.md)）。読み取るときは `bajutsu/common/config/accessors.py` の絞り込み用ヘルパーを使います。1 つのプラットフォームに決まっているコードは `require_ios` / `require_web` / `require_android` を使い、プラットフォームをまたいで値を安全に読みたいコードは「soft」な `ios_bundle_id` / `web_base_url` / `web_engine` / `android_package` を使います。`Effective` に `bundle_id` のようなフィールドは存在しません。

| `Effective` フィールド | 由来 | 備考 |
|---|---|---|
| `platform` | app < defaults < 導出 | ターゲットのプラットフォーム（`ios`/`android`/`web`）。明示 `platform` が優先、なければターゲットの `backend` が含意、なければ存在する識別子、それも無ければ `ios`。どの識別子を必須にするかを選びます（[BE-0009](../../roadmaps/BE-0009-cross-platform-abstractions/BE-0009-cross-platform-abstractions-ja.md)） |
| `platform_config.bundle_id`（`IosConfig`） | app | iOS のターゲット識別子。プラットフォームが `ios` のとき必須 |
| `platform_config.base_url`（`WebConfig`） | app | web のターゲット URL（Playwright backend）。プラットフォームが `web` のとき必須 |
| `platform_config.package`（`AndroidConfig`） | app | Android のターゲット識別子。プラットフォームが `android` のとき必須 |
| `platform_config.native_z`（`AndroidConfig`） | app | Android のみ: `nativeZ: true` は、opt-in した各ビュー自身の `View.getZ()` を端末に要求する。`elements.json` は診断用フィールド `nativeZ` として報告する（[BE-0355](../../roadmaps/BE-0355-native-z-position/BE-0355-native-z-position-ja.md)）。指定しなければ off。値を返すノード走査は画面の読み取りごとに走り、20 ミリ秒から 100 ミリ秒かかるうえ、テスト対象のアプリが `BajutsuAndroid` をリンクしてビューを opt-in していなければ何も報告しないためである。on にしたターゲットは、確認が取れたジェスチャの応答が省いてくれるはずだった往復も手放す。その応答は `nativeZ` の値を運べないため、位置がすべて未計測になるツリーを受け取る代わりに、そもそも作らないよう端末へ要求する（[BE-0407](../../roadmaps/BE-0407-step-latency-driver-internal-tuning/BE-0407-step-latency-driver-internal-tuning-ja.md) の作業単位 18〜19）。iOS に対応するフラグはない。レスポンダは BajutsuKit をリンクしたアプリにだけ応答するためである |
| `platform_config.headless`（`WebConfig`） | app | web backend のみ: `true`（既定）はヘッドレス、`false` はブラウザを画面に表示し低速再生する。`bajutsu run --headed / --no-headed` と Web UI の「show browser」トグルが実行ごとに上書きする。iOS は無視する |
| `platform_config.browser`（`WebConfig`） | app | web backend のみ: 駆動する Playwright の描画エンジン。`chromium`（既定）、`firefox`、`webkit` から選びます。いずれも Linux 上でヘッドレス実行できます。`bajutsu run/record --browser <engine>` が実行ごとに上書きし（フラグ > config > 既定）、`bajutsu run --browsers <list>` はクロスブラウザマトリクスを実行します（後述）。エンジンのブラウザバイナリが無ければ実行時に取得します。未知の値は config 読み込み時に拒否されます。iOS は無視します（[BE-0076](../../roadmaps/BE-0076-web-cross-browser-engines/BE-0076-web-cross-browser-engines-ja.md)） |
| `platform_config.device_mode`（`WebConfig`） | app | web backend のみ: ブラウザコンテキストを生成するときのデバイスモード。`deviceMode: desktop`（既定で、今日から変わりません）か、Playwright のデバイスプリセット名（例 `iPhone 13`）です。プリセットは viewport / `device_scale_factor` / `is_mobile` / `has_touch` / `user_agent` をエミュレートし、web ターゲットをそのモバイル端末として駆動します。これはデスクトップ級ブラウザでのエミュレーション（Chrome DevTools のデバイスツールバー）であり、実機ではありません（[drivers → Playwright](drivers.md#playwrightweb)）。ドライバーの中で `playwright.devices` に対して**遅延**して解決するため、config 読み込みが Playwright を import することはありません。不明なプリセットは config 読み込み時ではなくドライバー起動時に明示的なエラーで失敗します。トップレベルの `device`（iOS シミュレータ名で、web ターゲットは無視します）とは別物です。iOS / Android はこれを無視します（[BE-0228](../../roadmaps/BE-0228-web-device-mode-emulation/BE-0228-web-device-mode-emulation-ja.md)） |
| `device_provider` | app | このターゲットのデバイスの取得元です。`deviceProvider: { kind: local }`（既定で、今日どおりのローカル接続デバイス、`--udid` の経路）か、デバイスクラウドのアダプタが登録する別の `kind`（ホスト外でデバイスを予約し、その serial / endpoint を run に渡します）を指定します。`kind` は device provider の registry に対して **config 読み込み時ではなく実行時に**解決するため、決定的コアがクラウド SDK を import することはありません。未知の `kind` は、run が provider を解決する時点で明示的に失敗します。この解決を行うのは今のところ `bajutsu run` だけで、`record`・`crawl`・`audit --repeat` は従来どおりの方法でデバイスを解決するため、この項目を静かに無視します。seam はデバイスプールの手前にあり、run/CI の判定経路の完全に外側です（provider の仕事はデバイスの取得と解放だけです）。組み込みの provider は現時点で2種類あります。**`local`**（既定。ローカル接続デバイスの `--udid` 経路で、追加フィールド不要）と **`appium`**（セルフホストの Appium / WebDriver グリッドに予約済みの iOS デバイスが存在する場合の live 経路。`endpoint: <url>` が必須です。この endpoint は live の W3C WebDriver トランスポートで端から端まで駆動され、セレクタの解決はローカルの XCUITest backend と同じく Python 側で行います。詳細は[iOS デバイスクラウド](ios-device-cloud.md#ライブ--appium-エンドポイントのプロバイダー)を参照してください）。具象のクラウドアダプタは任意導入の別パッケージとして出荷します（[BE-0236](../../roadmaps/BE-0236-device-cloud-provider-abstraction/BE-0236-device-cloud-provider-abstraction-ja.md)、[BE-0238](../../roadmaps/BE-0238-ios-device-cloud-execution/BE-0238-ios-device-cloud-execution-ja.md)） |
| `launch_server` | app | 任意の `launchServer: {cmd, readyUrl, readyTimeout, cwd, env}`。run のために `baseUrl` のホストを起動し、終わったら停止します。`readyUrl`（既定は `baseUrl`）をプローブし、すでに応答すれば再利用、しなければ `cmd` を起動して準備が整うまで待ちます（固定 sleep ではなく条件待ち）。iOS の `build` の web 版です（[BE-0059](../../roadmaps/BE-0059-launch-target-server/BE-0059-launch-target-server-ja.md)）。`serve` 上の**アップロードされた**バンドルでは、ホストが `cmd` を直接実行することはなく、`serve --upload-exec` が統制します（[セルフホスティング](self-hosting.md#アップロードされた-config-のコマンド実行be-0090)を参照）。`sandbox` での実行には、追加フィールドとして `dockerImage`（Docker イメージ参照。例 `node:20-slim`）か `dockerfile`（バンドル相対のパスで、`docker build` でビルドします）のどちらか一方、加えて `port`（コンテナ内の待ち受けポート。ループバックのホストポートへ publish します）が必要です（[BE-0090](../../roadmaps/BE-0090-uploaded-config-command-execution/BE-0090-uploaded-config-command-execution-ja.md)） |
| `run_defaults.system_alert_handling` / `.erase` / `.network` / `.ios_tip_kit_handling` | app | 本来シナリオ単位や CLI フラグで指定する run のテスト動作設定に、アプリ単位の既定値を与えます（[BE-0177](../../roadmaps/BE-0177-run-behavior-target-config/BE-0177-run-behavior-target-config-ja.md)）。`systemAlertHandling` はシナリオと同じ形（`false`、または `{ rules, visionInstruction, pollInterval }`）を取りアラートガードの既定値になり、`erase` は `preconditions.erase` の、`network` はアプリのネットワーク収集の既定値になります。`erase` と `network` は **フラグ ＞ シナリオ ＞ これ ＞ ビルトイン既定**（erase は off、network は on）の順で解決し、`--headed`/`headless` と同じ重ね方です。`systemAlertHandling` の ON と OFF も同じ順です（`--system-alert-handling`/`--no-system-alert-handling` ＞ シナリオ ＞ これ ＞ ON）。ただし方針を持つキーは、それぞれの型で合わさります（[BE-0401](../../roadmaps/BE-0401-system-alert-handling-dsl-consolidation/BE-0401-system-alert-handling-dsl-consolidation-ja.md)）。リストである `rules` は内側の層から順に**連結**されるので、シナリオの答えをこの既定より先に試したうえで、どちらの層の宣言も消えません。スカラーである `pollInterval` は、値を持つもっとも内側の層が勝ちます（シナリオ、無ければ `--alert-poll-interval`、無ければこれ）。`visionInstruction` はどの層からも `run` に届きません。BE-0402 がこのキーの操作するフォールバックを取り除いたため、`run` は値を持つシナリオやターゲット config を実行前に拒否します。ここに書いた規則は、別のプロンプトについて規則を書いているシナリオの中でも、自分の名指すプロンプトには答えます。答えるときガードが通知を出し、シナリオ側は同じプロンプトの規則を書けば上書きできます。`labels` は [BE-0406](../../roadmaps/BE-0406-system-alert-declared-prompts/BE-0406-system-alert-declared-prompts-ja.md) で削除されました。ボタンのラベルが名指すのはボタンであってアラートではないため、ターゲット config が、どのシナリオも記述していない画面へのタップを許してしまうからです。[`systemAlertHandling` の層の重ね方](scenarios.md#層の重ね方1-つのキーは-3-か所から届く)を参照してください。`iosTipKitHandling` は TipKit の tip ガードの既定値です（iOS 専用、ビルトイン既定は**オフ**）。`--ios-tipkit-handling`/`--no-ios-tipkit-handling` で同じ優先順位に従います |
| `deeplink_scheme` | app | preconditions の deeplink で使う scheme |
| `backend` | app ?? defaults | プラットフォーム(`ios`/`android`/`web`/`fake`)か actuator(`xcuitest`)の安定度順リスト（単一文字列はリスト化）（[drivers](drivers.md#バックエンド選択と-actuator)） |
| `device` / `locale` | app ?? defaults | `locale` は launch 時に適用されます（`simctl` の launch 引数）。iOS の XCUITest バックエンドでは、cold spawn のたびに **Simulator 自身のシステム言語**も固定します。デバイスのグローバル設定ドメインに書き込み、SpringBoard がその言語で描画するよう再起動します。そのため、プロセス外の権限プロンプトのボタンのテキストは、Simulator がたまたま持っていた言語ではなく、この `locale` が予測するものになります。`preconditions.locale` が固定済みの値と異なるシナリオは、warm な runner を再利用せず cold respawn を強制します（[BE-0320](../../roadmaps/BE-0320-ios-system-alert-locale-determinism/BE-0320-ios-system-alert-locale-determinism-ja.md)） |
| `launch_env` / `launch_args` | app | preconditions が run 時にマージ追記 |
| `ready_when` | app | 任意の `readyWhen: { id: … }`。run を始める前に launch が出現を待つセレクタで、既定の「アプリが 2 要素以上を描画した」判定の代わりになります。最初の操作画面が常時表示のクロームの上に出るモーダルであるアプリ向けです（要素数の判定はモーダル提示前に返ってしまうことがあります）。`id` / `idMatches` はシナリオのセレクタと同様に OR 候補のリストを受け付けるので（`readyWhen: { id: [stable.row.1, stable_row_1] }`。BE-0221）、native な id 構文が異なる target も 1 つの `readyWhen` で扱えます。固定 sleep ではなく条件待ちです。指定するのは、その target の**すべて**のシナリオが同じ画面から始まるときに限ります。シナリオごとに最初の画面が異なる場合は、各シナリオの先頭に `wait` ステップを置いてください。readiness の最も強いシグナルは引き続き `readyWhen` です。iOS では、`BajutsuKit` の画面遷移 observer（BE-0310）を組み込んだ target が、namespace／要素数のヒューリスティックの上に、起動以降に報告された画面遷移という段を設定不要で得ます。ただし明示的な `readyWhen` はそれより上位で、起動直後の base 画面の遷移が、`readyWhen` が待つモーダルを先取りすることはありません。observer のシグナルが判定を担うのは、`readyWhen` が設定されていないときだけです |
| `id_namespaces` | app | doctor が参照 |
| `reserved_namespaces` | defaults | 情報用（doctor は app の `idNamespaces` のみで採点） |
| `mock_server` | app | ⚠️ スキーマのみ · 未配線 |
| `setup` | app | 既定の再利用前段（その steps を各シナリオの本編前に実行） |
| `evidence_dirs.scenarios` | app | このターゲットのシナリオディレクトリ。`run --target` は配下の `*.yaml` を全件読み、`record` は新規をここへ書く。config ファイル自身のディレクトリ基準の相対パス（`appPath` や兄弟の `evidence_dirs.baselines` / `.schemas` / `.goldens` も同様）で、`bajutsu` をどこから実行しても config は同じ挙動になる（BE-0242）。`run --scenario` / `record --out` で上書き |
| `capture` | defaults | 既定証跡（[evidence の注記](evidence.md#証跡の指示方法3-つ)） |
| `redact` | defaults ∪ app | マージ（下記） |
| `secrets` | defaults ∪ app | `${secrets.X}` を宣言する環境変数名。実値は証跡でマスク（[evidence](evidence.md#マスキングredact)） |
| `requires` | defaults ∪ app | ホスト型バックエンドでこのターゲットを実行するために、ワーカーが広告していなければならない capability トークン（[self-hosting](self-hosting.md#能力に基づくキューの振り分けbe-0166)、[BE-0166](../../roadmaps/BE-0166-capability-routed-queues/BE-0166-capability-routed-queues-ja.md)）。たとえば `[ios18, ipad]`。プラットフォーム軸は自動で加わるので、ランタイムやデバイス種別を固定したいときだけここにトークンを足します。ローカルの単一ワーカー実行では無視されます |
| `ai` | defaults < app（フィールドごと） | AI 経路のプロバイダ / モデル / エンドポイント / キー（[下記](#ai-プロバイダaibe-0047)）。省略（`None`）なら環境だけで決まります |
| `defaults.doctor.idCoverageOk` / `defaults.doctor.idCoverageFail` | defaults | doctor のグレード判定に使う id カバレッジのしきい値（[下記](#しきい値の設定defaultsdoctorbe-0024)）。既定は 0.9 / 0.7 です |

`backend` フィールドの検証で `_norm` が「単一文字列 → 1 要素リスト」に正規化します（defaults / app の両方に適用）。

### redact のマージ

config の `defaults.redact` と `targets.<name>.redact` は **union** されます（`_merge_redact` が `labels`/`headers`/`fields` を個別に和集合します）。さらにシナリオの `redact`（[evidence](evidence.md#マスキングredact)）が重なります。

### シークレット（`secrets:`）

`secrets:` は **環境変数名のリスト**で（`defaults` と `targets.<name>` の両方で宣言でき、`resolve` が和集合にします）、シナリオが入力に使える `${secrets.X}` 変数の宣言元です。`bajutsu run` は実行時に、宣言された各名前を環境から解決し、その値を action に展開（`${secrets.X}`）したうえで、**証跡に現れる箇所すべてでその実値をマスク**します（[evidence](evidence.md#マスキングredact)）。シナリオ source には `${secrets.X}` トークンだけが残り、実値は残りません。

### AI プロバイダ（`ai:`、BE-0047）

AI 経路、すなわち `record`、`crawl`、`triage --ai` は、任意の `ai` ブロックで設定した一つのプロバイダを通じてモデルへ到達します。このブロックは `defaults` と `targets.<name>` の両方で宣言でき、**フィールドごと**にマージされます（同じフィールドはターゲット側の値が勝ちます）。解決結果は `Effective.ai` に入るので、CLI と `serve` が一つの真実を共有します。これが「あなたの AI、あなたのキー、あなたのデータ」を支える仕組みです。どの AI 経路も、あなたが設定したキーとエンドポイントの下で動き、決定的な `run` ゲートはモデルをまったく呼びません（[BE-0047](../../roadmaps/BE-0047-ai-data-sovereignty/BE-0047-ai-data-sovereignty-ja.md)）。

```yaml
defaults:
  ai:
    provider: api-key                        # 登録済みのプロバイダ名。現状は api-key（既定）/ bedrock / ant / claude-code / none（AI 経路をすべて無効化）
    model:    claude-opus-4-8                 # 任意: その経路の既定モデルを上書き（環境変数 BAJUTSU_AI_MODEL でも可）
    effort:   high                            # 任意: 推論エフォート low/medium/high/xhigh/max（BAJUTSU_AI_EFFORT でも可）。claude-code
    language: auto                            # 任意: AI が生成する文章の出力言語 ja/en/auto（BAJUTSU_AI_LANGUAGE でも可）
    baseUrl:  https://ai-gateway.internal/v1  # 任意: 自己ホストのゲートウェイ / 社内プロキシ（api-key プロバイダ）
    keyEnv:   ANTHROPIC_API_KEY               # キーを保持する環境変数の「名前」。キーの値そのものは置かない
```

- **プロバイダは単一のインターフェースの背後に置かれたバックエンドです**（[BE-0104](../../roadmaps/BE-0104-vendor-neutral-ai-backend/BE-0104-vendor-neutral-ai-backend-ja.md)）。AI 経路は、プラットフォームが `Driver` インターフェースの背後のバックエンドであるのと同じように、ベンダー中立なシーム（`bajutsu/ai`）を通じてのみモデルへ到達します。そのため `provider` は固定された集合ではなく、**レジストリで検証される開放型**の値です。現在は `api-key`、`bedrock`、`ant`、`claude-code` のアダプタが同梱されています。前の三つは Anthropic アダプタを共有し、名前は認証手段を表します（直接の API キー、Bedrock の AWS 資格情報、`ant` CLI の OAuth トークンです。BE-0163。旧称 `anthropic` は今も `api-key` として解決されます）。`claude-code`（[BE-0176](../../roadmaps/BE-0176-claude-code-ai-backend/BE-0176-claude-code-ai-backend-ja.md)）はローカルの `claude` CLI をサブプロセス起動する別個のアダプタです。未知の名前は AI 経路が最初にプロバイダを解決する時点で明確なエラーとともに fail closed になります。この検証は config の読み込み時ではなく AI 層で行います。決定的なコアは AI プロバイダのスタックを import してはならないため（[BE-0112](../../roadmaps/BE-0112-layer-boundary-enforcement/BE-0112-layer-boundary-enforcement-ja.md)）、config は名前をそのまま受け取り、正当な名前を保持するレジストリが未登録の名前を拒否します。モデルファミリ（たとえば OpenAI 互換エンドポイント）の追加はアダプタの登録にあたり、下記の秘匿化とフェイルクローズの保証を構造上そのまま受け継ぎます。もう1つ `none` という名前も登録されています。これはベンダーではなくスイッチで、続く2つの項目で説明します。
- **`none` は「このリポジトリでは AI 経路を一切走らせない」という表明です**（[BE-0394](../../roadmaps/BE-0394-ai-provider-none-kill-switch/BE-0394-ai-provider-none-kill-switch-ja.md)）。`none` プロバイダのアダプタは何もしません。`provider: none` と書くだけで、あらゆる AI 経路が同時に無効になります。`record`、`crawl`、`triage --ai` は設定名を告げて終了コード 2 で終わります。`run` はどちらの向きにも無関係です。[BE-0402](../../roadmaps/BE-0402-run-alert-guard-drop-vision-fallback/BE-0402-run-alert-guard-drop-vision-fallback-ja.md) 以降、無効にすべき AI 経路をそもそも持ちません。そしてプロバイダのファクトリはバックエンドを返さずに例外を送出するため、どのコード経路も AI バックエンドを構築できません。プロバイダのキーを環境に置かないだけでも、ほぼ同じ挙動は得られます。この設定が足すのは、レビュアーが読める形の方針です。キーの不在はリポジトリのどこにも意図を残しませんし、`record` でシナリオを書くために export したキーは、以後のどのシェルでもオーサリング経路をモデルに接続したままにします。設定は環境変数より優先されます。コミットした `provider: none` は、環境を誰も管理していない継続的インテグレーション（CI）のランナーでも `BAJUTSU_AI_PROVIDER` に覆されません。プロバイダを何も指定していないマシンで1度だけ実行したい場合は、`BAJUTSU_AI_PROVIDER=none` を従来どおり使えます。`defaults` と `targets.<name>` のマージ規則は変わりません。`defaults` で AI を無効にしたうえで、特定のターゲットだけ有効に戻すこともできます。有効に戻す行は、誰の目にも触れない環境変数ではなく、同じファイルのなかでレビュアーが読む一行になります。
- **`serve` の Settings のドロップダウンは `none` を提示しません。** プロバイダは登録されていますが**選択可能ではない**ため、`/api/provider` は HTTP 400 で拒否します。組織の Settings での選択は `BAJUTSU_AI_PROVIDER` としてのみ起動するジョブへ渡り、プロジェクト自身の `ai.provider` がそれより優先されるからです。config でプロバイダを指定しているリポジトリでドロップダウンの `none` を選んでも効果はまったくなく、オフと書かれたスイッチの裏でジョブはモデルを呼び続けることになります。ここから1つの制限が生じますが、この項目は制限を解消せずに受け入れています。設定エンドポイントは Claude への到達可能性を、ターゲットの `ai` ブロックではなく組織が保存した選択から解決します。そのため `provider: none` を設定したリポジトリでも `claudeAvailable: true` を報告し、Web UI は record と crawl のタブを有効なままにします。スイッチ自体は効いています。これらのタブから始まるジョブはコマンドラインの起動です。リポジトリの config を自身で解決したうえ、モデルへ届く前に終了コード 2 で終わります。欠けているのは、タブを先にグレーアウトさせる事前の合図だけです。
- **キーは設定ファイルに置きません。** `keyEnv` は環境変数の名前を指すだけで、値は呼び出し時に環境から読みます。これにより秘密がリポジトリやアップロードされたバンドルに入りません。`baseUrl` は Anthropic SDK を自己ホストのゲートウェイやプロキシへ向けます（`Anthropic(base_url=…, api_key=os.environ[keyEnv])`）。スクリーンショットや要素ツリーは、ベンダーの既定先ではなく、設定したエンドポイントにだけ届きます。Bedrock は標準の AWS 資格情報チェーン（`AWS_REGION` と、環境変数 / 共有プロファイル / インスタンスまたはタスクロール）のままで、プロバイダ接頭辞付きの `model` を必要とします。
- **`ant` は API キー無しでサブスクリプション（SSO）のシートに課金します（BE-0163）。** `ant` プロバイダは公式の [Anthropic CLI](https://github.com/anthropics/anthropic-cli) 経由でモデルへ到達します。CLI を導入し、`ant auth login`（Claude Console に対するブラウザ経由の OAuth（SSO）サインイン）を実行してください。Bajutsu は呼び出し時に CLI からベアラートークンを読み、API キーではなく `auth_token` として SDK に渡すため、Claude の Pro / Max / Console のシートに課金されます。`ANTHROPIC_PROFILE` で名前付きの CLI プロファイルを選べます。API キーは不要で、すべての AI 経路（オーサリング、アラートガード、`triage --ai`）で画像もそのまま使えます。`ant` はユーザー自身が導入する外部バイナリで、Bajutsu が同梱・インストールすることはありません。
- **`claude-code` はローカル CLI 経由で Claude Code のサブスクリプションに課金します（BE-0176）。** `claude-code` プロバイダは Anthropic SDK ではなく [`claude` CLI](https://github.com/anthropics/claude-code)（`claude -p`、print モード）をサブプロセス起動するため、オーサリングや調査は、すでにサインイン済みの Claude Code の Pro / Max / Console のシート（`claude setup-token`、または対話ログイン）を使います。すべての AI 経路で画像もそのまま使えます。各スクリーンショットは呼び出しごとのスクラッチファイルに書き出し、そのパスをプロンプトで示したうえで、CLI にはそのディレクトリに限定した `Read` だけを許可して読ませます。画面上のテキストは信頼できない入力なので（[BE-0125](../../roadmaps/BE-0125-authoring-agent-tool-restriction/BE-0125-authoring-agent-tool-restriction-ja.md)）、それ以外のツールはすべて拒否し、権限の確認は fail closed にします。`ANTHROPIC_API_KEY` があっても CLI の呼び出しからは取り除き、API ではなくサブスクリプションに課金されるようにします。`claude` はユーザー自身が導入する外部バイナリで、Bajutsu が同梱・インストールすることはありません。CI ランナーやコンテナ、リモートの `serve` のように、`claude setup-token` の対話的なブラウザ認証を実行できない**ヘッドレスなホスト**では、それが可能な別のマシンで長寿命トークンを一度発行し、`CLAUDE_CODE_OAUTH_TOKEN` に設定します（シェル、`.env`、または `serve` の Settings パネルのいずれかで指定できます。パネルは API キーと並べてこのトークンを write-once で保持します。[BE-0215](../../roadmaps/BE-0215-claude-code-oauth-token-credential/BE-0215-claude-code-oauth-token-credential-ja.md)）。`claude` CLI はこのトークンを環境変数から読むので、ヘッドレスなホストでも対話ログインは要りません。
- **設定が先、環境変数はフォールバック。** 省略したフィールドは現状の環境変数（`BAJUTSU_AI_PROVIDER`、`ANTHROPIC_API_KEY`、`BAJUTSU_BEDROCK_MODEL`。`ant` プロバイダは `ANTHROPIC_PROFILE` を尊重しつつ CLI から資格情報を読みます）へフォールバックするので、`ai` ブロックの無い config はこれまでどおり動きます。
- **モデルとエフォートも設定が先で、環境変数がフォールバックです。** `model`（または `BAJUTSU_AI_MODEL`）はどのプロバイダでも既定モデルを上書きします。`effort`（または `BAJUTSU_AI_EFFORT`）は推論エフォートを `low`／`medium`／`high`／`xhigh`／`max` から指定し、`claude-code` プロバイダが CLI の `--effort` として反映します。config や環境変数から解決した未知の値はモデルの既定へフォールバックしますが、`serve` の **Settings** パネルは入力を検証し、未知の値はフォールバックせず拒否します（HTTP 400）。パネルは両方を変更できます。**ローカル**の `serve` では、保存したプロバイダ、モデル、エフォートが serve 所有のファイルに永続化され、次回の起動時に復元されるため（[BE-0184](../../roadmaps/BE-0184-persist-serve-ai-provider-settings/BE-0184-persist-serve-ai-provider-settings-ja.md)）、再起動しても起動時の環境変数へ戻ることはなくなりました。config の `ai:` ブロックは復元された値よりも優先されます。**ホスト型のマルチテナント**の `serve` では、この選択が**組織ごと**に解決・永続化されるため（[BE-0229](../../roadmaps/BE-0229-per-org-provider-settings-resolution/BE-0229-per-org-provider-settings-resolution-ja.md)）、各組織の `record`／triage／ドラフトの各経路は、その組織自身が保存した選択を使います。選択は共有のプロセス環境ではなく、ジョブごとの環境オーバーレイとしてジョブへ渡るので、ある組織の保存が別の組織の AI 実行を変えることはありません。`record` は解決結果を冒頭に表示します（`🤖 AI: <プロバイダ> · model <モデル> · effort <エフォート>`）。
- **出力言語も設定が先の、もう一つの設定項目です**（[BE-0188](../../roadmaps/BE-0188-configurable-ai-output-language/BE-0188-configurable-ai-output-language-ja.md)）。`language`（または `BAJUTSU_AI_LANGUAGE`）は、AI が自分で生成する文章（散文）、すなわち `record` の `from:` 由来と `crawl` の流れる推論を、どの言語で書くかを固定します。値は `ja` / `en` / `auto` のいずれかで、`auto`（既定）は現状の挙動を保ちます。`record` はゴールの言語に追従し、`crawl` は英語のままです。実行ごとに `record` / `crawl` の `--language` で上書きでき（フラグ > config > `auto`）、`serve` の **Settings** パネルの「出力言語」プルダウンからも設定できます。未知の値は `auto` へフォールバックします（パネルは HTTP 400 で拒否します）。この設定が制御するのは記述と調査の文章だけで、決定論的な `run` の判定には触れません。下記のデバイス `locale`（アプリや UI の言語を設定するもの）とは別物です。
- **フェイルクローズ。** `record`、`crawl`、`triage --ai` は、選択したプロバイダに使える資格情報が無いとき、プロバイダ別の明確なエラーで終了します。ホストされた既定先へ黙ってフォールバックするクライアントは決して構築しません。`run` は資格情報を一切参照しません（BE-0402）。
- **テキスト系入力は秘匿化しますが、スクリーンショットは秘匿化できません。** モデルへ送る要素ツリー、失敗のテキスト、（ユーザー指定もありうる）アラート指示は、証跡の書き出しと同じ run スコープの秘匿化（対象の `redact` キーと解決済みの秘密値）で隠します。スクリーンショットは画像であり、秘匿化はテキストを隠せても画素は隠せません。そこで二つ目の保証がスクリーンショットを受け持ちます。スクリーンショットを含むすべての入力は、あなたが設定したプロバイダ／エンドポイントにのみ送られます。
- **画面に表示された秘密は画素に残ります（BE-0151）。** 画像はマスクできないため、アプリが画面に表示する秘密（入力したパスワード、OTP、画面上の個人情報など）は、AI が見るスクリーンショットの画素にそのまま残ります。`record` は毎ターンのライブ画面を、`triage --ai` は run の `runs/` 証跡に保存された失敗時点のスクリーンショット（あれば）を送ります。この画像は設定から解決した AI プロバイダへ送られます。秘匿化が覆うのはテキスト（ネットワーク、要素ツリー、ログ）に現れる `${secrets.X}` の値であって、アプリが画面に描画した内容ではありません。これが不意打ちにならないよう、対象が `secrets:` をバインドしているとき、`record` と `triage --ai` は一度だけ警告を出します。これは緩和策ではなく告知です（視覚的な証跡こそが目的だからです）。この露出を完全に避けたいなら、秘密を含むフローで AI による作成を使わないか、テスト対象アプリで秘密を画面に出さないようにします。
- **使用量とコストは属性付きの台帳に記録します（[BE-0196](../../roadmaps/BE-0196-ai-usage-cost-ledger/BE-0196-ai-usage-cost-ledger-ja.md)）。** すべての AI 呼び出しは、そのトークンが何に使われたか（コマンド、プロバイダ、モデル、シナリオ）を付けた 1 行を JSON Lines（JSONL）台帳に追記し、プロバイダにトークン単価があればドル換算のコストも付けます。これは記録のみで、ベストエフォートで書き出し、決定的な `run` の判定には一切触れません。`ai` の下の 2 つの任意フィールドで調整します。

  ```yaml
  defaults:
    ai:
      usageLedger: runs/usage.jsonl              # 任意: 台帳のパス（既定は runs/usage.jsonl、"" で無効化）
      pricing:                                   # 任意: 同梱のトークン単価を上書き（100 万トークンあたりのドル）
        api-key/sonnet: { input: 3.0, output: 15.0, cacheWrite: 3.75, cacheRead: 0.3 }
  ```

  `usageLedger` は JSONL のパスを指定します。既定は（gitignore された `runs/` ツリー下の）`runs/usage.jsonl` で、明示的に空文字列にすると永続化を無効にします。`pricing` は同梱のデフォルト単価表を上書きするもので、`"プロバイダ/モデル"` をキーにします（モデル部分はモデル id にファミリで一致します。たとえば `api-key/sonnet` は任意の `claude-sonnet-*` に適用します）。トークン単価を持たないサブスクリプションプロバイダ（`ant`、`claude-code`）は、捏造したドルの値ではなく null のコストとともにトークン数を記録します。上のテキスト入力と同じく、台帳が保存するのは件数、単価、ラベルだけで、プロンプトや応答の内容は保存しません。

### mailbox（`email` ステップ）

`targets.<name>.mailbox` は、[`email`](scenarios.md#emailメールで届くコードをメールボックスから取得) ステップが 2FA / 検証コードを取得するためにポーリングする汎用 HTTP メールボックスを設定します。エンドポイントと認証情報をシナリオではなく config に置くためのものです。

```yaml
targets:
  myapp:
    mailbox:
      kind: http                                          # トランスポートアダプタ。省略時は http
      url: "${secrets.MAILBOX_URL}"                       # 受信箱エンドポイント（GET）。${secrets.*} は実行時に解決
      headers: { Authorization: "Bearer ${secrets.MAILBOX_TOKEN}" }
      # 任意のレスポンスマッピング。プロバイダ固有コードなしで任意の JSON を読むため:
      messages: "items"                                   # メッセージ配列へのドット区切りパス（既定: レスポンス自体が配列）
      fields: { to: to, subject: subject, body: text, receivedAt: receivedAt, id: id }
```

既定値はよくある形（`to` / `subject` / `body` / `receivedAt` / `id` を持つメッセージの配列）に合わせるので、準拠 API では `messages` / `fields` のマッピングは不要です。`email` ステップは受信箱を HTTP で読み、ステップ開始より新しいメッセージ（`id` で判定）だけを残し、一致するものを待ってコードを取り出します。決定的で LLM 非依存です（[BE-0046](../../roadmaps/BE-0046-otp-email-steps/BE-0046-otp-email-steps-ja.md)）。

`kind` は、メールボックスの背後にあるトランスポートアダプタを選びます。メールボックスは 1 つのインターフェースの背後にある backend であり、ベンダーではなくトランスポート（`http`、将来は `imap`）でキーを引くので、トランスポートを増やすことは runner を分岐させることではなくアダプタを登録することになります（[BE-0186](../../roadmaps/BE-0186-mailbox-provider-registry/BE-0186-mailbox-provider-registry-ja.md)）。`kind` は任意で、省略時は `http` になるので既存の `mailbox:` ブロックはそのまま動きます。未知の `kind` はフォールバックせず、きれいな config エラーで run を落とします。現在出荷しているのは `http` だけです。ベンダーではなくトランスポートでキーを引くのは、ベンダー間の違いが JSON のフィールド名だけで、それは `fields` が吸収するからです。

### Webhook 通知（`notify:`、BE-0099）

`notify:` はトップレベルの Webhook エンドポイントのリストで、`bajutsu run` が判定後の副作用として POST します。LLM を一切使わず、決定的な判定に影響することもない Slack 優先の配信経路です（[BE-0099](../../roadmaps/BE-0099-webhook-run-notifications/BE-0099-webhook-run-notifications-ja.md)）。

```yaml
notify:
  - format: slack                              # レンダラ。現状出荷しているのは slack だけ
    url: "${secrets.SLACK_WEBHOOK_URL}"         # webhook URL。${secrets.*} は実行時に解決
    on: [failure]                               # failure（既定）／change／recovery／always／start
    targets: []                                 # 任意: このシナリオ名だけに絞る。空なら run 全体が対象
```

- `on` は、このエンドポイントを発火させるイベントを選びます。`failure`（いずれかのシナリオが失敗、既定）、
  `always`（すべての run）、`change` / `recovery`（run 全体の判定が、同じ config ソースの直前の run から
  反転した場合。判定は runs ディレクトリ配下のその run の `manifest.json` から読みます）、`start`（run
  開始前に、専用のメッセージで 1 回だけ発火。`on` が `[start]` だけのエンドポイントはそこでしか発火せず、
  判定後には発火しません）。
- `targets`（トップレベルの config の `targets.<name>` マップとは無関係です）は、リストにある名前の
  シナリオだけに通知を絞ります。空（既定）なら run 内のすべてのシナリオが対象です。
- `targets.<name>.notify` は、そのターゲットについてトップレベルのリストを**丸ごと**上書きします
  （マージはしません）。省略すればトップレベルの `notify:` をそのまま継承します。
- 配信はベストエフォートです。タイムアウトに上限を設け、数回リトライし、失敗しても警告をログに出す
  だけです。判定や終了コードを変えることは決してなく、`--zip` や `--evidence-store` と同じ、判定後
  専用の規律に従います。現状レンダリングされるのは `format: slack` だけです（失敗したシナリオを最大 5
  件挙げ、残りは「…and N more」でまとめる Block Kit メッセージ）。未知の `format`、または `url` に
  未解決の `${secrets.*}` トークンが残っている場合は、壊れたペイロードを送る代わりに警告をログに出して
  スキップします。

### org（`orgs:`、マルチテナントのサーバ backend）

`orgs:` は、ホスト型サーバ backend のテナントを宣言します（[BE-0015](../../roadmaps/BE-0015-web-ui-public-hosting/BE-0015-web-ui-public-hosting-ja.md)）。各 org は、所属メンバーと、書き込みを許す GitHub Team（`editorTeams`）、その org が持つ targets を列挙します。所属メンバーの指定は3通りで、明示の GitHub login（`members`）、GitHub org 全体（`githubOrgs`）、単一の GitHub Team（`githubTeams`）です。

```yaml
orgs:
  acme:
    members: [alice, bob]                   # 明示の GitHub login
    githubOrgs: [acme-gh]                    # この GitHub org の全員（read:org の OAuth scope が必要）
    githubTeams: [acme-gh/qa]                # この Team の直接メンバー（GitHub org 全体は含めない）
    editorTeams: [acme-gh/scenario-maintainers] # これらの Team の直接メンバーは editor になり、サインインもできる
    allowedRepositories: [acme/app]          # これらのリポジトリの CI ジョブがこの org として振る舞える
    targets: [demo, checkout]
```

`allowedRepositories` は、人間の名簿の隣に並ぶ org の**機械**の名簿です（[BE-0414](../../roadmaps/BE-0414-ci-oidc-machine-identity/BE-0414-ci-oidc-machine-identity-ja.md)）。継続的インテグレーション（CI）のジョブが OpenID Connect（OIDC）トークンを交換して、この org として振る舞う短命のセッションを得られるリポジトリを挙げます。ロールは一切付きません。パイプラインを律するのは endpoint の許可リストであって、viewer／editor／admin ではありません。エントリには `"<owner>/<repo>"` という名前だけを書くか、その名前に `environment`、`ref`、`workflowRef` のいずれかの絞り込みを添えたオブジェクトを書きます。設定の手順、operator が指定しなければならない audience、交換の endpoint、名前の再利用とフォークにまつわる注意点は、[セルフホスティング](self-hosting.md#ci-ジョブを認証する任意be-0414)にあります。

OAuth ログイン時にユーザは自分の org に割り当てられます。順に、明示の `members`、GitHub org メンバーシップからの `githubOrgs` 一致、直接の Team メンバーシップからの `githubTeams` または `editorTeams` 一致を見ます。Team を最後に見るのは、org に Team を足したときに既存の login の所属先を動かさないためです。以後はその org の targets だけが見え、run の artifacts／scenarios／baselines はその org 専用のオブジェクトストレージ prefix の下に置かれます。どの org にも挙げられていない target は単一の `default` org に入るので、`orgs:` ブロックの**無い** config はシングルテナントです。CLI とローカルの `serve` は `orgs:` を一切参照しません。

GitHub OAuth を構成すると、org メンバーシップがアクセスも決めます（[BE-0313](../../roadmaps/BE-0313-github-org-team-rbac/BE-0313-github-org-team-rbac-ja.md)）。サインインには構成済みの org への所属が必要です。`members`、`githubOrgs`、`githubTeams`、`editorTeams` のいずれかで、成功すると **viewer** ロールが付きます。org の `editorTeams` のいずれかの直接メンバーは **editor** に昇格し、サーバ全体で 1 つ以上の admin Team（`BAJUTSU_OAUTH_ADMIN_TEAMS`。[セルフホスティング](self-hosting.md#2-github-oauth-を足す任意)を参照）のいずれかのメンバーは **admin** になります。`editorTeams` は昇格させるだけでなくサインインも許します。書き込みを許した Team を `githubTeams` に重ねて書く必要はありません。`editorTeams` がリストなのは、`githubOrgs` と `githubTeams` がリストなのと同じ理由です。1つの org が複数の GitHub organization にまたがることがあり、枠が1つでは organization ごとの書き込み Team を挙げられません。旧来の単数形 `editorTeam` を書いた config もそのまま動きます。`serve` がそのキーを `editorTeams` に畳み込み、名前の変更が中途半端で単数形が複数形の隣に残っている場合は両方を畳み込みます。ただし、文書に載るのは複数形だけなので、名前は変更してください。`githubTeams` の各エントリ、`editorTeams` の各エントリ、`BAJUTSU_OAUTH_ADMIN_TEAMS` の各エントリは、いずれも 1 つのフラットな Team で、`"<github-org>/<team-slug>"` の形で書きます。どれについても、その下にネストした Team は一致しません。比較は3つとも大文字小文字を区別しません。GitHub 自身が org login と Team slug をそう解決するからです。したがって OAuth を使う構成では `orgs:` ブロックの宣言が必須です。ただし、構成済みの admin Team のメンバーだけは、この所属がなくてもサインインできます。GitHub の Teams API 自体がエラーを返している間を除きます。そのため、`orgs:` ブロックが壊れている、あるいは存在しない状態だけでは、admin 全員が締め出されることはありません。Team だけで許可された admin は、`orgs:` のどのエントリにも属さないため `default` org に置かれます。そのためこの復旧経路に頼るデプロイは、`default` という名前の実際の org を宣言しないでください。宣言すると、復旧した admin のユーザ行、audit の記録、オブジェクトストレージの prefix が、中立な catch-all ではなくそのテナントの中に入ってしまいます。なお、Team だけを名簿とする org は、GitHub の Teams API が答えることに依存します。`/user/teams` は fail closed で、Team を勝手に作り出しません。そのため API がエラーを返している間は、所属が Team だけの login は受け入れられるのではなく拒否されます。

**データベースを繋いだデプロイでは、この6つのフィールドのうち5つを読むのは一度きりです**（[BE-0375](../../roadmaps/BE-0375-serve-org-lifecycle-management/BE-0375-serve-org-lifecycle-management-ja.md)）。`orgs` テーブルがまだ空だった1回の起動時に、`serve` は各 org のメンバーシップを、この serve を**起動したときの** config からデータベースへ写します。対象は `members`、`githubOrgs`、`githubTeams`、`editorTeams`、`allowedRepositories` です。以後のサインインはデータベースだけを見て解決します。この書き写しはデプロイの一生に一度だけです。org が1つでも入っている状態で起動すれば、廃止済みのものが1つあるだけでも何も写さないので、後から config を書き換えても、その config を積んだまま再起動しても、admin の与り知らないところでテナントが増えたり形を変えたりすることはありません。起動後に Web UI や `POST /api/config` で bind した config は、`orgs:` に何が書かれていても、そもそも一切書き写しません。

メンバーシップの編集は、以後この config ファイルではなく Orgs ページから行います。この5つのフィールドをここで書き換えても効果はありません。`serve` は、まだこの5つを宣言しているエントリの org 名を挙げて警告を記録するので、operator は編集が消えたと戸惑う代わりに、ファイルが決定権を持たなくなったことに気付けます。ここで引き続き効くのは `targets` です。したがって、そうしたデプロイでは `targets:` だけに削ぎ落としたエントリが本来の姿になります。書き写しの起きる起動より前に削ぎ落としても安全なので、順序を誤る余地はありません。`targets` だけを宣言したエントリは書き写しの対象から外れるため、その org が「誰も受け入れない」状態で固定されることはありません。

同じ名前の target を2つの org がそれぞれ要求してもよく、どちらの org もその target に対して認可されます。bind している config が1つである以上、その名前が解決する `targets:` の定義は両者で共有されます。

**API 経由で bind した config――アップロードした bundle、compose した組、Git ソース――は、target の所有について `orgs:` ブロックを一切参照しません。** その config はある org **として** bind されたので、宣言されている target はすべてその org のものであり、ほかのどの org のものでもありません。ブロックに何が書かれていても変わりません。デプロイが管理していないファイルから所有を読むことは、そのファイルにメンバーシップを書かせないのと同じ信頼の問題であり、しかも黙って失敗していました。bundle の `orgs:` が唯一の target を自分の所属しない org のものだと主張していると、target の一覧が空になり、理由は何も示されませんでした。アップロードする bundle からは `orgs:` を外してください。そこでは何も決めません。データベースを繋いでいないデプロイは、以上のいずれとも無関係で、引き続きすべてのフィールドをこのファイルから読みます。

## CLI からの選択

CLI（コマンドラインインターフェース）のすべてのコマンドは、`--target <name>` で 1 つのターゲットを選択し、`--config`（既定 `bajutsu.config.yaml`）で config を指定します。`--backend ios`（またはプラットフォーム/actuator のカンマ区切り）で解決順序を上書きできます（[cli](cli.md)）。

### クロスブラウザマトリクス（`--browsers`、BE-0076）

`bajutsu run --browsers chromium,firefox,webkit` は、選んだシナリオをエンジンごとに 1 回ずつ実行し、**エンジン × シナリオの合否マトリクス**を 1 つ出力します。これは `--browser` と同じエンジン軸を複数指定する書き方で（web backend のみ。`--browsers chromium` は `--browser chromium` と同じであり、エンジンが 1 つなら通常の単一エンジン経路を通ります）、**指定したすべてのエンジンがすべてのシナリオに合格したときだけ**緑になります（all-must-pass）。Chromium と Firefox では緑なのに WebKit では赤になるシナリオは、描画エンジンの非互換が機械的に検出された箇所です。「Chrome では動くが Safari では壊れる」という、単一エンジンのテストでは決して見えない不具合がこれにあたります。判定はエンジンごとの既存の決定的な `run` の結果を集約したものに過ぎず、AI は判定に入りません。

各エンジンは専用のブラウザプールに対する独立した 1 パスなので、その証跡は `runs/<id>/<engine>/<NN-scenario>/` の下に置かれ、エンジン間で衝突しません。実行後は run のルートに `manifest.json`、`junit.xml`、`report.html` を **1 組だけ**まとめます。manifest はエンジンごとの判定を集約した `matrix` ブロックを持ち、report はエンジン × シナリオのグリッドを描画し、JUnit は各ケースにエンジンを織り込むので（`classname="bajutsu.<engine>"`）、CI からは `chromium.login` と `webkit.login` が別々のケースに見えます（[reporting](reporting.md#manifestjson)）。リストに未知のエンジンがあれば、`--browser` と同じく、どのブラウザも起動する前に終了コード 2 で終わります。3 つのエンジンはいずれも Linux 上でヘッドレス実行できるため、マトリクスは Mac もデバイスファームも要らず通常のゲート内で走ります。firefox／webkit のバイナリは実行時に取得します。

### Git リポジトリからの config（BE-0063）

`--config` は **Git ソース**も受け付けます。ローカルにチェックアウトせずに、テストリポジトリのスイートを実行できます（例：`bajutsu run --config github:acme/mobile-tests@v1.4.0:e2e/bajutsu.config.yaml --target checkout`）。

```
github:<owner>/<repo>[@<ref>][:<path>]                          # GitHub ショートハンド
git+https://<host>/<owner>/<repo>.git[@<ref>][#<path>]          # 一般形（ホストは将来用に予約）
```

- **現状で実装済みのホストは GitHub だけです。** 一般形 `git+https://<host>/…` は解析します（GitHub Enterprise / GitLab などへの拡張の余地を残すため）が、`github.com` 以外のホストは今のところ、黙って github.com を叩くのではなく明確なエラーで失敗します。
- Git ソースから実行した run は、解決したコミットを `manifest.json` の provenance（`configSource: { host, owner, repo, ref, sha }`）に**記録**します。ブランチ指定の run でも、実際に実行した正確なコミットが分かり、後から再現できます（[reporting](reporting.md#manifestjson)）。
- `<ref>` はブランチ、タグ、コミット SHA（既定はリポジトリのデフォルトブランチ）、`<path>` はリポジトリ内の config パス（既定はルートの `bajutsu.config.yaml`）です。スキームを認識できない値は、従来どおり**ローカルパス**として扱います。
- ref を不変のコミット SHA に解決し、その部分木を content-addressed なキャッシュ（`~/.cache/bajutsu/gitsrc/<host>/<owner>/<repo>/<sha>/`）に展開して、そこから config を読みます。config の `scenarios` / `baselines` / `schemas` / `appPath` は相対パスで、**チェックアウトのルート**を基準に解決します。これはローカル config が自身のディレクトリを基準にするのと同じ「config の置き場所を基準にする」ルールで、基準点が取得したツリーのルートになるだけです。YAML だけでなくツリー全体が付いてきます。取得してきた config は信頼できないので、パスはチェックアウト配下に**閉じ込め**ます。絶対パスや `../` による逸脱は拒否します。一方、operator が信頼できるローカルファイルは config ファイル自身のディレクトリを基準に解決し、閉じ込めません（兄弟ディレクトリを指してよい）。
- 取得したばかりのチェックアウトにはビルド済みバイナリが無く、手元で先にビルドする「最初の一回」もありません。そこで Git ソースの `run` は**アプリをオンデマンドでビルド**します。`appPath` が設定されていてバイナリが無いとき、config の `build` コマンドを**チェックアウトのルート**から実行し（`make -C demos/showcase swiftui-build` のような `build` の相対パスはそこを基準にします）、その後に実行を続けます。ビルドが失敗すれば明快に終了します。ローカルパスの `run` は従来どおりで、ビルドはせず、バイナリが無ければエラーになります。
- **固定コミット SHA**（`@<sha>`）は再現可能で、初回取得後はオフラインで動きます。ブランチ（やタグ）は毎回解決し直します。
- **非公開リポジトリには資格情報が要ります**（[BE-0224](../../roadmaps/BE-0224-github-private-repo-config-auth/BE-0224-github-private-repo-config-auth-ja.md)）。トークンは**取得のたびに**解決するので、ローテーションした秘密も再起動なしで反映されます。解決の順序は、設定済みの **GitHub App installation**（`BAJUTSU_GITHUB_APP_ID` と秘密鍵）、次に serve で入力した資格情報（`BAJUTSU_GIT_CONFIG_TOKEN`）、次に `GITHUB_TOKEN` / `GH_TOKEN`、次に `gh auth token`、それも無ければ匿名です。トークンはログに出しません。付与は**最小権限**にしてください。すべての非公開リポジトリに読み書きを与えてしまう classic の広い `repo` スコープの個人アクセストークン（PAT）ではなく、対象のリポジトリだけに **Contents: read** 権限で絞った**細粒度（fine-grained）**の PAT、または App installation を選びます。無人で動く self-hosted の `serve` は、人ではなくサービスに紐づく短命な installation トークンである **GitHub App** で認証してください（[self-hosting の非公開リポジトリへのアクセス](self-hosting.md#git-config-source-の非公開リポジトリへのアクセスbe-0224)を参照）。アクセスが無いときの取得失敗は、素の 404 ではなく、レート制限、組織のシングルサインオン（SSO）の認可不足、トークンの拒否、あるいは「`<owner>/<repo>` に Contents: read を持つ資格情報を用意してください」といった、**本当の原因**を名指すメッセージで報告します。
- `bajutsu run` にはゲート向けスイッチが 2 つあります。**`--config-offline`** はキャッシュを使いネットワークに触れません（オフラインでは解決できないので固定 `@<sha>` が必要）。**`--require-pinned-config`** は Git config がコミット SHA を固定していなければ失敗します。ゲートではブランチもタグも動きうるので、SHA だけを認めます。
- serve の UI も Git ソースを bind します。起動時の `serve --config github:…`、または「Open config」ダイアログの
  「From a Git repository」欄でチェックアウトを実体化し、そのルートから serve します（[cli → serve](cli.md#serve)）。**非公開**リポジトリ向けには、このダイアログに資格情報の入力欄があります（BE-0224）。細粒度の PAT や App トークンを入力すると、serve の秘密ストアを通じて **write-once** で保存します。値はマスクして表示し、二度と読み出しません（ローカルの serve ではプロセスの環境変数に保持し、ホスト型バックエンドでは組織ごとに暗号化して保存します）。アクセス不足の診断はダイアログ内にその場で表示します。
- 残りの後続: `record` / `crawl` の読み取り専用入力（生成物は SHA キーのキャッシュではなくローカルの `--out` に書く）です。

## 新しいターゲットのオンボーディング

新しいターゲットを追加するには、**アプリ側の準備と config への 1 エントリ追加**を行います。ツール本体への変更は不要です。

1. **実装規約を適用する**。主要要素に `accessibilityIdentifier` を（アプリの名前空間で）付け、状態を
   label / traits / value に露出させ、launch hook を用意し、アニメーションを無効化します。
2. **`targets.<name>` を追加する**。`bundleId`（必須）/ `deeplinkScheme` / 既定 `launchEnv` /
   `idNamespaces` などです。
3. **（任意）再利用前段を用意する**。ログインなどを `setup:` シナリオに切り出し、その steps を各シナリオの本編前に実行します（app 単位かシナリオ単位で指定）。
4. **`bajutsu doctor --target <name>` で検証する**。規約充足度スコアを見ます（下記）。
5. **シナリオを配置する**。識別子はそのアプリの名前空間で書きます。

## 識別子の命名規約

`accessibilityIdentifier` は **`<namespace>.<element>` のドット区切り**です。すべて小文字で、各セグメントは `[a-z0-9-]` です。先頭セグメントが名前空間で、`idNamespaces` に宣言した集合のいずれかである必要があります。

```
settings.reindex            # <namespace=settings>.<element=reindex>
home.search
list.row.<id>               # 動的行: 末尾は「データ由来の安定キー」（index 由来は禁止）
```

3 つの不変条件:

1. **画面内で一意**。同一画面に同じ id を 2 つ置きません（[selectors の曖昧検出](selectors.md#解決セマンティクス)）。
   繰り返し要素はデータ由来キーで一意化し（`list.row.3`）、集合操作には `idMatches` + `count` を使います。
2. **非ローカライズかつデータ由来**。id に表示文言を使いません（翻訳で壊れるため）。
3. **名前空間で前置**。すべての id を宣言済みの名前空間で始めます。

showcase の id カタログは [showcase](showcase.md)（全体は `demos/showcase/SPEC.md`）にあります。

## doctor（規約充足度スコア）

実装: `bajutsu/common/doctor.py`。**AI 非依存で決定的**です。1 画面の `query()`（CLI は actuator で取得した現在画面）を解析してスコアを出します。

> `doctor` はまず**実行可能ゲート**（`preflight.py`）を確認し、その後でスコアを出します。ゲートが確認する内容は、選んだバックエンドが必要とするものです。iOS（XCUITest）バックエンドなら `xcodebuild` / `xcrun`、および起動済みシミュレータ。web（Playwright）バックエンドなら Playwright パッケージとその Chromium ブラウザ（`uv sync --extra web` と `playwright install chromium`）です。続いて現在画面を採点します。web ターゲットでは新しいブラウザをターゲットの `baseUrl` に遷移させてそのページを採点し、iOS では起動済みシミュレータの画面を採点します。スコアの対象は、依然として現在表示されている画面だけです（入口や現在画面のみで、全画面は網羅しません）。

### 指標（`Score`）

操作可能要素（trait ∈ `ACTIONABLE_TRAITS` = button / link / textField / searchField / textView / switch / slider / tab / cell）を母数として測定します。

| 指標 | 定義 | しきい値（既定） |
|---|---|---|
| `idCoverage` | id を持つ操作可能要素の割合 | ✓ ≥ 0.9 / warn 0.7–0.9 / fail < 0.7 |
| `namespaceConformance` | id の先頭が `idNamespaces` に一致する割合 | 規約外を `off_namespace` に列挙 |
| `duplicateIds` | 1 画面内の id 重複数 | 1 件でも Blocked |

### グレード判定

- **Blocked**: 画面に actionable な要素が 1 つも無い（多くは空画面、まだ読み込まれていない、または想定外の画面で、`render` がその旨を示します）、id 重複あり、**または** `idCoverage` < `idCoverageFail`（既定 0.7）。
- **Ready**: `idCoverage` ≥ `idCoverageOk`（既定 0.9）**かつ** `namespaceConformance` == 1.0。
- **Partial**: それ以外（実行はできるが、座標フォールバックやフレーキーの予告）。

### しきい値の設定（`defaults.doctor`、BE-0024）

グレード判定に使う id カバレッジのしきい値は `defaults.doctor` で変更できます。テスト用 id を付ける必要のない装飾的要素が多いアプリでは、しきい値を下げる（`idCoverageOk` や `idCoverageFail` を低めに設定する）ことで、ツール本体を変更せずに判定を緩められます。

```yaml
defaults:
  doctor:
    idCoverageOk:   0.85   # 既定 0.9。カバレッジがこの値以上で "Ready" の候補になります
    idCoverageFail: 0.6    # 既定 0.7。カバレッジがこの値未満で "Blocked" になります
```

両方とも [0, 1] の範囲で指定し、`idCoverageOk` は `idCoverageFail` 以上でなければなりません。範囲外の値は config 読み込み時に拒否されます。省略すると既定値（0.9 / 0.7）が適用されるため、既存の config はそのまま動きます。

### 出力

`render(score)` が人間向けのサマリを返します。不足している要素は **具体的に列挙**するので、id をどこに追加すればよいかがそのまま分かります。

```
grade: Partial
idCoverage: 0.83 (5/6)
namespaceConformance: 1.00
duplicateIds: 0
  missing id: label='Close' traits=['button'] frame=(...)
```

CLI の `doctor` は Blocked のとき終了コード 1 で終了します（[cli](cli.md#doctor)）。
