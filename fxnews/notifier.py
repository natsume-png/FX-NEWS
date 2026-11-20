"""スマホへの通知と、通知の文面づくり。

送る先は2つから選べる(config.ini の [notify] service)。

■ ntfy(無料の通知アプリ)
  公式説明(2026年11月に確認)の「Publish as JSON」にあわせている:
  サーバーの一番上の住所(https://ntfy.sh/)に、JSON を POST する。
  同じ sequence_id で送り直すと、前の通知が新しい内容に差しかわる(公式説明に名前があるのはウェブ版と Android 版)。

■ Pushover(通知ごとに音を選べる通知アプリ)
  公式説明 https://pushover.net/api (2026年11月20日に確認)にあわせている:
  - 送り先: https://api.pushover.net/1/messages.json に POST。token(送り主の番号)・user(受け取り番号)・message が必須。
  - 本文は 1024文字まで、見出しは 250文字まで、リンクは 512文字まで。
  - priority: -1 は音なし、0 はふつう、1 は Pushover の「おやすみ時間」の設定があっても鳴る。
  - 送れる数: 1つのアカウントで月 10,000通。そのアカウントの送り主ぜんぶで分け合う(2026年5月から)。
    届くと、月が変わるまで、そのアカウントのどの送り主も送れなくなる(HTTP 429)。
    残りの数は、返事の見出し X-Limit-App-Remaining で分かる。
  - 4xx(受けつけられない)は、同じ内容を送り直さない。5xx やつながらないときは、5秒以上あけてからやり直す。
  - 同時につなぐのは2本まで。
  前の通知を差しかえる仕組みはないので、急騰急落の「原因つき」は、音なしの別の通知として送る。
"""

import json
import logging
import re
import threading
import time
from urllib.parse import urlencode

from .net import HttpError, NetError, request
from .rates import CCY_JA, CCYS, PAIR_JA, PAIRS
from .util import cut_bytes, cut_chars, jst, next_utc_month_start, utc_day, utc_month

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


SERVICES = ("ntfy", "pushover")
SERVICE_JA = {"ntfy": "ntfy", "pushover": "Pushover"}
PUSHOVER_KEY_RE = re.compile(r"^[A-Za-z0-9]{30}$")  # Pushover の番号は、英数字ちょうど30文字
# Pushover にはじめから入っている音(公式説明の一覧。2026年11月20日に確認)
PUSHOVER_SOUNDS = (
    "pushover", "bike", "bugle", "cashregister", "classical", "cosmic", "falling", "gamelan", "incoming",
    "intermission", "magic", "mechanical", "pianobar", "siren", "spacealarm", "tugboat", "alien", "climb",
    "persistent", "echo", "updown", "vibrate", "none",
)
PUSHOVER_RETRY_WAIT = 5.0  # やり直すまでに待つ秒数(Pushover の決まり: 5秒以上あける)


def pushover_priority(priority, followup=False):
    """このプログラムの鳴らし方の強さ(1〜5)を、Pushover の priority(-1・0・1)に直す。

    1・2 → -1(音なし)、3 → 0(ふつう)、4・5 → 1(Pushover の「おやすみ時間」でも鳴る)。
    前の通知のつづき(followup)は、2回鳴らさないように -1 にする。
    2(止めるまで鳴りつづける)は使わない。
    """
    if followup or priority <= 2:
        return -1
    return 0 if priority == 3 else 1


class Notifier:
    """スマホに通知を送る係。ntfy か Pushover の、設定で選んだほうに送る。"""

    def __init__(self, cfg, store):
        self.store = store
        self.last_error = ""
        service = cfg.get("notify", "service").lower()
        self.bad_service = "" if service in SERVICES else service
        self.service = service if service in SERVICES else "ntfy"
        # ntfy
        self.server = cfg.get("ntfy", "server").rstrip("/")
        self.topic = cfg.get("ntfy", "topic")
        self.token = cfg.get("ntfy", "access_token")
        # Pushover
        self.po_user = cfg.get("pushover", "user_key")
        self.po_token = cfg.get("pushover", "api_token")
        self.po_base = cfg.get("pushover", "api_base").rstrip("/")
        self.po_device = cfg.get("pushover", "device")
        self.po_sounds = {
            "news": cfg.get("pushover", "sound_news"),
            "important": cfg.get("pushover", "sound_important"),
            "spike": cfg.get("pushover", "sound_spike"),
            "system": cfg.get("pushover", "sound_system"),
        }
        self.monthly_limit = cfg.getint("pushover", "monthly_limit")
        self.reserve = cfg.getint("pushover", "reserve")
        self.remaining = None  # Pushover のアカウントぜんぶの、今月の残り(返事の見出しから分かったとき)
        self.account_limit = None
        self.retry_wait = PUSHOVER_RETRY_WAIT
        self._blocked_until = 0.0  # Pushover に「今月はもう送れない」と言われたら、この時刻まで送らない
        self._limit_reset = 0.0  # アカウントの送れる数が元にもどる時刻(返事の見出しから分かったとき)
        self._po_lock = threading.Lock()
        self.daily_limit = cfg.getint(self.service, "daily_limit")
        self._local = threading.local()  # 係(スレッド)ごとの覚え書き

    @property
    def limit_reason(self):
        """この係(スレッド)がさいごに送ろうとしたとき、上限のせいで送らなかったなら、その理由。ちがえば空文字。"""
        return getattr(self._local, "limit_reason", "")

    @limit_reason.setter
    def limit_reason(self, value):
        self._local.limit_reason = value

    # ---- いまの状態 ----
    @property
    def name_ja(self):
        return SERVICE_JA[self.service]

    @property
    def can_replace(self):
        """前に送った通知を、あとから差しかえられるか。"""
        return self.service == "ntfy"

    def available(self):
        if self.service == "pushover":
            return bool(PUSHOVER_KEY_RE.match(self.po_user) and PUSHOVER_KEY_RE.match(self.po_token))
        return bool(self.topic)

    def missing(self):
        """送れない理由(設定が足りないとき)。足りていれば空文字。"""
        if self.available():
            return ""
        if self.service == "pushover":
            return "Pushover の番号(config.ini の [pushover] user_key と api_token)が未設定、または形がちがう"
        return "通知のあて先(ntfy の topic)が未設定"

    def sent_today(self):
        return int(self.store.usage_get(utc_day(), self.service))

    def sent_month(self):
        return int(self.store.usage_get(utc_month(), self.service))

    def _count(self):
        self.store.usage_add(utc_day(), self.service, 1)
        self.store.usage_add(utc_month(), self.service, 1)

    def limits_text(self):
        """きょうと今月に送った数と上限(画面や通知に出す用)。"""
        text = "きょう %d通 / 上限 %d通" % (self.sent_today(), self.daily_limit)
        if self.service == "pushover":
            text += "、今月 %d通 / 上限 %d通" % (self.sent_month(), self.monthly_limit)
            if self.remaining is not None:
                text += "、アカウントぜんぶの今月の残り %d通" % self.remaining
        return text

    # ---- 送る ----
    def send(self, title, message, priority=3, click=None, sequence_id=None, tags=None, kind="news", followup=False):
        """通知を1通送る。戻り値は (成功か, だめだった理由)。

        kind は通知の種類(news・spike・system)。Pushover では、種類ごとに音を変える。
        followup は、前に送った通知のつづき(急騰急落の「原因つき」)のとき True。
        """
        self.limit_reason = ""
        if not self.available():
            return False, self.missing()
        if kind != "system" and self.daily_limit > 0 and self.sent_today() >= self.daily_limit:
            self.limit_reason = "きょうの通知の上限(%d通)に達した" % self.daily_limit
            return False, self.limit_reason
        if self.service == "pushover":
            return self._send_pushover(title, message, priority, click, kind, followup)
        return self._send_ntfy(title, message, priority, click, sequence_id, tags)

    def _send_ntfy(self, title, message, priority, click, sequence_id, tags):
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
                self._count()
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

    # ---- Pushover ----
    def pushover_sound(self, kind, priority):
        """通知の種類と強さから、鳴らす音の名前を決める。"""
        if kind == "news" and priority >= 4:
            kind = "important"
        return self.po_sounds.get(kind) or self.po_sounds.get("news") or ""

    def _note_pushover_limits(self, headers):
        """返事の見出しから、アカウントぜんぶの今月の上限と残りを読む。"""
        try:
            if headers.get("x-limit-app-remaining") is not None:
                self.remaining = int(headers["x-limit-app-remaining"])
            if headers.get("x-limit-app-limit") is not None:
                self.account_limit = int(headers["x-limit-app-limit"])
            if headers.get("x-limit-app-reset") is not None:
                self._limit_reset = float(headers["x-limit-app-reset"])
        except (TypeError, ValueError):
            pass

    def _send_pushover(self, title, message, priority, click, kind, followup):
        now = time.time()
        if self._limit_reset and now >= self._limit_reset:
            # 月が変わって、送れる数が元にもどった。前に聞いた残りの数は、もう古いので忘れる
            self.remaining = None
            self._limit_reset = 0.0
        if now < self._blocked_until:
            self.limit_reason = "Pushover の今月の送れる数を使い切ったので、%s(日本時間)まで送れません" % jst(self._blocked_until, "%m/%d %H:%M")
            return False, self.limit_reason
        if kind != "system":
            if self.monthly_limit > 0 and self.sent_month() >= self.monthly_limit:
                self.limit_reason = "今月の通知の上限(%d通)に達した" % self.monthly_limit
                return False, self.limit_reason
            if self.reserve > 0 and self.remaining is not None and self.remaining <= self.reserve:
                self.limit_reason = "Pushover のアカウントぜんぶの今月の残りが %d通になった(ほかの通知のために残す数は %d通)" % (self.remaining, self.reserve)
                return False, self.limit_reason
        po_priority = pushover_priority(priority, followup)
        fields = {
            "token": self.po_token,
            "user": self.po_user,
            "title": cut_chars(title, 250),
            "message": cut_chars(message, 1024) or "-",
            "priority": str(po_priority),
        }
        sound = self.pushover_sound(kind, priority)
        if sound and po_priority >= 0:
            fields["sound"] = sound
        if self.po_device:
            fields["device"] = self.po_device
        if click and len(click) <= 512:
            fields["url"] = click
            fields["url_title"] = "もとの記事を開く"
        data = urlencode(fields).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        last = ""
        with self._po_lock:  # 1通ずつ順番に送る(Pushover の決まり: 同時につなぐのは2本まで)
            for attempt in range(2):
                try:
                    resp = request(self.po_base + "/1/messages.json", method="POST", headers=headers, data=data, timeout=10)
                    self._note_pushover_limits(resp.headers)
                    try:
                        status = resp.json().get("status")
                    except (ValueError, AttributeError):
                        status = None
                    if status == 1:
                        self._count()
                        return True, ""
                    last = "Pushover の返事が読めませんでした(%s)" % " ".join(resp.text().split())[:80]
                    break
                except HttpError as exc:
                    self._note_pushover_limits(exc.headers)
                    if exc.status == 429:
                        reset = 0.0
                        try:
                            reset = float(exc.headers.get("x-limit-app-reset") or 0)
                        except (TypeError, ValueError):
                            reset = 0.0
                        self._blocked_until = reset if reset > time.time() else next_utc_month_start()
                        last = "Pushover の今月の送れる数を使い切りました(%s)" % exc
                        self.limit_reason = last
                        break
                    if 400 <= exc.status < 500:
                        last = "Pushover が受けつけませんでした(%s)" % _pushover_errors(exc)
                        break  # 同じ内容を送り直しても通らないので、やり直さない
                    last = "Pushover がエラーを返しました(%s)" % exc
                except NetError as exc:
                    last = "Pushover につながりません(%s)" % exc
                if attempt == 0:
                    time.sleep(self.retry_wait)
        self.last_error = last
        log.warning(last)
        return False, last

    def pushover_get(self, path):
        """Pushover に、送り主の番号をつけて問い合わせる(残りの数、音の一覧)。戻り値は dict。"""
        resp = request("%s%s?%s" % (self.po_base, path, urlencode({"token": self.po_token})), timeout=10)
        self._note_pushover_limits(resp.headers)
        return resp.json()


def _pushover_errors(exc):
    """Pushover のエラーの返事から、理由の文を取り出す(番号は出さない)。"""
    try:
        errors = json.loads(exc.body.decode("utf-8", errors="replace")).get("errors") or []
        text = "; ".join(str(e) for e in errors)
    except (ValueError, AttributeError):
        text = ""
    return ("HTTP %s %s" % (exc.status, text[:160])).strip()
