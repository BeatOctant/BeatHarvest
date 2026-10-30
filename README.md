# OctantHarvest

> **To use BeatOctant** (install, known issues): [Getting Started](https://github.com/BeatOctant/OctantSpecs/blob/main/GettingStarted_en.md) · [はじめかた（日本語）](https://github.com/BeatOctant/OctantSpecs/blob/main/GettingStarted_ja.md)
>
> BeatOctant is written mostly with AI coding agents (Anthropic's Claude). The maintainer decides what to build and reviews every change. It does not generate maps; it recommends existing maps from BeatSaver. ([作り方（AI の使い方）](https://github.com/BeatOctant/OctantSpecs/blob/main/README.md#作り方ai-の使い方))
>
> OctantHarvest builds the distributed vector DB of BeatSaver maps for BeatOctant, a Beat Saber map recommender (the OctantPath mod for Beat Saber PC and OctantPath for Quest).
>
> BeatOctant is an unofficial fan project for Beat Saber. It is not affiliated with, endorsed by, or sponsored by Beat Games or Meta. Beat Saber and Meta Quest are trademarks of their respective owners. Beat Games・Meta とは関係のない非公式の MOD・ツールです。
>
> 報告と案は Discord の BeatOctant サーバー（バグは `#bug-reports`、案は `#feedback-ideas`）で受けます。外からの pull request は受けていません。

配布するベクトル DB を作る側のホストです。BeatSaver の定期的な収集、OctantVector での自動の埋め込み、公開を受け持ちます。OctantCompass が推薦のために OctantVector を包むのと対になる部品です。

仕様の文書はすべて [OctantSpecs](https://github.com/BeatOctant/OctantSpecs) にあります。このリポジトリにはセットアップと使い方だけを書きます。

- 責務の境界: [憲法の原則 4b](https://github.com/BeatOctant/OctantSpecs/blob/main/constitution.md#4b-octantharvest-は配布-db-を作る側のホスト)
- 収集結果の形（OctantVector が読むもの）: [契約4（OctantHarvest crawl to OctantVector）](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/generation-input.md)
- OctantVector の生成側の関数: [契約1（OctantHarvest to OctantVector）](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/engine-generation.md)
- BeatSaver への接し方: [外部依存](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/external.md)

- 設定ファイルの項目: [octantharvest/config.md](https://github.com/BeatOctant/OctantSpecs/blob/main/octantharvest/config.md)

## 組み合わせる版

支えるのは、各部品の最新の版どうしの組み合わせだけです（[decisions.md の全体と段階](https://github.com/BeatOctant/OctantSpecs/blob/main/decisions.md#全体と段階)、2026-10-22）。この README を書いた時点の相手は次のとおりです。

| 相手 | 版 | つなぐもの |
| --- | --- | --- |
| OctantVector | v0.12.1（`pyproject.toml` の範囲は `>=0.12.0,<0.13`） | [契約1 生成側（OctantHarvest → OctantVector）](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/engine-generation.md) 1.1、[契約4（OctantHarvest の収集結果 → OctantVector）](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/generation-input.md) |
| OctantVectorPlugins | main（`PLUGIN_API` 0.3。OctantVector 0.8 系のもの） | OctantVector の `octantvector.plugin` |
| OctantCompass（作った DB を読む側） | 最新 | [契約2（DB 生成 ↔ 推薦コア）](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/vector-db.md) 1.1、[契約5（OctantHarvest → 配布先 → OctantCompass）](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/distribution.md) 1.1 |

## セットアップ

Python 3.11.4 以上（OctantVector の下限）を使います。OctantVector は、OctantVector のリポジトリの版のタグから入れます（[契約1 の版の付け方](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/engine-generation.md#版の付け方)）。音や画像のモデルを使うビュー（CLAP、MERT、WavLM、Whisper、SigLIP）は OctantVector 本体になく、[OctantVectorPlugins](https://github.com/BeatOctant/OctantVectorPlugins) のプラグインで足します。歌声のビュー（`vocal_` で始まるもの）には `[vocal]`（Demucs）も要ります。

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install "octantvector[embed,vocal] @ git+https://github.com/BeatOctant/OctantVector@v0.12.1"
```

公式の DB のビューを作るには、4 つのプラグインを入れます。使わない系統は入れなくて構いません。WavLM（`octantvector-wavlm`）の歌声のビュー `vocal_wavlm_sv` は、モデルが CC BY-SA 3.0 で、継承の条件が DB の非営利の条件と両立しないおそれがあるので、公式の DB には入れません（2026-10-23）。

```bash
.venv/bin/pip install "octantvector-clap[vocal] @ git+https://github.com/BeatOctant/OctantVectorPlugins#subdirectory=clap"
```

```bash
.venv/bin/pip install "octantvector-mert[vocal] @ git+https://github.com/BeatOctant/OctantVectorPlugins#subdirectory=mert"
```

```bash
.venv/bin/pip install "octantvector-whisper @ git+https://github.com/BeatOctant/OctantVectorPlugins#subdirectory=whisper"
```

```bash
.venv/bin/pip install "octantvector-siglip @ git+https://github.com/BeatOctant/OctantVectorPlugins#subdirectory=siglip"
```

```bash
.venv/bin/pip install -e ".[dev]"
```

設定は `octantharvest.json`（または環境変数 `OCTANTHARVEST_SETTINGS` の指すファイル）に書きます。書かなかった項目は既定値になります。既定値は、パッケージに同梱した [`src/octantharvest/defaults.json`](src/octantharvest/defaults.json) にあります（`crawl.catalogue`・`crawl.download` の数値は `null` で、[スクリプトの既定値](#収集のスクリプトを直接使う)のまま）。相対パスは設定ファイルのフォルダからの位置です。

```json
{
  "paths": {"catalogues": "/data/beatsaver/catalogue", "archive": "/data/beatsaver/archive",
            "store": "/data/octantvector/store", "releases": "/data/octantvector/releases",
            "publish": "/data/octantvector/publish", "models": "/data/octantvector/models"},
  "views": ["meta_tags@2", "chart_patterns@2", "clap_htsat_unfused@1", "clap_concepts@1", "mert_v1_95m@1",
            "vocal_mert_v1_95m@1", "vocal_clap_htsat_unfused@1", "whisper_language@1", "siglip_cover@1",
            "beatsaver_mappers"],
  "presets": "/path/to/OctantHarvest/presets/official.json",
  "notice": "/path/to/OctantHarvest/DB-NOTICE.md",
  "octantvector": {"workers": 12, "device": "auto"},
  "crawl": {"artists": {"userAgent": "OctantHarvest/0.1 ( me@example.org )"}},
  "signing": {"key": "~/.config/octantharvest/signing.pem"},
  "publish": {"url": "https://example.org/octantvector-db/"}
}
```

`views` は DB に入れるビューの一覧で、必ず書きます（OctantVector 側の既定の一覧はありません）。`daily` と `first-release` は、どちらもこの一覧を計算して DB に入れます。`beatsaver_mappers` は計算するビューではなく、版を作るたびにカタログの譜面の作者（BeatSaver のアカウント ID）から作る関係のビューです。

```bash
.venv/bin/octantharvest status
```

`status` の `views` には、設定の各ビューが計算できるかが出ます。`ready` なら OctantVector にそのビューのエンコーダーがあり、読むモデルのファイルも `paths.models` にそろっています。エンコーダーがないとき（プラグインが入っていない、OctantVector の版がプラグインの範囲の外など）はその理由が、モデルが足りないときは足りないファイルが出ます。何も計算せず、モデルも取りに行きません。重い実行の前の確かめに使えます。

## 使い方

最初の 1 回は、収集し、設定の `views` が使うモデルを取ってから、最初の版を作ります。最初の版は全量で、校正もここで決まります。

```bash
.venv/bin/octantharvest crawl
```

```bash
.venv/bin/octantharvest models
```

```bash
.venv/bin/octantharvest first-release
```

`models` は、ビューが読むモデルのファイル（Hugging Face のリポジトリの決まった revision）を `paths.models` の下の `hf/<repo>/<revision>/` に取り、`models.json` に各ファイルの SHA-256 を残します。記録どおりのファイルは取り直しません。記録と違うファイルがあると止まり、黙って置き換えることはしません（消せば取り直します）。OctantVector はモデルを取りに行かず、ここからだけ読みます。`first-release` と `daily` も最初（`daily` は収集の前）に同じことをするので、ビューを足したときのモデルもそこで取れます。OctantVector が計算できないビューがあれば、収集や計算の前にそこで止まります。

2 回目からは、毎日の実行をスケジューラから呼びます。モデルの確認、収集、足りない分の計算、校正のずれの確認、次の版の書き出しまでをして、`<releases>/daily.jsonl` に 1 行ずつ結果を残します。ずれが `recalibrateDrift` を超えたビューがあれば、設定の `views` を校正し直し、その版は全量になります。公開はしません。OctantVector が頼まれたとおりにできなかったとき（ビューの名前の誤り、モデルがない、手札が DB で使えないなど）は、その理由を `daily.jsonl` にその回のエラーとして残し、トレースバックなしで止まります。GPU のメモリが足りないとき（VR のサーバーなど、ほかのプログラムが使っているとき）は、OctantVector が一度に計算するマップを減らしてやり直します。1 件でも入らないマップが残ったら、版を書かずに止まり、`daily.jsonl` にその件数を残します。そのマップは失敗とは記録されないので、GPU を空けてから `daily --no-crawl`（`first-release` のときはもう一度 `first-release`）を流せば計算して版を書きます。

```bash
.venv/bin/octantharvest daily
```

cron なら、たとえば毎日 4 時に動かします。

```text
0 4 * * * cd /data/octantharvest && .venv/bin/octantharvest daily >> daily.log 2>&1
```

収集（`crawl` と `daily`）は、毎回 BeatSaver のカタログ全体をたどり直しはしません（2026-10-14）。いちばん新しい完了した取得を起点に、その後に更新・追加されたマップ（`sort=UPDATED`）と削除されたマップ（`/maps/deleted`）だけを取って起点のカタログに当て、同じ形の完全なカタログを作ります。投票ではマップの `updatedAt` が変わらないので、最後に `GET /vote` で、投票のあったマップの upvote・downvote・score を直します。未完了や失敗した取得は起点にしません。`crawl.keepCaptures` で古い取得を消すときも、次の起点になるいちばん新しい完了した取得は残ります。

カタログ全体をたどるのは、次のときだけです。プレイ数などほかの統計と、`updatedAt` の変わらない変化は、このときに直ります。

- 完了した取得がまだないとき
- 起点がもとにしている最後の全体の取得から、`crawl.fullWalkDays` 日（0 なら自動ではたどらない）たったとき
- 直すために `crawl --full` を実行したとき

```bash
.venv/bin/octantharvest crawl --full
```

`--full` は、その日の取得がまだないときだけ動きます。その日の取得があるときは、そのフォルダを消してから実行します。途中で止まった取得は、`--full` なしの `crawl` で、始めたときのやり方のまま続きから取れます。起点にするいちばん新しい完了した取得のカタログが、その `manifest.json` の記録と合わないときは、その日の取得を始める前に止まります。そのフォルダを消せば一つ前の完了した取得から、`crawl --full` なら全体をたどって取ります。

書き出した版は、確かめてから承認します。承認すると、その版の配る方（差分か全量）に `signing.key` の鍵で署名します（[契約5（OctantHarvest to OctantCompass）の署名](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/distribution.md#署名)）。承認した版だけが索引に載ります。

開発中は設定の `signing.allowUnsigned` を `true` にすると、鍵なしで承認でき、署名のない版も索引に載ります（受け取る OctantCompass 側でも署名なしを受け入れる設定が要ります）。

署名の鍵は最初に 1 回だけ作ります。秘密鍵はリポジトリに入れません。表示された公開鍵（`publicKey`）を設定の `signing.publicKey` に書くと、`publish` はその鍵で署名した版を索引に載せます。書かないときに載るのは、OctantVector が同梱する鍵（メンテナーの鍵）で署名した版だけです。

```bash
.venv/bin/octantharvest keygen
```

```bash
.venv/bin/octantharvest approve 2026.10.03
```

```bash
.venv/bin/octantharvest publish
```

### 推薦の手札

公式の配布 DB に入れる推薦の手札（OctantPath に出るボタン）は `presets/official.json` です（[プリセットの 8 節](https://github.com/BeatOctant/OctantSpecs/blob/main/plans/presets.md#8-手札の定義を配布-db-に移す2026-10-10)）。設定の `presets` にこのファイルの場所を書くと、`release` が DB で使えるかを確かめてマニフェストに書きます。使えない手札があれば、その版は書かれずに止まります。

```json
{"presets": "/path/to/OctantHarvest/presets/official.json"}
```

### 利用条件とクレジット

DB 全体の利用条件とクレジットは [DB-NOTICE.md](DB-NOTICE.md) です。設定の `notice` にこのファイルの場所を書くと、`release` がマニフェストの `notice` に書きます（書かなければ前の版のものを引き継ぎます）。モデルや移植したコードなど、ビューごとのクレジットは各エンコーダが持ち、マニフェストのビューの `credits` に自動で入ります。どちらも DB の中を渡るので、DB に別のファイルを添える必要はありません。OctantCompass の詳細設定で読めます。

```json
{"notice": "/path/to/OctantHarvest/DB-NOTICE.md"}
```

手札の重みは、ビューの id（`clap_htsat_unfused` のように版なし、または `id@版`）で書きます。役割の名前（`music` など）は書けません（2026-10-14）。OctantVector 同梱の既定の手札は重みのない「すべて」だけで、それ以外の手札は公式の手札にしかありません。「似た曲」「似たマップ」「挑戦」は、選んでいる譜面がなくても使えます（そのときはプレイ履歴が基準です。2026-10-14）。

「好みのジャンル」は、語のビュー（`clap_concepts`）の 58 語のうち音で当たると分かった 9 語（OctantSpecs の spike R1）から、好きなマップが平均で一番近い語を OctantVector が選び、その語に近いマップの中から好みの音に近い曲を出します（`genreFrom`、OctantVector 0.10.0）。`clap_concepts` を DB に入れたときだけ出ます。語を選んで探す手札（ジャンル、ジャンルで似た曲）は、結果が安定しなかったので外して、これに入れ替えました（2026-10-29。聴き比べは OctantVector の spikes/2026-10-29-genre-view）。

「連想でたどる」は、起点に似た曲を 10 曲出したら、まだ出していない中で起点に一番近い曲を次の起点にして探し直します（`chainEvery`、OctantVector 0.11.0）。次の起点にした曲そのものは出しません。似た曲より遠くまで行くかわりに、好きなマップに当たる数は減ります（測定は OctantVector の spikes/2026-10-27-implicit-seeds）。

表示名（`label`・`group`・`hint`、入力と選択肢の `label`）は `{"en": …, "ja": …}` の形で英語と日本語を持ちます。どちらを出すかは OctantPath が選び、英語が既定です（2026-10-11）。英語は推測で付けたので、見直す余地があります。「すべて」「おすすめ」「久しぶり」の英語（All、Recommended、Revisit）は推測ではなく決めたものです（2026-10-11）。

音の重みも手札で決めています。「似た曲」と「好みのジャンル」は CLAP と MERT（`mert_v1_95m`）を半々に、「すべて」「おすすめ」「挑戦」「久しぶり」は音を CLAP だけにしています（開発に使った 1 人分のプレイ履歴での評価、2026-10-10）。そのため、公式の手札で版を作るには `views` に `mert_v1_95m` も要ります。

「似た曲（言語・カバー・作者）」は、「似た曲」と比べるために別の手札として足したものです（2026-10-20）。曲調（`clap_htsat_unfused`・`mert_v1_95m` を 0.5 ずつ）と近いアーティスト（0.5）に、カバー画像の近さ（`siglip_cover` 0.25）、歌の言語の一致（`whisper_language` 0.175）、譜面の作者の一致（`beatsaver_mappers` 0.175）を足します。upvote が 2 以下のランダムな起点 300 件の上位 10 件で、「似た曲」よりジャンルのタグの一致が 0.176 から 0.247 に、日本語の起点で日本語の曲が 62% から 93% に上がりました（Spike、2026-10-20）。公式の手札で版を作るには、この 3 つも `views` に要ります。

「曲調と歌声が似た曲」は、「似た曲」と同じ曲調のビュー（`clap_htsat_unfused`・`mert_v1_95m`）と、歌声の 2 つのビュー（`vocal_mert_v1_95m`・`vocal_clap_htsat_unfused`。曲から歌声を分離して埋め込んだもの）の両方で近い曲を出します（つまみ `all`、OctantVector 0.12.0）。曲調の近さと歌声の近さをそれぞれ全マップの中の順位（百分位）に直し、低いほうで並べるので、片方だけ極端に近い曲は上に来ません。足し算（`weights` で 3・3・3・3）だったときは、難易度などで候補が絞られるほど片方だけ近い曲が混ざっていました（上位 30 件のうち、候補が 3% のとき 3.8%、0.5% のとき 11.3%。`all` では 0.1% と 3.0%。OctantVector の spikes/2026-10-30-all-views）。歌声のビューを使うのは、全件の評価（ボーカルのある 100,598 マップ、近い 10 件）で、同じアーティストの割合が曲調だけの 14.87% から 15.64% に上がり、ボーカルだけで探すより曲の雰囲気が崩れにくかったためです（2026-10-11。このときは歌声に `vocal_wavlm_sv` も入れていました。WavLM はモデルの条件から公式の DB に入れないことにしたので外し（2026-10-23）、2 つでの評価はまだしていません）。公式の手札で版を作るには、この 2 つも `views` に要ります。計算には OctantVector の `vocal` の extras（Demucs）と、各プラグインの `[vocal]` が要ります。声のない曲は出ません。種の曲に声がなければ、歌声の組は使えなかったものとして返され、曲調だけで並べます。

`publish` は `<publish>` に、アップロードするファイル（大きいものは分割）と契約5 の索引 `index.json` を書きます。アップロードは手で行います。

公開（Dev 版）の DB は、索引を使わずにこの tar で配ります。承認した版の全量を tar 1 つに書き出します（`<publish>/handoff/<dbVersion>.full.tar`）。差分で配る版は全量にまだ署名がないので、このとき全量にも署名します。受け取った人は、OctantCompass の「ファイルから取り込む」でこの tar を選びます（[契約5](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/distribution.md)）。

```bash
.venv/bin/octantharvest handoff 2026.10.03
```

### 近いアーティスト

「似た曲」に近いアーティストを入れるため、マップのアーティストを MusicBrainz で引き、その似たアーティストを ListenBrainz から取ります（[OctantSpecs の近いアーティスト](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/vector-db.md#ビューの種類)）。どちらも 1 秒に 1 回までです（設定の `crawl.artists.minDelay` を 1 秒より短くすると始まりません）。MusicBrainz は User-Agent に連絡先を求めるので、設定の `crawl.artists.userAgent` に連絡先（メールアドレスか公開の URL）の入った User-Agent を書きます。書いていないと始まりません。上限はサービスごとなので、2 つを並べて同時に引きます。応答を待つあいだも次のリクエストを始めます（同時に待つのは `crawl.artists.inflight` 本まで。始める間隔は 1 秒以上のまま）。1 回の実行は、それぞれ `crawl.artists.budget` 回のリクエストで止まり、次の実行が続きから取ります。マップの多いアーティストから取ります。結果は `paths.artists` に残ります。

```bash
.venv/bin/octantharvest artists
```

`first-release` と `daily` は、OctantVector を呼ぶ前にキャッシュから OctantVector の入力（`paths.artists` の `map_artists.jsonl.gz` と `similar_artists.jsonl.gz`。名前は入れない）を書き出して渡します。OctantVector はそれを関係のビュー `listenbrainz_artists`（版は取得した日付）として DB に入れます。公式の手札の「すべて」「似た曲」「近いアーティスト」はこのビューに重みを付けるので、ビューのない DB では `release` が止まります。書き出しだけをするときは次のとおりです:

```bash
.venv/bin/octantharvest artists --export
```

## 自分の DB を作って配る

OctantHarvest は、だれでも自分のビューと手札で DB を作って配れるようにしてあります。下は索引（`index.json`）で配る手順です。公式の公開（Dev 版）の DB は、`approve` までは同じで、そのあと `publish` の代わりに `handoff` の tar 1 つで配っています（[使い方](#使い方)の `handoff`）。

1. OctantHarvest と OctantVector を入れ（[セットアップ](#セットアップ)）、使うビューのプラグインを入れます。自分で書いたビューも、OctantVector の `octantvector.plugin` を使うプラグインとして足せます（不安定版の API です。OctantVectorPlugins の README）。
2. 設定に、`views`（DB に入れるビューの id）、`presets`（手札のファイル。重みはビューの id で書く）、`notice`（DB-NOTICE.md の場所）、`signing.key`（署名の鍵）、`paths.models`（モデルの置き場所）、`crawl.artists.userAgent`（近いアーティストを使うなら、連絡先の入った User-Agent）を書きます。
3. 順に実行します。`keygen` が表示した公開鍵（`publicKey`）は、設定の `signing.publicKey` に書きます。OctantVector が同梱していない鍵なので、書かないと `publish` が版を索引に載せません。

```bash
.venv/bin/octantharvest keygen
```

```bash
.venv/bin/octantharvest crawl
```

```bash
.venv/bin/octantharvest models
```

```bash
.venv/bin/octantharvest first-release
```

```bash
.venv/bin/octantharvest approve <dbVersion>
```

```bash
.venv/bin/octantharvest publish
```

4. `<publish>` にできたファイルを、好きな場所（GitHub Releases など）に置きます。設定の `publish.url` は、その置き場所の URL です。利用条件とクレジットは DB のマニフェストに入っているので、添えなくてかまいません。
5. 使う人は、OctantCompass の設定の `index_url` をその `index.json` にし、作った人の公開鍵を `trusted_keys` に足します。

### かかる時間と置き場所

最初の 1 回は、公式の DB の作者の PC で、ダウンロードから DB の書き出しまで約 2.5 日かかりました。所要時間は GPU・CPU・回線で大きく変わります。

- CPU: AMD Ryzen 9 9950X3D（16 コア 32 スレッド）
- メモリ: 64 GB
- GPU: NVIDIA GeForce RTX 5080（16 GB）
- OS: Ubuntu 24.04.5 LTS（Linux 7.0.0）
- zip の置き場: 外付けの HDD

下の表は、この PC での 2026 年 10 月、約 14.5 万マップのときの目安です。

| 段階 | 作者の PC での時間 | 主に効くもの |
|---|---|---|
| カタログの取得（`crawl`） | 30 分ほど | BeatSaver API の間隔（1 秒以上） |
| マップの zip のダウンロード（`crawl`） | 約 1 日（約 700 GB） | 回線と、BeatSaver への同時数・開始数の上限 |
| 近いアーティスト（`crawl`） | 1 回 3,000 件ずつで、そろうまで毎日回して数週間 | MusicBrainz・ListenBrainz の間隔（1 秒以上） |
| 曲調・歌声・譜面のビューの計算（`first-release`） | 約 9 時間 | GPU（VRAM が少ないと遅くなる）と、音声のデコードの CPU |
| 言語・カバー・作者のビューの計算（`whisper_language`・`siglip_cover`・`beatsaver_mappers`） | 約 9.3 時間 | GPU（ほとんどが Whisper large-v3） |
| DB の書き出しと署名 | 数分 | — |

- GPU が遅い PC では、計算が数倍になることがあります。VRAM 16 GB でもぎりぎりです。足りないときは一度に計算するマップを減らして続けるので遅くなり、それでも入らないマップがあれば版を書かずに止まります（上の `daily`）。CPU だけでは、歌声の分離（Demucs）があるので現実的ではありません。
- zip は消さずに HDD に置いておくのを勧めます。ビューを足したり作り直したりするとき、ダウンロードなしで計算だけで済みます。読み出しの速さは普通の HDD で足ります。
- 2 回目からは変わった分だけを取って計算するので、毎日の更新は数分です。

## 収集のスクリプトを直接使う

`crawl` と `daily` は、次の 2 つのスクリプトを設定の数値で呼びます。単独でも動きます。

### カタログの取得

BeatSaver の公開メタデータ API から全マップのカタログを取ります。ログインは使わず、マップの zip もダウンロードしません。リクエストは直列で、429 には `Retry-After` に従って待ちます。止めても、同じコマンドをもう一度実行すると続きから再開します。

```bash
python3 src/octantharvest/crawl/beatsaver_catalogue_capture.py self-test
```

```bash
python3 src/octantharvest/crawl/beatsaver_catalogue_capture.py capture --output ./beatsaver-catalogue-capture
```

少しだけ試すときは `--max-pages 3` を付けます。この場合はわざと未完了のまま止まり、`manifest.json` は作りません。

前の完了した取得を `--base` に渡すと、全体をたどらずに、その後に変わった分だけを取ります（差分。`crawl` と `daily` はこちらを使います）。

```bash
python3 src/octantharvest/crawl/beatsaver_catalogue_capture.py capture --output ./capture-2 --base ./beatsaver-catalogue-capture
```

取得は次の段階で進みます。`status` で状態を見られます。

- `base_created`: `sort=CREATED` で、取得を始めた時刻より前に作られた全マップをたどる。差分では、この代わりに `--base` の取得のカタログから始める
- `delta_updated`: 取得中（差分では、起点の取得の `snapshotCutoff` の 1 時間前から）に更新・新規作成されたマップを足す
- `delta_deleted`: `delta_updated` の終わりまでに削除されたマップを除く
- `votes`: `GET /vote` で、取得を始めた時刻（差分では起点の取得を始めた時刻）の 1 時間前から投票のあったマップの upvote・downvote・score を直す。投票では `updatedAt` が動かないため

`createdAt` を軸にするのは、長い取得の間にマップが更新されても、ページの位置がずれて取りこぼさないためです。API のカーソルには時刻が同じときの順番がありません。`delta_updated` と `delta_deleted` は、100 件ちょうどのページのあと、その末尾の時刻の 1 マイクロ秒前から取り直すので、時刻の同じマップを取りこぼしません（同じ時刻のマップが 1 ページ 100 件を埋めるときは、その時刻の後へ進み、`manifest.json` に警告を残します）。`base_created` では、100 件ちょうどのページの末尾で時刻が重なったときに警告を残します。差分の取得は起点の取得のカタログを読むので、`build` で作り直すには起点の取得が残っている必要があります。途中で止めた取得は、始めたときのやり方（全体か、どの起点からの差分か）のまま再開します。

```bash
python3 src/octantharvest/crawl/beatsaver_catalogue_capture.py status --output ./beatsaver-catalogue-capture
```

最後まで成功したときだけ `manifest.json` ができます。検証と、保存済みのページからの作り直しもできます。

```bash
python3 src/octantharvest/crawl/beatsaver_catalogue_capture.py verify --output ./beatsaver-catalogue-capture --require-manifest
```

```bash
python3 src/octantharvest/crawl/beatsaver_catalogue_capture.py build --output ./beatsaver-catalogue-capture
```

出力のフォルダには次のものができます。OctantVector が読むのは `catalogue.jsonl.gz` です。

| ファイル | 中身 |
| --- | --- |
| `catalogue.jsonl.gz` | 削除されていないマップのカタログ。1 行に 1 マップの JSON |
| `tombstones.jsonl.gz` | 削除されたマップの ID と時刻 |
| `manifest.json` | 完了したときだけできる。取得の範囲、ページの証拠、ダイジェスト、件数、差分なら起点の取得（`base`） |
| `build-summary.json` | カタログを作ったときの件数とダイジェスト |
| `capture-state.json` | 再開用の状態 |
| `ledger.jsonl` | リクエストごとの URL、カーソル、応答のダイジェスト、件数 |
| `raw/` | 受け取った応答そのもの（gzip） |
| `source/swagger.json.gz` | 取得したときの API の説明 |

### アーカイブのダウンロード

カタログにあるすべての版（`versions[].hash`）の zip を 1 回ずつダウンロードし、`<出力先>/maps/<hash の先頭 2 文字>/<hash>.zip` に置きます。転送の長さ、zip の全メンバーの CRC を確かめてから書き込み、zip の SHA-256 を `download.journal.jsonl` に記録します。止めても、同じコマンドをもう一度実行すると続きから再開します。

```bash
python3 src/octantharvest/crawl/beatsaver_bulk_download.py ./beatsaver-catalogue-capture/catalogue.jsonl.gz /path/to/archive --dry-run
```

```bash
python3 src/octantharvest/crawl/beatsaver_bulk_download.py ./beatsaver-catalogue-capture/catalogue.jsonl.gz /path/to/archive
```

- 一時ファイルは `/dev/shm`（なければ HDD 以外の `/tmp`）に置き、HDD に書くのは 1 つのスレッドだけです
- 一時的なエラー（429、5xx、タイムアウト）は待ってから再試行し、後の回にまわします。404・410・451 は「なし」として記録します
- 403、空き容量の不足など、人が見るべき状態では止まります
- 上にないそのほかの一時的でない失敗（ほかの HTTP のエラー、`--max-file-mib` を超える zip、BeatSaver の外への転送）は「止めた」として記録します。何回続けても後の回にまわる版も「止めた」にします（`--give-up-after-runs`）

「なし」と「止めた」の版は、次の回からは飛ばします（後の回にまわり続けて止めた版だけは、`--recheck-given-up-days` の日数がたつともう一度試します）。`--max-file-mib` を上げたあとなどに取り直すときは、`--retry-blocked`（「止めた」の版）か `--retry-permanent`（「なし」の版）を付けて、このスクリプトを手で動かします。`crawl` と `daily` はこの 2 つを渡しません。

数値（並列数、1 秒あたりの開始数、タイムアウトなど）とその既定値は `--help` に出ます。`crawl` と `daily` は、設定ファイルの `crawl.catalogue`・`crawl.download` に書いた値だけを渡し、書かなかった値はスクリプトの既定値のままです。

## CI

pull request ごとに GitHub Actions がテストを動かします（`.github/workflows/test.yml`）。文書だけの変更では動かず、同じ pull request に続けて push すると前の実行を止めます。CI が入れる OctantVector はワークフローの `OCTANTVECTOR_TAG` のタグで、`pyproject.toml` の依存の範囲に入るものにします。

## 由来

2 つのスクリプトは、開発用の PC にあった既存のものを取り込みました。元のファイルは、作業用の非公開のリポジトリの最初のコミットにそのまま入っています。

| いまの名前 | 元のファイル | 元の sha256 |
| --- | --- | --- |
| `src/octantharvest/crawl/beatsaver_bulk_download.py` | `beatsaver_bulk_download_v0.2.1.py` | `0e641d81b9f2369ef1a4c178d82c558165be4886b45dd5bf3945c49694f30131` |
| `src/octantharvest/crawl/beatsaver_catalogue_capture.py` | `beatsaver_catalogue_capture_v2.py` | `bcd39b4aa453ce7d8bdb6bac6e91eac5b1cbe376e6f189ff4792fc2b5fc8071a` |

取り込んだあとも OctantHarvest の部品として手を入れています（User-Agent の既定値、壊れた zip の扱い、あきらめるまでの回数と再確認、使わない部分の削除など）。何を変えたかは、作業用の非公開のリポジトリの git の履歴にあります（公開の写しには履歴がありません）。

## ライセンス

[MIT](LICENSE)（2026-10-12）。

これはコードのライセンスです。OctantHarvest が作る配布 DB は CC BY-NC-SA 4.0 で、[DB-NOTICE.md](DB-NOTICE.md) に書きました。この文は DB のマニフェストに入り、ビューごとのクレジットと一緒に DB の中を渡ります。
