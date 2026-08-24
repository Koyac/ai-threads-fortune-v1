# ai-threads-fortune

Threads の「ギャル×星座占い」アカウント「藍」を、GitHub Actions と Gemini API だけで完全自動運用するシステム。
サーバー・データベースは使わない。実行のたびにスクリプト自身がこのリポジトリへ commit / push し、
投稿ログや週次の方針もすべてリポジトリ内のファイルとして残る。

詳しい仕様は [要件定義書_v1.md](../要件定義書_v1.md) を参照。

## 必要なもの

- Python 3.12
- GitHub Secrets に登録する3つの値
  - `THREADS_ACCESS_TOKEN`
  - `THREADS_USER_ID`
  - `GEMINI_API_KEY`

## ディレクトリ構成

```
seeds.jsonl                  # 投稿の種(消費キュー)。build_seeds.py で生成
ephemeris.jsonl              # 1年分の天体イベント。build_ephem.py で生成
strategy.md                  # 週次で書き換わる投稿方針(唯一の可変状態)
gyaru_lexicon.md             # 語彙・語尾・禁止表現
gyaru_zodiac_prompt.md       # 「ギャル×星座占い」の生成方針・フォーマット詳細(post.pyが読み込む)
data/posts.jsonl             # 投稿ログ(埋め込みベクトル含む)
data/metrics.jsonl           # 日次メトリクスのスナップショット
scripts/
  build_seeds.py             # 【初回のみ】投稿の種を生成
  build_ephem.py             # 【初回のみ】天体イベントを生成
  post.py                    # 【日次・1日2投稿】投稿する(起動は3回。3回目はその日の取りこぼし回収)
  collect.py                 # 【日次・1回】メトリクスを収集する
  aggregate.py                # 【週次】LLMを使わずに集計する
  review.py                  # 【週次】集計結果をもとにAIが方針を更新する
.github/workflows/
  daily.yml                  # 日次スケジュール(post.py / collect.py)
  weekly.yml                 # 週次スケジュール(aggregate.py -> review.py)
```

## 1日2投稿を落とさないための仕組み

- **起動は1日3回**(11:45 / 21:45 / 23:40 JST)。post.py はその日の投稿数を数えてから動くので、
  余分に起動しても二重投稿にはならず、失敗した枠だけを後から回収できる。
- 前の枠を取りこぼしていた場合は、1回の実行で最大2本まとめて投稿して追いつく。
- 生成した本文が**500文字(Threadsの上限)を超えていたら投稿せずに作り直す**。
  最後まで収まらない場合だけ、自然な切れ目で自動的に切り詰めてから投稿する。
- 過去の投稿と似すぎていた場合も、以前のように種を捨てて0本で終わらせず、
  候補の中で一番似ていないものを採用して必ず1本投稿する。
- Gemini が 503(混雑)を返したときは待って再試行する。

### Gemini無料枠の使い方

無料枠は `gemini-3.6-flash` の生成が1日20回まで。1本の投稿につき生成は通常1回で、
作り直しを含めても **1回の実行あたり4回**を上限にしている(`MAX_GENERATE_CALLS_PER_RUN`)。
起動3回 × 4回 = 最大12回/日 + 週次レビュー1回で、枠に収まる計算。
上限に達した場合は異常終了せず、次の起動に持ち越す。

## 公開リポジトリとして運用する上での注意

このリポジトリはpublicで運用している。**GitHub Actionsの実行ログもWeb上で誰でも読める**ため、
認証情報がログに出ないようコード側で対策してある。

### 秘密情報の扱い

- 認証情報は**必ずGitHub Secretsに入れる**。コードやワークフローに直接書かない
- ログに出す文字列は `scripts/_secrets.py` の `redact()` を必ず通す。
  例外メッセージ・APIレスポンス・gitコマンドの出力は、外部由来なので何が混ざるか分からない
- `main()` は `run_safely()` で包む。未捕捉の例外が出たとき、トレースバックには
  リクエストURLがそのまま載る（Threads APIはクエリに `access_token` を乗せる）ため、
  素で出すとトークンが公開ログに残ってしまう
- GitHub側でもSecretsは自動マスクされるが、URLエンコードされるなどして
  登録値と完全一致しない形で出るとすり抜ける。`redact()` はその穴を埋めるためのもの

**新しくログ出力を足すときは、外部由来の文字列を `{変数}` のまま埋め込まないこと。**

### 公開されるもの・されないもの

| 公開される | 公開されない |
| --- | --- |
| ソースコード一式 | GitHub Secretsの中身 |
| `data/posts.jsonl`（投稿本文・media_id） | — |
| `data/metrics.jsonl`（views/いいね/フォロワー数） | — |
| `strategy.md` の変更履歴（AIの判断理由） | — |
| Actionsの実行ログ | — |

投稿本文とmedia_idはThreads上で元々公開されている情報なので問題ない。
一方で**フォロワー数や反応率の推移が誰でも追える**状態になる点は認識しておくこと。
これを見せたくない場合はリポジトリをprivateにする。

### トークンの失効

Threadsの長期アクセストークンは**60日で失効する**。自動更新は実装していないので、
期限を控えて手動で取り直すこと。切れると投稿が止まる。

なお、**トークンが漏れた疑いがあるときは、まずMeta側でトークンを無効化して再発行する。**
リポジトリからログを消しても、フォークやキャッシュに残っている可能性があるため、
「消す」より「失効させる」ほうが確実。

## セットアップ

投稿できるようになるまでの手順は [SETUP.md](SETUP.md) を参照。

## 各スクリプトを単体で試す

すべてリポジトリのルートから実行する想定。

```bash
python scripts/build_seeds.py
python scripts/build_ephem.py
python scripts/post.py
python scripts/collect.py
python scripts/aggregate.py | python scripts/review.py
```
