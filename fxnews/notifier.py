"""スマホへの通知(ntfy という無料の通知アプリを使う)と、通知の文面づくり。

ntfy への送り方は公式説明(2026年11月に確認)の「Publish as JSON」にあわせている:
サーバーの一番上の住所(https://ntfy.sh/)に、JSON を POST する。
同じ sequence_id で送り直すと、前の通知が新しい内容に差しかわる(公式説明に名前があるのはウェブ版と Android 版)。
"""

import logging
import time

from .net import HttpError, NetError, request
from .rates import CCY_JA, CCYS, PAIR_JA, PAIRS
from .util import cut_bytes, jst, utc_day

log = logging.getLogger("fxnews.notifier")

ARROW = {"up": "↑", "down": "↓", "flat": "→"}
CONF_JA = {"high": "高", "mid": "中", "low": "低"}


def tag_line(tags, only_moves=False):
    """{'JPY': 'up', ...} を「円↑ ドル↓ ユーロ→ ポンド→」の形にする。"""
    parts = []
    for ccy in CCYS:
        d = tags.get(ccy, "flat")
        if only_moves and d == "flat":
            continue
        parts.append("%s%s" % (CCY_JA[ccy], ARROW[d]))
    return " ".join(parts)


def news_tags(res):
    return {"JPY": res["jpy"], "USD": res["usd"], "EUR": res["eur"], "GBP": res["gbp"]}


def build_news_notification(item, res):
    """ニュースの通知の (見出し, 本文, 鳴らし方の強さ) を作る。"""
    tags = news_tags(res)
    head = tag_line(tags, only_moves=True)
    title = "%s｜%s" % (head, res["title"]) if head else res["title"]
    lines = []
    if res.get("summary"):
        lines.append(res["summary"])
    if res.get("reason"):
        lines.append("▶ %s" % res["reason"])
    if res.get("raw"):
        lines.append("(解説なし: %s)" % res.get("raw_reason", "Claude が使えませんでした"))
    else:
        lines.append("%s(見立て・確からしさ: %s)" % (tag_line(tags), CONF_JA.get(res.get("confidence"), "低")))
    when = jst(item.published or item.fetched, "%H:%M")
    lines.append("%s・重要度 %d/5・%s・%s" % (res["category"], res["importance"], item.source_name, when))
    if not res.get("raw") and item.title and item.title != res["title"]:
        lines.append("原文: %s" % item.title[:160])
    priority = {5: 5, 4: 4}.get(res["importance"], 3)
    return title, "\n".join(lines), priority


def build_spike_notification(sp, cause="", recent=None):
    """急騰・急落の通知の (見出し, 本文, 鳴らし方の強さ) を作る。"""
    from .rates import fmt_price, spike_title

    head = tag_line(sp["tags"], only_moves=True)
    title = "%s｜%s" % (head, spike_title(sp)) if head else spike_title(sp)
    lines = []
    pair = sp["pair"]
    lines.append(
        "%s %s → %s(%+.2f%%、およそ%d秒で)"
        % (PAIR_JA[pair], fmt_price(pair, sp["from"]), fmt_price(pair, sp["to"]), sp["pct"], int(sp["elapsed"]))
    )
    others = ["%s %+.2f%%" % (PAIR_JA[p], sp["changes"][p]) for p in PAIRS if p != pair and p in sp["changes"]]
    if others:
        lines.append("ほかの組合せ: " + "、".join(others))
    moved = []
    for ccy in CCYS:
        d = sp["tags"].get(ccy)
        if d == "up":
            moved.append("%sが買われている(%+.2f%%)" % (CCY_JA[ccy], sp["strength"][ccy]))
        elif d == "down":
            moved.append("%sが売られている(%+.2f%%)" % (CCY_JA[ccy], sp["strength"][ccy]))
    if moved:
        lines.append("動いた通貨: " + "、".join(moved))
    if cause:
        lines.append("▶ 考えられる原因: %s" % cause)
    if recent:
        lines.append("直前のニュース:")
        for row in recent[:3]:
            lines.append("・[%s] %s" % (jst(row["ts"], "%H:%M"), (row.get("title_ja") or row.get("title") or "")[:60]))
    lines.append("急騰急落・%s" % jst(sp["ts"], "%H:%M:%S"))
    priority = 5 if sp["ratio"] >= 2 else 4
    return title, "\n".join(lines), priority


class Notifier:
    def __init__(self, cfg, store):
        self.server = cfg.get("ntfy", "server").rstrip("/")
        self.topic = cfg.get("ntfy", "topic")
        self.token = cfg.get("ntfy", "access_token")
        self.daily_limit = cfg.getint("ntfy", "daily_limit")
        self.store = store
        self.last_error = ""

    def available(self):
        return bool(self.topic)

    def sent_today(self):
        return int(self.store.usage_get(utc_day(), "ntfy"))

    def send(self, title, message, priority=3, click=None, sequence_id=None, tags=None):
        """通知を1通送る。戻り値は (成功か, だめだった理由)。"""
        if not self.available():
            return False, "通知のあて先(ntfy の topic)が未設定"
        if self.daily_limit > 0 and self.sent_today() >= self.daily_limit:
            return False, "きょうの通知の上限(%d通)に達した" % self.daily_limit
        body = {
            "topic": self.topic,
            "title": cut_bytes(title, 240),
            "message": cut_bytes(message, 3800),
            "priority": int(min(5, max(1, priority))),
        }
        if click:
            body["click"] = click
        if sequence_id:
            body["sequence_id"] = str(sequence_id)
        if tags:
            body["tags"] = list(tags)
        headers = {}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        last = ""
        for attempt in range(2):
            try:
                request(self.server + "/", method="POST", headers=headers, data=body, timeout=10)
                self.store.usage_add(utc_day(), "ntfy", 1)
                return True, ""
            except HttpError as exc:
                last = "ntfy がエラーを返しました(%s)" % exc
                if exc.status in (400, 401, 403, 404, 413, 429):
                    break
            except NetError as exc:
                last = "ntfy につながりません(%s)" % exc
            time.sleep(1.0 + attempt)
        self.last_error = last
        log.warning(last)
        return False, last
