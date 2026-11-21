"""型ごとの成績: 「この型のニュースが届いたあと、為替はどう動いたか」を数える。

数え方(変えるときは、ここと README の説明を一緒に直す):
- 起点は、このプログラムがニュースを受け取った時刻と、そのときの値(= スマホに届くころの値)。
  発表の瞬間ではない。「知らせを見てから動いたら、どうだったか」を数えたいので、こうしている。
- 5分後・15分後・60分後・1日後の値とくらべて、上がった回数・下がった回数・平均の動きを出す。
  値は、1分ごとに記録している値(売値と買値の真ん中)から引く。
- 日本時間の朝 6:00〜7:10 に届いたもの、その時間に答えが出るものは数えない
  (売値と買値が大きく離れて、値がゆがむ時間のため)。
- 同じ型・同じ向きが30分以内に続いたら、最初の1回だけ数える(同じ出来事を二重に数えないため)。
- 前に出た話のくり返し(Claude が dup とした記事)は数えない。
- 発表から15分より後に届いた記事は数えない(見張りを止めていたあいだの記事を、あとからまとめて
  受け取ったときなど。届いたときには、もう相場が動き終わっているため)。
- 1日後は 24時間後。相場が閉まっていたら(金曜のニュースなど)、72時間後(月曜の同じ時刻)で見る。
- 回数が MIN_COUNT に届かないうちは、目安にしない。

ここで出すのは、過去に何回どう動いたか、という事実だけ。売り買いをすすめるものではない。
"""

import datetime as dt
import json
import math
import time

from .analyzer import KATA_JA, KATA_MUKI, kata_label
from .rates import PAIR_JA
from .util import JST

HORIZONS = ((300, "5分後"), (900, "15分後"), (3600, "60分後"), (86400, "1日後"))
MIN_COUNT = 20  # これより少ない回数は、まだ目安にしない
MERGE_SEC = 1800  # 同じ型・同じ向きがこの秒数のうちに続いたら、最初の1回だけ数える
DAWN_FROM = 6 * 60  # 日本時間 6:00
DAWN_TO = 7 * 60 + 10  # 日本時間 7:10
LATE_SEC = 900  # 発表からこの秒数より後に届いた記事は、数えない
LOOKBACK_DAYS = 730
SESSIONS = (("東京の時間(7:10〜15:00)", 7 * 60 + 10, 15 * 60), ("欧州の時間(15:00〜21:00)", 15 * 60, 21 * 60), ("ニューヨークの時間(21:00〜翌6:00)", 21 * 60, 30 * 60))
# A の確かめ(別の通貨ペア)で見る組合せ。型が日本の話なら円のからむペア、米国の話ならドルのからむペア。
OTHER_PAIRS = ("EUR_JPY", "GBP_JPY", "EUR_USD", "GBP_USD")
TREND_SEC = 4 * 3600  # C の確かめ(大きな流れ)で、何時間前の値とくらべるか


def jst_minutes(ts):
    d = dt.datetime.fromtimestamp(ts, JST)
    return d.hour * 60 + d.minute


def in_dawn(ts):
    """日本時間の朝 6:00〜7:10 か(この時間は数えない)。"""
    return DAWN_FROM <= jst_minutes(ts) < DAWN_TO


def pip_size(pair):
    """値動きを数える小さな単位。円のからむペアは 1銭(0.01円)、それ以外は 0.0001。"""
    return 0.01 if pair.endswith("JPY") else 0.0001


def pip_name(pair):
    return "銭" if pair.endswith("JPY") else "pips"


def price_after(store, pair, ts, sec):
    """ts の sec 秒後の値。数えない時間・値がない時間なら None。"""
    targets = [ts + sec]
    if sec >= 86400:
        targets.append(ts + sec + 2 * 86400)  # 相場が閉まっていたら(金曜のニュースなど)、月曜の同じ時刻
    for target in targets:
        if in_dawn(target):
            return None
        price = store.rate_at(pair, target)
        if price is not None:
            return price
    return None


def is_late(row):
    """発表から LATE_SEC より後に届いた記事か(発表の時刻が分からないものは、遅れていないとみなす)。"""
    published = row.get("published")
    return published is not None and row["ts"] - published > LATE_SEC


def collapse(rows):
    """数えられる記録だけを残す。rows は古い順。

    - 届くのが遅かった記事、朝の数えない時間に届いた記事は、のぞく。
    - 同じ型・同じ向きが MERGE_SEC のうちに続いたものは、最初の1回だけ残す。
    """
    kept = []
    last = {}
    for row in rows:
        if is_late(row) or in_dawn(row["ts"]):
            continue
        key = (row.get("kata"), row.get("muki"))
        if key in last and row["ts"] - last[key] < MERGE_SEC:
            continue
        last[key] = row["ts"]
        kept.append(row)
    return kept


def measure(store, rows, pair="USD_JPY", horizons=HORIZONS, now=None):
    """記録の1件ずつに、そのあとの動き(単位は 銭 か pips)をつけて返す。

    戻り値は [{…記録の中身…, "base": 起点の値, "diff": {秒: 動き か None}}]。
    朝の数えない時間に届いたもの、起点の値がないものは、入れない。
    """
    now = time.time() if now is None else now
    pip = pip_size(pair)
    out = []
    for row in rows:
        if in_dawn(row["ts"]):
            continue
        try:
            base = float(json.loads(row["mids"])[pair])
        except (TypeError, ValueError, KeyError):
            continue
        if base <= 0:
            continue
        diffs = {}
        for sec, _label in horizons:
            if row["ts"] + sec > now:
                diffs[sec] = None
                continue
            price = price_after(store, pair, row["ts"], sec)
            diffs[sec] = None if price is None else (price - base) / pip
        item = dict(row)
        item["base"] = base
        item["diff"] = diffs
        out.append(item)
    return out


def summarize(values):
    """動きの並びから、回数・上げ・下げ・平均・真ん中の値を出す。"""
    vals = [v for v in values if v is not None]
    n = len(vals)
    up = sum(1 for v in vals if v > 0)
    down = sum(1 for v in vals if v < 0)
    if n:
        ordered = sorted(vals)
        mid = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0
        mean = sum(vals) / n
    else:
        mid = mean = 0.0
    return {"n": n, "up": up, "down": down, "same": n - up - down, "mean": mean, "median": mid}


def chance_of_tilt(up, down):
    """上げと下げが本当は五分五分だとして、これ以上のかたよりが、たまたま出る確率(0〜1)。

    コインを (up + down) 回投げて、表と裏の差が、いまの差以上になる確率と同じ。
    """
    n = up + down
    if n == 0:
        return 1.0
    k = max(up, down)
    tail = sum(math.comb(n, i) for i in range(k, n + 1)) / float(2 ** n)
    return min(1.0, 2.0 * tail)


def summaries(measured, horizons=HORIZONS):
    return {sec: summarize([m["diff"].get(sec) for m in measured]) for sec, _label in horizons}


def kata_summary(store, kata, muki, min_importance=3, pair="USD_JPY", now=None):
    """ある型・ある向きの成績。{秒: {n, up, down, same, mean, median}}。"""
    now = time.time() if now is None else now
    rows = store.kata_rows(now - LOOKBACK_DAYS * 86400, kata=kata, muki=muki, min_importance=min_importance)
    return summaries(measure(store, collapse(rows), pair=pair, now=now))


def fmt_tally(s, unit="銭", with_mean=True):
    text = "上げ%d回・下げ%d回" % (s["up"], s["down"])
    if s["same"]:
        text += "・同じ%d回" % s["same"]
    if with_mean and s["n"]:
        text += "(平均%+.1f%s)" % (s["mean"], unit)
    return text


def kata_line(store, kata, muki, min_importance=3, now=None):
    """通知につける1行。「この型のニュースのあと、ドル円が過去にどう動いたか」。型がなければ空文字。"""
    if not kata or kata not in KATA_JA or kata == "other":
        return ""
    label = kata_label(kata, muki)
    try:
        summ = kata_summary(store, kata, muki, min_importance=min_importance, now=now)
    except Exception:
        return "型: %s" % label
    s15, s60 = summ[900], summ[3600]
    if s15["n"] == 0:
        return "型: %s(この型のあとのドル円の動きは、これから数えます)" % label
    if s15["n"] < MIN_COUNT:
        return "型: %s｜この型のあとのドル円(15分後): %s。まだ%d回なので、目安になりません(%d回から)" % (
            label, fmt_tally(s15, with_mean=False), s15["n"], MIN_COUNT)
    text = "型: %s｜この型のあとのドル円: 15分後は%s" % (label, fmt_tally(s15))
    if s60["n"] >= MIN_COUNT:
        text += "、60分後は%s" % fmt_tally(s60)
    return text


def usual_swing(store, pair="USD_JPY", sec=900, days=30, now=None):
    """ふだんの揺れ: sec 秒のあいだに、平均でどれだけ動くか(上げ下げを問わない大きさ)。(平均, 数えた回数)。"""
    now = time.time() if now is None else now
    bars = store.rate_bars(pair, now - days * 86400, now)
    close = {b["ts"]: b["c"] for b in bars}
    pip = pip_size(pair)
    total = 0.0
    count = 0
    for ts in sorted(close):
        if ts % sec or in_dawn(ts) or in_dawn(ts + sec):
            continue
        later = close.get(ts + sec)
        if later is None:
            continue
        total += abs(later - close[ts]) / pip
        count += 1
    return (total / count if count else 0.0), count


def session_of(ts):
    m = jst_minutes(ts)
    if m < DAWN_FROM:
        m += 24 * 60  # 0:00〜6:00 は、前の日のニューヨークの時間のつづき
    for name, start, end in SESSIONS:
        if start <= m < end:
            return name
    return ""


def trend_of(store, pair, ts, base):
    """大きな流れ: TREND_SEC 前の値より、いまが上なら 'up'、下なら 'down'。分からなければ ''。"""
    before = store.rate_at(pair, ts - TREND_SEC)
    if before is None or before == base:
        return ""
    return "up" if base > before else "down"


def yen_example(mean_pips, pair="USD_JPY", units=10000):
    """平均の動きを、「1万通貨を持っていたら何円か」に直す(円のからむペアだけ)。"""
    if not pair.endswith("JPY"):
        return ""
    yen = mean_pips * pip_size(pair) * units
    return "1万%sを持っていたとすると %+d円" % ({"USD": "ドル", "EUR": "ユーロ", "GBP": "ポンド"}.get(pair.split("_")[0], "通貨"), int(round(yen)))


def run_kata_report(store, out=print, min_importance=3, now=None):
    """型ごとの成績を、文章の表で出す。戻り値は {(型, 向き): {秒: まとめ}}。"""
    now = time.time() if now is None else now
    since = now - LOOKBACK_DAYS * 86400
    all_rows = store.kata_rows(since, min_importance=min_importance)
    rows = collapse(all_rows)
    measured = measure(store, rows, now=now)
    late = {}
    for row in all_rows:
        if is_late(row):
            late[row.get("source") or "?"] = late.get(row.get("source") or "?", 0) + 1
    out("型ごとの成績(ニュースが届いたあと、ドル円がどう動いたか)")
    out("")
    out("・数えているのは、重要度 %d 以上のニュース。届いたときのドル円の値を起点に、そのあとの値とくらべます。" % min_importance)
    out("・単位の「銭」は 0.01円。たとえば +5.0銭 は、159.20円が 159.25円になった、ということです。")
    out("・朝 6:00〜7:10(日本時間)に届いたもの、その時間に答えが出るものは、数えません(売値と買値が大きく離れる時間のため)。")
    out("・同じ型・同じ向きのニュースが30分以内に続いたら、最初の1回だけ数えます。")
    out("・発表から15分より後に届いた記事は、数えません(届いたときには、もう相場が動き終わっているため)。")
    out("・%d回に届かない型は、まだ目安になりません(たまたまの偏りと、見分けがつかないため)。" % MIN_COUNT)
    out("・過去に何回どう動いたか、という事実の集計です。これからも同じように動く、という約束ではありません。")
    out("")
    out("記録の数: 型のついたニュース %d件(うち、数えられるもの %d件)・1分ごとの値 %d本" % (store.count_kata_items(), len(measured), store.count_rate_bars()))
    if late:
        out("届くのが遅くて数えなかった記事: %s" % "、".join("%s %d件" % (k, v) for k, v in sorted(late.items())))
    swing, swing_n = usual_swing(store, now=now)
    if swing_n >= 50:
        out("ふだんの揺れ(この30日): ドル円は、15分のあいだに平均で %.1f銭 動いています(上げ下げを問わない大きさ。%d回の平均)" % (swing, swing_n))
    result = {}
    groups = {}
    for m in measured:
        groups.setdefault((m["kata"], m["muki"]), []).append(m)
    if not groups:
        out("")
        out("まだ、数えられる記録がありません(型のついたニュースが届いて、5分以上たつと、たまりはじめます)。")
        return result
    order = sorted(groups, key=lambda k: (list(KATA_JA).index(k[0]) if k[0] in KATA_JA else 99, KATA_MUKI.get(k[0], ()).index(k[1]) if k[1] in KATA_MUKI.get(k[0], ()) else 9))
    for key in order:
        kata, muki = key
        group = groups[key]
        summ = summaries(group)
        result[key] = summ
        out("")
        out("■ %s(%d件)" % (kata_label(kata, muki) or kata, len(group)))
        for sec, label in HORIZONS:
            s = summ[sec]
            if s["n"] == 0:
                out("  %s: まだ答えが出ていません" % label.rjust(4, "　"))
                continue
            line = "  %s: %d回 %s" % (label.rjust(4, "　"), s["n"], fmt_tally(s))
            if s["n"] >= MIN_COUNT:
                chance = chance_of_tilt(s["up"], s["down"])
                line += "｜%s｜たまたまでも、100回に%d回は出る偏り" % (yen_example(s["mean"]), int(round(chance * 100)))
            else:
                line += "｜まだ目安になりません"
            out(line)
        s15 = summ[900]
        if s15["n"] < MIN_COUNT:
            continue
        # ---- 確かめ A: 別の通貨ペアでも、同じ向きに動いているか
        out("  [確かめA・別の通貨ペア(15分後)]")
        for pair in OTHER_PAIRS:
            other = summaries(measure(store, group, pair=pair, now=now))[900]
            if other["n"]:
                out("    %s: %d回 %s" % (PAIR_JA[pair], other["n"], fmt_tally(other, unit=pip_name(pair))))
        # ---- 確かめ B: 時間帯で分けても、同じか
        out("  [確かめB・届いた時間帯(15分後)]")
        for name, _start, _end in SESSIONS:
            part = summarize([m["diff"].get(900) for m in group if session_of(m["ts"]) == name])
            if part["n"]:
                out("    %s: %d回 %s" % (name, part["n"], fmt_tally(part)))
        # ---- 確かめ C: 大きな流れ(4時間前とくらべて上か下か)で分けても、同じか
        out("  [確かめC・大きな流れ(届く4時間前より、ドル円が上か下か。15分後)]")
        for trend, name in (("up", "上向きのとき"), ("down", "下向きのとき")):
            part = summarize([m["diff"].get(900) for m in group if trend_of(store, "USD_JPY", m["ts"], m["base"]) == trend])
            if part["n"]:
                out("    %s: %d回 %s" % (name, part["n"], fmt_tally(part)))
    # ---- Claude の見立て(ドル円↑・↓)は、当たっていたか
    out("")
    out("■ Claude の見立て(通知の「ドル円↑」「ドル円↓」)の当たり外れ")
    any_guess = False
    for sec, label in HORIZONS[1:3]:
        hit = miss = 0
        for m in measured:
            guess = m.get("usdjpy")
            diff = m["diff"].get(sec)
            if guess not in ("up", "down") or diff is None or diff == 0:
                continue
            if (diff > 0) == (guess == "up"):
                hit += 1
            else:
                miss += 1
        if hit + miss:
            any_guess = True
            line = "  %s: 当たり%d回・外れ%d回" % (label, hit, miss)
            if hit + miss >= MIN_COUNT:
                line += "｜たまたまでも、100回に%d回は出る偏り" % int(round(chance_of_tilt(hit, miss) * 100))
            else:
                line += "｜まだ目安になりません"
            out(line)
    if not any_guess:
        out("  まだ答えが出ていません")
    return result
