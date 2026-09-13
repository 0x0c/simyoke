# 継続的インテグレーション（CI）

2 つの別々のトピックを扱います。

1. **このリポの CI**。ツール自体をガードします（`.github/workflows/`）。
2. **あなたのアプリの CI で bajutsu を回す**。再利用できる composite action とレシピです。

## このリポの CI

| Workflow | ランナー | タイミング | 内容 |
|---|---|---|---|
| [`ci.yml`](../../.github/workflows/ci.yml)（`check` ジョブ） | Linux | `main` への push、全 PR（プルリクエスト） | Python 3.13 上で `make check` ゲート一式を実行します。ロックの鮮度（`uv lock --check`）、整形（`ruff format --check`）、lint（`ruff`）、シェル lint（`shellcheck`）、ワークフロー lint（`actionlint`）、型（`mypy bajutsu demos scripts` と、テストスイートに対する `mypy --allow-untyped-defs tests`。BE-0388）、カバレッジ下限つきの `pytest`（`--cov-fail-under=89`）です。ロジック層はシミュレータ不要なので速く安価です |
| [`mcp-wire.yml`](../../.github/workflows/mcp-wire.yml) | Linux | 手動 + `bajutsu/mcp/**`・`bajutsu mcp` CLI・wire テストに触れる PR | **MCP ワイヤ round-trip** のレーン（BE-0301）で、ジョブは **wire (stdio)** の 1 つです。`bajutsu mcp --transport stdio` を実際のサブプロセスとして起動し、`mcp` SDK の `fastmcp.Client` で stdio 越しに駆動します（`pytest tests/test_mcp_wire.py -m mcp_wire -n0`）。in-process の `tests/test_mcp.py` が触れない JSON-RPC のフレーミング、ツールスキーマの広告、リソース URI のエンコーディングを検証します。ツール一覧を取得し、`bajutsu_doctor` / `bajutsu_run` の呼び出しを round-trip し、run 証跡のリソースを読み取ります。いずれも `fake` バックエンドに対して行います（`bajutsu_doctor` はサーバ内でデバイス不要の実ロジックを走らせ、`bajutsu_run` は実際の `bajutsu run` を起動しますが、その verdict は環境依存なので、round-trip では verdict 行が transport を往復することだけを確認します）。`mcp_wire` マーカーがこのスイートを高速ゲートから外します（pyproject の `addopts` の `not mcp_wire`）。実サブプロセス + stdio IPC は in-process 呼び出しより時間に敏感なので、まず**ゲート対象外**の PR ごとのシグナルとして着地させ、安定を確認してから必須化します。BE-0282 の `network (playwright)` が示した前例です |
| [`web-e2e.yml`](../../.github/workflows/web-e2e.yml) | Linux | 手動 + 全 PR + マージキュー（必須の `E2E (web)` チェック） | **web（Playwright）バックエンド**のレーン（BE-0279）で、ヘッドレス Chromium に対する 6 ジョブから成ります。**smoke (playwright)** は `demos/web` のシナリオを実行し（`make -C demos/web e2e`）、**dogfood (serve UI)** は Bajutsu 自身の serve SPA を駆動し（BE-0058）、**conformance (playwright)** は BE-0114 のドライバコントラクトを実ブラウザに対して実行します。**network (playwright)** は実ネットワーク経路（`page.route` の介入、`requestfinished` のキャプチャ、`mocked` フラグ、実際にキャプチャした証拠の redaction）を動かします（`make -C demos/web e2e-network`、BE-0282）。**codegen (playwright)** はシナリオからネイティブな Playwright テストを生成し、実際の `@playwright/test` ランナーで実行します（`make -C demos/web codegen-e2e`、BE-0293）。`ios-e2e.yml` の `codegen (xcuitest)` の web 版です。**onboarding (doctor / provision)** は、ブラウザが本当に入っていないホストに対して `bajutsu.common.provisioning.provision` を実行し、provision 後に `doctor` の環境ゲートを再確認します（BE-0304）。**Mac / Simulator 不要**で、コアがプラットフォーム非依存であることを示します。6 つとも同じ `changes` 検出ジョブ（`scripts/e2e_changes.py`、`E2E_LANE=web`）でパスゲートします。**onboarding** を除く、決定論的で実行環境に依存しない 5 つのジョブが、常に結果を報告する `E2E (web)` ジョブ（必須チェック）に集約され、**onboarding** は PR ごとの参考シグナルにとどまります（BE-0304）。web 経路に影響しえない PR はブラウザジョブを飛ばし、ゲートは合格します。**network (playwright)** と **codegen (playwright)** はいずれもまず PR ごとのシグナルとして着地させましたが、CI で安定を確認できたので、現在は `E2E (web)` の `needs:` に昇格しています。**network** は android-e2e.yml ですでにゲート入りしている `network (adb)` の web 版、**codegen** は `ios-e2e.yml` ですでにゲート入りしている `codegen (xcuitest)` の web 版にあたります |
| [`dependency-audit.yml`](../../.github/workflows/dependency-audit.yml) | Linux | 手動 + 週次 + `pyproject.toml` / `uv.lock` を触る `main` への push / PR | ロックした依存グラフ（`uv export` → `pip-audit --no-deps`）を脆弱性 DB に照合します。結果はロックファイルと DB だけで決まるので、依存の変更時と、変わらない pin に対して新たに公表された脆弱性を拾う週次スケジュールで実行します |
| [`swift.yml`](../../.github/workflows/swift.yml) | macOS | `main` への push + `BajutsuKit/**` を触る PR | [BajutsuKit](../../BajutsuKit) の `swift build` + `swift test` を実行します。純 Foundation のロジック（リクエスト照合 / モック解析）をシミュレータ無しで単体テストします。実機でのインターセプトそのものは `ios-e2e.yml` がカバーします |
| [`ios-e2e.yml`](../../.github/workflows/ios-e2e.yml) | macOS | 手動 + 全 PR + マージキュー（必須の `E2E (iOS)` チェック） | **iOS（XCUITest）バックエンド**のレーンで、showcase に対する 10 個の macOS ジョブから成り、いずれも XCUITest バックエンド（常駐の BajutsuRunner）で走ります。**build (app + runner)** ジョブが、showcase の 2 つのアプリ（SwiftUI + UIKit）と常駐ランナーを一度だけビルドし、1 つの `ios-build` アーティファクトとしてアップロードします。consumer のジョブは、ビルドし直すのではなくこの成果物をダウンロードしてインストールするので、コールドの Swift ビルドはジョブごとにではなく一度だけ支払われます。**run (xcuitest)** は同型のゲート担当レーンで、かつては別々の `smoke` / `gestures` / `permission` / `runner-actuation` ジョブだったシナリオを、1 回の `bajutsu run` にまとめたものです。常駐ランナーをシナリオ間で warm 再利用します（BE-0291）。ただしこのレーンでは再利用を 1 回に制限しているため（`BAJUTSU_XCUITEST_MAX_WARM_REUSES: 1`。混雑した CI ホストでの run 中のランナークラッシュを抑えるためです）、実際に走る 12 個のシナリオドキュメント（7 個のファイルが保持する全シナリオです。BE-0315 が通知許可をネイティブにしたため、除外する `ai` タグ付きのシナリオはありません）は、コールドの `xcodebuild test-without-building` の起動を一度ではなく 6 回支払います。これは置き換えた 4 個の個別ジョブの合計より 2 回多いのですが、その 4 個のジョブはそれぞれ自前の Swift ビルドと起動も支払っていたので、レーン全体で一度だけ支払う共有の `build` ジョブの分だけ、正味のコストは今も下がっています。内訳は、Stable カタログ（`smoke.yaml`）、ピンチ / 回転の 2 本指ジェスチャ（`gestures_multitouch.yaml`、BE-0019）、常駐ランナーチャネルの `/type`（`search.yaml`）と `/swipe` + `/back`（`notices.yaml`、BE-0281）、提示された `UIAlertController` の各ボタンのタップ（`alert.yaml`。`uniquelyIdentifiedElement` の重複グループの分岐に到達する唯一のシナリオです。この分岐は `swift test` ではコンパイルされません）、決定論的な `permissions` フィールドと、BE-0315 以降はネイティブなリアクティブ `systemAlertHandling` の通知許可（`permission.yaml`、BE-0276 の location と BE-0315 の通知。どちらもゲート上で走ります）、プロアクティブな `handleSystemAlert` ステップ（`permission_system_alert.yaml`、BE-0316。SpringBoard の通知許可プロンプトの「許可」ボタンを、視覚モデルなしのアクセシビリティクエリでタップします）です。`run` は各シナリオを個別の verdict まで実行し、どれかが失敗すればゲートを赤くするので、シナリオごとの合否はジョブ名ではなく run のレポートに移ります。**actuation (xcuitest)** は、このレーンでゲート対象外の受け皿となるジョブです。まだ `run` に昇格していない実機の XCUITest アクチュエーション（BE-0281）として、Stable の起動タブ上で `back`（`navigation.yaml`）とデバイス制御（`setLocation` / クリップボード / `push`、`device.yaml` + `push.yaml`）を実行し、あわせてテキスト編集の 4 つのアクチュエータ（`text_editing.yaml`）も実行します。さらに、アクチュエートできる 3 つ目のバックエンドとして、シナリオ作成機能（BE-0285）も駆動します。Log タブのライブなカウンタ値を取り込む `extract`（`extract.yaml`）、ループ本体で詳細画面を push / pop して反復のあいだにツリーを変化させながら Stable の 5 行を反復する `forEach`（`foreach.yaml`）、data-driven の行（`data_driven.yaml`）、`relaunch`（`relaunch.yaml`）の 4 つです。この 4 つは showcase の共有シナリオファイルをそのまま使い（BE-0221）、デバイス制御のシナリオより前に並べています。配信された通知や OS レベルの許可が、この 4 つの前に来ないようにするためです。リストの最後は、このレーンで唯一プロセスをまたぐ本物のペースト（`paste_system_alert.yaml`、BE-0369）です。`setClipboard` がアプリの外からペーストボードに値を書くため、アプリ自身の読み取りが iOS のペースト同意プロンプトを上げ、`handleSystemAlert: { prompt: paste }` がネイティブなアクセシビリティ照会でそれに応答します。許可と拒否のそれぞれを、対象の locale のもとと `locale: ja_JP` のもとで実行します。日本語の 2 本は、日本語で描画される SpringBoard に対してラベルの対応表を確かめます。実機の新しいカバレッジなので、`run` へ昇格させる前にここで安定性を確かめます。**golden (xcuitest)** は BE-0006 の要素ツリー golden を XCUITest で実行します（`golden.yaml`）。`android-e2e.yml` の `golden (adb)` に対応する iOS 版です。**codegen (xcuitest)** はシナリオからネイティブ XCUITest を生成し（`make -C demos/showcase ui-test`）、`xcodebuild` で実行します（テスト時に bajutsu / AI は不要です）。UITests スキームをコンパイルするジョブで、常駐ランナーとは別の成果物なので、自前でビルドし、`build` アーティファクトの consumer にはなりません。**bundled-runner (xcuitest)** は、config の `testRunner` ではなく wheel バンドルから解決したランナーで、SwiftUI と UIKit 両方の a11y アプリに対して smoke シナリオを実行し（BE-0292）、ランナーがアプリに依存しないことを確認します。**conformance (xcuitest)** はドライバ conformance スイート（BE-0114）を XCUITest バックエンドに対して実機で実行します。スイート実行中の Simulator インフラ障害（常駐ランナーのクラッシュ、`base.BackendCrashError`）は、`bajutsu run` と同じように復旧します。新しいデバイスを取り直して該当テストを再実行し、上限はテストごとに再試行回数と壁時計の再起動予算（`BAJUTSU_CRASH_RETRIES` / `BAJUTSU_CRASH_RECOVERY_BUDGET`）で区切ります。これによりこの必須チェックは、ホストのフレーキーではなく本物の契約違反で赤くなり、毎回クラッシュする場合は今までどおり明確に失敗します。各再起動は、アップロードする `conformance-recovery-report` アーティファクトに数え上げられます（BE-0334）。固まった CoreSimulator はホスト側の障害ですが、デバイスを作り直しても直りません。作り直しの手順そのものが、いま停滞したのと同じ `simctl` 呼び出しでできているうえ、その停滞は放っておいても解消するからです。そこでスイートは再試行せず、ジョブログでホスト障害として名指しし、同じアーティファクトに数え上げます。あわせて、アプリのデータコンテナの解決をテストごとではなくリースごとに行い、その 1 回の読み取りが期限を超えたときだけもう一度試します。2 回目で解消した停滞は何も赤くしないので、先行指標として同じアーティファクトに数え上げます。あとでチェックを固まらせるのは、悪化していくこの同じホストだからです（BE-0378）。**fault-injection (xcuitest)** は、健全なランナーを駆動する代わりに、ランナー自身のホストプロセスにシグナルを送ります（BE-0305）。短い `SIGSTOP` は、固まったランナーと同じようにチャネルをハングさせるので、一過性リトライ（BE-0207）が吸収しなければなりません。リトライ予算を超える凍結は、crash-recovery（BE-0287）が乗り越えなければなりません。`SIGKILL` では、無関係なタイムアウトではなく実行中のランナー障害を名指しするクラッシュ診断で読み取りが終わらなければなりません。各ケースは、検証対象の層に到達したというチャネル自身のログ記録を合図に障害を解除し、推測した待ち時間には頼りません。**visual (xcuitest)** は Stable カタログをコミット済みの `baselines_ios/` ベースラインとピクセル比較します（`make -C demos/showcase e2e-visual`）。ステータスバーと「Liquid Glass」タブバーはマスクします。`e2e-visual` は自前でアプリをビルドし直すため、`codegen` と同様に `build` アーティファクトの consumer にはなりません。**network (xcuitest)** は、iOS の transport の上でネットワークの実経路を動かします（`make -C demos/showcase e2e-network`。BE-0282）。`web-e2e.yml` の `network (playwright)` と `android-e2e.yml` の `network (adb)` にあたる iOS 版です。BajutsuKit のアプリ内 `URLProtocol` が、モックされた `POST /post` に 201 で応答し（実サーバなら 200 を返します）、各 exchange をループバック上の collector へ報告します。続く 2 本目のシナリオは、スタブを介さないカタログの `GET` を発行し、観測されたという事実だけをアサートします。そのうえで `demos/showcase/network/assert_network_evidence.py` が、永続化された `network.json` をアサートします。モックされた exchange の `Authorization` ヘッダと `password` のフィールドがマスクされていること、生の秘密情報がファイルのどこにも残っていないこと、そしてスタブを介さない exchange が `mocked` を false として記録されていること、つまりマッチ範囲の広すぎるモックが、何もスタブしていない通信を自分のものだと主張できないことです。このうちマスクの確認が、BE-0282 が埋めようとしたギャップです。純粋な redaction のテストはすべて手で組み立てた exchange をアルゴリズムに渡すので、実際にキャプチャした資格情報が出荷される証跡でマスクされるかは、iOS 上ではここ以外で観測できません。このジョブは必要なものを自前でビルドする `make` ターゲットを動かすので、`codegen` や `visual` と同様に `build` アーティファクトの consumer にはなりません。このレーンには、デバイスを 2 台使う **pool (xcuitest)** ジョブ（BE-0298）がありましたが、取り下げました。2 台目の Simulator を起動し、1 回の `bajutsu run --workers 2` に対してデバイスプールの分離の主張を検証するジョブでしたが、その判定を返せたのは 5 回のうち 1 回だけです。残る 4 回は、プールの検査ではなくホストで崩れました（`SimRenderServer` が自身のディスパッチキューでクラッシュする、固まった CoreSimulator に対して `simctl uninstall` がタイムアウトする、runner のチャネルが run の途中で到達不能になる）。赤くなるたびに 10 倍課金のランナーを 30 分ほど使い、動画記録とタッチマーカーとシナリオの半分を削っても、崩れる位置が早まっただけでした。実際の並行デバイスに対するこの主張の検証は、いま Android レーンの `pool (adb)` だけが担い、このレーンでは高速スイートの記帳の証明（`tests/runner/test_pool.py`）だけが残ります。共有の `setup-ios-toolchain` コンポジットアクションが、macOS の各ジョブが繰り返す Xcode / uv / xcodegen / シミュレータ起動の手順をまとめています（`build` ジョブはシミュレータ不要なので `boot: false` を渡します）。10 個のジョブはすべて同じ `changes` 検出ジョブでパスゲートされ、そのうち `build`、`run`、`codegen`、`bundled-runner`、`conformance` は、常に結果を報告する単一の `E2E (iOS)` ジョブ（必須チェック）に集約されます。`actuation`、`golden`、`visual`、`network` も同じ検出ジョブでパスゲートされますが、`E2E (iOS)` の `needs:` には意図的に含めていません。新しく配線した XCUITest のアクチュエーションは、まずは参考シグナルとして着地させ（Simulator レーンにはフレーキーの実績があるため、BE-0218）、安定を確認してからゲートに昇格させます。要素ツリーの `golden` は決定論的で実行環境に依存しないため、そのドリフトを参考シグナルとして出し、`visual` のピクセルのベースラインはホスト依存（Simulator のレンダラが Xcode / デバイス / OS で変わります）だからです。`fault-injection` は BE-0282 と同じ道筋をたどりました。まずシグナルとして着地し、レーンを赤くしていた runner の HTTP サーバの欠陥が直ったあと、失敗のない 64 回の実行を根拠に `needs:` へ入りました（BE-0305）。`network` も実機の新しいカバレッジなので、同じくまずシグナルとして着地させます。その web 版が `E2E (web)` に加わる前にたどったのと同じ道筋です。いずれもドリフトやフレーキーを PR のブロックではなく単独ジョブのシグナルとして出します（`visual` が採取したスクリーンショットは `ios-e2e-visual-run` としてアップロードし、ベースラインの再採取に使います） |
| [`android-e2e.yml`](../../.github/workflows/android-e2e.yml) | Linux | 手動 + 全 PR + マージキュー（必須の `E2E (android)` チェック） | **Android（adb）バックエンド**のレーン（BE-0208）で、iOS や web のレーンのジョブ分割にならった観点ごとの 8 ジョブから成り、各ジョブが自前の x86_64 API 34 AVD を KVM のもとで起動します（`reactivecircus/android-emulator-runner`）。これに加えて、AVD を起動しない九つ目の **warm gradle cache (adb)** ジョブが、`smoke`/`golden`/`conformance`/`fault-injection`/`pool` がそろってビルドする Compose と resident server の APK を先に一括でビルドします。この五つのジョブは、その分だけ依存解決とコンパイルを五回コールドでやり直さずに済み、温めておいた Gradle の依存キャッシュとビルドキャッシュを復元してビルドを走らせます。Views はここではビルドしません。Views を必要とするのは `smoke` だけで、ここで温めると残り四つのジョブが（ジョブレベルの `needs:` のため）使いもしない Views のコンパイルを待つことになるからです。`smoke` 自身の Views ビルドは、このジョブができる前と同じく温めないままです。`network`/`visual`/`codegen` は意図してこの `needs:` から外しています（`codegen` はさらに Compose の AndroidTest（計装）バリアントを必要としますが、warm gradle cache (adb) はこれをビルドしません）。ジョブレベルの `needs:` は、Gradle と無関係な AVD キャッシュのステップまで巻き込んでビルドが終わるまでその軽いジョブ自身を丸ごと止めてしまうため、キャッシュヒットで得られる以上の待ち時間を強いることになるからです。同一ラン内で並行実行するジョブどうしは、保存前のキャッシュを `actions/cache` で共有できません。そのため warm gradle cache (adb) を先に走らせ、`smoke`/`golden`/`conformance`/`fault-injection`/`pool` の五つが `needs: warm-gradle-cache` を宣言します。`cache-read-only` はどのジョブも明示的には渡さず、composite action 側の安全なデフォルト（`true`、読み取り専用）に任せています。`gradle/actions` が文書化しているマルチジョブでのキャッシュ共有パターンです。warm gradle cache (adb) が外れる、あるいは失敗しても失うのは時間だけで、正しさは変わりません。そのため下流の各ジョブの `if:` は `always()` ではなく `!cancelled()` を使います。`always()` では、android-e2e.yml 自身の `concurrency: cancel-in-progress` で打ち切られたはずの実行でもジョブが動き続けてしまうためです。`!cancelled()` は、warm gradle cache (adb) の成功に対する `needs` の暗黙の依存を明示的な許容条件で吸収しつつ、実行そのものが打ち切られた場合はそこで止まります。`E2E (android)` の `needs:` にも加えていません。**smoke (adb)** は Compose と Views の showcase APK をビルドし、Stable タブのシナリオ（コアの id/tap/type/value のフローに、詳細画面への push と pop で戻る back ナビゲーションを加えたもの）を `--backend android` で実行します（`make -C demos/showcase/android e2e`）。**golden (adb)** は Compose の Stable カタログの golden 要素ツリーを実機でチェックし（`make -C demos/showcase/android e2e-golden`、BE-0006 / BE-0208 ユニット 4）、続いて resident チャネルを切って再実行して（`make -C demos/showcase/android e2e-fallback`、BE-0245）両方の読み取り経路が一致することを確かめます。**network (adb)** は BE-0283 のネットワークキャプチャの検証で、`request` ステップが、BajutsuAndroid のインターセプタが `adb reverse` の collector ブリッジ越しに報告する実際のエミュレータ通信を観測します。**conformance (adb)** はドライバ conformance スイート（BE-0114）を実 adb バックエンドに対して実行します。`ios-e2e.yml` の `conformance (xcuitest)` の Android 版です。**fault-injection (adb)** はディスプレイをスリープさせて、実際の読み取り元が本当に空の要素ツリーを返す状態を作り、`CoordinateTreeDriver` の transient-empty リトライが、高速スイートのテストでは組み立てるしかない実際の条件を乗り越えることと、リトライ予算を超えて空が続いた場合に明確に失敗することを検証します（`make -C demos/showcase/android e2e-fault-injection`、BE-0305）。**visual (adb)** はピクセル VRT を実行します（後述）。**pool (adb)** は、このレーンで唯一エミュレータを 2 台使うジョブです（BE-0298）。キャッシュした AVD の 2 台目のインスタンスを `-read-only` で、emulator-runner アクション自身のステップの内側から起動します（`scripts/android_pool_e2e.sh`。このアクションにはエミュレータを 2 台使うモードがなく、アクションが起動するエミュレータはそのステップのあいだしか生きていないためです）。状態に依存しないシナリオ 4 本を 1 回の `bajutsu run --workers 2` で走らせ（`make -C demos/showcase/android e2e-pool`）、分離の主張を `scripts/assert_pool_isolation.py` で検証します。2 台とも `-memory 3072 -cores 1` と `-read-only` で動かします。`-read-only` は、2 つのエミュレータプロセスが 1 つの AVD を共有できるようにするフラグで、ジョブ自身のエミュレータにも渡します。AVD を読み書きで掴んだインスタンスは AVD をロックし、2 台目をそのまま拒否するからです。`-read-only` はスナップショットの読み込みも無効にするので、このジョブはこのレーンで唯一、常に 2 台ともコールドブートします。自前の AVD キャッシュエントリが買うのは、resume ではなく AVD の作成です。変更フィルタも、レーン全体のシグナルではなく、より狭い `pool` 出力を使います。**Mac / Simulator 不要**で、iOS と web の e2e レーンに並ぶ 3 つ目のバックエンドの Linux 版です。`changes` 検出ジョブ（`scripts/e2e_changes.py`、`E2E_LANE=android`）でパスゲートし、必須の集約ジョブ `E2E (android)` に集約します（BE-0279）。AVD は（ローカル検証の arm64 ではなく）x86_64 にして、x86_64 ランナー上で KVM が加速できるようにしています。golden のベースラインは arm64 で採取していますが、比較がフィールド単位で frame は健全性チェックだけのため、x86_64 でも通ります。sheet/cover のフロー（`components`、`modals`）も、このレーンに限って条件待ちの上限を引き上げることで含めています。`make -C demos/showcase/android e2e` が `BAJUTSU_MIN_WAIT_TIMEOUT`（既定 15 秒）を各待ちのタイムアウトの下限として渡すためです。ソフトウェアレンダリングのエミュレータは、共有シナリオの 5 秒の待ちに収まらないほど遅くモーダルを描画します。条件待ちは条件が満たされた瞬間に返るので、上限を広げても固定の待ち時間にはならず安全な上限にとどまり、共有シナリオには手を入れません（`timeout: 5` はどのバックエンドでも同じです）。深いスクロールのフロー（`controls`、`notices`）もこのレーンに加えました。`controls` はボタン群の下にある segmented control の値ノードを、`notices` は一覧のずっと下のほうにある行を、それぞれ `scroll`（BE-0326）で画面内に入れます。`scroll` は非慣性でツリーを再クエリするステップで、対象の中心が画面内に入った時点で止まります。固定距離のスワイプ連鎖は、密度の高い Android の画面（2400px）では iOS（約 900pt）に比べて画面のごく一部しか進まず、バックエンドごとの距離調整が要りましたが、`scroll` の再クエリはその調整を不要にします（BE-0208 ユニット 5）。`system` と `modals` も、この変更で同じように `scroll` で遠くの対象を画面内に入れます（Permissions タブの `sys.paste.value`、Log タブの `log.dialog.value`）。これでこのレーンの 4 つのフローが `scroll` に依存します。単一タッチのジェスチャのフロー（`gestures`）もこのレーンに加えました。adb ドライバが、root 化したエミュレータでは生の `sendevent` によるタッチ列でダブルタップを実行するようになったためです（`e2e` ターゲットが先に `adb root` を実行します）。2 回のタップが 1 回の `adb shell` のなかで発火するので、タップごとに `input` の JVM を起動していたときには超過していたプラットフォームのダブルタップの受付時間に収まります。root 化していないデバイスでは、従来どおり `input tap` にフォールバックします。対象を画面内に入れるためのスワイプは、ユニット 5 のもう一方の仕組みに支えられています。既定の方向スワイプは画面サイズに対する一定の割合（`_SWIPE_FRACTION`、`bajutsu/common/orchestrator/actions/handlers/gestures.py`）だけ進むので、密度の高い Android の画面（2400px）でも iOS の画面（約 900pt）でも同じ割合をカバーします。固定距離では、そうはいきませんでした（BE-0208 ユニット 5）。マルチタッチのジェスチャのフロー（`gestures_multitouch`）もこのレーンに加えました（BE-0232）。adb ドライバが、root 化したエミュレータでピンチ / 回転を生の 2 スロットの `sendevent` スイープ（2 つの接点が複数フレームにわたって一緒に動きます）として実行するので、iOS が XCUITest で動かす共有シナリオが Android でもそのまま動きます。単一タッチのダブルタップと違って `input` へのフォールバックはなく（2 本指のジェスチャは近似できません）、root を要し、なければ明確に失敗します。ランタイム権限のフロー（`permission`）もこのレーンに加えました（BE-0208 ユニット 6。BE-0210 の事前付与を検証します）。これは iOS レーンが走らせるのと同じ `permission.yaml` です。付与の仕組みはシナリオではなく config にあるので、1 つのファイルで両方をまかなえます。`showcase-compose` が `POST_NOTIFICATIONS` を事前に付与するため（`grantPermissions` により lease 時に `pm grant` を実行します）、Android の `RequestPermission` コントラクトはダイアログを出さずに付与済みとして即座に返り、シナリオの `systemAlertHandling` ガードはここでは発火しません。よってこのレーンでもフローは決定的なまま（LLM も固定の待ち時間もなし）に保たれます（通知を事前付与できない iOS では、このガードが代わりに「Allow」をタップします）。デバイス制御のフロー（`device`）もこのレーンに加えました（BE-0208 ユニット 5）。GPS の位置を上書きし（`emu geo fix`）、クリップボードを書き込んで読み戻し、落ち着いた画面を再度確認します。これは iOS レーンが走らせるのと同じ `device.yaml` です。`setLocation` もクリップボードも両プラットフォームで宣言されるので、一つのファイルが iOS でも Android でも動きます（iOS 専用の `push` は `push.yaml` に分けました）。Stable の起動タブ上で、デバイス制御ファミリのうち `setLocation` と `clipboard` の両方を動かしています。`cmd clipboard` は実機では黙って何もせず、Android 10 以降はフォアグラウンドのアプリしかクリップボードを触れないため、クリップボードは showcase が `BajutsuAndroid` から組み込むアプリ内レシーバを経由します（BE-0233）。この書き込みと読み戻しは、強い assertion です。割り込みハンドラのフロー（`interrupts`）もこのレーンに加えました（BE-0314）。これは実際のアプリでは発火しない構文デモで、`interrupts` エントリの `condition` は画面上の何にも一致しないため、その裏で走るのは `firstlook` と同じ Stable→Horse→お気に入りのフローです。アプリや config に追加の変更は要りません。`firstlook` に対してこのフローが加えるのは、生きたツリーに対する BE-0314 の確認経路です。`wait` は自分のポーリング用ツリーに相乗りするので追加のコストはかかりませんが、素の act ステップは（このシナリオが `screenChanged` ポリシーを宣言していないため）ガード用のクエリを 1 回追加で払います。こうして、一度も発火しないハンドラが実機で何も乱さないこと、act 1 回につき読み取り 1 回分で収まることを、高速スイートの fake では示せない形で証明します。Views 版のレーンにも加えました。id はドット区切りの SPEC 形式とアンダースコア区切りの Android Views 形式の両方を持ち（BE-0221）、`firstlook` がすでに Views ツリーで実証済みの同じ id なので、そのまま動きます。`visual (adb)` ジョブは Compose の Stable カタログのピクセル視覚回帰チェックを実行します（`make -C demos/showcase/android e2e-visual`、BE-0208 ユニット 4）。要素ツリーの golden とは異なり、ピクセルのベースラインはホスト依存です。x86_64 のソフトウェアレンダラ（swiftshader）とローカルの arm64 エミュレータはピクセル単位で食い違うため、このベースラインは arm64 ではなくこの x86_64 レーンで採取してコミットします（`demos/showcase/scenarios/visual/baselines_android/`）。上部のステータスバーはマスクするので、時計が比較を揺らすことはありません。`uiautomator (codegen)` ジョブは codegen の出力経路です（`make -C demos/showcase/android e2e-codegen`、BE-0294）。`ios-e2e.yml` の `codegen (xcuitest)` の Android 版で、`codegen_android.yaml` からネイティブ UI Automator（Kotlin）テストを再生成し、Gradle の `connectedAndroidTest` が Compose の a11y アプリと計装 APK をビルドして両方をインストールし、生成テストをエミュレータに対して実行します（テスト時に bajutsu / adb ドライバ / AI は不要です）。ビルド前に再生成するので、古いチェックインがエミッタや `androidx.test.uiautomator` API のドリフトを覆い隠すことはありません。決定論的で実行環境に依存しないジョブ、すなわち `smoke (adb)`、`conformance (adb)`、`network (adb)`、`fault-injection (adb)` を、常に結果を報告する集約ジョブ `E2E (android)`（必須チェック、BE-0279）に集約します。`golden (adb)`、`visual (adb)`、`uiautomator (codegen)`、`pool (adb)` はその `needs:` から意図的に外し、参考シグナルにとどめます（要素ツリーの golden は上流依存の変化で赤くなりえ、ピクセルのベースラインはホスト依存で、codegen とエミュレータ 2 台のレーンは BE-0282 の前例にならいまずシグナルとして着地させるためです）。`fault-injection (adb)` は BE-0282 の道筋を最後まで歩みました。73 回の実行で失敗は 1 回、しかもそれはレーンが検証している内容ではなくエミュレータが起動しなかったことによるもので、同じ期間の `conformance (adb)` は 67 回中 4 回失敗しています。これがシグナルの地位から集約ジョブの `needs:` へ押し上げ、リトライの実際の検出に入った回帰は、マージが無視できる赤ではなくマージを止める赤になりました（BE-0305）。`tests/test_e2e_gate_needs.py` は高速スイートで、各集約ジョブの構成と、ゲートが `needs:` に挙げたジョブはそれぞれの結果が読まれるという不変条件を固定し、さらに verdict のスクリプト自体を実行して、それぞれの結果が本当にチェックを赤にすることも固定します。依存の失敗が、集約ジョブが green と数えるパススキップとして通ってはならないからです。これは iOS の `E2E (iOS)` が引くのと同じ判断基準です |
| [`devicefarm.yml`](../../.github/workflows/devicefarm.yml) | Linux | **手動のみ**（`workflow_dispatch`） | **AWS Device Farm へのバッチ投入**（BE-0235）。showcase の Compose APK をビルドし、Bajutsu と config とシナリオをパッケージ化して [`scripts/devicefarm_submit.py`](../../scripts/devicefarm_submit.py) に渡します。このスクリプトが、`bajutsu run --backend adb` を Device Farm のホストで実行するカスタム環境のテスト仕様をアップロードし、実行をポーリングし、成果物をダウンロードして、**Bajutsu 自身の manifest 判定**（Device Farm の分類ではありません）を表示します。決定的なコアの外側の CI 側のグルーなので、判定に LLM は触れません。起動は `workflow_dispatch` のみで（push / PR では動かず、必須チェックにもなりません）、認証は GitHub OIDC から発行する短命の AWS 認証情報（`AWS_DEVICEFARM_ROLE_ARN`）を `devicefarm` の Environment にスコープし、プロジェクトとデバイスプールの ARN はリポジトリ変数に置きます。いずれかが未設定ならジョブは緑の no-op になり、運用者がアカウントを接続するまで休止します。実アカウントでのシリアル解決の実証は文書化した手動手順（[AWS Device Farm](devicefarm.md) を参照）とし、決定的ゲートからは意図的に外しています |
| [`ai-smoke.yml`](../../.github/workflows/ai-smoke.yml) | Linux | **手動のみ**（`workflow_dispatch`） | **実モデル**のレーンで、ジョブは2つ、どちらも**ゲート対象外**のシグナルです（BE-0282 の前例）。**smoke (direct Anthropic API)**（BE-0300）は、Bajutsu 自身のアダプタコード（`bajutsu.ai.anthropic.AnthropicBackend`）を通して実プロバイダを些細な強制ツールのプロンプトで呼び出し、返り値がベンダー中立な `MessageResponse` として空でなくパースできることだけを検証します（`pytest tests/test_ai_backend_live_smoke.py -m live -n0`）。ほかのアダプタテストはいずれも手書きの `FakeAnthropic` を駆動するので、実 API の実際の形を再観測するものはこれ以外にありません。トランスポートとスキーマの検証であって、モデルの品質検証ではありません。**accuracy (system-alert guard)**（BE-0308）は、実モデルに対するもう一方の問いを立てます。トランスポートが往復するかではなく、返ってきた答えが正しいかどうかです。ショーケースアプリから捕捉した本物の iOS システムダイアログを視覚アラートロケータ（`bajutsu.agents.alerts.ClaudeAlertLocator`）に見せ、返ってきた座標が正しい dismiss コントロールの frame 内に収まることを検証します（`pytest tests/test_real_model_alerts.py -m live -n0`）。これがこのガード自身の安全性の主張です。ほかのガードのテストはいずれも、テスト作者が打ち込んだ `AlertDecision` を渡すだけなので、実モデルに本物のアラートを見せて判断させるテストは1つもありません。ダイアログは `tests/fixtures/be0308/` にコミット済みの捕捉物です。通知プロンプト、位置情報プロンプト、iOS のプロセス間ペースト同意、そしてショーケースアプリ自身の削除確認の4つで、最後のものはガードがその破壊的なボタンに手を伸ばしてはならないケースです。Simulator は要らないので、ライブなのはモデル呼び出しだけであり、各捕捉物の整合性を確かめる決定的な検証は認証情報なしで毎回のゲートを走ります。どちらのジョブも `run` / CI の判定に LLM を置きません（prime directive 1）。いずれも AI の authoring 周縁だけを動かします。`live` マーカーが両方のスイートを高速ゲートから外します（pyproject の `addopts` の `not live`）。起動はどちらも `workflow_dispatch` のみで、push / PR では動かないため、フォークからの実行が認証情報を目にすることはありません（`devicefarm.yml` が引く境界と同じです）。認証は `ai-smoke` の Environment にスコープした `ANTHROPIC_API_KEY` のリポジトリ secret です。未設定ならテストが自身をスキップし、どちらのジョブも緑の no-op になるので、運用者が secret を接続するまでレーンは休止します。配線したのは直接 Anthropic API アダプタのみです。Bedrock は稼働中の AWS ロールを、`ant` はサインイン済みの OAuth CLI シートを要し、いずれも現実的には CI の secret にできないためで、それらの `-m live` テストはローカルや手動でなら実行できます |

### マージを止める E2E チェックはどれか（BE-0279）

各バックエンドのレーン、すなわち iOS（`E2E (iOS)`）、Android（`E2E (android)`）、web（`E2E (web)`）は、
常に結果を報告する集約ジョブを 1 つずつ持ち、それがそのレーンの必須チェックになります。バックエンドごとに
集約ジョブを分けることで切り分けが保たれ、赤いチェックが壊れたバックエンドを名指しします。**あるチェックが
マージを止めるのは、それが決定論的で実行環境に依存しない場合に限ります。** 結果が実行環境や上流依存に左右される
チェックは参考シグナルにとどめます。実行はされ、ドリフトを自分のジョブ上に出しますが、マージは止めません。

- **ピクセル視覚回帰（VRT）**、すなわち `visual` ジョブ。ピクセルのベースラインはホスト依存で（Simulator や
  エミュレータのレンダラは OS・デバイス・ツールチェインで変わります）、そのドリフトは Bajutsu 側の変更と
  無関係です。集約ジョブの `needs:` から外します。
- **要素ツリーの golden**、すなわち iOS / Android の `golden` ジョブ。決定論的ですが、Android ではツリーを
  上流の実機側サーバを通して読み取るため、その変化が Bajutsu 側の変更と無関係に赤くしえます。そのため
  golden のドリフトは、マージのブロックではなく PR ごとの参考シグナルとして出すのが適切です。集約ジョブの `needs:` から外します。

必須チェックが `paths:` フィルタで飛ばされると、いつまでも保留のままマージを止めてしまいます。そのため、
どのレーンもトリガでのパスゲートはしません。各レーンは全 PR（とマージキュー）で走り、代わりに `changes`
ジョブが重いジョブをパスゲートします。`changes` ジョブは [`scripts/e2e_changes.py`](../../scripts/e2e_changes.py)
を `E2E_LANE=ios|android|web` で実行します（レーンごとの関連度フィルタで、`tests/test_e2e_changes.py`
で単体テストしています）。集約ジョブは `if: always()` で走るので、パスによる省略は合格として報告され、無関係な
PR は走りもブロックもされません。新しい必須の集約ジョブを `main` のブランチ保護の規則に登録するのはリポジトリ外の
管理作業で、正確なチェック名を用いて管理者が行います。

このフィルタは `bajutsu/` の既定値を反転しています（BE-0333）。ファイルが「見てもらう」ために加わる必要のある
手書きのポジティブリスト（未掲載のものは黙って何も発火しなかった）ではなく、共有のコアがパッケージ *全体* を
一括して拾い、明示的に分類したものだけを差し引きます。すなわち、どのレーンも実行しない周辺（`_PERIPHERY_EXCLUSIONS`。
各項目が除外の理由を持ちます — analytics / analysis スタック、MCP サーバ、AI アダプタ、GitHub と cloud の連携、
そして混在パッケージ `agents` / `crawl` / `cli.commands` の個々の周辺モジュール）と、各レーンが再主張する
backend 固有のリーフ（`_LANE_CLAIMED`）です。どちらにも名指しされないファイルは一括で拾われ、誰かが分類するまで
全 3 レーンを発火させます — 無駄なジョブという安全側で、ポジティブリストが生んでいた黙った過小発火の代わりです。
この既定値が、ミスの一クラスをまとめて退治します。トップレベルのモジュールがパッケージへ分割されたケース
（`bajutsu/config` と `bajutsu/platform_lifecycle` はどちらもこのずれを起こし、レーンがまさに動かすためにある
コードを変更してもレーン全体が飛ばされていました）、新しいトップレベルモジュールや CLI コマンド、そして run パスが
import しているのにリストが一度も名指ししなかったファイル — 毎 run が書き込むマニフェスト書き出し `bajutsu/common/report/`
がその筆頭です — が、パッケージとモジュールが一括の拾い上げで等しく照合されるようになったため、いずれも発火します。

分類の健全性は、いくつかのテストが保ちます。`test_run_path_closure_is_gated_or_excluded` は run パスの静的な `ast`
import 閉包を辿り、到達したファイルがゲート対象でも分類済み周辺項目でもなければ落とします。run パスが *新たに*
import し始めた未分類のモジュールが、数か月後に「謎めいた緑の必須チェック」として表面化するのではなく、`make check`
で落ちるようにするためです。`test_periphery_exclusion_paths_exist` と `test_every_plain_literal_path_in_the_filter_exists`
は、各除外項目と各素のパスをツリーへ解決するので、リネームや削除は、パターンが何も照合しないまま残る代わりに、
ゲートの失敗として現れます。さらに共有のコアは、バックエンドごとの2つのディレクトリ（`bajutsu/common/drivers/`、
`bajutsu/platform_lifecycle/environments/`）を、各レーンが名指しするリーフを除いて一括して拾います。そして、
どちらの配下にも「1つもレーンを発火させないファイル」がないことを確認するテストと、各 `_LANE_CLAIMED` のリーフが
少なくとも1つのレーンに再主張されることを確認するテストがあります。よって、新しく足したバックエンドのモジュールは
見落とされるのではなく過剰に発火し、除外だけされて誰も主張しないリーフはゲートを落とします。反転した既定値の
過剰発火のコストは、出荷前に計測しました。直近 80 件のマージ済みプルリクエストでは、旧来のポジティブリストと
3 レーンすべてで同一に発火しました（`scripts/e2e_overfire_report.py`）。

`changes` ジョブは、安全だと証明できる唯一の場合について、もう一段だけ絞り込みます（BE-0322）。変更がレーンの
シナリオファイルだけに閉じているときは、レーン全体ではなく、変更されたシナリオを宣言しているジョブだけを発火させ
ます。フィルタは各 iOS ジョブがワークフローで宣言している `scenarios:` を読みます。加えて `codegen` ジョブと `visual`
ジョブについては、シナリオをワークフロー入力ではなく Makefile ターゲットで名指ししているため、`demos/showcase/Makefile`
のターゲットが走らせるシナリオを読みます（BE-0338）。こうして、変更されたシナリオからそれを読み込むジョブへの対応が、
ジョブの実際の実行内容からずれることはありません。そして `relevant` に加えて、各ジョブが条件に使う `shared` フラグと
`affected` ジョブ配列を出力します。テストが、Makefile から読んだ対応づけをそのターゲットに固定するので、ターゲットが
シナリオを増減させると、対応づけも一緒に動かない限りゲートが落ちます。それ以外の場合はすべて、従来どおりレーン全体を
発火させます。共有コードの変更（任意のシナリオに影響しうるドライバ / ランナー / アプリ / ワークフローのコード）、シナリオ
の部分集合を宣言しない次元ジョブ `conformance`（ドライバ適合性ハーネス全体を駆動します）、そしてジョブがシナリオで
キーづけられていないレーン（Android と web）です。この判断はレーン全体の側に倒すので、変更が壊しえたジョブを取りこぼす
ことはありません。そして `git` の差分、ワークフロー自身の宣言、そしてその Makefile ターゲットだけを読むので、経路に
LLM は入らず、実行の合否判定にも一切影響しません。

dev ツールは `dev` 依存グループにあるため、Linux ジョブは `uv sync --group dev` → `uv run
--no-sync …` で実行します（素の `uv run` はデフォルト集合に再同期して落としてしまいます）。
このゲートは [`make check`](../../Makefile) と [`pre-push`](../../.githooks/pre-push) フックを
段ごとにミラーしており、`actionlint` と `gitleaks`（どちらも CI が個別に導入するツールで、手元に
ないときは各段が通知を出して飛ばします）以外は新規クローンでも `uv` だけで同一に走ります。これが
「ローカルで緑」＝「CI で緑」を担保します。スキルのずれの検査は例外ではなく `uv` 側です。`apm-cli` は
`dev` の依存なので（BE-0390）、`make lint-skills` にスキップの分岐はありません。

### すべてのジョブが宣言するタイムアウト

[`.github/workflows/`](../../.github/workflows/) のジョブは、すべて `timeout-minutes` を設定します。
GitHub が代わりに当てるデフォルトは 360 分だからです。そのまま止まったジョブは、赤いチェックが出るまで
ランナーを占有し、Actions の実行時間を消費し続けます。実際に web レーンでそれが起きました。
`conformance (playwright)` と `codegen (playwright)` は、成功するときには 2 分から 7 分で終わります。
それにもかかわらず、どちらも 4 時間近くでキャンセルされました。

上限がそのジョブ自身の見積もりから決まる場合は、独自の値を設定し、根拠を隣のコメントに書きます。
AWS Device Farm への投入は 180 分、iOS のシナリオジョブは 60 分、Android の `smoke` は 30 分、
Android のその他のシナリオジョブは 25 分、`claude-review` は 20 分、Android の Gradle キャッシュを
温めるジョブと `ai-smoke` の2つの実モデルジョブはそれぞれ 15 分、`mcp-wire` は 10 分です。そうした
根拠を持たないジョブは、リポジトリのデフォルトである **30 分**を取ります。計測できた
成功ジョブのうちもっとも長いのは CodeQL の Swift 解析の約 20 分なので、30 分なら余裕をもって収まります。
それでいて、止まったジョブは 30 分以内に明確な失敗として現れます。

`timeout-minutes` を持たないジョブが 2 つだけあります。[`docs-refresh.yml`](../../.github/workflows/docs-refresh.yml)
と [`roadmap-refresh.yml`](../../.github/workflows/roadmap-refresh.yml) にある、再利用可能ワークフロー
[`refresh.yml`](../../.github/workflows/refresh.yml) を呼び出すジョブです。呼び出し側のジョブに
`timeout-minutes` を書くと、GitHub が受け付けないためです。代わりに `refresh.yml` 自身のジョブが持つ
30 分の上限が、両方の呼び出し側を覆います。

### E2E ジョブが失敗したときに集まる証跡（BE-0361、BE-0367）

E2E でもっとも厄介な失敗は、何もクラッシュしない失敗です。iOS では常駐 XCUITest ランナーが
`Timed out while requesting screenshot` を報告し、テストメソッドを終えます。これを受けて Python 側が
実行中クラッシュを宣言します。常駐 XCUITest ランナーとは、`bajutsu` が `xcodebuild test-without-building` で起動する
XCTest ホストのことです。`bajutsu` はこのホストを、ループバックの Hypertext Transfer
Protocol（HTTP）チャネルで駆動します。Android の失敗はもう少し多様です。adb バックエンドが要素
ツリーを読む常駐 UI Automator サーバが応答しなくなる、エミュレータ自身の描画が固まる、ジョブが
`timeout-minutes` を使い切る、のいずれかです。どの場合もプロセスは1つも死んでいないので、クラッシュレポートの走査は何も見つけません。そして、デバイスの描画サービスが固まったのか、ホストがデバイスから資源を奪ったのかを
言い当てる状態は、人がジョブを開く頃には消えています。

両レーンとも、この問いに同じ形で答えます。何を見られるかによって3層に分かれます。第1層は `bajutsu`
の内側です。ストールがいつ起きたかを知っているのは、実行中のプロセスだけだからです。第2層は CI から
見たデバイスです。デバイスを保持しているものにしか読めません。第3層は時系列で見たホストです。ジョブと
並んで走るサンプラにしか記録できません。成果物は1つを除いてすべて `runs/diagnostics/` の下に置かれます。
この場所は各ジョブがすでにアップロードしています。例外は後述するシナリオ単位のクラッシュ証跡で、
失敗したシナリオ自身の証跡ディレクトリに置かれます。ジョブ全体ではなく、その1つのシナリオに属する
証跡だからです。集めたものが判定に入ることはありません。収集はファイルを書くだけで、合否は従来どおり
決定的なアサーションが決めます。

第1層は、両バックエンドとも1つの環境変数で有効になります。`BAJUTSU_STALL_DIAGNOSTICS` は捕捉の
書き込み先ディレクトリを指し、バックエンドの接頭辞をあえて持ちません。操作者が1つ設定すれば、ジョブが
どのバックエンドを駆動していてもフックが有効になります。一方、捕捉が何を**読む**かはバックエンドごとです。
両者が共有するコマンドは1つもないからです。Simulator は `simctl` に、macOS ホストは `vm_stat` に
答え、エミュレータは `adb` に、Linux ホストは `top` に答えます。そこで各バックエンドは自分のプローブ
一式だけを持ち寄り、共通の捕捉がそれ以外を引き受けます。オプトインの門、1回の捕捉あたりの実時間の
予算、トリガーごとに2回という回数、そして各プローブの結果を書き出すサマリです。上限をトリガーごとに
分けているのは、頻発する1つのトリガーが、説明したい対象であるクラッシュの届く前に共有の上限を
使い切ってしまわないようにするためです。頻発していたのは映像のトリガーでした。発火のもとになる確認
処理が誤った信号を見ていたため、このランナーでは緑の実行を含む全シナリオで発火していました。いまは
そうなりませんが、クラッシュを繰り返す実行では依然として複数のトリガーが競合するので、分けたままに
しています。この2レーンの外では未設定で、
未設定ならフックは何もしません。

#### iOS レーン

`codegen` を除き、Simulator を起動する iOS ジョブはすべて3層を `runs/` 配下へ集めます。`codegen` が
対象外なのは、このジョブが `.xcresult` だけをアップロードし、収集物を載せる `runs/` の成果物を
持たないからです。

- **`bajutsu` の内側。** `BAJUTSU_XCUITEST_RESULT_BUNDLES` は、ランナーの起動ごとに
  `-resultBundlePath` を与えます。こうして `runs/runner-logs/result-<udid>-<port>.xcresult` が、
  testmanagerd 自身の観測、すなわち正確な XCTest の失敗とそのタイムスタンプを記録します。捕捉した
  標準出力が伝えるのは、その言い換えにすぎません。同じ引数列で `-collect-test-diagnostics never` も
  固定します。既定の `on-failure` のままだと、`xcodebuild` は Simulator の `system.logarchive` を
  result bundle へ埋め込み、1回の起動ぶんで 163 MB を計測しました。これは以下の狙いを絞った抽出が
  置き換えるはずの、丸ごと収集そのものです。ストール時の捕捉は、チャネルが実行中クラッシュを宣言した
  瞬間と、`recordVideo` が1バイトも生まなかった瞬間に発火します。捕捉は所要時間を計測した `simctl` の
  スクリーンショット、描画を担うプロセスへの `sample`、`ps` と `vm_stat` のスナップショットを
  `runs/diagnostics/stalls/stall-NN-<理由>-<pid>/` へ書きます。
- **`bajutsu` の内側、シナリオ単位、オプトインなし**（BE-0421）。クラッシュリカバリのリトライを
  すべて使い切って失敗したシナリオには、そのシナリオ自身の証跡ディレクトリ `runs/<run_id>/<sid>/` の下に
  `crash-diagnostics/` ディレクトリが作られます。中身は、クラッシュしたランナーが捕捉した出力の末尾
  （クラッシュ警告が折り込む20行よりはるかに多く、それでも上限つきです）と、障害を起こした
  `xcodebuild` プロセスについて macOS がレポートを書いていた場合は、その `.ips` クラッシュレポートです。
  シナリオの失敗文字列がこのディレクトリを名指しするので、存在を先に知っている必要はありません。これは
  次に挙げる走査のローカル実行版であり、シナリオ単位版でもあります。走査のほうはジョブ全体のレポート
  置き場をまとめてアーカイブにするため、複数のシナリオが走ったジョブでは、どのクラッシュがどのシナリオの
  ものかを言えません。しかも CI でしか走らないので、ローカルの `bajutsu run` でクラッシュを再現しようと
  する開発者は、そこから何も得られませんでした。どちらの証跡も best-effort です。`ReportCrash` が
  書かなかったレポートや、そもそも `DiagnosticReports` ディレクトリを持たないホストでは、その項目が
  欠けるだけで、何も失敗しません。ランナーの出力を集めるのは、ランナー自身の生存信号が名指しする2つの
  クラッシュ形、すなわち `xcodebuild` が終了した場合と、捕捉した出力が終了を示しているのにプロセスが
  居残っている場合です。プロセスが動き続けてそうしたマーカーも書かないまま、チャネルだけが応答しなくなる
  形は、BE-0354 がデバイス差し替えへ引き上げる Simulator の固着です。この形ではこのディレクトリは
  空のままになります。
- **CI から見た Simulator と CoreSimulator。** Simulator を駆動する各ジョブが呼ぶ複合アクション
  [`collect-ios-diagnostics`](../../.github/actions/collect-ios-diagnostics/action.yml) が担います。
  軽い段は毎回走ります。`CoreSimulator.log` の末尾、起動済みデバイス自身の CoreSimulator ログ
  ディレクトリ、`.ips` / `.crash` の先へ広げた（この失敗が実際に残すハング、スピン、jetsam の
  レポートまで含む）クラッシュレポートの走査、そして実行をまたいで失敗をランナーイメージと
  ハードウェア世代に結びつけるホストのスナップショット（`sw_vers`、`sysctl`、`xcodebuild
  -version`、`simctl list`）です。重い段は、そのジョブの実行ステップ自身が失敗したときだけ走ります。
  `xcrun simctl diagnose` と、対象を絞った2つの unified log の抽出です。
- **時系列で見たホスト。** 同じアクションの `start` フェーズが担います。バックグラウンドのサンプラが
  20秒ごとに `top`、`vm_stat`、`memory_pressure` を `runs/diagnostics/host-telemetry.log` へ
  追記します。一発の描画プローブが、スクリーンショットに何秒かかるか、5秒の `recordVideo` が
  そもそも1バイトでも生むかを記録します。サンプラはあらゆる実行ループの外にいる観測者なので、
  その間隔は採取の周期であって、判定が依存する待ち合わせではありません。`top` の行は CPU ではなく
  常駐サイズで並べます。`top -l 1` は差分を取らないため全プロセスが 0.0% を報告し、CPU で並べると
  定数で並べることになるからです。最初の収集では、残った行に試験対象のアプリが一度も入りませんでした。
- **何も走る前のホスト。** これも `start` が担います。上限付きの `ps aux` と `vm_stat` を一度だけ
  `runs/diagnostics/ps-baseline.txt` へ書きます。ストール時の捕捉が撮る `ps` は、壊れた時点で何が
  常駐していたかには答えますが、それが平常と違うのかには答えられません。最初の収集では、この問いを
  計測ではなく起動環境の引数から判断せざるをえませんでした。これと描画プローブはジョブごとに一度だけ
  書くので、ジョブが2度目の `start` を呼んでも、実行前の値が実行中の値で上書きされることはありません。

unified log の抽出を2つに分けているのには理由があります。自然な想定はむしろ逆なので、ここに
書いておきます。スクリーンショットを担うゲストプロセス、すなわち `SpringBoard`、`backboardd`、
`testmanagerd` は、ホストの unified log に**書き込みません**。起動済みデバイスで
実測したところ、これらのプロセス名で絞ったホスト側の `log show` は、見出し行だけを返しました。
ゲストのエントリはデバイス自身のログストアにあるため、アクションは `simctl spawn` を通して
ゲストの内側で2つ目の `log show` を走らせます。ホスト側の抽出は、実際にそこへ書き込む
CoreSimulator のサービスプロセス向けに残しています。ホスト側の抽出1つで済ませていたら、空のファイルが
できていました。この収集が置き換えた、構造上必ず空になる成果物と同じものです。

#### Android レーン

同じ3層を集めるのは、Android Virtual Device（AVD）を起動するジョブすべてです。`uiautomator
(codegen)` も含みます。むしろこのジョブこそ収集が効きます。このジョブがアップロードする経路は、
どれも Gradle が生成テストを**実行して**初めて生まれるからです。その手前で落ちる原因は、AVD の boot の
固まり、codegen やビルドの失敗、ジョブ自身の `timeout-minutes` の発火です。いずれの場合も
`if-no-files-found: ignore` が成果物を黙って捨てていました。自分のテストの外側で落ちたジョブは、1つも
アップロードできていなかったのです。ただし `bajutsu run` を回す6ジョブと違い、このジョブはテスト時に
adb ドライバを走らせないので、階層読み取りのトリガーはここでは発火しません。第1層のフックは、ホスト側の
レコーダ（[`screenrecord.py`](../../demos/showcase/android/screenrecord.py)）が録画のバイト増加を
確認する経路です。描画が固まった状態、つまりこのレーンの既知のフレークでは、この確認が通りません。

- **`bajutsu` の内側。** ストール時の捕捉の発火点は2つだけで、どちらも意図的に絞ってあります。
  1つめは常駐チャネルの階層読み取りのフォールバックで、ドライバがチャネルを諦めてリース残りを
  `uiautomator dump` のサブプロセスに切り替える地点です。読み取りチャネルが一時的に不安定なので
  はなく、失われたことを意味する地点にあたります。2つめは、デバイス側の `screenrecord` が動いて
  いるだけでなく実際にバイトを吐いているかどうかを見る確認で、描画が固まった状態と、そもそも録画が
  始まっていない状態とを切り分けます。どちらの瞬間も、ホストの `ps` と `top` のスナップショット、
  `dumpsys SurfaceFlinger --latency`、`logcat` の末尾を
  `runs/diagnostics/stalls/stall-NN-<理由>-<pid>/` へ書きます。アクチュエーションの経路は意図的に
  除いてあります。常駐チャネルのエラーのうち2つは、まったく健全な実行でも発火するからです。
  アクチュエーション用のエンドポイントを持たない古いサーバと、二重に操作しないようドライバが
  「届いた」と扱う応答の喪失の2つです。ここで捕捉すると、本当のストールが使うべきトリガーごとの
  上限を先に使い切ってしまいます。
- **CI から見たエミュレータ。**
  [`scripts/collect_android_diagnostics.sh`](../../scripts/collect_android_diagnostics.sh) が
  担います。この半分が複合アクションではなくスクリプトなのは、Android レーンが iOS レーンから
  もっとも鋭く分かれる制約のためです。このレーンでデバイスに触るステップは、すべて
  `reactivecircus/android-emulator-runner` ステップの**内側**で走ります。このステップは AVD を
  起動し、`script:` を実行し、終わりに AVD を落とします。したがって、その後ろに置いた通常の
  ステップも、ステップレベルの `if: failure()` による制御も、デバイスが1台も繋がっていない状態で
  走り、何も集められません。そこで掃き出しは各ジョブ自身の `script:` の末尾から呼び、実行コマンド
  自身の終了コードで段を切り替えます。この形は、このレーンの `poll_cpuinfo` ポーラが失敗する実行を
  生き延びるためにすでに使っている形と同じです。軽い段は毎回走ります。全バッファの `logcat -d`
  ダンプに加えて、`dumpsys meminfo`、`getprop`、`adb devices -l` による環境スナップショットです。
  環境スナップショットは、失敗した実行を API レベルと ABI に結びつけます。全バッファのダンプが読むのは、
  シナリオごとの `deviceLog` インターバル証拠と違って、デバイス自身が保持するリングバッファです。
  そのため、どのシナリオの取得も始まる前に落ちたジョブについても示せるものが残ります。重い段は、
  実行自身が失敗したときだけ走ります。Android 自身の総合収集ツールである `adb bugreport` と、
  root 権限で取る `/data/tombstones` のネイティブクラッシュレポート、`/data/anr/` の
  Application Not Responding（ANR）トレースです。この2種類のクラッシュレポートは、iOS では
  ホスト自身の診断レポートから何もせずに手に入りますが、Android ではデバイス側にあります。
  掃き出しは、コマンドごとだけでなく掃き出し全体にも上限を持ちます。理由は、プロセス内の取得が
  プローブごとのタイムアウトの上にさらに予算を持つのと同じです。この掃き出しが記録しようとしている
  デバイスの固着では、`adb` の読み取りはどれも上限いっぱいまで固まります。そのためコマンドごとの
  上限だけでは、収集がジョブの `timeout-minutes` の大半を使い切り、報告できたはずの失敗が、途中で
  切れた収集物だけを抱えたキャンセル済みジョブに変わってしまいます。期限で打ち切った読み取りは、
  その旨を自分のファイルに書き残します。「予算を使い切った」と「この読み取りは何も見つけなかった」は、
  デバイスについて別のことを述べているからです。
- **時系列で見たホスト。** 複合アクション
  [`collect-android-diagnostics`](../../.github/actions/collect-android-diagnostics/action.yml) が
  担います。各ジョブのエミュレータステップの前に置く `start` フェーズが、バックグラウンドのサンプラを
  起動します。サンプラは `top` と `free` の出力を約20秒ごとに
  `runs/diagnostics/host-telemetry.log` へ追記します。後ろに置く `collect` フェーズが、その
  サンプラを止めます。読むのは Linux ランナーだけなので、この層はステップとして成立します。そして
  エミュレータステップを外側から挟むからこそ、エミュレータステップごと落ちたジョブもカバーできます。

このホストのサンプラは、レーンの既存の `poll_cpuinfo` 入力を置き換えず、並存します。両者は別の側から
別のものを測っているからです。`poll_cpuinfo` は adb 越しにゲスト側の見え方を読み、サンプラはホスト
自身の負荷を読みます。この違いは、`poll_cpuinfo` がオプトインであるのに対してサンプラが常時オンで
ある理由でもあります。ホスト側で20秒ごとに取るサンプルは adb のトラフィックを一切発生させず、頻度は
10分の1です。`poll_cpuinfo` をデフォルトで無効にしている観測者効果を持ちません。

2つのレーンが複合アクションを分けたままなのは意図的です。`collect-ios-diagnostics` は `simctl`、
CoreSimulator、macOS の unified log を読みます。`collect-android-diagnostics` が読むのは Linux
ホストです。
足回りのツールに共通の面はないので、1つのアクションにまとめても、共通のロジックではなくバックエンド
での分岐になります。両者が共有しているのは設計、すなわち軽い段と重い段の分割、`start` と `collect` の
テレメトリのフェーズです。そして `bajutsu` の内側では、捕捉そのものを共有しています。

**赤くなったレーンが集めた証跡の読み方。** ここまでの証跡は `gh run view --log-failed` には現れません。
失敗したジョブ自身の `runs/` アーティファクトの中にアップロードされます。そのため、ログだけで
診断するセッションからは見えないままです。

エージェントスキル
[`investigate-ci-failure`](../../.apm/skills/investigate-ci-failure/SKILL.md) は、そのアーティファクト
を取得します。そして中身を
[`known-ci-failure-patterns.md`](../../.apm/skills/investigate-ci-failure/references/known-ci-failure-patterns.md)
に記録された症状と突き合わせます。そのうえで、失敗を既知のインフラ由来の flaky、実際の欠陥、未分類の
いずれかに分類します。[`pr-followup`](../../.apm/skills/pr-followup/SKILL.md) は修正に手をつける前に
これを実行します。ホスト由来の flaky にコード修正で答える事態を避け、回帰を flaky として見送る事態も
避けるためです。このスキルは報告するだけで、編集や push や再実行はしません。

## あなたのアプリの CI で回す

> bajutsu はプレリリース（未公開）です。PyPI 公開までは vendor（submodule / checkout）して、その
> checkout からアクションを実行してください。アクションは bajutsu の `pyproject.toml` に対して
> `uv sync` を実行します。

bajutsu は CI 向けの出力を生成します。`junit.xml`、自己完結の `report.html`、`0`/`1` 終了
コード、そして Actions 内では失敗**アノテーション** + ジョブ**サマリ**です。macOS ランナーでは次のようにします。

1. 起動済みのシミュレータに **アプリ（と XCUITest ランナー）をビルドしてインストール**します。これはアプリごとに異なるためあなたの担当です（`xcodebuild`
   + `xcrun simctl install`）。
2. [`bajutsu-e2e`](../../.github/actions/bajutsu-e2e/action.yml) composite action で **bajutsu を実行**します。
   このアクションは依存の同期、シナリオの実行（`run --score` を付けるので、ログに入口画面の規約グレードが残ります）、そして
   成果物（report、スクリーンショット、動画、`network.json`）のアップロードを行います。XCUITest バックエンドは pip の extra を必要としません。ランナーは HTTP 越しに駆動し、`xcodebuild` はランナー上の Xcode に付属するためです。

```yaml
jobs:
  e2e:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: maxim-lobanov/setup-xcode@v1
        with: { xcode-version: latest-stable }
      - uses: astral-sh/setup-uv@v6
        with: { enable-cache: true }
      # --- アプリのビルド + インストール（あなたのビルド、起動済みシミュレータへ） ---
      - run: xcodebuild -scheme MyApp -destination 'generic/platform=iOS Simulator' -derivedDataPath dd build
      - id: sim
        run: |
          udid=$(xcrun simctl create ci "iPhone 16")
          xcrun simctl boot "$udid"; xcrun simctl bootstatus "$udid" -b
          echo "udid=$udid" >> "$GITHUB_OUTPUT"
      - run: xcrun simctl install "${{ steps.sim.outputs.udid }}" dd/Build/Products/Debug-iphonesimulator/MyApp.app
      # --- bajutsu 実行 ---
      - uses: your-org/bajutsu/.github/actions/bajutsu-e2e@main
        with:
          scenarios: e2e/*.yaml
          app: myapp
          udid: ${{ steps.sim.outputs.udid }}
```

### アノテーション + ジョブサマリ

`GITHUB_ACTIONS` がセットされていると、`bajutsu run` は失敗シナリオごとに `::error::` アノテー
ション（PR にインライン表示）を出し、`$GITHUB_STEP_SUMMARY` に PASS/FAIL 表を追記します。フラグ
不要で、Actions 環境を自動検出します。

### 補足

- **JUnit**：`junit.xml` はレポートの隣に書き出されます。これを test-reporter 系アクション（例 `dorny/test-reporter`）に渡すと、テスト結果をインライン表示できます。
- **決定性**：シナリオの [`mocks`](../network.md#deterministic-mocks) で通信をスタブし、ライブサーバへの依存をなくします。
- **規約スコア**：composite action は `run --score` を渡すので、run が実行時の最初の起動から算出した入口画面の規約グレード（`Ready` / `Partial` / `Blocked`、`doctor` が報告するスコアと同じもの）を stderr に出力します。この出力は診断だけを目的としており、合否の判定には一切関与しません。スコアを run に畳み込むことで、2 つめの XCUITest ランナーをコールド起動する別立ての `doctor` ステップを避けられます。実行可能性や capability のチェックを含むより広いプリフライトを行うには、ローカルで `bajutsu doctor` を実行してください。CI での env や権限の実行可能性ゲート（`xcodebuild` / Xcode の存在チェック）は今後の課題です。
- **影響ステップの選択**：[`bajutsu impact`](cli.md#impact) は `git` の diff がどのシナリオステップに影響しうるかを報告するので、パイプラインは素早いフィードバックのためにそれらのステップを先に並べられます。安全な既定は加算的で、スイート全体は流したまま、影響のあるものを先に置くだけです。マージ前の run を影響集合まで絞り込むのは opt-in で、健全なのは `impact` が示す2つの安全策があるときに限ります。レポートが**不完全**なとき（`--json` の `complete` が `false`、すなわち diff がどの参照 id にも対応しない変更を含むとき）はフルスイートへ退避すること、そして決定的なスイート全体を粗い cadence（マージ時、夜間、あるいはリリース時）で依然として流すことです。合否の判定は常に `run` に残ります。
