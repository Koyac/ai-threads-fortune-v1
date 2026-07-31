"""
日次ジョブ本体。GitHub Actions から1日6回呼ばれ、そのたびに実行される。

1回の実行でやること(要件定義書の処理順そのまま):
    1. 取りこぼし判定    ... 投稿が遅れていないか確認する
    2. 種を1件取得       ... seeds.jsonl から次のお題を取り出す
    3. 天体イベントを引く ... ephemeris.jsonl を今日の日付で調べる
    4. A/Bパターンを決定 ... strategy.md に書かれたパターンを順番に割り当てる
    5. 生成             ... Gemini に投稿文を書かせる
    6. 重複チェック      ... 過去の投稿と似すぎていないか埋め込みで確認する
    7. 投稿             ... Threads APIに2段階で投稿する
    8. 記録             ... data/posts.jsonl に追記する
    9. commit & push    ... 変更をリポジトリに書き戻す

実行方法:
    python scripts/post.py
"""

import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google import genai
from google.genai import types
import requests

ROOT = Path(__file__).resolve().parent.parent
SEEDS_PATH = ROOT / "seeds.jsonl"
EPHEM_PATH = ROOT / "ephemeris.jsonl"
STRATEGY_PATH = ROOT / "strategy.md"
LEXICON_PATH = ROOT / "gyaru_lexicon.md"
POSTS_PATH = ROOT / "data" / "posts.jsonl"

JST = timezone(timedelta(hours=9))

# 1日の投稿スロット(JST)。"01:00"だけ日付をまたぐ点に後で注意する。
SLOT_TIMES = ["07:00", "12:00", "18:00", "21:00", "23:00", "01:00"]

GENERATION_MODEL = "gemini-3.6-flash"
EMBEDDING_MODEL = "gemini-embedding-001"

DEDUP_THRESHOLD = 0.85          # これを超えたら「似すぎ」と判定する
MAX_DEDUP_RETRIES = 3           # 1つの種につき、生成をやり直す回数の上限
MAX_CATCHUP_POSTS_PER_RUN = 2   # 遅れを取り戻すために1回の実行でまとめて投稿してよい本数の上限


# ============================================================
# 小さなユーティリティ(jsonlの読み書き・時刻の扱いなど)
# ============================================================

def load_jsonl(path: Path) -> list[dict]:
    """jsonl(1行1JSON)ファイルを読み込んで、辞書のリストにして返す。ファイルが空でもOK。"""
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def rewrite_jsonl(path: Path, records: list[dict]) -> None:
    """リスト全体で jsonl ファイルを丸ごと書き直す。seeds.jsonl の used/skipped 更新に使う。"""
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, record: dict) -> None:
    """1行だけ末尾に追記する。data/posts.jsonl への記録に使う。"""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def operational_date(dt_jst: datetime):
    """「運用日」を返す。07:00始まり・翌07:00終わりの1つの束として今日の6スロットを扱うための工夫。

    daily.yml の最後のスロットは 01:00 JST で、これはカレンダー上は翌日になってしまう。
    そのままだと「今日は何本投稿したか」の集計がスロットの途中でズレてしまうため、
    07:00より前の時刻は「前日の続き」とみなすように7時間分シフトしてから日付を取る。
    """
    return (dt_jst - timedelta(hours=7)).date()


def slot_datetime(op_date, slot_str: str) -> datetime:
    """"07:00"のようなスロット文字列を、指定した運用日における実際の日時に変換する。"""
    hour, minute = map(int, slot_str.split(":"))
    day = op_date + timedelta(days=1) if hour < 7 else op_date
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=JST)


# ============================================================
# strategy.md の読み取り
# ============================================================

def parse_pattern_definitions(strategy_text: str) -> dict[str, str]:
    """strategy.md の「## A/Bパターン」セクションから、今使えるパターン一覧を取り出す。

    週次ジョブ(review.py)は、負けたパターンをこのセクションから削除することで
    「今後使わない」を実現する設計にしている。つまりここで拾えるパターンの集合は
    「1週目は必ずA/B/C/Dの4つ」「2週目以降はreview.pyが間引いた後の残り」になる。
    """
    patterns: dict[str, str] = {}
    in_section = False
    for raw_line in strategy_text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            in_section = line[3:].strip().startswith("A/Bパターン")
            continue
        if in_section:
            m = re.match(r"-\s*([A-Za-z]+)\s*:\s*(.+)", line)
            if m:
                patterns[m.group(1)] = m.group(2).strip()
    if not patterns:
        raise ValueError("strategy.md の「## A/Bパターン」セクションからパターンを読み取れませんでした。")
    return patterns


def pick_pattern(patterns: dict[str, str], total_posts_so_far: int) -> tuple[str, str]:
    """A→B→C→D...と、ランダムではなく順番に均等に割り当てる。

    「これまでに投稿した本数」を使って割り当てを進めるので、
    1週目はA→B→C→D→A→B...と自然に巡回する。
    """
    codes = list(patterns.keys())
    code = codes[total_posts_so_far % len(codes)]
    return code, patterns[code]


def pattern_to_zodiac_and_voice(pattern_meaning: str) -> tuple[bool, str]:
    """パターンの説明文(例:"星座に言及する × ギャル口調")から、記録用のフラグを取り出す。"""
    zodiac = "星座に言及しない" not in pattern_meaning
    voice = "gyaru" if "ギャル口調" in pattern_meaning else "standard"
    return zodiac, voice


# ============================================================
# 天体イベント・語尾の抽出
# ============================================================

def find_ephemeris_event(date_str: str) -> dict | None:
    """ephemeris.jsonl から、指定した日付(YYYY-MM-DD)のイベントを1件探す。無ければNone。"""
    for line in EPHEM_PATH.read_text(encoding="utf-8").splitlines() if EPHEM_PATH.exists() else []:
        if not line.strip():
            continue
        event = json.loads(line)
        if event["date"] == date_str:
            return event
    return None


def extract_recent_endings(posts: list[dict], n: int = 5) -> list[str]:
    """直近n件の投稿から、文末の言い回しをざっくり抜き出す(簡易ヒューリスティック)。

    本格的な形態素解析はせず、句点・感嘆符で区切った最後のかたまりの末尾数文字を
    「語尾」の代わりとして使う。厳密さより「同じ言い回しの連発を防ぐ」目的で十分。
    """
    endings = []
    for post in posts[-n:]:
        text = (post.get("text") or "").strip()
        sentences = [s for s in re.split(r"[。！\n]", text) if s.strip()]
        if sentences:
            endings.append(sentences[-1].strip()[-8:])
    return endings


# ============================================================
# Gemini呼び出し(生成・埋め込み)
# ============================================================

def call_with_backoff(func, max_attempts: int = 3):
    """Gemini API呼び出し用の共通リトライ処理。指数バックオフで最大3回まで試す。"""
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - Gemini側の例外を包括的に受けてリトライしたい
            if attempt == max_attempts:
                raise
            wait_seconds = 2 ** attempt
            print(f"[post] Gemini呼び出し失敗(試行{attempt}/{max_attempts}): {exc} -> {wait_seconds}秒後に再試行")
            time.sleep(wait_seconds)


def build_prompt(strategy_text, lexicon_text, seed, ephem_event, pattern_code, pattern_meaning, recent_endings) -> str:
    if ephem_event:
        ephem_block = f"{ephem_event['event']}（{ephem_event['sign']}、テーマ: {ephem_event['theme']}）"
    else:
        ephem_block = "特筆すべき天体イベントはありません"

    endings_block = "、".join(recent_endings) if recent_endings else "(まだ実績なし)"

    return f"""あなたはThreadsで恋愛占いアカウントを運用しているペルソナ「藍」です。
以下の情報だけを元に、Threadsに投稿する本文を1つ書いてください。

# 今週の投稿方針 (strategy.md)
{strategy_text}

# 使ってよい語彙・禁止表現 (gyaru_lexicon.md)
{lexicon_text}

# 今回のお題
- 関係フェーズ: {seed["phase"]}
- 感情: {seed["emotion"]}
- シーン: {seed["scene"]}

# 本日の天体イベント
{ephem_block}

# 今回のパターン: {pattern_code}（{pattern_meaning}）

# 直近5投稿の文末表現(そのままの繰り返しは禁止)
{endings_block}

# 出力ルール
- strategy.md の「固定ルール」セクションを必ず守ること
- 出力は投稿本文のみ。前置き・説明・引用符・見出しは一切つけないこと
"""


def generate_post_text(client: genai.Client, prompt: str) -> str:
    def _call():
        response = client.models.generate_content(model=GENERATION_MODEL, contents=prompt)
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("Geminiから空の応答が返ってきました")
        return text

    return call_with_backoff(_call)


def embed_text(client: genai.Client, text: str) -> list[float]:
    def _call():
        response = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY"),
        )
        return list(response.embeddings[0].values)

    return call_with_backoff(_call)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def is_too_similar(embedding: list[float], existing_embeddings: list[list[float]]) -> bool:
    return any(cosine_similarity(embedding, e) >= DEDUP_THRESHOLD for e in existing_embeddings)


# ============================================================
# 種の管理(取得・消化済み更新)
# ============================================================

def find_next_unused_seed_index(seeds: list[dict]) -> int | None:
    for i, seed in enumerate(seeds):
        if not seed.get("used"):
            return i
    return None


def generate_unique_post(client, strategy_text, lexicon_text, ephem_event, pattern_code, pattern_meaning,
                          recent_endings, seeds, existing_embeddings):
    """種を1つずつ試しながら、重複しない投稿文ができるまで繰り返す。

    1つの種につき最大 MAX_DEDUP_RETRIES 回まで生成し直し、それでも似すぎている場合は
    その種を「投稿されないまま消化済み(skipped)」にして次の種に進む。
    「必ず同一スロットで投稿を完了させる」という要件があるため、成功するまでこの外側の
    ループは続ける(種がすべて尽きた場合だけ例外を送出する)。
    """
    while True:
        seed_index = find_next_unused_seed_index(seeds)
        if seed_index is None:
            raise RuntimeError("未使用の種(seed)がもうありません。build_seeds.py の再実行を検討してください。")
        seed = seeds[seed_index]

        for attempt in range(1, MAX_DEDUP_RETRIES + 1):
            prompt = build_prompt(strategy_text, lexicon_text, seed, ephem_event,
                                   pattern_code, pattern_meaning, recent_endings)
            text = generate_post_text(client, prompt)
            embedding = embed_text(client, text)
            if not is_too_similar(embedding, existing_embeddings):
                return seed_index, text, embedding
            print(f"[post] 種 {seed['id']} の生成が既存投稿と似すぎています(試行{attempt}/{MAX_DEDUP_RETRIES})")

        # ここに来たのは MAX_DEDUP_RETRIES 回とも似すぎていた場合。
        # この種は諦めて「消化済み・未投稿」として記録し、次の種へ移る。
        seeds[seed_index]["used"] = True
        seeds[seed_index]["skipped"] = True
        rewrite_jsonl(SEEDS_PATH, seeds)
        print(f"[post] 種 {seed['id']} は重複が解消できずスキップしました")


# ============================================================
# Threads API
# ============================================================

class ThreadsRateLimited(Exception):
    """Threads APIがレート制限を返したことを表す例外。呼び出し側はリトライせず終了する。"""


RATE_LIMIT_ERROR_CODES = {4, 17, 32, 613}


def _check_rate_limit(response: requests.Response) -> None:
    if response.status_code == 429:
        raise ThreadsRateLimited(f"HTTP 429: {response.text}")
    try:
        body = response.json()
    except ValueError:
        return
    error = body.get("error") if isinstance(body, dict) else None
    if error and error.get("code") in RATE_LIMIT_ERROR_CODES:
        raise ThreadsRateLimited(f"レート制限エラー: {error}")


def post_to_threads(text: str, access_token: str, user_id: str) -> str:
    """Threads APIの2段階投稿(下書き作成 → 公開)を実行し、公開後のmedia_idを返す。"""
    base = f"https://graph.threads.net/v1.0/{user_id}"

    create_resp = requests.post(f"{base}/threads", data={
        "media_type": "TEXT",
        "text": text,
        "access_token": access_token,
    }, timeout=30)
    _check_rate_limit(create_resp)
    create_resp.raise_for_status()
    creation_id = create_resp.json()["id"]

    publish_resp = requests.post(f"{base}/threads_publish", data={
        "creation_id": creation_id,
        "access_token": access_token,
    }, timeout=30)
    _check_rate_limit(publish_resp)
    publish_resp.raise_for_status()
    return publish_resp.json()["id"]


# ============================================================
# git commit & push
# ============================================================

def commit_and_push(message: str, max_attempts: int = 10) -> None:
    """変更をコミットしてpushする。投稿は既に成功しているため、二重投稿を防ぐために
    pushが成功するまで(リモートの更新を取り込みながら)リトライし続ける。
    """
    subprocess.run(["git", "config", "user.name", "github-actions"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", "seeds.jsonl", "data/posts.jsonl"], cwd=ROOT, check=True)

    commit_result = subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True, text=True)
    if commit_result.returncode != 0 and "nothing to commit" not in commit_result.stdout:
        raise RuntimeError(f"git commit に失敗しました: {commit_result.stderr}")

    for attempt in range(1, max_attempts + 1):
        push_result = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
        if push_result.returncode == 0:
            return
        print(f"[post] git push 失敗(試行{attempt}/{max_attempts}): {push_result.stderr.strip()}")
        # 他のジョブが先にpushしていた可能性があるので、取り込んでから再挑戦する。
        subprocess.run(["git", "pull", "--rebase"], cwd=ROOT, capture_output=True, text=True)
        time.sleep(min(2 ** attempt, 30))

    raise RuntimeError(
        "git push が繰り返し失敗しました。投稿自体は成功済みのため、"
        "二重投稿を避けるためにも手動でリポジトリの状態を確認してください。"
    )


# ============================================================
# メイン処理
# ============================================================

def main() -> None:
    access_token = os.environ["THREADS_ACCESS_TOKEN"]
    user_id = os.environ["THREADS_USER_ID"]
    api_key = os.environ["GEMINI_API_KEY"]

    client = genai.Client(api_key=api_key)

    now = datetime.now(JST)
    today_str = now.date().isoformat()
    op_date = operational_date(now)

    posts = load_jsonl(POSTS_PATH)
    seeds = load_jsonl(SEEDS_PATH)
    if not seeds:
        print("[post] seeds.jsonl が空です。先に build_seeds.py を実行してください。", file=sys.stderr)
        sys.exit(1)

    # --- 1. 取りこぼし判定 ---
    due_slots = [s for s in SLOT_TIMES if slot_datetime(op_date, s) <= now]
    posts_today = [
        p for p in posts
        if operational_date(datetime.fromisoformat(p["posted_at"]).astimezone(JST)) == op_date
    ]
    remaining_slots = due_slots[len(posts_today):]
    to_post = min(len(remaining_slots), MAX_CATCHUP_POSTS_PER_RUN)

    if to_post <= 0:
        print("[post] 現時点で投稿すべきスロットはありません。何もせず終了します。")
        return

    strategy_text = STRATEGY_PATH.read_text(encoding="utf-8")
    lexicon_text = LEXICON_PATH.read_text(encoding="utf-8")
    existing_embeddings = [p["embedding"] for p in posts if p.get("embedding")]

    made_any = False
    for slot in remaining_slots[:to_post]:
        # --- 3. 天体イベントを引く ---
        ephem_event = find_ephemeris_event(today_str)

        # --- 4. A/Bパターンを決定 ---
        patterns = parse_pattern_definitions(strategy_text)
        pattern_code, pattern_meaning = pick_pattern(patterns, len(posts))
        zodiac, voice = pattern_to_zodiac_and_voice(pattern_meaning)

        recent_endings = extract_recent_endings(posts)

        # --- 2・5・6. 種の取得〜生成〜重複チェック ---
        try:
            seed_index, text, embedding = generate_unique_post(
                client, strategy_text, lexicon_text, ephem_event,
                pattern_code, pattern_meaning, recent_endings, seeds, existing_embeddings,
            )
        except RuntimeError as exc:
            print(f"[post] {exc}")
            break
        seed = seeds[seed_index]

        # --- 7. 投稿 ---
        try:
            media_id = post_to_threads(text, access_token, user_id)
        except ThreadsRateLimited as exc:
            print(f"[post] Threads APIがレート制限中のため、この実行はここで終了します: {exc}")
            break

        # --- 8. 記録 ---
        # media_id は collect.py が後で `/{media-id}/insights` を叩くときに使う、
        # Threads側がこの投稿につけた本当のID(要件定義書のid例とは別物)。
        seeds[seed_index]["used"] = True
        record = {
            "id": f"p{len(posts) + 1:05d}",
            "media_id": media_id,
            "seed_id": seed["id"],
            "pattern": pattern_code,
            "zodiac": zodiac,
            "voice": voice,
            "phase": seed["phase"],
            "emotion": seed["emotion"],
            "scene": seed["scene"],
            "slot": slot,
            "text": text,
            "embedding": embedding,
            "posted_at": now.isoformat(),
        }
        posts.append(record)
        existing_embeddings.append(embedding)
        append_jsonl(POSTS_PATH, record)
        rewrite_jsonl(SEEDS_PATH, seeds)
        made_any = True

        # --- 9. commit & push ---
        commit_and_push(f"post: {slot} {seed['phase']}/{seed['emotion']}/{seed['scene']} (pattern {pattern_code})")
        print(f"[post] {slot} 分の投稿が完了しました (seed={seed['id']}, pattern={pattern_code})")

    if not made_any:
        print("[post] 今回の実行では投稿できませんでした。")


if __name__ == "__main__":
    main()
