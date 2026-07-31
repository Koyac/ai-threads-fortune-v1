"""
今日から1年分の天体イベントを計算して ephemeris.jsonl に書き出すスクリプト。

build_seeds.py と同じく、企画が始まる前に1回だけ実行する。
天体の位置計算には skyfield ライブラリと、NASA JPLが配布している
「JDEデータファイル」(de421.bsp) を使う。初回実行時に自動でダウンロードされ、
このフォルダに保存される（数MB程度、2回目以降はダウンロードし直さない）。

拾うイベントは3種類:
    - 新月・満月（そのときに月がどの星座にいるか）
    - 水星逆行の開始・終了
    - 金星の星座移動（金星が次の星座に移る瞬間）

実行方法:
    python scripts/build_ephem.py
"""

import json
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
from skyfield import almanac
from skyfield.api import load

ROOT = Path(__file__).resolve().parent.parent
EPHEM_PATH = ROOT / "ephemeris.jsonl"

# 投稿は日本時間(JST)で運用しているので、天体イベントの日付も JST の暦日に合わせる。
JST = timezone(timedelta(hours=9))

# 何日分のイベントを計算するか。
DAYS_AHEAD = 365

# 黄道十二星座。0度〜30度が牡羊座、30度〜60度が牡牛座…という単純な等分割で扱う
# （西洋占星術の「サイン」は本来もう少し複雑な決め方もあるが、
#  ここでは中立的なテーマ付けの材料として使うだけなので、単純な等分割で十分）。
ZODIAC_SIGNS = [
    "牡羊座", "牡牛座", "双子座", "蟹座", "獅子座", "乙女座",
    "天秤座", "蠍座", "射手座", "山羊座", "水瓶座", "魚座",
]

# イベント種別ごとに固定で割り当てる一語のテーマ。
# 「必ず〇〇になる」のような断定的な予言にならないよう、中立的な言葉を選んでいる。
THEMES = {
    "新月": "始まり",
    "満月": "気づき",
    "水星逆行開始": "見直し",
    "水星逆行終了": "再始動",
    "金星の星座移動": "移ろい",
}


def sign_of(longitude_degrees: float) -> str:
    """黄道座標上の経度(0〜360度)から、対応する星座名を返す。"""
    index = int(longitude_degrees // 30) % 12
    return ZODIAC_SIGNS[index]


def to_jst_date(t) -> str:
    """skyfieldの時刻オブジェクト(UTC基準)を、JSTの日付文字列(YYYY-MM-DD)に変換する。"""
    dt_utc = t.utc_datetime()
    dt_jst = dt_utc.astimezone(JST)
    return dt_jst.date().isoformat()


def collect_moon_phase_events(ts, eph, t0, t1) -> list[dict]:
    """新月・満月のイベントを集める（上弦・下弦は今回は使わないので除外する）。"""
    earth, moon = eph["earth"], eph["moon"]

    # almanac.moon_phases は、月の満ち欠けが 新月(0)→上弦(1)→満月(2)→下弦(3) と
    # 切り替わる瞬間を自動で見つけてくれるskyfieldの標準機能。
    phase_func = almanac.moon_phases(eph)
    times, phases = almanac.find_discrete(t0, t1, phase_func)

    events = []
    for t, phase in zip(times, phases):
        if phase == 0:
            event_name = "新月"
        elif phase == 2:
            event_name = "満月"
        else:
            continue  # 上弦(1)・下弦(3)は今回のスコープ外

        # そのイベントが起きた瞬間、月が黄道上のどこ(＝どの星座)にいたかを計算する。
        longitude, _latitude, _distance = earth.at(t).observe(moon).apparent().ecliptic_latlon()
        events.append({
            "date": to_jst_date(t),
            "event": event_name,
            "sign": sign_of(longitude.degrees),
            "theme": THEMES[event_name],
        })
    return events


def collect_mercury_retrograde_events(ts, eph, t0, t1) -> list[dict]:
    """水星が逆行を開始する瞬間・終了する瞬間を集める。

    「逆行」とは、地球から見た水星が普段と逆向き（黄道座標で見て経度が減る向き）に
    動いて見える期間のこと。占星術で「コミュニケーションが乱れる」などとよく言われる。
    """
    earth, mercury = eph["earth"], eph["mercury"]

    def is_retrograde(t):
        # ごく短い時間(約14分後)との経度の差から、進んでいる向きを判定する。
        # find_discrete はこの関数に「複数時刻をまとめた配列」を渡してくるので、
        # numpy で配列のまま計算できるようにしておく必要がある。
        t_next = ts.tt_jd(t.tt + 0.01)
        lon_now = earth.at(t).observe(mercury).apparent().ecliptic_latlon()[0].degrees
        lon_next = earth.at(t_next).observe(mercury).apparent().ecliptic_latlon()[0].degrees
        # 0度またぎ(359度→1度など)で符号がおかしくならないよう -180〜180に正規化する
        diff = (np.asarray(lon_next) - np.asarray(lon_now) + 180) % 360 - 180
        return diff < 0

    is_retrograde.step_days = 1.0  # 1日刻みで境目を探索する

    times, states = almanac.find_discrete(t0, t1, is_retrograde)

    events = []
    for t, started_retrograde in zip(times, states):
        event_name = "水星逆行開始" if started_retrograde else "水星逆行終了"
        longitude = earth.at(t).observe(mercury).apparent().ecliptic_latlon()[0].degrees
        events.append({
            "date": to_jst_date(t),
            "event": event_name,
            "sign": sign_of(longitude),
            "theme": THEMES[event_name],
        })
    return events


def collect_venus_sign_change_events(ts, eph, t0, t1) -> list[dict]:
    """金星が次の星座へ移り変わる瞬間を集める。"""
    earth, venus = eph["earth"], eph["venus"]

    def sign_index(t):
        longitude = earth.at(t).observe(venus).apparent().ecliptic_latlon()[0].degrees
        return (np.asarray(longitude) // 30).astype(int) % 12

    sign_index.step_days = 1.0

    times, states = almanac.find_discrete(t0, t1, sign_index)

    events = []
    for t, sign_idx in zip(times, states):
        events.append({
            "date": to_jst_date(t),
            "event": "金星の星座移動",
            "sign": ZODIAC_SIGNS[int(sign_idx)],
            "theme": THEMES["金星の星座移動"],
        })
    return events


def main() -> None:
    if EPHEM_PATH.exists() and EPHEM_PATH.read_text(encoding="utf-8").strip():
        print(f"[build_ephem] {EPHEM_PATH} は既に存在し、空ではありません。上書きを避けるため何もしませんでした。")
        print("[build_ephem] 作り直したい場合は、先に既存の ephemeris.jsonl を削除してから実行してください。")
        return

    ts = load.timescale()
    # de421.bsp: 1900年〜2050年をカバーする、NASA JPL提供の標準的な天体暦データ。
    # 初回だけ自動ダウンロードされ、以後はこのフォルダにキャッシュされる。
    eph = load("de421.bsp")

    t0 = ts.now()
    t1 = ts.tt_jd(t0.tt + DAYS_AHEAD)

    events = (
        collect_moon_phase_events(ts, eph, t0, t1)
        + collect_mercury_retrograde_events(ts, eph, t0, t1)
        + collect_venus_sign_change_events(ts, eph, t0, t1)
    )

    # 日付順に並べ替えてから書き出す(post.pyが日付で検索しやすいように)。
    events.sort(key=lambda e: e["date"])

    with EPHEM_PATH.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    print(f"[build_ephem] {len(events)} 件の天体イベントを {EPHEM_PATH} に書き出しました。")


if __name__ == "__main__":
    main()
