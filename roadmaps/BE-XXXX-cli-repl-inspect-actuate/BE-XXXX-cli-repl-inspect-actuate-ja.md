[English](BE-XXXX-cli-repl-inspect-actuate.md) · **日本語**

# BE-XXXX — 要素ツリーを閲覧し id で操作する対話シェル

<!-- BE-METADATA -->
| 項目 | 値 |
|---|---|
| 提案 | [BE-XXXX](BE-XXXX-cli-repl-inspect-actuate-ja.md) |
| 提案者 | [@0x0c](https://github.com/0x0c) |
| 状態 | **提案** |
| トラッキング Issue | [検索](https://github.com/bajutsu-e2e/bajutsu/issues?q=is%3Aissue+label%3Aroadmap-tracking+in%3Atitle+"BE-XXXX") |
| トピック | オーサリング体験 |
<!-- /BE-METADATA -->

## はじめに

`bajutsu repl`は、起動中のターゲットに対して手動で操作する対話シェルです。操作者はまずアプリを
1回起動し、そのあとは1コマンドずつ入力します。`tree`は現在の画面の要素ツリーを読み取ります。
`tap <id>`はその要素の1つを操作します。操作者は毎回の結果を確かめてから、次のコマンドを決めます。
このシェルは、`record`(ゴール指向のAIオーサリング)と`crawl`(自律探索)の隣に位置し、両コマンドが
すでに使っている[`Driver`](../../docs/ja/glossary.md#driver-backend-actuator-platform)
インタフェースを通じてターゲットに到達する、3つ目の手段です。`record`や`crawl`と異なり、`repl`
は大規模言語モデル(LLM)に何も尋ねず、シナリオも書き出しません。`repl`は、`query()`と、各
[バックエンド](../../docs/ja/glossary.md#driver-backend-actuator-platform)がすでに実装している
操作メソッドを呼び出すだけの、薄いループにすぎません。そのため、XCUITest・adb・Playwrightは、
読み取りと操作についてはバックエンド固有のコードなしにこのシェルを手に入れます(起動と終了の経路
だけは、この共通部分を超える分岐を持ちます。詳細設計を参照)。

## 動機

[セレクタ](../../docs/ja/glossary.md#シナリオのオーサリング)がどの要素に解決されるかを知る作業は、
今日、その問い自体の重さに見合わないコストがかかります。操作者は、Xcode の Accessibility
Inspector やブラウザの開発者ツールで、ツリーを目で読むことができます。しかし、その読み取りは
バックエンド固有の手段に頼ります。しかも、シナリオのステップが実際に照合する、正規化
された`id`・`label`・`traits`フィールドは表示されません。`bajutsu doctor`はすでにコマンドライン
からライブな画面を読みますが、それが評価するのはidの網羅率やnamespace外のidといった規約であり、
ドライバは1回だけ確かめて畳んでしまいます。ツリーを一覧として読ませることも、idを操作することも
しません。Bajutsu内では、`record`と`crawl`のどちらもAI呼び出しを介してアプリを操作し、シナリオや
画面マップという、それぞれ固有の成果物向けに整形された出力を作ります。`serve`のWeb UIが持つ
Authorビューは、これより近いところまで来ています。`/api/capture/start`と`/api/capture/resolve`
([BE-0262](../../roadmaps/BE-0262-serve-author-live-step-picker/BE-0262-serve-author-live-step-picker-ja.md))
は、ライブなドライバを起動し、AI呼び出しなしに画面上のクリックをセレクタへ解決します。ただし、
この選択肢はブラウザのタブの中で動くピッカーであり、シナリオに足す1ステップを選ぶために作られて
いて、打ったidが解決するかどうかには答えません。現在の画面はどう見えていて、その中の
1つのidを操作すると何が起きるか。この直接的でバックエンドに依存しない問いへ、コマンドラインから
答える手段は、どれも用意されていません。

`repl`はこの問いに直接答えます。`tree`は現在の要素ツリーを表示します。`tap <id>`はその要素の1つを
操作します。もう一度`tree`を実行すれば、何が変わったかがわかります。AIの往復も、シナリオファイル
も、バックエンドごとのインスペクタを覚える手間も要りません。オーサリング中にセレクタが一致しない
事態はよくあります。idの誤記、別の要素による隠蔽、待機後にしか現れない要素などが原因です。今日、
この原因を突き止めるには、候補のシナリオステップを書いて実行し、事後にマニフェストが捕捉した
ツリーを読む必要があります。`repl`は、この一連の作業を、起動中のアプリに対して`tree`と`tap`を
打つだけに縮めます。実装後は、操作者が`bajutsu repl --target <name>`を起動して`tree`を実行し、
見えている要素のidを読みます。そのうち1つを`tap`すれば、画面の変化を手元だけで確かめられます。
この確認は数秒で終わり、runを実行してレポートを読むという一連の流れに取って代わります。

## 詳細設計

`bajutsu repl --target <name> [--udid <id>] [--backend <list>] [--erase/--no-erase]
[--headed/--no-headed] [--browser <engine>] [--config <path>]`は、アプリを起動します。ターゲットの
解決とバックエンドの選択は、`record`がすでに呼んでいる同じ共有CLIヘルパー、
`_load_effective_with_source`と`_select_actuator_or_exit`(`bajutsu/cli/_shared.py`)を再利用
します。デバイスの起動は、`record`と`crawl`が使っているのと同じ`launch_driver`
(`bajutsu/common/runner/launch.py`)を再利用し、`udid`の解決(`playwright`アクチュエータでは省略)
とデバイスの起動を済ませてからドライバを渡します。この起動より前に、`repl`は設定で`launchServer`
が宣言されている場合、`run`・`record`・`crawl`・`audit`がすでに共有している
`_start_launch_server_or_exit`ヘルパー(`bajutsu/cli/_shared.py`)を通じて、ターゲット自身のサーバ
を起動します。停止は、`record`や`crawl`と同じく`atexit`で行います。この手順がなければ、
`baseUrl`を`launchServer`から供給するWebターゲットでは、ブラウザは何も待ち受けていないホストを
開いてしまい、`tree`はどれもエラーページの要素を読むことになります。`--headed`/`--no-headed`と
`--browser`はWebバック
エンド専用で、`record`・`crawl`・`run`が共有する`_with_headed`と、`record`・`run`が使う
`_resolve_browser`(`bajutsu/cli/_shared.py`)を再利用します(`crawl`に`--browser`はありません)。
この2つは、このシェルにとって特に重要です。headlessな
ブラウザのままでは、操作者が変化を確かめる画面そのものがありません。起動すると、`repl`は解決した
バックエンドとターゲットを表示し、続けて`bajutsu>`というプロンプトを出します。

v1のコマンドは、id中心の小さな集合にとどめます。

| コマンド | 動作 |
|---|---|
| `tree [--json]` | ドライバが`SettledReadProvider`を実装していれば(adb)`driver.settled_query()`を、そうでなければ`driver.query()`を呼び、`id`・`label`・`traits`・`value`・`frame`の表(またはJSON)として表示します |
| `find <substring>` | 同じツリーのうち、`id`または`label`に`<substring>`を含む行だけに絞り込みます |
| `tap <id>` | `driver.tap({"id": "<id>"})`を呼びます |
| `type <id> <text>` | `<id>`をタップしてフォーカスしてから、`driver.type_text("<text>")`を呼びます |
| `back` | `driver.back()`を呼びます |
| `screenshot [path]` | `driver.screenshot(path)`を呼びます。`path`省略時は自動で名前を付けます |
| `help` | 上記のコマンド一覧を表示します |
| `exit` / `quit` | シェルを終了します。ローカルなSimulator・デバイス側のアプリ(`xcuitest`・`adb`)はそのまま起動状態を保ちますが、このプロセス自身が所有するセッションは、対応する`Environment.teardown`と同じ呼び出しで終了します。Webバックエンドのブラウザは`cast(base.BackendLifecycle, driver).close()`(`WebEnvironment.teardown`と同じ呼び出しで、`Driver`自体は`close()`を宣言していません)、`--udid https://…`のライブ経路では`XcuitestLiveEnvironment.teardown`が削除するWebDriverセッション(放置すればグリッド上に予約されたまま期限切れまで残ります)です |

ツリーを読む操作、あるいはツリーに対して解決するすべてのコマンドは、`run`の各ハンドラと同じ
やり方で、アクチュエーション用の読み取りを求めます。ドライバが実装していれば
`SettledReadProvider.settled_query()`(`bajutsu/common/drivers/base/settled_read_provider.py`)を、
そうでなければ、そのドライバ自身の読み取りがすでに十分な`query()`を使います。read-lagバリア
([BE-0332](../../roadmaps/BE-0332-read-lag-barrier/BE-0332-read-lag-barrier-ja.md))は、adbで
`settled_query()`を安全にする土台であり、`tap`の直後に実行した`tree`が、操作前の古い
スナップショットを読んでしまう事態を防ぎます。`resolve_unique`がすでに持つ、「0件または複数件の
一致は失敗させる」という契約は、`run`が出すのと同じ`ElementNotFound`・`AmbiguousSelector`という
メッセージで、そのコマンドを即座に失敗させます。どちらの経路も、操作者の意図を推測しません
(prime directive 2、決定性優先)。`tap`は、`run`自身が使う`_tap_with_recovery`
(`bajutsu/common/orchestrator/actions/handlers/gestures.py`)を経由せず、`driver.tap()`を直接
呼びます。そのため、別の要素に覆われたターゲットに対しては、`run`ならまず範囲を区切った
スクロールを試みて成功するところを、`repl`では`ElementNotTappable`を送出します。これは意図した
v1の割り切りであり、こっそり回避すべき不具合ではありません(検討した代替案を参照)。

`tap`と`type`は、要素を`id`だけで指定します。これは、`run`が受け付ける完全な
[セレクタ](../../docs/ja/glossary.md#シナリオのオーサリング)構文(`id`・`idMatches`・`label`・
`labelMatches`・`traits`・`value`・`within`・`index`)より、意図的に狭い範囲です。この絞り込みには
理由があります。v1をレビューしやすい小ささに保てる点と、シェルの実際の使い方に合っている点です。
`tree`はすでに各要素の`label`と`traits`を表示するので、操作者はその行を読んで、idを入力するだけで
済みます。`id`を持たない要素も、完全なセレクタなら`label`や`traits`で指定できますが、id単独の
v1ではそこまで届きません。ツリーに要素そのものが現れない場合(no-idのアプリでタブバーの個々の
タブは現れないことが多い)は、別の問題であり、`record`のvisionフォールバックが埋める役目です
(`docs/recording.md`)。`repl`はそれを行いません。visionを足すと、AI呼び出しを避ける目的で作った
ツールに、AI呼び出しを呼び戻すことになるからです。`tap`と`type`を、残りの`Selector`のフィールド
(`idMatches`・`label`・`labelMatches`・`traits`・`value`・`within`・`index`)に広げる作業は、シェル
本体がリリースされたあとの、別スコープの自然な拡張です。

ジェスチャ(`swipe`・`scroll`・`pinch`・`rotate`)と、プラットフォーム固有の操作
(`set_picker_value`・`select_option`)は、v1の対象外とします。`tap`・`type_text`・`back`・
`screenshot`は、セレクタを確かめたり、手で1つの流れをたどったりするときに、操作者がまず使う操作
をひととおり満たします。残りの`Driver`のメソッドは、コマンド解析の形が固まったあとであれば、
素直に追加できます。すべてを一度に追加すると、この項目は、1回でレビューできる変更の範囲を
超えてしまいます。

`repl`は、`record/`・`crawl/`と並ぶ、独立した最上位パッケージ`bajutsu/repl/`に置きます
(`docs/architecture.md`のモジュール一覧に行を1つ追加するので、`make lint-module-map`は通り
続けます)。コマンド解析、`tree`・`find`の表示、`ElementNotFound`・`AmbiguousSelector`・
`ElementNotTappable`の各経路には、`run`の各ハンドラと同じように`FakeDriver`を使った高速スイート
のテストを付けます。そのため、新しいモジュールは最初のPRから
`coverage-floors.json`のファイル単位のフロアを満たし、あとから追いかける必要がありません。

## 検討した代替案

- **プラットフォーム固有のツール(Xcode の Accessibility Inspector、ブラウザの開発者ツール)で
  ツリーを読む。** 却下しました。各ツールはバックエンドごとに異なり、その結果は、Bajutsuの他の
  部分がツリーを読むのに使う、バックエンドに依存しない唯一の
  [`Driver`](../../docs/ja/glossary.md#driver-backend-actuator-platform)という接点を通りません。
  しかも、どのツールも、Bajutsuのセレクタが実際に照合する正規化された`id`・`label`・`traits`
  フィールドを表示しません。そこで読んだidが、`run`が解決するidと一致する保証はありません。
- **新しいコマンドの代わりに、`serve`のAuthorライブステップピッカーを使う。** この項目では
  却下しました。`/api/capture/start`・`/api/capture/resolve`
  ([BE-0262](../../roadmaps/BE-0262-serve-author-live-step-picker/BE-0262-serve-author-live-step-picker-ja.md))
  は、AI呼び出しなしにライブなドライバを起動し、画面上のクリックをセレクタへ解決する機能を、
  すでに提供しています。ただし、それはシナリオに足す1ステップを選ぶために作られたブラウザの
  ピッカーを通してです。打ったidが解決するかどうかには答えず、端末でidを打ってその場で操作する
  経路もありません。ブラウザのタブとエディタのどちらも開かずに済ませたいという、この項目が動機
  とする、より狭い需要には答えません。
- **`repl`の`tap`を、`run`の`_tap_with_recovery`経由にする。** v1では却下しました。
  `base.raise_if_covered`が送出する`ElementNotTappable`を、覆っている要素の名前ごとそのまま
  見せるほうが、セレクタを確かめている最中にはより役立つ答えになります。回復を望む操作者は、
  シナリオ側で明示的な`scroll`ステップを書き、そこで`tap`すればよいだけです。代償は、`repl`が
  `run`なら回復する場面で失敗を報告することです。覆われたターゲットに対して、両者の答えが
  食い違いうる、という点は、詳細設計で述べたとおりです。
- **新しいコマンドの代わりに、`record`に「手動モード」フラグを足す。** 却下しました。`record`の
  ループは、スクリーンショットから操作を提案する`ClaudeAgent`を中心に組まれており、必ずシナリオ
  の書き出しで終わります。そこに人間が打つコマンドの経路を継ぎ足すと、AI駆動の経路と非AIの経路が
  1つのモジュールの中で絡み合います。`repl`は意図してシナリオを書き出さないツールなので、コマンド
  を分けたほうが両方とも読みやすく保てます。
- **最初から`tap`・`type`に完全なセレクタ(`label`・`labelMatches`・`index`)を受け付けさせる。**
  v1では見送り、id単独の指定にとどめました(詳細設計を参照)。この小さい範囲でも、この項目の動機
  となった問いにはすでに答えられます。しかも、コマンドライン上でのセレクタ構文の解析まで一度に
  決める変更より、1回でレビューできる変更として収まります。
- **標準入力やファイルからコマンド列を読む、非対話的なモード。** この項目では見送りました。対話
  シェル単体で、まずコマンドの集合を検証できます。バッチモードは、そのコマンド集合が固まった
  あとの、別スコープの拡張です。

## 進捗

> 開発の進行に合わせて常に最新の状態に保ってください。チェックリストは *詳細設計* の MECE な
> 作業分解（作業の単位ごとに 1 つ）に対応し、ログには変更内容と時期（古い順）を PR へのリンクと
> ともに記録します。

- [ ] 新しい`bajutsu/repl/`パッケージでの`bajutsu repl`コマンドの土台。
  `_load_effective_with_source`・`_select_actuator_or_exit`・`_start_launch_server_or_exit`・
  `launch_driver`の再利用(launchサーバは`atexit`で停止)、
  `--headed`・`--no-headed`・`--browser`、`bajutsu>`プロンプトのループ、`help`・`exit`・`quit`
  (終了時のWebバックエンドの`cast(base.BackendLifecycle, driver).close()`と、
  `--udid https://…`のライブ経路での`XcuitestLiveEnvironment.teardown`によるWebDriverセッション
  終了を含みます)。
- [ ] `tree`・`tree --json`・`find <substring>`。`settled_query()`・`query()`とread-lagバリアの
  経路を再利用します。
- [ ] `tap <id>`・`type <id> <text>`。`run`と同じ形で`ElementNotFound`・`AmbiguousSelector`・
  `ElementNotTappable`を表示します。
- [ ] `back`・`screenshot [path]`。
- [ ] `FakeDriver`を使った高速スイートのテスト。コマンド解析、`tree`・`find`の表示、
  `ElementNotFound`・`AmbiguousSelector`・`ElementNotTappable`の各経路を対象にします。
- [ ] `docs/cli.md`と`docs/ja/cli.md`のリファレンス節。あわせて、`repl`によって古くなるCLIの一覧
  (`docs/glossary.md`と`docs/ja/glossary.md`のCLI動詞の表、`docs/architecture.md`のコマンド一覧
  ([BE-0113](../../roadmaps/BE-0113-design-doc-realignment/BE-0113-design-doc-realignment-ja.md)))
  と、`bajutsu/repl/`自身の`docs/architecture.md`モジュール表への行(`make lint-module-map`)も
  更新します。

## 参考

- [`Driver`](../../docs/ja/glossary.md#driver-backend-actuator-platform)プロトコル —
  `bajutsu/common/drivers/base/driver.py`
- [`Selector`](../../docs/ja/glossary.md#シナリオのオーサリング) —
  `bajutsu/common/scenario/models/selector.py`
- `SettledReadProvider` — `bajutsu/common/drivers/base/settled_read_provider.py`
- `BackendLifecycle` — `bajutsu/common/drivers/base/backend_lifecycle.py`
- `_start_launch_server_or_exit` — `bajutsu/cli/_shared.py`
- `XcuitestLiveEnvironment` — `bajutsu/common/platform_lifecycle/environments/xcuitest_live.py`
- `record`と`crawl` — `repl`が隣に位置する、既存の2つのTier 1オーサリング経路(`docs/cli.md`)
- [BE-0332 — read-lagバリア](../../roadmaps/BE-0332-read-lag-barrier/BE-0332-read-lag-barrier-ja.md)
- [BE-0262 — Author エディタにライブなステップ選択と target 単位に絞った run を導入する](../../roadmaps/BE-0262-serve-author-live-step-picker/BE-0262-serve-author-live-step-picker-ja.md)
