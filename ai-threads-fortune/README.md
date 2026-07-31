# ai-threads-fortune

Threads の恋愛占いアカウント「藍」を、GitHub Actions と Gemini API だけで完全自動運用するシステム。
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
data/posts.jsonl             # 投稿ログ(埋め込みベクトル含む)
data/metrics.jsonl           # 日次メトリクスのスナップショット
scripts/
  build_seeds.py             # 【初回のみ】投稿の種を生成
  build_ephem.py             # 【初回のみ】天体イベントを生成
  post.py                    # 【日次・1日6回】投稿する
  collect.py                 # 【日次・1回】メトリクスを収集する
  aggregate.py                # 【週次】LLMを使わずに集計する
  review.py                  # 【週次】集計結果をもとにAIが方針を更新する
.github/workflows/
  daily.yml                  # 日次スケジュール(post.py / collect.py)
  weekly.yml                 # 週次スケジュール(aggregate.py -> review.py)
```

## セットアップの流れ(概要)

1. `pip install -r requirements.txt`
2. `python scripts/build_seeds.py` と `python scripts/build_ephem.py` をそれぞれ1回だけ実行し、
   `seeds.jsonl` / `ephemeris.jsonl` を生成する
3. GitHub Secrets に上記3つの値を登録する
4. リポジトリの Settings → Actions → General で
   Workflow permissions を「Read and write permissions」にする
   (`post.py` たちが `git push` できるようにするため)
5. 「Actions」タブから `daily` ワークフローを `workflow_dispatch` で1回手動実行し、
   実際に1本投稿できることを確認する

以降は `daily.yml` / `weekly.yml` のスケジュールに沿って、人の手を介さず自動で動き続ける。

## 各スクリプトを単体で試す

すべてリポジトリのルートから実行する想定。

```bash
python scripts/build_seeds.py
python scripts/build_ephem.py
python scripts/post.py
python scripts/collect.py
python scripts/aggregate.py | python scripts/review.py
```
