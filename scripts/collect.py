"""
日次メトリクス収集ジョブ。1日1回(daily.ymlの最後のスロットのタイミング)だけ実行する。

data/posts.jsonl に記録されている全投稿について、現時点の反応(views/likes/replies/
reposts/quotes)をThreads APIから取得し、アカウント全体のフォロワー数と合わせて
data/metrics.jsonl に1行ずつ追記する。同じ投稿でも日を追うごとに新しい行が増えていく
(上書きしない)ので、時間が経つにつれて反応がどう伸びたかを後から追える。

実行方法:
    python scripts/collect.py
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
POSTS_PATH = ROOT / "data" / "posts.jsonl"
METRICS_PATH = ROOT / "data" / "metrics.jsonl"

JST = timezone(timedelta(hours=9))
POST_METRICS = "views,likes,replies,reposts,quotes"


class ThreadsApiError(RuntimeError):
    """Threads APIエラーを、アクセストークンを含まない形でログへ出すための例外。"""


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def extract_metric_values(insights_response: dict) -> dict:
    """Threads Insights APIのレスポンスから、指標名と値の対応表を取り出す。

    Meta のInsights APIは指標の種類によって形が微妙に違う("values"配列を持つものと、
    "total_value"を持つもの)ため、両方に対応できるようにしている。
    """
    result = {}
    for item in insights_response.get("data", []):
        name = item.get("name")
        if "values" in item and item["values"]:
            result[name] = item["values"][0].get("value")
        elif "total_value" in item:
            result[name] = item["total_value"].get("value")
    return result


def raise_for_status_without_token(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError:
        body = response.text.strip()
        body = re.sub(r"(access_token=)[^&\s\"]+", r"\1***", body)
        if len(body) > 500:
            body = body[:500] + "..."
        detail = f": {body}" if body else ""
        raise ThreadsApiError(f"HTTP {response.status_code}{detail}") from None


def fetch_post_insights(media_id: str, access_token: str) -> dict:
    url = f"https://graph.threads.net/v1.0/{media_id}/insights"
    response = requests.get(url, params={"metric": POST_METRICS, "access_token": access_token}, timeout=30)
    raise_for_status_without_token(response)
    return extract_metric_values(response.json())


def fetch_followers_count(user_id: str, access_token: str) -> int | None:
    url = f"https://graph.threads.net/v1.0/{user_id}/threads_insights"
    response = requests.get(url, params={"metric": "followers_count", "access_token": access_token}, timeout=30)
    raise_for_status_without_token(response)
    return extract_metric_values(response.json()).get("followers_count")


def commit_and_push(message: str, max_attempts: int = 10) -> None:
    subprocess.run(["git", "config", "user.name", "github-actions"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", "data/metrics.jsonl"], cwd=ROOT, check=True)

    commit_result = subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True, text=True)
    if commit_result.returncode != 0 and "nothing to commit" not in commit_result.stdout:
        raise RuntimeError(f"git commit に失敗しました: {commit_result.stderr}")

    for attempt in range(1, max_attempts + 1):
        push_result = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
        if push_result.returncode == 0:
            return
        print(f"[collect] git push 失敗(試行{attempt}/{max_attempts}): {push_result.stderr.strip()}")
        subprocess.run(["git", "pull", "--rebase"], cwd=ROOT, capture_output=True, text=True)
    raise RuntimeError("git push が繰り返し失敗しました。")


def main() -> None:
    access_token = os.environ["THREADS_ACCESS_TOKEN"]
    user_id = os.environ["THREADS_USER_ID"]

    posts = load_jsonl(POSTS_PATH)
    fetched_at = datetime.now(JST).isoformat()

    collected = 0
    for post in posts:
        media_id = post.get("media_id")
        if not media_id:
            continue  # 投稿記録はあるがmedia_idが無い(古いテストデータ等)場合はスキップ
        try:
            metrics = fetch_post_insights(media_id, access_token)
        except ThreadsApiError as exc:
            # 1件の投稿でエラーが起きても、他の投稿の収集は続ける。
            print(f"[collect] post {post['id']} のインサイト取得に失敗しました: {exc}")
            continue

        append_jsonl(METRICS_PATH, {
            "type": "post",
            "post_id": post["id"],
            "media_id": media_id,
            "fetched_at": fetched_at,
            **metrics,
        })
        collected += 1

    followers_count = fetch_followers_count(user_id, access_token)
    append_jsonl(METRICS_PATH, {
        "type": "account",
        "fetched_at": fetched_at,
        "followers_count": followers_count,
    })

    print(f"[collect] 投稿 {collected} 件分のメトリクスと、フォロワー数を記録しました。")
    commit_and_push(f"collect: {fetched_at} 時点のメトリクスを記録")


if __name__ == "__main__":
    main()
