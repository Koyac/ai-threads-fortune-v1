"""
投稿の「種」を全パターン作って seeds.jsonl に書き出すスクリプト。

企画が始まる前に、これ1回だけ実行する（毎日実行するスクリプトではない）。
post.py が日々の投稿を作るとき、この seeds.jsonl の先頭から1件ずつ取り出して消費していく。

実行方法:
    python scripts/build_seeds.py
"""

import itertools
import json
import random
from pathlib import Path

# このファイル (scripts/build_seeds.py) から見て1つ上の階層がリポジトリのルート。
# GitHub Actions からでもローカルからでも、どこから実行しても正しい場所に書き出せるようにしている。
ROOT = Path(__file__).resolve().parent.parent
SEEDS_PATH = ROOT / "seeds.jsonl"

# --- 3つの軸。ここを増減させるだけで全体の組み合わせ数が変わる ---
# シーンは今後増やす可能性が高いので、ここにまとめておく（要件定義書の指示通り）。
PHASES = ["片思い", "駆け引き", "付き合いたて", "倦怠期", "別れ際", "失恋後", "復縁"]
EMOTIONS = ["不安", "嫉妬", "諦め", "期待", "後悔"]
SCENES = [
    "既読無視", "帰り道", "深夜の通知", "友達の惚気", "元恋人のSNS",
    "二人きりの沈黙", "誕生日", "既読がついた瞬間", "送信取り消し",
    "週末の予定", "共通の友人", "別れ話の後の連絡",
]


def build_seeds() -> list[dict]:
    """3軸の直積（全部の組み合わせ）を作り、順番をシャッフルして返す。

    7(関係フェーズ) × 5(感情) × 12(シーン) = 420通り。
    シャッフルするのは、似たフェーズ・感情の投稿が連日続かないようにするため。
    """
    combinations = list(itertools.product(PHASES, EMOTIONS, SCENES))
    random.shuffle(combinations)

    seeds = []
    for i, (phase, emotion, scene) in enumerate(combinations, start=1):
        seeds.append({
            "id": f"s{i:04d}",       # s0001, s0002, ... のような連番ID
            "phase": phase,
            "emotion": emotion,
            "scene": scene,
            "used": False,           # post.py がこの種を使ったら True にする
            # 以前は重複で弾かれた種に "skipped" を付けて捨てていたが、それが原因で
            # 1本も投稿できない日が出たため廃止した。今は重複が解消できなくても
            # 一番似ていない候補を採用して必ず投稿する（post.py の generate_unique_post 参照）。
        })
    return seeds


def main() -> None:
    if SEEDS_PATH.exists() and SEEDS_PATH.read_text(encoding="utf-8").strip():
        # 誤って2回実行すると、投稿済みかどうかの情報(used)ごとキューが
        # シャッフルし直されてしまう。運用中の事故を防ぐため、
        # 既に中身があるなら安全のために止める。
        print(f"[build_seeds] {SEEDS_PATH} は既に存在し、空ではありません。上書きを避けるため何もしませんでした。")
        print("[build_seeds] 本当に作り直したい場合は、先に既存の seeds.jsonl を削除してから実行してください。")
        return

    seeds = build_seeds()

    with SEEDS_PATH.open("w", encoding="utf-8") as f:
        for seed in seeds:
            # jsonl形式 = 1行に1つのJSONオブジェクト。あとから1行ずつ読み書きしやすい。
            f.write(json.dumps(seed, ensure_ascii=False) + "\n")

    print(f"[build_seeds] {len(seeds)} 件の種を {SEEDS_PATH} に書き出しました。")


if __name__ == "__main__":
    main()
