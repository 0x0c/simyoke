[English](../reporting.md) · **日本語**

# レポート（manifest.json / JUnit / CTRF / HTML）

> 1 回の run は、1 つ以上のシナリオ（`list[RunResult]`）を実行します。その結果を 4 つの形式で
> 書き出します。`manifest.json` が、レポートと CI（継続的インテグレーション）の **唯一の情報源**です。
>
> 実装: `bajutsu/common/report/`（パッケージ。段階で分割: `format` → `manifest` / `richtext` → `rows` / `panels` → `html`）。

関連: [run-loop の実行結果](run-loop.md#実行結果データ構造) · [evidence](evidence.md)

---

## 出力レイアウト

```
runs/<runId>/
├── manifest.json     # step → outcome の相関（単一の真実）
├── junit.xml         # CI 連携（1 シナリオ = 1 testcase）
├── ctrf.json         # Common Test Report Format（PR コメントやダッシュボードなど、より豊かな CI の消費側向け）
├── report.html       # 自己完結 HTML（外部アセット無し）
└── <sid>/            # シナリオ単位の証跡（FileSink 使用時）
    └── <stepId>/     # ステップごとの証跡
        ├── before.png    # screenshot（ステップが動作する前）
        ├── after.png     # screenshot（動作したあと）
        ├── elements.json # query() ダンプ
        ├── segment.mp4   # video（区間）
        └── device.log    # deviceLog（区間）
```

`runId` は `YYYYMMDD-HHMMSS` の形式で、`bajutsu/common/run_meta/id.py`（[BE-0200](../../roadmaps/BE-0200-run-id-contract/BE-0200-run-id-contract-ja.md)）が一箇所で採番します。この形式は report、Web UI、その他すべての呼び出し元で共有する単一の契約です。`sid` は `{NN}-{slug}` の形式で、ゼロ埋めされた実行順の連番と、シナリオを読み込んだ元ファイルの語幹（`login_flow.yaml` なら `login_flow`）をつなげたものです。ファイルから読み込まれていないシナリオ（メモリ上で直接組み立てられたものなど、元ファイルが不明な場合）では、代わりにシナリオの `name:` フィールドをスラッグ化した値になります
（[BE-0417](../../roadmaps/BE-0417-scenario-result-folder-naming/BE-0417-scenario-result-folder-naming-ja.md)）。`stepId` は `step.name` または `step<i>` です。

スラッグは、元ファイルの語幹から作る場合も `name:` から作る場合も、60 文字に切り詰めます（[BE-0420](../../roadmaps/BE-0420-scenario-slug-length-cap/BE-0420-scenario-slug-length-cap-ja.md)）。元ファイルの語幹には Unicode の文字がそのまま残るため、上限はバイト数ではなく文字数で数えます。全角文字や日本語の名前は、1文字あたりのバイト数が多くなりがちです。American Standard Code for Information Interchange（ASCII）は、1文字を1バイトで表します。文字数で数えることで、日本語の名前も ASCII と同じ長さに切り詰められます。ファイル名や `name:` フィールドが上限を超えるシナリオも、最後まで実行できます。証跡ディレクトリの名前は、見分けのつく先頭部分だけになります。`Scenario.name` 自体は切り詰めないので、`manifest.json` と `report.html` には元の名前がそのまま残ります。切り詰めた結果スラッグが一致する2つのシナリオも、`runs/<runId>/<sid>/` がスラッグの前に置く実行順の連番で区別できます。

## manifest.json

`RunResult` 以下はすべて dataclass なので、`manifest_dict` でステップ / expect の結果がそのまま落ちます。ただし`wall_offset_s`は例外です（詳細は後述）。これは run 内だけで使う変換定数であり、永続化後は意味を持たないため除外されます。それ以外のタイムスタンプはすべて絶対的な実時刻（エポック秒）なので、記録した run のプロセスが終了したあとも同じ意味を保ちます（[evidence](evidence.md#区間証跡video--devicelog--apptrace)参照）。

```json
{
  "runId": "20260605-101530",
  "ok": true,
  "backend": "xcuitest",
  "scenarios": [
    {
      "scenario": "onboard, log in, and increment the counter",
      "ok": true,
      "backend": "xcuitest",
      "steps": [
        {
          "index": 5, "action": "tap", "ok": true, "reason": "",
          "duration_s": 0.12,
          "assertion_results": [],
          "artifacts": [
            { "name": "before.png", "kind": "screenshot", "provider": "driver",
              "depicts": "xcuitest:before" },
            { "name": "after.png", "kind": "screenshot", "provider": "driver",
              "depicts": "xcuitest:after" }
          ]
        }
      ],
      "expect_results": [
        { "ok": true, "kind": "value", "detail": "value equals='2': id='counter.value'", "reason": "" }
      ],
      "failure": null
    }
  ]
}
```

- `ok`（トップ）: 全シナリオが ok なら true です。
- `backend`: その run を操作したアクチュエータです（`xcuitest`、テストでは `fake`）。アクチュエータは
  run ごとに 1 つ固定なので、トップレベルは通常 1 つの名前です。各シナリオも自分の `backend` を持ちます
  （[drivers](drivers.md#バックエンド選択と-actuator)）。
- `steps[].duration_s`: 各ステップの計時です。run が求める経過時間は、すべて単調増加時計で測ります。実時刻の時計は run の途中で過去へ戻ることがあり（Network Time Protocol（NTP）による補正、手動での時刻変更）、経過時間やタイムアウトを壊すからです。実時刻の時計はシナリオごとに一度だけ読み、記録するタイムスタンプにだけ使います。
- `steps[].started_at`: そのステップが始まった絶対的な実時刻（エポック秒）です。記録する値に動画の補正は入りません。録画のどこへシークするかは、描画のときに下の`video_anchor_s`を引いて求めます。[report.html](#reporthtml)はその値を**steps**テーブルの`at`列に表示します。補正済みのオフセットではなく生の時刻を保存するので、保存済みの run からシーク位置を計算し直せます。たとえば基準時刻の求め方を修正したときも、シナリオの実行し直しは要りません（[BE-0348](../../roadmaps/BE-0348-absolute-timestamp-recording/BE-0348-absolute-timestamp-recording-ja.md)）。
- `video_anchor_s`（シナリオごと）: そのシナリオの動画が始まった絶対的な実時刻です。確定した録画の長さが実時間の測定値になっている場合はその長さから測り、そうでない場合は録画の開始確認の信号から取ります（[evidence](evidence.md#区間証跡video--devicelog--apptrace)）。描画する側は、どの表示でもこの値を引きます。`schemaVersion` 6 より前に記録された run は、この値を持ちません。その`started_at`はすでに動画基準の相対値なので、描画する側は欠けた基準時刻を`0.0`と読み、記録どおりに表示します。
- `wall_offset_s`（run 内だけの値で、永続化されません）: その run が、自分自身の単調増加時計の値を実時刻のエポックへ変換するために足す差分です。通信ログの受信時刻（単調増加時計）を、`video_anchor_s`と同じ基準時刻へ載せるためだけに使います。manifest には残りません。単調増加時計の値もmanifestには残らないので、あとから読む側にはこの差分を使って変換する対象自体がないからです。
- `steps[].artifacts`: そのステップで取れた証跡の来歴です（[evidence](evidence.md#アーティファクトの来歴provider)）。
- `steps[].actuations`: そのステップのあいだにドライバが画面に対して実際に行ったことです。タップが送った座標、スワイプが動いた端点、各ジェスチャを運んだ経路が入ります。これが `actionLog` の証跡種別で、ファイルではなく manifest に内在します（[evidence](evidence.md#各ステップが画面に対して実際に行ったことactionlog)）。`schemaVersion` 5 より前に記録された run は持ちません。`expect_actuations` はシナリオ末尾の `expect` の再チェックについて同じものを持ちます。そこではシステムアラートガードが、載せる先のステップなしに操作しうるからです。`schemaVersion` 7 からは、操作した要素がドライバの既定の規則の指す要素と異なる理由を `substitution` として持つことがあります。それより前の run は持たず、いまその項目がないのと同じに読めます。
- `network.json`の`startedAt`（シナリオごとに1ファイルで、上のmanifestには出てきません）: 観測した各通信が始まった絶対的な実時刻です。`steps[].started_at`と同じ土俵に立ち、同じシナリオの基準時刻を通して導かれるので、描画する側は両方から`video_anchor_s`を引きます。両者がどのように1本のタイムラインへ織り込まれるかは[report.html](#reporthtml)を参照してください。
- `failure`: 失敗時の要約です（例 `"step 3 (tap): 一致なし: {...}"`）。成功なら null です。
- `provenance`（トップ、任意）: run の同一性スタンプです（[BE-0049](../../roadmaps/BE-0049-determinism-flakiness-audit/BE-0049-determinism-flakiness-audit-ja.md)）。`scenarioHash`（実行した `scenario.yaml` の `sha256:` フィンガープリント）、`toolVersion`（`bajutsu.__version__`）、`gitRevision`（コミット。git チェックアウト内の run のときだけ付く）、そして config が Git ソース由来のとき（[BE-0063](../../roadmaps/BE-0063-git-config-source/BE-0063-git-config-source-ja.md)）は `configSource`（`{ host, owner, repo, ref, sha }`。ブランチ指定の run が実際に実行した正確なコミット）を持ちます。蓄積した run を同一性でグルーピングできるので、フィンガープリントが変わっていないのに判定が反転すれば、それは編集ではなく**真の flakiness** だと分かります。純粋なメタデータで、`ok` には一切入りません。（このブロックが出るようになった時点で `schemaVersion` は `3` 以上です。現在は `10` です。）
- `target`（トップ、任意）: この run が実行した target です。「Android の target は通るのに iOS の target は落ちる」を、保存済みのデータから計算できます（[BE-0404](../../roadmaps/BE-0404-collapse-project-layer/BE-0404-collapse-project-layer-ja.md)）。1 つの run が解決する target は 1 つなので、シナリオごとではなく `backend` の隣に置きます。`serve` はこれを run の行へ写し、target どうしを順位付けします。target を持たない run では省かれます。（この項目が出るようになった時点で `schemaVersion` は `10` 以上です。）
- `label`（トップ、任意）: run 履歴の区切りです。bind している config 自身の名前か、運用者が `run --label` で上書きした値が入ります。不透明なメタデータで、解析も config との照合もせず、`ok` の入力にもなりません。`serve` を再起動したあとに 2 つの config の run を読み分けられるのは、この値のおかげです。label を持たない run では省かれます。（この項目が出るようになった時点で `schemaVersion` は `10` 以上です。）
- `idb`（トップ、任意、レガシー）: 古い manifest には `idb_companion` / client のバージョンブロックが残っていることがあります（BE-0005）。idb バックエンドとともに BE-0290 で廃止され、今は書き出されません。未知のトップレベルキーは読み込み時に無視されるため、これを含む古い manifest も問題なく読めます。
- `matrix`（トップ、任意）: クロスブラウザのエンジン × シナリオのグリッドで、`bajutsu run --browsers` の run のときだけ出ます（[BE-0076](../../roadmaps/BE-0076-web-cross-browser-engines/BE-0076-web-cross-browser-engines-ja.md)）。`scenarios` はフラットな結果リストのままで、各エントリに `engine` が付きます。`matrix` は `{ engines, scenarios, cells: { "<scenario>": { "<engine>": { ok, sid, failure } } } }` で、エンジンごとの判定を集約しただけのものです（report はこれをグリッドとして描画します）。`ok` はエンジン × シナリオのすべてに対する all-must-pass です。単一エンジン／iOS の run では省かれます。複数ファイルにまたがる run では `name` の一意性は保証されません。そのため、同じ名前のシナリオが2つ目以降にあると、最初のセルを上書きするのではなく、`(2)`、`(3)` などの接尾辞が付いた別の行として保持します。（このブロックが出るようになった時点で `schemaVersion` は `4` 以上です。）

## junit.xml

CI 連携用です。**1 シナリオ = 1 `<testcase>`**。失敗シナリオには `<failure>` が付き、その `text` に
各ステップ / expect の ok/FAIL と理由が並びます。`--browsers` のマトリクス run では、各ケースにエンジンが織り込まれ（`classname="bajutsu.<engine>"`）、CI からは `chromium.login` と `webkit.login` が別々のケースに見えます（BE-0076）。単一エンジンの run は `classname="bajutsu"` のままです。

```xml
<testsuite name="bajutsu" tests="2" failures="1">
  <testcase name="..." classname="bajutsu"/>
  <testcase name="..." classname="bajutsu">
    <failure message="step 1 (tap): ...">step 0 tap: ok
step 1 tap: FAIL 一致なし: {...}</failure>
  </testcase>
</testsuite>
```

## ctrf.json

[Common Test Report Format（CTRF）](https://ctrf.io/)への出力です（[BE-0161](../../roadmaps/BE-0161-ctrf-report-export/BE-0161-ctrf-report-export-ja.md)）。CTRF はオープン標準の JSON テストレポートです。`ctrf-io` の GitHub Actions（PR コメントやジョブサマリーの出力ツール）、ツールをまたぐダッシュボード、flaky なテストの分析といった消費側のエコシステムが育ちつつあり、これらはツールごとのアダプターなしに CTRF を読めます。JUnit XML が run を名前、所要時間、失敗内容の塊まで削ぎ落とすのに対し、CTRF は Bajutsu の構造化された詳細（ステップごとの結果、エンジンとデバイス、第一級のアタッチメントとしてのアーティファクト）を運びます。これらは JUnit には収まる場所がありません。この出力は **`manifest.json` の純粋な射影**であり、同じデータを `junit.xml` の隣に別の形で並べるだけなので、記録が二重になるわけではありません。判定より後に書くので判定を動かすこともありません（LLM は関与せず、合否にも影響しません）。

文書は `{ reportFormat: "CTRF", specVersion, generatedBy, timestamp, results }` で、`results` が `tool` / `summary` / `tests`（＋任意の `environment` / `extra`）を持ちます。

```json
{
  "reportFormat": "CTRF",
  "specVersion": "0.0.0",
  "generatedBy": "bajutsu",
  "results": {
    "tool": { "name": "bajutsu", "version": "…" },
    "summary": { "tests": 2, "passed": 1, "failed": 1, "skipped": 0, "pending": 0, "other": 0,
                 "start": 1717581300000, "stop": 1717581302300, "duration": 2300 },
    "tests": [
      { "name": "login", "status": "passed", "duration": 1500,
        "steps": [{ "name": "tap", "status": "passed", "extra": { "duration": 500 } }],
        "browser": "chromium", "device": "iPhone 15 (iOS 17.2)",
        "attachments": [{ "name": "00-login/scenario.mp4", "contentType": "video/mp4", "path": "00-login/scenario.mp4" }] }
    ]
  }
}
```

- `summary.duration` と各 `tests[].duration` はミリ秒（Σ／シナリオごとの `duration_s`）で、CTRF の消費側が拠り所にするフィールドであり、正確です。`summary.start` と文書の `timestamp` は `YYYYMMDD-HHMMSS` の runId から導出します。runId は UTC で採番されるため、UTC として解釈します。`stop = start + duration` です。テストごとの絶対 start/stop は、シナリオごとの絶対エポックが要る（オプションの後続作業）ため、近似せず省きます。実行時のホスト状態は一切載せないので、`bajutsu report` は同じ実行の `ctrf.json` をバイト単位で同一に再生成します。
- `tests[].status` は `passed` / `failed` です。Bajutsu の run が出す状態はこの二つだけで、他の CTRF の集計は `0` のままです。
- CTRF の `step` は `{ name, status, extra }` しか許さないので、ステップのより豊かなデータ（duration、reason、ステップごとのアサーション、アーティファクト）は `step.extra` に入れます。name／status だけを描画する消費側にはきれいな一覧が見え、Bajutsu を理解するツールは extra を読めます。
- アタッチメントの `contentType` は、アーティファクトの `kind` → MIME の対応表（`video`→`video/mp4`、`screenshot`→`image/png`、`deviceLog`→`text/plain`、`elements`／`network`／`appTrace`→`application/json`）から決め、未知の kind には `application/octet-stream` を充てます。`path` は manifest と同じく実行ディレクトリからの相対パスです。
- `--browsers` のマトリクス run では、エンジン × シナリオの各セルが 1 つの CTRF テストになります。エンジンはテストの `name` と `browser` フィールドに入れ（JUnit の `classname` に倣います）、エンジン × シナリオのグリッドは `results.extra.matrix` に持ちます。Bajutsu のその他の余剰（`sid`、`expect` の結果、アラート、`skipped_captures`）は、テストごとの `extra` に入ります。
- CTRF は秘匿処理済みの manifest から射影されるので、同じ秘匿処理を継承します（[BE-0047](../../roadmaps/BE-0047-ai-data-sovereignty/BE-0047-ai-data-sovereignty-ja.md)）。生のシークレットは届きません。

### CI で ctrf.json を消費する

`ctrf.json` は `junit.xml` の隣にあるので、CI ジョブへの組み込みは消費側の一手順で済みます。たとえば `ctrf-io/github-test-reporter` アクションは、これを PR コメントやジョブサマリーに変換します。

```yaml
- uses: ctrf-io/github-test-reporter@v1
  with:
    report-path: runs/*/ctrf.json
  if: always()
```

## report.html

人間が見る自己完結 HTML（インライン CSS、外部アセット無し）です。ヘッダには run id と全体 PASS/FAIL、
その下にシナリオファイル名（`source_name`）を表示します。`source_name` は run 全体のラベルです。
単一ファイルならそのファイル名、複数ファイルやディレクトリ指定なら親ディレクトリ名になります。
さらにファイルレベルの `description` があれば表示します。

各シナリオ行のサマリには、シナリオ名と、そのシナリオ自身の元ファイル名（`source_files`）を表示します。
この元ファイル名は `source_name` とは別物です。`bajutsu run` がディスクからシナリオを読んだ場合だけ
設定され、オフライン再描画では設定されません。設定があれば、横にシナリオレベルの `description` も
表示します。こうして run 全体でシナリオ名 + ファイル名 + description を提示します。

シナリオ定義とその実行結果は 1 つの Steps タブに統合され、ラベル付きセクション
（**preconditions** / **steps** / **expectations**）ごとにテーブルで描画されます。steps テーブル：`#` / `result`（PASS/FAIL ピルを独立カラムで）/
`action`（色付きバッジ）/ `detail`（対象説明）/ `at` / `view`（スクリーンショット＋レポート内 element tree
ビューア: キャプチャした要素を別タブではなくページ内オーバーレイで開く）/ `reason`。
`#` 列には、`step_lines` がシナリオファイル中の行番号を復元できたとき、`2:15` のようにその行番号も
併記します。`setup` やコンポーネント展開でステップ構成が変わった場合、行番号は誤って示すより
省略します。`at` セルには、所要時間が 0.0 秒以外に丸まるとき、セルの末尾に括弧書きで経過時間も
表示します（例: `3.2s (1.1s)`）。時間のかかった `wait` や動作の遅いジェスチャーが、録画を開かなくても
わかります。
表示するスクリーンショットは、動作後の `after.png` です
（[evidence](evidence.md#区間証跡video--devicelog--apptrace)）。動作したステップは、いずれも
`after.png` を記録します。動作する前に失敗したステップは `after.png` を記録しないため、
動作前の `before.png` を表示します。ビューアで要素にカーソルを合わせると、
その要素の枠をスクリーンショット上に重ねます。枠の座標は `elements.json` から来ます。
`elements.json` も、ステップが動作したあとに取り直します。そのため枠と、その下にある画素とは
同じ瞬間を表します。ただし例外が1つあります。動作しないステップ（`assert` と `wait`）は、
そのステップが落ち着いたときのツリーを再利用します（BE-0259）。このツリーは、スクリーンショットの
あとではなく前の瞬間を表します。serve のエディタの要素ピッカーも、同じ理由から同じ画像を使います。
画像と要素ツリーが *別々の* 画面を表すこともあります。
[`web` ブロック](scenarios.md#webwebview-の-dom-コンテキストに入る)のネイティブ画像と WebView の要素
ツリーの組や、`after.png` を失った実行結果が該当します。そのときビューアは、両方を見せたうえで枠を
描きません。枠がない理由は要素ツリーのボタンに添えます
（[evidence](evidence.md#アーティファクトが写した画面depicts)）。
serve のエディタは、そうしたステップでは画像そのものを出しません。出せば、どこをクリックしても、
その画素にはいなかった要素へ解決してしまうからです。
detail 中の識別子（`#home.title`）と定数リテラル（`”text”` や数値）は、控えめなインライントークンで
描画します。ソリッドな action/assert バッジと視覚的に区別されるため、変数と定数を一目で識別できます。`assert` ステップの複数チェックはネストしたテーブルになり、1 アサーション 1 行で
`kind` / `target` / `comparison` のセルに分割します（読みにくい `a; b; c` 形式を解消）。実行されなかった
ステップ（失敗で停止）も skipped として残ります。

観測した通信を時系列で steps に差し込みます（各々シナリオ開始からのオフセットで配置）。HTTP メソッドを中立バッジ、ステータスを `result` 列に置き、通信の設定（method / endpoint / status / duration / ヘッダ）を detail セル内のネストしたテーブルで表示します。どの通信を出すかはシナリオの `network.filter.domains`（URL ホスト）で絞ります。Network タブは引き続き全件を載せます。

preconditions テーブルは折りたたみ可（key / value）。
expectations テーブルは並行カラム `result` / `kind`（バッジ）/ `target`（検査対象セレクタ。例:
`#counter.value`）/ `comparison`（例 `== “2”`）/ `reason`（同じ id/定数トークン）です。Rich / YAML
トグルで同じタブを構造化ビューと生のシナリオ YAML に切り替えられます。新しく bake した `bajutsu run`
のレポートでは、その YAML はシナリオ自身の元ファイルから切り出した生テキストです。コメントや整形は
パースしたモデルから作り直さないため、そのまま残ります。リテラルな `totp.secret` はその場で
マスクします。`setup` やコンポーネント展開でステップ構成が変わったシナリオでも、この元テキストは
著者が書いたとおり正確なので保持します。ただしその場合、`#` 列の行番号は失われます。

`data`/`dataFile` によるデータ駆動シナリオは、常に構造化データから作り直した YAML を表示します。
生テキストは表示しません。各行は同じ元テンプレートを共有します。`expand_data` は行ごとに
`${row.*}` を置換しますが、ステップ数は変えません。そのため生テキストをそのまま出すと、どの行も
同じ未置換のテンプレートになり、実際に実行した内容を示せません。

オフライン再描画（`bajutsu report`、後述）は、元ファイルの生テキストを run に保存していないため、
従来どおり構造化データから作り直した YAML を使います。

`visual` の expectation は行の下に **baseline と actual のインタラクティブ比較ビュー**を描画します。
4 モード: **Swipe**（仕切りをドラッグして左右にワイプ）/ **Onion**（スライダーで actual を
baseline に重ねてクロスフェード）/ **Blend**（`mix-blend-mode: difference`。同一画素は黒、差分画素は強調表示）/ **Diff**（マシンが算出した確定ピクセル diff。アサーションの `exclude` 領域は
マスク済み。失敗時のみ表示）。`diff <pct>%` バッジが付き、初回実行（actual のみ存在）では `no baseline yet`
バッジになります。チェックが合格しなかった場合は **Approve as baseline** ボタンが表示され、撮影スクリーンショットを
ベースラインディレクトリへ昇格させます。これは `/api/approve` への `POST` なので `bajutsu serve`
経由で開いたときだけ機能します（ディスクから直接開いたレポートでは非表示）。CLI 版は
[`bajutsu approve`](cli.md#approve)。

失敗行は赤背景です。ステップをクリックすると録画をその時刻にシークしますが、**自動再生はしません**
（停止中なら停止のまま、再生中なら再生を続けます）。シーク先は各ステップの`started_at`から`video_anchor_s`を引いた値（**steps**テーブルの`at`列）であり、シナリオのステップループが始まった生の瞬間ではなく、録画そのものから測った原点を基準にしています。原点を測れない場合は、分かっている範囲でもっとも確からしい動画の実際の開始時刻を基準にします（[evidence](evidence.md#区間証跡video--devicelog--apptrace)）。クリックした行が実際に示す瞬間にシークが着地するのは、この基準のためです。この結果、目に見える変化が1つあります。動画を録画するAndroidまたはWebのシナリオでは、この描画時に求めた動画基準の秒数（**steps**テーブルの`at`列）が、シナリオ自身の`duration_s`を超えることがあります。この2つは、そもそも別のものを測っているためです。動画のタイムラインは実行本体のステップループより前から始まりますが、`duration_s`はそのステップループ自体の長さを測ります。これは想定された挙動であり、どちらかの数値が誤っているわけではありません。ステップのスクリーンショットをクリックすると原寸ライトボックスが
開き、**← / →**（または画面上の矢印）で run 内の全スクリーンショットを**シナリオをまたいで**順送りできます
（キャプションにシナリオ、ステップ、位置を表示）。run のアクチュエータはヘッダの `driver: <backend>`
チップと各シナリオ行の小バッジで表示します。Device Log / App Trace は別タブのままです。

## 書き出し API

```python
def write_report(run_dir, run_id, results, definitions=None, sources=None, source_name=None, description=None, provenance=None, writer=None, target=None, label=None, source_files=None, step_lines=None) -> Path  # 4 形式を書く。definitions=シナリオ毎の dict、sources=生 YAML、source_name=シナリオファイル名（run 全体）、description=ファイルレベルの説明、source_files/step_lines=live run が復元できたときの各シナリオ自身のファイル名とステップの元の行番号、provenance=run の同一性スタンプ（BE-0049）
def write_html_and_junit(run_dir, run_id, results, definitions=None, sources=None, source_name=None, description=None, provenance=None, writer=None, source_files=None, step_lines=None) -> None  # 再生成できる側だけ（report.html + junit.xml + ctrf.json）。manifest.json は触らない。再描画が使う。provenance は CTRF の tool/environment フィールドに使う
def manifest_dict(run_id, results, *, source_name=None, provenance=None, target=None, label=None) -> dict  # バージョン付き render モデル（schemaVersion）。manifest の素（テスト、検査用）
def run_provenance(scenario_yaml, *, git_revision, config_source=None) -> dict  # run の同一性スタンプ: scenarioHash + toolVersion + 任意の gitRevision（BE-0049）+ 任意の configSource（BE-0063）
def ctrf_json(run_id, results, *, provenance=None) -> dict  # 実行結果モデルの CTRF への射影（BE-0161）。provenance は tool.version / environment.commit に使う
def junit_xml(results) -> str
def html_report(run_id, results, run_dir=None, definitions=None, sources=None, source_name=None, description=None, source_files=None, step_lines=None) -> str
def scenario_render_inputs(scenarios, plan_sources=None) -> tuple[list[dict], list[str]]  # (definitions, sources)。初回 bake と再描画で共有。plan_sources（宣言されたシナリオ名がキー）が復元できたときは、sources に各シナリオ自身の生 YAML を使う。復元できなければ従来どおり構造化データから作り直す
def scenario_source_meta(scenarios, plan_sources=None) -> tuple[list[str | None], list[list[int] | None]]  # (source_files, step_lines)。scenario_render_inputs の sources と対応する
```

`runner.run_and_report` がこの `write_report` を呼び、CLI に `(results, manifest_path)` を返します
（[run-loop](run-loop.md#runner実行パイプライン)）。CLI は全シナリオ成功なら終了コード 0、失敗で 1 です。

## レポートの再生成（BE-0068）

レポートは **run dir に保存されたデータの純粋なレンダリング**です。そのため、完了した run を再実行せずに現行テンプレでオフライン再描画できます。`manifest.json` が**バージョン付き**（`schemaVersion`）で無損失の render モデルで、`report.load` がその逆変換です。`results_from_manifest()` が `RunResult` を復元し、`load_run(run_dir)` が render モデル全体（outcome は `manifest.json`、シナリオの plan は `scenario.yaml`）を復元します。`bajutsu report <run>`（[cli](cli.md#report)）がそれを使って `report.html`＋`junit.xml`＋`ctrf.json` を書き直します。再描画は記録済みの outcome を再提示するだけで、assertion を再実行したり verdict を変えたりしません。古い run も、新しいバージョンにしかないセクションを捏造せず「not captured」と表示して描画します。
