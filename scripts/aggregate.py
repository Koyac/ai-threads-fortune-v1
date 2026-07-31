"""
週次の集計スクリプト。LLMは一切使わず、Pythonだけで決定論的に集計する。

data/posts.jsonl (どの投稿がどのパターン・フェーズ・スロットだったか) と
data/metrics.jsonl (各投稿のviews/likes等のスナップショット) を突き合わせて、
直近7日間の投稿についての集計結果をMarkdownの表として標準出力に出す。

この出力は weekly.yml の中で `python scripts/aggregate.py | python scripts/review.py`
のようにパイプでつなぎ、review.py がそのままGeminiに渡す想定。

実行方法:
    python scripts/aggregate.py
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTS_PATH = ROOT / "data" / "posts.jsonl"
METRICS_PATH = ROOT / "data" / "metrics.jsonl"

JST = timezone(timedelta(hours=9))
AGGREGATION_WINDOW_DAYS = 7

# 投稿スロットは時刻の文字列だと素直に並ばない(01:00は深夜=1日の最後のスロット)ので、
# 表示順を明示的に決めておく。
SLOT_ORDER = ["07:00", "12:00", "18:00", "21:00", "23:00", "01:00"]


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def response_rate(metrics: dict) -> float | None:
    views = metrics.get("views")
    if not views:
        return None
    reactions = (metrics.get("likes") or 0) + (metrics.get("replies") or 0) \
        + (metrics.get("reposts") or 0) + (metrics.get("quotes") or 0)
    return reactions / views


def latest_metrics_by_post_id(metrics_rows: list[dict]) -> dict[str, dict]:
    """post_idごとに、一番新しい(=最後に追記された)メトリクス行だけを残す。"""
    latest: dict[str, dict] = {}
    for row in metrics_rows:
        if row.get("type") == "post":
            latest[row["post_id"]] = row  # 後から出てくる行で上書きされるので、自然と最新が残る
    return latest


def join_posts_with_metrics(posts: list[dict], metrics_rows: list[dict]) -> list[tuple[dict, dict]]:
    latest = latest_metrics_by_post_id(metrics_rows)
    joined = []
    for post in posts:
        metrics = latest.get(post["id"])
        if metrics:
            joined.append((post, metrics))
    return joined


def summarize_by(joined: list[tuple[dict, dict]], key_func, sort_key=None) -> list[tuple]:
    """(post, metrics)のペアをkey_funcでグループ分けし、件数・平均views・平均反応率を出す。"""
    groups: dict = defaultdict(list)
    for post, metrics in joined:
        groups[key_func(post)].append(metrics)

    keys = list(groups.keys())
    keys.sort(key=sort_key) if sort_key else keys.sort(key=str)

    rows = []
    for key in keys:
        metrics_list = groups[key]
        views_list = [m["views"] for m in metrics_list if m.get("views") is not None]
        rate_list = [r for r in (response_rate(m) for m in metrics_list) if r is not None]
        avg_views = sum(views_list) / len(views_list) if views_list else None
        avg_rate = sum(rate_list) / len(rate_list) if rate_list else None
        rows.append((key, len(metrics_list), avg_views, avg_rate))
    return rows


def format_table(headers: list[str], rows: list[tuple]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        cells = []
        for value in row:
            if isinstance(value, float):
                cells.append(f"{value:.1f}" if abs(value) >= 1 else f"{value:.3f}")
            elif value is None:
                cells.append("—")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def followers_increase(metrics_rows: list[dict], window_start: datetime) -> int | None:
    account_rows = [r for r in metrics_rows if r.get("type") == "account"]
    account_rows.sort(key=lambda r: r["fetched_at"])
    in_window = [r for r in account_rows if datetime.fromisoformat(r["fetched_at"]) >= window_start]
    if len(in_window) < 2:
        return None
    return in_window[-1]["followers_count"] - in_window[0]["followers_count"]


def main() -> None:
    posts = load_jsonl(POSTS_PATH)
    metrics_rows = load_jsonl(METRICS_PATH)

    now = datetime.now(JST)
    window_start = now - timedelta(days=AGGREGATION_WINDOW_DAYS)

    recent_posts = [p for p in posts if datetime.fromisoformat(p["posted_at"]) >= window_start]
    joined = join_posts_with_metrics(recent_posts, metrics_rows)

    print(f"# 直近{AGGREGATION_WINDOW_DAYS}日間の集計結果")
    print(f"(集計対象期間: {window_start.date().isoformat()} 〜 {now.date().isoformat()} JST)")
    print()

    print("## A/Bパターン別")
    print(format_table(
        ["パターン", "投稿数", "平均views", "平均反応率"],
        summarize_by(joined, lambda p: p["pattern"]),
    ))
    print()

    print("## 星座言及の有無別")
    print(format_table(
        ["星座言及", "投稿数", "平均views", "平均反応率"],
        summarize_by(joined, lambda p: "あり" if p["zodiac"] else "なし"),
    ))
    print()

    print("## 口調別")
    print(format_table(
        ["口調", "投稿数", "平均views", "平均反応率"],
        summarize_by(joined, lambda p: "ギャル" if p["voice"] == "gyaru" else "標準"),
    ))
    print()

    print("## 関係フェーズ別")
    print(format_table(
        ["フェーズ", "投稿数", "平均views", "平均反応率"],
        summarize_by(joined, lambda p: p["phase"]),
    ))
    print()

    print("## 投稿スロット別")
    print(format_table(
        ["スロット", "投稿数", "平均views", "平均反応率"],
        summarize_by(joined, lambda p: p["slot"], sort_key=lambda s: SLOT_ORDER.index(s) if s in SLOT_ORDER else 99),
    ))
    print()

    increase = followers_increase(metrics_rows, window_start)
    increase_text = f"{increase:+d}人" if increase is not None else "算出に必要なデータがまだありません"
    print(f"## 期間中のフォロワー増加数\n{increase_text}")


if __name__ == "__main__":
    main()
