"""答え合わせ: 通知につけた札(円↑ など)が、そのあとの値動きと合っていたかを数える。"""

import json
import time

CCY_KEYS = (("jpy", "JPY", "円"), ("usd", "USD", "ドル"), ("eur", "EUR", "ユーロ"), ("gbp", "GBP", "ポンド"))
STEPS = (("out5", "5分後"), ("out15", "15分後"), ("out60", "60分後"))
FLAT_BAND = 0.02  # これ未満(%)しか動かなかったら「ほぼ動かず」


def judge(direction, moved):
    """札の向きと、実際の動き(%)から 'hit' / 'miss' / 'flat' を返す。"""
    if abs(moved) < FLAT_BAND:
        return "flat"
    return "hit" if (moved > 0) == (direction == "up") else "miss"


def tally(rows):
    """{段階: {分類: {'hit': n, 'miss': n, 'flat': n}}} を作る。"""
    result = {}
    for row in rows:
        try:
            analysis = json.loads(row["analysis"])
        except (TypeError, ValueError):
            continue
        if analysis.get("raw") or analysis.get("category") == "急騰急落":
            continue
        category = analysis.get("category", "その他")
        for col, _label in STEPS:
            if not row.get(col):
                continue
            try:
                outcome = json.loads(row[col])
            except ValueError:
                continue
            for key, ccy, _ja in CCY_KEYS:
                direction = analysis.get(key)
                if direction not in ("up", "down") or ccy not in outcome:
                    continue
                verdict = judge(direction, float(outcome[ccy]))
                for cat in (category, "合計"):
                    slot = result.setdefault(col, {}).setdefault(cat, {"hit": 0, "miss": 0, "flat": 0})
                    slot[verdict] += 1
    return result


def run_report(store, days=7, out=print):
    rows = store.outcome_rows(time.time() - days * 86400)
    result = tally(rows)
    out("答え合わせ(直近 %d日。通知 %d件)" % (days, len(rows)))
    out("札(↑・↓)をつけた通貨が、そのあと本当にその向きに動いたかを数えています。")
    out("「ほぼ動かず」は、動きが %.2f%% 未満だったものです。" % FLAT_BAND)
    if not result:
        out("")
        out("まだ記録がありません(通知してから5分以上たつと、記録がたまりはじめます)。")
        return result
    for col, label in STEPS:
        if col not in result:
            continue
        out("")
        out("■ %s" % label)
        cats = result[col]
        for cat in sorted(cats, key=lambda c: (c == "合計", c)):
            s = cats[cat]
            decided = s["hit"] + s["miss"]
            rate = "%d%%" % round(100.0 * s["hit"] / decided) if decided else "-"
            out("  %-8s 当たり %3d / 外れ %3d / ほぼ動かず %3d  (当たりの割合 %s)" % (cat, s["hit"], s["miss"], s["flat"], rate))
    return result
