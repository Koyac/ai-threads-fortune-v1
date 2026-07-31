"""
週次ジョブ本体。aggregate.py が出したMarkdownの集計結果を標準入力から受け取り、
Geminiにその週の判断をさせて strategy.md / gyaru_lexicon.md を書き換える。

想定される呼び出し方(weekly.ymlの中):
    python scripts/aggregate.py | python scripts/review.py

このスクリプトが一番気をつけているのは、Geminiに「固定ルール」セクションを
書き換えさせないこと。書き換え後の内容を保存する前に必ず検証し、
少しでも変わっていたら何も保存せずエラー終了する。

実行方法(単体テスト時):
    cat 集計結果.md | python scripts/review.py
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
STRATEGY_PATH = ROOT / "strategy.md"
LEXICON_PATH = ROOT / "gyaru_lexicon.md"

JST = timezone(timedelta(hours=9))
REVIEW_MODEL = "gemini-3.6-flash"

FIXED_RULES_HEADING = "## 固定ルール"
CHANGELOG_HEADING = "## 変更履歴"
BANNED_PHRASES_HEADING = "## 禁止（古い・AIっぽい表現）"

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "decisive_axis": {
            "type": "string",
            "description": "明確な差が出た軸(例: 口調、星座言及の有無、投稿スロットなど)",
        },
        "losing_option": {
            "type": "string",
            "description": "その軸の中で、今後使わないと判断した負けた方の選択肢",
        },
        "next_experiment": {
            "type": "string",
            "description": "差が出なかった軸について、翌週に試す新しい二択の提案",
        },
        "reasoning": {
            "type": "string",
            "description": "判断理由。根拠となった数字を含めた日本語3文",
        },
        "banned_phrases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "AIっぽさが出ていた表現。gyaru_lexicon.mdの禁止欄に追記する語",
        },
        "updated_strategy_md": {
            "type": "string",
            "description": "更新後のstrategy.md全文(「変更履歴」への追記はこちらでは行わなくてよい。コード側で追記する)",
        },
    },
    "required": [
        "decisive_axis", "losing_option", "next_experiment",
        "reasoning", "banned_phrases", "updated_strategy_md",
    ],
}


def extract_section(markdown_text: str, heading: str) -> str:
    """指定した"## 見出し"から、次の"## "見出し(またはファイル末尾)までの範囲を取り出す。

    見出しは "## 固定ルール（週次ジョブは絶対に変更しないこと）" のように補足の説明が
    付いていることがあるので、完全一致ではなく前方一致で探す。ここを完全一致にすると
    見出しがずっと見つからず、常に空文字列同士を比較して「一致している」と誤判定してしまう
    (固定ルールの検証が実質無効になる)ため、この前方一致は安全上重要。
    """
    lines = markdown_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith(heading):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip().startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end]).strip()


def append_changelog_entry(strategy_text: str, date_str: str, reasoning: str) -> str:
    lines = strategy_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(CHANGELOG_HEADING):
            entry = f"- {date_str}: {reasoning}"
            lines.insert(i + 1, entry)
            return "\n".join(lines) + "\n"
    raise ValueError(f"strategy.md に「{CHANGELOG_HEADING}」セクションが見つかりませんでした。")


def append_banned_phrases(lexicon_text: str, phrases: list[str]) -> str:
    if not phrases:
        return lexicon_text

    lines = lexicon_text.splitlines()
    existing = {line.strip("- ").strip() for line in lines}
    new_lines = [f"- {p}" for p in phrases if p not in existing]
    if not new_lines:
        return lexicon_text

    for i, line in enumerate(lines):
        if line.strip().startswith(BANNED_PHRASES_HEADING):
            insert_at = i + 1
            # 同じ見出しの下に既にある項目の直後に追記したいので、次の"## "まで進む
            while insert_at < len(lines) and not lines[insert_at].strip().startswith("## "):
                insert_at += 1
            lines[insert_at:insert_at] = new_lines
            return "\n".join(lines) + "\n"

    raise ValueError(f"gyaru_lexicon.md に「{BANNED_PHRASES_HEADING}」セクションが見つかりませんでした。")


def build_prompt(current_strategy_text: str, aggregate_report: str) -> str:
    return f"""あなたはThreadsの恋愛占いアカウント「藍」の運用方針を、週次データに基づいて更新する担当者です。

# 現在のstrategy.md
{current_strategy_text}

# 以下は1週間のA/Bテスト結果です。

{aggregate_report}

次の形式でJSONを出力してください。

1. 明確な差が出た軸を1つ挙げ、負けた方を今後使わないと決めること
2. 差が出なかった軸については、翌週に試す新しい二択を1つ提案すること
3. 判断理由を、根拠となった数字を含めて日本語3文で書くこと
4. AIっぽさが出ていた表現があれば gyaru_lexicon.md の禁止欄に追記する語を挙げること
5. 更新後の strategy.md 全文

制約:
- 必ず1つ、方針の根幹に関わる変更を提案すること
- 数値の微調整のみの変更は認めない
- 「固定ルール」セクションは絶対に変更しないこと
- フォロワー数ではなく、平均viewsと反応率を判断根拠にすること
"""


def call_gemini(client: genai.Client, prompt: str) -> dict:
    response = client.models.generate_content(
        model=REVIEW_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
        ),
    )
    return json.loads(response.text)


def commit_and_push(message: str, max_attempts: int = 10) -> None:
    subprocess.run(["git", "config", "user.name", "github-actions"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", "strategy.md", "gyaru_lexicon.md"], cwd=ROOT, check=True)

    commit_result = subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True, text=True)
    if commit_result.returncode != 0 and "nothing to commit" not in commit_result.stdout:
        raise RuntimeError(f"git commit に失敗しました: {commit_result.stderr}")

    for attempt in range(1, max_attempts + 1):
        push_result = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
        if push_result.returncode == 0:
            return
        print(f"[review] git push 失敗(試行{attempt}/{max_attempts}): {push_result.stderr.strip()}")
        subprocess.run(["git", "pull", "--rebase"], cwd=ROOT, capture_output=True, text=True)
    raise RuntimeError("git push が繰り返し失敗しました。")


def main() -> None:
    api_key = os.environ["GEMINI_API_KEY"]
    aggregate_report = sys.stdin.read()
    if not aggregate_report.strip():
        print("[review] 標準入力が空です。`aggregate.py | review.py` の形で実行してください。", file=sys.stderr)
        sys.exit(1)

    current_strategy_text = STRATEGY_PATH.read_text(encoding="utf-8")

    client = genai.Client(api_key=api_key)
    prompt = build_prompt(current_strategy_text, aggregate_report)
    result = call_gemini(client, prompt)

    updated_strategy_text = result["updated_strategy_md"]

    # --- 固定ルールが書き換えられていないかを、保存する前に必ず検証する ---
    old_fixed_rules = extract_section(current_strategy_text, FIXED_RULES_HEADING)
    new_fixed_rules = extract_section(updated_strategy_text, FIXED_RULES_HEADING)
    if old_fixed_rules != new_fixed_rules:
        print("[review] 固定ルールセクションが書き換えられています。安全のため保存を中断します。", file=sys.stderr)
        print("--- 元の固定ルール ---", file=sys.stderr)
        print(old_fixed_rules, file=sys.stderr)
        print("--- Geminiが返した固定ルール ---", file=sys.stderr)
        print(new_fixed_rules, file=sys.stderr)
        sys.exit(1)

    today_str = datetime.now(JST).date().isoformat()
    final_strategy_text = append_changelog_entry(updated_strategy_text, today_str, result["reasoning"])

    lexicon_text = LEXICON_PATH.read_text(encoding="utf-8")
    final_lexicon_text = append_banned_phrases(lexicon_text, result["banned_phrases"])

    STRATEGY_PATH.write_text(final_strategy_text, encoding="utf-8")
    LEXICON_PATH.write_text(final_lexicon_text, encoding="utf-8")

    print(f"[review] 今週の判断軸: {result['decisive_axis']} (負け: {result['losing_option']})")
    print(f"[review] 来週の実験案: {result['next_experiment']}")
    print(f"[review] 理由: {result['reasoning']}")

    commit_and_push(f"review: {today_str} の方針更新 ({result['decisive_axis']})")


if __name__ == "__main__":
    main()
