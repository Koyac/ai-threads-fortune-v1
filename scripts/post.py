"""
日次ジョブ本体。GitHub Actions から1日3回呼ばれる(11:45 / 21:45 / 23:40 JST)。
投稿するのは1日2本で、3回目は「その日まだ投稿できていない分」を回収するためだけの起動。
その日の投稿数を数えてから動くので、余分に呼ばれても二重投稿にはならない。

1回の実行でやること(要件定義書の処理順そのまま):
    1. 取りこぼし判定    ... 投稿が遅れていないか確認する(遅れていれば最大2本まとめて追いつく)
    2. 種を1件取得       ... seeds.jsonl から次のお題を取り出す
    3. 天体イベントを引く ... ephemeris.jsonl を今日の日付で調べる
    4. A/Bパターンを決定 ... strategy.md に書かれたパターンを順番に割り当てる
    5. 生成             ... Gemini に投稿文を書かせる
    6. 文字数チェック    ... Threadsの上限500文字を超えていたら作り直す(超えたまま投稿すると400エラーになる)
    7. 重複チェック      ... 過去の投稿と似すぎていないか埋め込みで確認する
    8. 投稿             ... Threads APIに2段階で投稿する
    9. 記録             ... data/posts.jsonl に追記する
   10. commit & push    ... 変更をリポジトリに書き戻す

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
from google.genai.errors import ClientError as GeminiClientError
import requests

from _secrets import redact as redact_secrets, run_safely

ROOT = Path(__file__).resolve().parent.parent
SEEDS_PATH = ROOT / "seeds.jsonl"
EPHEM_PATH = ROOT / "ephemeris.jsonl"
STRATEGY_PATH = ROOT / "strategy.md"
LEXICON_PATH = ROOT / "gyaru_lexicon.md"
ZODIAC_PROMPT_PATH = ROOT / "gyaru_zodiac_prompt.md"
POSTS_PATH = ROOT / "data" / "posts.jsonl"

JST = timezone(timedelta(hours=9))

# 1日の投稿スロット(JST)。label は集計用の名目スロット、due は実行してよい最短時刻。
# GitHub Actions 側で15分早く起動してランダム待機することで、実投稿は12:00/22:00の
# 前後15分あたりに散る。slotにはlabelを記録し、週次集計が細かい時刻で割れないようにする。
POSTING_WINDOWS = [
    {"label": "12:00", "due": "11:45"},
    {"label": "22:00", "due": "21:45"},
]

# 12:00=お昼休みのスクロールタイム、22:00=お風呂上がり〜就寝前の
# 「一人でスマホと向き合う時間」。このジャンル(既読無視・深夜の不安ネタ)は夜の方が刺さりやすいため
# 22:00寄りに設定している。どちらも07:00より後なので日付をまたぐ処理は不要。

GENERATION_MODEL = "gemini-3.6-flash"
EMBEDDING_MODEL = "gemini-embedding-001"

# Threads APIの本文上限は500文字。これを1文字でも超えると投稿APIが
# 400 Bad Request を返して落ちる(strategy.mdの「500文字以内」はプロンプトに
# 書いてあるだけで、AIが守らなければ何の効力もない)。投稿する直前に
# コード側で必ず確認し、超えていたら生成し直す・最後は自動で削る。
MAX_POST_LENGTH = 500
SAFE_POST_LENGTH = 450          # プロンプトで狙わせる目安。上限ぴったりを狙わせると超えやすい

DEDUP_THRESHOLD = 0.90          # これを超えたら「似すぎ」と判定する(0.85だとgyaru_zodiac_prompt.mdの
                                 # 固定フォーマット(12星座列挙など)が構造的に似るだけで誤検知しやすかったため緩めた)
MAX_DEDUP_RETRIES = 3           # 1つの種につき、生成をやり直す回数の上限

# --- Gemini無料枠(有料プランではない)の使い切りを防ぐための上限 ---
# gemini-3.6-flash の generate_content は無料枠だと「1日20回まで」しかない(2026-07時点)。
# daily.yml は1日3回起動する(12:00 / 22:00 / 23:40の取りこぼし回収)ので、
#     3回起動 × 4回 = 最大12回/日
# に収まるよう1回あたり4回を上限にする。weekly.ymlのreview.py(週1回=1回)を足しても
# 13回/日で、20回の枠に余裕を残せる。
#
# 通常運転なら1本の投稿につき生成は1回(=1日2回)しか使わない。4回という枠は
# 「文字数オーバーや重複で作り直したとき」「取りこぼしを2本まとめて投稿するとき」用の余白。
# 枠を使い切った場合は投稿せずに終了し、次の起動に持ち越す(異常終了はしない)。
#
# なお、投稿の重複チェックに使う埋め込み(gemini-embedding-001)は生成とは別枠で、
# しかも呼ぶのは「文字数チェックを通った本文」だけなので、こちらが枯れる心配はない。
MAX_CATCHUP_POSTS_PER_RUN = 2   # 「1日2投稿」を落とさないため、前の枠を取りこぼしていたら2本まとめて追いつく
MAX_GENERATE_CALLS_PER_RUN = 4  # 1回の実行でGeminiの生成を呼んでよい回数の上限(種をまたいでも合計でこの回数まで)


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
    """リスト全体で jsonl ファイルを丸ごと書き直す。seeds.jsonl の used 更新に使う。"""
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, record: dict) -> None:
    """1行だけ末尾に追記する。data/posts.jsonl への記録に使う。"""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def operational_date(dt_jst: datetime):
    """「運用日」を返す。07:00始まり・翌07:00終わりの1つの束として投稿日を扱うための工夫。

    以前の運用では深夜スロットがあり、カレンダー上の日付と運用日がズレることがあった。
    将来また深夜スロットを戻しても集計が崩れないよう、
    07:00より前の時刻は「前日の続き」とみなすように7時間分シフトしてから日付を取る。
    """
    return (dt_jst - timedelta(hours=7)).date()


def slot_datetime(op_date, slot_str: str) -> datetime:
    """"11:45"のようなスロット文字列を、指定した運用日における実際の日時に変換する。"""
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

def is_daily_quota_error(exc: GeminiClientError) -> bool:
    """429のうち「1日の上限(無料枠を使い切った)」かどうかを判定する。

    無料枠には「1日あたり(PerDay)」と「1分あたり(PerMinute)」の2種類の上限があり、
    どちらも429で返ってくる。1分あたりの方は少し待てば復活するので、
    ここで区別して、待って意味がある方だけリトライさせる。
    判定できない場合は安全側に倒して「1日の上限」とみなす(=無駄打ちしない)。
    """
    message = str(exc)
    if re.search(r"PerMinute|per minute|PerMinutePer", message):
        return False
    return True


def call_with_backoff(func, max_attempts: int = 5):
    """Gemini API呼び出し用の共通リトライ処理。指数バックオフで最大5回まで試す。

    503 UNAVAILABLE(「今このモデルが混んでいます」)は時間をおけば直る一時的な障害なので、
    回数と待ち時間を多めに取って粘る。
    429のうち「1分あたりの上限」も待てば直るので、60秒あけて1回だけ待ち直す。
    逆に、待っても直らないもの(429=1日の無料枠切れ / 400=リクエスト不正 / 401・403=キー不正)は
    即座に呼び出し元へ投げ返す。
    """
    minute_quota_waits = 0
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except GeminiClientError as exc:
            # 4xx系は基本的に粘っても無駄なので、そのまま上へ返して呼び出し元に判断させる。
            if exc.code == 429 and not is_daily_quota_error(exc) and minute_quota_waits < 2:
                minute_quota_waits += 1
                print(f"[post] Geminiの1分あたりの上限に当たりました。60秒待って再試行します: {redact_secrets(exc)}")
                time.sleep(60)
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - Gemini側の一時的な障害を包括的に受けてリトライしたい
            if attempt == max_attempts:
                raise
            wait_seconds = min(2 ** attempt, 30)
            print(f"[post] Gemini呼び出し失敗(試行{attempt}/{max_attempts}): {redact_secrets(exc)} -> {wait_seconds}秒後に再試行")
            time.sleep(wait_seconds)


def build_prompt(strategy_text, lexicon_text, zodiac_prompt_text, seed, ephem_event, pattern_code, pattern_meaning,
                  recent_endings, retry_note: str = "") -> str:
    if ephem_event:
        ephem_block = f"{ephem_event['event']}（{ephem_event['sign']}、テーマ: {ephem_event['theme']}）"
    else:
        ephem_block = "特筆すべき天体イベントはありません"

    endings_block = "、".join(recent_endings) if recent_endings else "(まだ実績なし)"

    # 作り直しのときだけ、前回どこが駄目だったかを本文の直前に差し込む。
    retry_block = f"\n# 前回の生成のやり直し理由(必ず直すこと)\n{retry_note}\n" if retry_note else ""

    return f"""あなたはThreadsで「ギャル×星座占い」アカウントを運用しているペルソナ「藍」です。
以下の情報だけを元に、Threadsに投稿する本文を1つ書いてください。

# アカウントの生成方針・フォーマット詳細 (gyaru_zodiac_prompt.md)
{zodiac_prompt_text}

# 今週の投稿方針 (strategy.md)
{strategy_text}

# 使ってよい語彙・禁止表現 (gyaru_lexicon.md)
{lexicon_text}

# 今回のお題(状況の素材として使うこと。関係フェーズに縛られず、gyaru_zodiac_prompt.md のテーマ・フォーマットに合わせて膨らませてよい)
- 関係フェーズ: {seed["phase"]}
- 感情: {seed["emotion"]}
- シーン: {seed["scene"]}

# 本日の天体イベント
{ephem_block}

# 今回のパターン: {pattern_code}（{pattern_meaning}）

# 直近5投稿の文末表現(そのままの繰り返しは禁止)
{endings_block}
{retry_block}
# 文字数(最優先の絶対条件)
- 本文は改行・記号・絵文字も1文字として数えて、合計{MAX_POST_LENGTH}文字以内に必ず収めること。1文字でも超えると投稿できずに失敗する
- 目安は{SAFE_POST_LENGTH}文字前後。書き終えたら必ず自分で文字数を数え、超えていたら削ってから出力すること
- 「○○されたときの12星座」形式のときは、12星座すべてを入れると必ず超える。星座は4〜6個だけに絞り、1星座あたり「牡羊座：〜」の1行(20文字前後)にまとめること
- 説明を足すのではなく、一番刺さる行だけを残して他を捨てること

# 出力ルール
- strategy.md の「固定ルール」セクションを必ず守ること
- gyaru_zodiac_prompt.md の「最重要ルール」「避けること」を必ず守ること(抽象的な性格診断で終わらせない)
- 出力は投稿本文のみ。前置き・説明・引用符・見出し・コードブロックは一切つけないこと
"""


def sanitize_generated_text(text: str) -> str:
    """AIが余計に付けがちな装飾(コードブロック・前置き・全体を囲む引用符)を取り除く。

    これらが混ざったままだと投稿がダサくなるうえ、文字数も無駄に消費する。
    """
    text = text.strip()

    # ```〜``` で囲まれている場合は中身だけ取り出す。
    fence = re.match(r"^```[a-zA-Z]*\n(.*?)\n?```$", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # 「投稿本文:」のような前置きが1行目に付いている場合は落とす。
    text = re.sub(r"^(投稿本文|本文|出力)\s*[:：]\s*\n?", "", text)

    # 全体が引用符で囲まれている場合だけ外す(本文中の鉤括弧は残す)。
    if len(text) >= 2 and text[0] in "「『\"'" and text[-1] in "」』\"'":
        inner = text[1:-1]
        if text[0] not in inner and text[-1] not in inner:
            text = inner.strip()

    # 3行以上の空行は2行にまとめる(見た目の問題であり、文字数の節約にもなる)。
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def trim_to_limit(text: str, limit: int = MAX_POST_LENGTH) -> str:
    """最後の安全弁。上限を超えている本文を、できるだけ自然な切れ目で上限内に収める。

    まず段落、次に行、最後に文(。！？)の単位で後ろから捨てていき、
    それでも収まらない場合だけ強制的に切り詰める。
    生成側で収まっているのが正常なので、ここが働いたらログに出す。
    """
    if len(text) <= limit:
        return text

    for separator in ("\n\n", "\n", None):
        if separator is None:
            # 文単位。区切り文字を残したまま分割する。
            chunks = re.findall(r"[^。！？]*[。！？]|[^。！？]+$", text)
            joiner = ""
        else:
            chunks = text.split(separator)
            joiner = separator
        while len(chunks) > 1:
            chunks = chunks[:-1]
            candidate = joiner.join(chunks).strip()
            if candidate and len(candidate) <= limit:
                return candidate

    return text[:limit].rstrip()


def generate_post_text(client: genai.Client, prompt: str) -> str:
    def _call():
        response = client.models.generate_content(model=GENERATION_MODEL, contents=prompt)
        text = sanitize_generated_text(response.text or "")
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


def max_similarity(embedding: list[float], existing_embeddings: list[list[float]]) -> float:
    """既存投稿の中で「一番似ているもの」との類似度を返す。0件なら0.0。"""
    if not existing_embeddings:
        return 0.0
    return max(cosine_similarity(embedding, e) for e in existing_embeddings)


# ============================================================
# 種の管理(取得・消化済み更新)
# ============================================================

def find_next_unused_seed_index(seeds: list[dict]) -> int | None:
    for i, seed in enumerate(seeds):
        if not seed.get("used"):
            return i
    return None


class GenerationBudgetExhausted(RuntimeError):
    """1回の実行で使ってよいGemini生成回数(MAX_GENERATE_CALLS_PER_RUN)を使い切ったことを表す。

    Gemini無料枠の1日の上限に収めるための安全弁。この例外は「今回はもう諦めて
    次の実行に任せる」という正常系の一部として扱い、呼び出し側はエラー終了せず
    ログを出して次のスロットに委ねる。
    """


def generate_unique_post(client, strategy_text, lexicon_text, zodiac_prompt_text, ephem_event, pattern_code, pattern_meaning,
                          recent_endings, seeds, existing_embeddings, calls_budget: int = MAX_GENERATE_CALLS_PER_RUN):
    """次の種を使って、投稿できる本文を1つ作って返す。

    1つの種につき最大 MAX_DEDUP_RETRIES 回まで生成し直す。やり直す理由は2つ:
        - 文字数オーバー ... Threadsの500文字上限を超えている(そのまま投稿すると400エラーで落ちる)
        - 重複          ... 過去の投稿と似すぎている

    重要なのは、リトライを使い切っても「投稿しない」で終わらせないこと。
    以前は似すぎた種を次々スキップして1本も投稿できずに終わることがあったため、
    最後は候補の中で「一番似ていないもの」を採用して必ず1本返す。
    12星座列挙のような固定フォーマットは構造的に似た埋め込みになりやすく、
    中身が違っても閾値を超えることがあるので、この方が実運用に合う。

    無料枠を使い切らないよう、生成回数が calls_budget に達したら
    GenerationBudgetExhausted を送出する(その種は消化済みにしないので、次回また候補になる)。
    """
    seed_index = find_next_unused_seed_index(seeds)
    if seed_index is None:
        raise RuntimeError("未使用の種(seed)がもうありません。build_seeds.py の再実行を検討してください。")
    seed = seeds[seed_index]

    calls_made = 0
    best: tuple[str, list[float], float] | None = None  # (本文, 埋め込み, 最大類似度)
    retry_note = ""

    for attempt in range(1, MAX_DEDUP_RETRIES + 1):
        if calls_made >= calls_budget:
            if best is not None:
                break
            raise GenerationBudgetExhausted(
                f"この実行でのGemini生成回数の上限({MAX_GENERATE_CALLS_PER_RUN}回)に達しました。"
            )

        prompt = build_prompt(strategy_text, lexicon_text, zodiac_prompt_text, seed, ephem_event,
                               pattern_code, pattern_meaning, recent_endings, retry_note)
        text = generate_post_text(client, prompt)
        calls_made += 1

        # --- 文字数チェック(投稿の成否に直結するので、重複チェックより先に見る) ---
        if len(text) > MAX_POST_LENGTH:
            print(f"[post] 種 {seed['id']} の生成が{len(text)}文字で上限{MAX_POST_LENGTH}文字を超えました"
                  f"(試行{attempt}/{MAX_DEDUP_RETRIES})")
            retry_note = (f"前回の出力は{len(text)}文字あり、上限{MAX_POST_LENGTH}文字を超えていた。"
                          f"内容の方向性は変えず、{SAFE_POST_LENGTH}文字以内まで削って書き直すこと。"
                          "星座を列挙する形式なら数を減らし、1星座1行に圧縮する。")
            if attempt < MAX_DEDUP_RETRIES:
                continue
            # 最後の試行でも長い場合だけ、自動で切り詰めて使う(投稿を落とさないため)。
            text = trim_to_limit(text)
            print(f"[post] 上限に収まらなかったため、{len(text)}文字に自動で切り詰めました")

        embedding = embed_text(client, text)
        similarity = max_similarity(embedding, existing_embeddings)

        if similarity < DEDUP_THRESHOLD:
            return seed_index, text, embedding, calls_made

        print(f"[post] 種 {seed['id']} の生成が既存投稿と似すぎています"
              f"(類似度{similarity:.3f} / 試行{attempt}/{MAX_DEDUP_RETRIES})")
        if best is None or similarity < best[2]:
            best = (text, embedding, similarity)
        retry_note = ("前回の出力は過去の投稿と内容が似すぎていた。"
                      "同じ切り口・同じオチを避け、扱う星座・場面・ツッコミどころを大きく変えること。")

    # ここに来たのは全リトライが「似すぎ」だった場合。一番マシな候補を採用して必ず1本投稿する。
    text, embedding, similarity = best
    print(f"[post] 種 {seed['id']} は重複を解消できなかったため、"
          f"最も似ていない候補(類似度{similarity:.3f})を採用します")
    return seed_index, text, embedding, calls_made


# ============================================================
# Threads API
# ============================================================

class ThreadsRateLimited(Exception):
    """Threads APIがレート制限を返したことを表す例外。呼び出し側はリトライせず終了する。"""


class ThreadsPostFailed(Exception):
    """Threads APIが投稿を受け付けなかったことを表す例外(文字数オーバー等)。

    requestsのHTTPErrorをそのまま投げると「400 Bad Request」としか出ず原因が分からないため、
    APIが返したエラー本文を必ず添えて投げ直す。
    """

    def __init__(self, message: str, *, is_auth_error: bool = False):
        super().__init__(message)
        self.is_auth_error = is_auth_error


RATE_LIMIT_ERROR_CODES = {4, 17, 32, 613}

# トークン切れ・権限不足。時間をおいても直らず、人がトークンを取り直すしかない。
AUTH_ERROR_CODES = {102, 190, 200, 2500}

# 「コンテナがまだ見つからない」を表すエラー。作成直後の反映待ちで返ることがある。
MEDIA_NOT_FOUND_CODE = 24
MEDIA_NOT_FOUND_SUBCODE = 4279009

# コンテナが公開可能になるまで待つ上限と、確認の間隔(秒)。
CONTAINER_READY_TIMEOUT = 90
CONTAINER_POLL_INTERVAL = 3


def _check_rate_limit(response: requests.Response) -> None:
    if response.status_code == 429:
        raise ThreadsRateLimited(f"HTTP 429: {redact_secrets(response.text)}")
    try:
        body = response.json()
    except ValueError:
        return
    error = body.get("error") if isinstance(body, dict) else None
    if error and error.get("code") in RATE_LIMIT_ERROR_CODES:
        raise ThreadsRateLimited(f"レート制限エラー: {redact_secrets(error)}")


def _raise_for_threads_error(response: requests.Response, step: str) -> None:
    """Threads APIのエラー応答を、原因が分かるメッセージに変換して投げ直す。"""
    if response.ok:
        return

    error = {}
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
    except ValueError:
        pass

    code = error.get("code")
    detail = redact_secrets(error.get("message") or response.text.strip()[:500])
    raise ThreadsPostFailed(
        f"Threads APIが{step}を拒否しました (HTTP {response.status_code} / code={code}): {detail}",
        is_auth_error=code in AUTH_ERROR_CODES,
    )


def _is_media_not_found(response: requests.Response) -> bool:
    """「コンテナがまだ見つからない」エラーかどうかを判定する。"""
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    return (
        error.get("error_subcode") == MEDIA_NOT_FOUND_SUBCODE
        or error.get("code") == MEDIA_NOT_FOUND_CODE
    )


def wait_for_container_ready(creation_id: str, access_token: str) -> None:
    """下書きコンテナが公開可能になるまで待つ。

    Threads APIのコンテナ作成は非同期で、作成直後に公開しようとすると
    「Media Not Found」(code 24 / subcode 4279009)で弾かれることがある。
    エラーには is_transient: false と入っているが、実際にはタイミング依存の
    一時的な失敗なので、statusがFINISHEDになるのを待ってから公開する。

    反映前はこのstatus取得自体も同じMedia Not Foundを返すことがあるため、
    その場合はエラーにせず待ち続ける。
    """
    deadline = time.monotonic() + CONTAINER_READY_TIMEOUT
    last_status = "作成直後"

    while True:
        resp = requests.get(
            f"https://graph.threads.net/v1.0/{creation_id}",
            params={"fields": "status,error_message", "access_token": access_token},
            timeout=30,
        )
        _check_rate_limit(resp)

        if resp.ok:
            body = resp.json()
            status = body.get("status")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                detail = redact_secrets(str(body.get("error_message")))
                raise ThreadsPostFailed(
                    f"Threads APIのコンテナが公開できない状態です (status={status}): {detail}"
                )
            last_status = status or "不明"
        elif _is_media_not_found(resp):
            # まだ反映されていないだけ。待って確認し直す。
            last_status = "未反映"
        else:
            _raise_for_threads_error(resp, "コンテナ状態の確認")

        if time.monotonic() >= deadline:
            raise ThreadsPostFailed(
                f"Threads APIのコンテナが{CONTAINER_READY_TIMEOUT}秒以内に公開可能になりませんでした "
                f"(最後のstatus={last_status})"
            )
        time.sleep(CONTAINER_POLL_INTERVAL)


def post_to_threads(text: str, access_token: str, user_id: str) -> str:
    """Threads APIの2段階投稿(下書き作成 → 公開)を実行し、公開後のmedia_idを返す。"""
    if len(text) > MAX_POST_LENGTH:
        # ここに来る時点で生成側の文字数制御が漏れている。投稿を落とさないよう切り詰める。
        print(f"[post] 投稿直前チェック: {len(text)}文字あったため{MAX_POST_LENGTH}文字以内に切り詰めます")
        text = trim_to_limit(text)

    base = f"https://graph.threads.net/v1.0/{user_id}"

    create_resp = requests.post(f"{base}/threads", data={
        "media_type": "TEXT",
        "text": text,
        "access_token": access_token,
    }, timeout=30)
    _check_rate_limit(create_resp)
    _raise_for_threads_error(create_resp, "下書きの作成")
    creation_id = create_resp.json()["id"]

    # 作成直後は公開できないことがあるので、準備できるまで待つ。
    wait_for_container_ready(creation_id, access_token)

    publish_resp = requests.post(f"{base}/threads_publish", data={
        "creation_id": creation_id,
        "access_token": access_token,
    }, timeout=30)
    _check_rate_limit(publish_resp)
    _raise_for_threads_error(publish_resp, "公開")
    return publish_resp.json()["id"]


# ============================================================
# git commit & push
# ============================================================

def commit_and_push(message: str, paths: list[str] | None = None, max_attempts: int = 10) -> None:
    """変更をコミットしてpushする。投稿は既に成功しているため、二重投稿を防ぐために
    pushが成功するまで(リモートの更新を取り込みながら)リトライし続ける。

    paths を指定しない場合は投稿成功時のデフォルト(seeds.jsonl + data/posts.jsonl)。
    """
    if paths is None:
        paths = ["seeds.jsonl", "data/posts.jsonl"]

    subprocess.run(["git", "config", "user.name", "github-actions"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", *paths], cwd=ROOT, check=True)

    commit_result = subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True, text=True)
    if commit_result.returncode != 0 and "nothing to commit" not in commit_result.stdout:
        raise RuntimeError(f"git commit に失敗しました: {redact_secrets(commit_result.stderr)}")

    for attempt in range(1, max_attempts + 1):
        push_result = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
        if push_result.returncode == 0:
            return
        print(f"[post] git push 失敗(試行{attempt}/{max_attempts}): {redact_secrets(push_result.stderr.strip())}")
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
    due_slots = [
        window["label"]
        for window in POSTING_WINDOWS
        if slot_datetime(op_date, window["due"]) <= now
    ]
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
    zodiac_prompt_text = ZODIAC_PROMPT_PATH.read_text(encoding="utf-8")
    existing_embeddings = [p["embedding"] for p in posts if p.get("embedding")]

    if to_post > 1:
        print(f"[post] 前のスロットを取りこぼしているため、この実行で{to_post}本まとめて投稿して追いつきます。")

    made_any = False
    calls_used = 0                  # この実行で使ったGeminiの生成回数(投稿をまたいで合算する)
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
            seed_index, text, embedding, calls_made = generate_unique_post(
                client, strategy_text, lexicon_text, zodiac_prompt_text, ephem_event,
                pattern_code, pattern_meaning, recent_endings, seeds, existing_embeddings,
                calls_budget=MAX_GENERATE_CALLS_PER_RUN - calls_used,
            )
            calls_used += calls_made
        except RuntimeError as exc:
            print(f"[post] {redact_secrets(exc)}")
            break
        except GeminiClientError as exc:
            if exc.code == 429:
                print(f"[post] Geminiの無料枠(1日の上限)を使い切ったため、この実行はここで終了します: {redact_secrets(exc)}")
                break
            print(f"[post] Geminiがリクエストを受け付けませんでした。この実行はここで終了します: {redact_secrets(exc)}", file=sys.stderr)
            break
        seed = seeds[seed_index]

        # --- 7. 投稿 ---
        # data/posts.jsonl に「実際に投稿した本文」を残すため、切り詰めるならここで済ませる。
        text = trim_to_limit(text)
        try:
            media_id = post_to_threads(text, access_token, user_id)
        except ThreadsRateLimited as exc:
            print(f"[post] Threads APIがレート制限中のため、この実行はここで終了します: {redact_secrets(exc)}")
            break
        except ThreadsPostFailed as exc:
            # 投稿は成功していないので、種は消化済みにせず次回にそのまま回す。
            print(f"[post] 投稿に失敗しました: {redact_secrets(exc)}", file=sys.stderr)
            print(f"[post] 失敗した本文({len(text)}文字): {text[:200]}", file=sys.stderr)
            if exc.is_auth_error:
                # トークン切れは待っても直らず、人がSETUP.mdの手順でトークンを取り直すしかない。
                # 気づけるようにここだけは異常終了させる。
                print("[post] アクセストークンが無効か期限切れです。SETUP.md の手順でトークンを取り直してください。",
                      file=sys.stderr)
                sys.exit(1)
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

        # 取りこぼしを2本まとめて投げるときは、連投に見えないよう少し間を空ける。
        if slot != remaining_slots[:to_post][-1]:
            print("[post] 続けてもう1本投稿します。連投を避けるため90秒待機します。")
            time.sleep(90)

    if not made_any:
        print("[post] 今回の実行では投稿できませんでした。")


if __name__ == "__main__":
    run_safely(main, "post")
