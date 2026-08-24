# セットアップ手順

## 1. Threads APIのトークン・ユーザーIDを取得

1. [Meta for Developers](https://developers.facebook.com/) でアプリを作成
2. 「製品を追加」→ **Threads API** を追加
3. スコープを有効化: `threads_basic` / `threads_content_publish` / `threads_manage_insights`
4. 「Threadsテスターを追加」で投稿先アカウントを追加し、Threads側で招待を承認
5. 短期アクセストークンを取得(Graph API Explorer等)
6. 長期トークン(60日)に交換:
   ```
   GET https://graph.threads.net/access_token
     ?grant_type=th_exchange_token&client_secret={Secret}&access_token={短期トークン}
   ```
   → `access_token` が `THREADS_ACCESS_TOKEN`
7. ユーザーIDを取得:
   ```
   GET https://graph.threads.net/v1.0/me?fields=id,username&access_token={長期トークン}
   ```
   → `id` が `THREADS_USER_ID`

画面構成が変わっていたら[公式ドキュメント](https://developers.facebook.com/docs/threads)を参照。

## 2. Gemini APIキーを取得

[Google AI Studio](https://aistudio.google.com/) →「Get API key」→ `GEMINI_API_KEY`

## 3. 初期データを作成してpush

```bash
pip install -r requirements.txt
python scripts/build_seeds.py
python scripts/build_ephem.py
```

生成された `seeds.jsonl` / `ephemeris.jsonl` は commit & push しておく(リポジトリに無いと post.py が「seeds.jsonl が空です」で止まる)。

## 4. GitHub Secretsを登録

このフォルダをGitHubリポジトリにpushし、Settings → Secrets and variables → Actions で登録:
- `THREADS_ACCESS_TOKEN`
- `THREADS_USER_ID`
- `GEMINI_API_KEY`

さらに Settings → Actions → General → Workflow permissions を「Read and write permissions」に。

## 5. 動作確認

「Actions」タブ → `daily` → 「Run workflow」で手動実行 → Threadsに投稿されるか確認。

以降は `daily.yml` / `weekly.yml` のスケジュールで自動運用される。
