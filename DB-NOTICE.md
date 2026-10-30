# BeatOctant 配布 DB の利用条件とクレジット

OctantHarvest が作る BeatOctant の配布 DB（マニフェスト、表、ビューのベクトル、校正ファイル）は、[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) で提供します（2026-10-12）。
非営利で使い、改変したものを配るときは同じ条件にし、この文とビューごとのクレジットを残してください。

The BeatOctant distributed DB is licensed under CC BY-NC-SA 4.0. Keep this notice and the credits of each view.

## DB 全体のクレジット

| もの | 使い方 | 条件 |
| --- | --- | --- |
| [BeatSaver](https://beatsaver.com/) | マップとメタ情報（表）の出所 | BeatSaver の利用規約 |
| [BeatLeader analyzer](https://github.com/BeatLeader/beatleader-analyzer)、[JoshaParity](https://github.com/Joshabi/JoshaParity) | 譜面の表の Pass・Tech（OctantVector のスイングシミュレーションに移植した計算式で、マップから計算） | MIT |

## ビューごとのクレジット

モデル（CLAP、MERT、Demucs など）、移植した計算式（BeatLeader の analyzer、JoshaParity）、近いアーティスト（MusicBrainz、ListenBrainz）のクレジットは、それぞれのビューが持ちます（マニフェストのビューの `credits`）。OctantCompass の詳細設定に、DB に入っているビューの分が出ます。

NC（非営利）は MERT（CC BY-NC 4.0）と BeatOctant を非営利とした決定から、SA（継承）は MusicBrainz のタグと評価（CC BY-NC-SA 3.0）から来ます。決めた経緯は OctantSpecs の open.md にあります。WavLM（CC BY-SA 3.0）の歌声のビューは、SA 3.0 の継承の条件と DB の NC が両立しないおそれがあるので、公式の DB には入れていません（2026-10-23）。

この文は `release` が DB のマニフェストの `notice` に書くので、DB を配るときに別に添える必要はありません。
