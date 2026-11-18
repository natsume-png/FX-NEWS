"""X(旧Twitter)の投稿を、流れてくるそばから受け取る(Filtered Stream)。

X の公式説明(2026年11月に確認)にあわせている:
- ルール(どの投稿を受け取るかの条件)の登録: /2/tweets/search/stream/rules
- 受け取り口: /2/tweets/search/stream(つなぎっぱなし。20秒ごとに空行の合図が来る)
- 料金: 受け取った投稿1件ごと(同じ投稿は同じ日のうちは1回だけ数える)

お金がかかるので、1日と1か月の上限を自分でも数えて、届いたら止める。
投稿者の情報(includes)は、料金がどう数えられるか公式説明に書かれていないので、頼まない。
だれの投稿かは、ルールにつけた名前(tag)で見分ける。
"""

import http.client
import json
import logging
import threading
from urllib.parse import urlencode

from .models import Item
from .net import HttpError, NetError, open_stream, request, retry_after_seconds
from .util import parse_date, seconds_until_next_utc_day, utc_day, utc_month

log = logging.getLogger("fxnews.x")

RULES_PATH = "/2/tweets/search/stream/rules"
STREAM_PATH = "/2/tweets/search/stream"
FIELDS_FULL = "created_at,author_id,lang,note_tweet"
FIELDS_MIN = "created_at,author_id"


def plan_rule_changes(current, desired):
    """いま X に登録されているルールと、設定に書いたルールをくらべて、消すIDと足すルールを決める。"""
    want = {}
    for rule in desired:
        value = (rule.get("value") or "").strip()
        if value:
            want[value] = (rule.get("tag") or "").strip()
    delete_ids = []
    have = set()
    for rule in current or []:
        value = (rule.get("value") or "").strip()
        tag = (rule.get("tag") or "").strip()
        if value in want and want[value] == tag and value not in have:
            have.add(value)
        else:
            delete_ids.append(str(rule.get("id")))
    add = [{"value": v, "tag": t} for v, t in want.items() if v not in have]
    return delete_ids, add


def parse_stream_line(line):
    """流れてきた1行を読む。戻り値は ("post", 中身) / ("error", 説明) / (None, None)。"""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line:
        return None, None
    try:
        obj = json.loads(line)
    except ValueError:
        return None, None
    if not isinstance(obj, dict):
        return None, None
    data = obj.get("data")
    if isinstance(data, dict) and data.get("id"):
        text = data.get("text") or ""
        note = data.get("note_tweet")
        if isinstance(note, dict) and note.get("text"):
            text = note["text"]
        tags = [r.get("tag") for r in obj.get("matching_rules", []) if isinstance(r, dict) and r.get("tag")]
        return "post", {
            "id": str(data["id"]),
            "text": text,
            "created_at": parse_date(data.get("created_at")),
            "author_id": str(data.get("author_id") or ""),
            "tags": tags,
        }
    if obj.get("errors"):
        return "error", json.dumps(obj.get("errors"), ensure_ascii=False)[:300]
    return None, None


class XStream(threading.Thread):
    def __init__(self, rules, cfg, store, emit, stop, alert=None, health=None):
        super().__init__(name="x-stream", daemon=True)
        self.rules = rules or []
        self.base = cfg.get("x", "api_base").rstrip("/")
        self.token = cfg.get("x", "bearer_token")
        self.price = cfg.getfloat("x", "price_per_post_usd")
        self.cap_yen = cfg.getfloat("x", "monthly_cap_yen")
        self.day_cap = cfg.getint("x", "daily_cap_posts")
        self.usd_jpy = cfg.getfloat("general", "usd_jpy")
        self.store = store
        self.emit = emit
        self.stop_event = stop
        self.alert = alert  # 上限に届いたときなどに、利用者に知らせるための関数
        self.health = health
        self.fields = FIELDS_FULL
        self._alerted = set()
        self.state = "未接続"

    def enabled(self):
        return bool(self.token) and bool(self.rules)

    def headers(self):
        return {"Authorization": "Bearer %s" % self.token}

    # ---- 上限 ----
    def month_cap_posts(self):
        if self.cap_yen <= 0 or self.price <= 0:
            return 0
        return int(self.cap_yen / self.usd_jpy / self.price)

    def counts(self):
        return (
            int(self.store.usage_get(utc_day(), "x_posts")),
            int(self.store.usage_get(utc_month(), "x_posts")),
        )

    def spent_yen(self):
        return self.counts()[1] * self.price * self.usd_jpy

    def budget_block(self):
        """上限に届いていれば ("day" か "month", 説明) を返す。届いていなければ None。"""
        day, month = self.counts()
        cap = self.month_cap_posts()
        if cap and month >= cap:
            return "month", "X の今月の上限(%d円 = %d件)に届きました。来月まで X の読み取りを止めます" % (
                int(self.cap_yen),
                cap,
            )
        if self.day_cap and day >= self.day_cap:
            return "day", "X のきょうの上限(%d件)に届きました。日付が変わるまで(日本時間の午前9時)止めます" % self.day_cap
        return None

    # ---- ルール ----
    def get_rules(self):
        resp = request(self.base + RULES_PATH, headers=self.headers(), timeout=20)
        return resp.json().get("data") or []

    def sync_rules(self):
        """設定(sources.json)に書いたルールを、X の側に登録する。"""
        delete_ids, add = plan_rule_changes(self.get_rules(), self.rules)
        if delete_ids:
            request(
                self.base + RULES_PATH,
                method="POST",
                headers=self.headers(),
                data={"delete": {"ids": delete_ids}},
                timeout=20,
            )
        if add:
            resp = request(self.base + RULES_PATH, method="POST", headers=self.headers(), data={"add": add}, timeout=20)
            errors = resp.json().get("errors")
            if errors:
                log.warning("X: 登録できなかったルールがあります: %s", json.dumps(errors, ensure_ascii=False)[:500])
        log.info("X: ルールをそろえました(消した %d、足した %d)", len(delete_ids), len(add))

    # ---- 受け取り ----
    def to_item(self, post):
        name = " / ".join(post["tags"]) if post["tags"] else "X の投稿"
        first_line = " ".join(post["text"].split())
        return Item(
            source="x",
            source_name="X: %s" % name,
            kind="x",
            key="x|%s" % post["id"],
            title=first_line[:140],
            url="https://x.com/i/web/status/%s" % post["id"],
            text=post["text"],
            published=post["created_at"],
            extra={"author_id": post["author_id"], "tags": post["tags"]},
        )

    def handle_line(self, line):
        kind, payload = parse_stream_line(line)
        if kind == "error":
            log.warning("X: 相手からの知らせ: %s", payload)
            return None
        if kind != "post":
            return None
        if not self.store.mark_seen("x|%s" % payload["id"]):
            return None
        self.store.usage_add(utc_day(), "x_posts", 1)
        self.store.usage_add(utc_month(), "x_posts", 1)
        return self.to_item(payload)

    def read_stream(self):
        """つないで、切れるか上限に届くまで読みつづける。"""
        url = "%s%s?%s" % (self.base, STREAM_PATH, urlencode({"tweet.fields": self.fields}))
        resp = open_stream(url, headers=self.headers(), timeout=45)
        self.state = "受信中"
        if self.health:
            self.health.ok("x")
        try:
            while not self.stop_event.is_set():
                line = resp.readline()
                if not line:
                    return  # 相手が接続を切った
                item = self.handle_line(line)
                if item is not None:
                    self.emit(item)
                    if self.budget_block():
                        return
        finally:
            try:
                resp.close()
            except Exception:
                pass
            self.state = "未接続"

    def _tell(self, key, message):
        """同じ知らせを何度も出さないようにして、利用者に知らせる。"""
        if key in self._alerted:
            return
        self._alerted.add(key)
        log.warning(message)
        if self.alert:
            self.alert(message)

    def run(self):
        if not self.enabled():
            return
        synced = False
        net_wait = 1.0
        http_wait = 5.0
        while not self.stop_event.is_set():
            block = self.budget_block()
            if block:
                which, message = block
                period = utc_day() if which == "day" else utc_month()
                self._tell("%s|%s" % (which, period), message)
                self.state = "上限で停止中"
                if self.stop_event.wait(min(seconds_until_next_utc_day(), 1800.0)):
                    break
                continue
            wait = 1.0
            try:
                if not synced:
                    self.sync_rules()
                    synced = True
                self.read_stream()
                net_wait = 1.0
                http_wait = 5.0
            except HttpError as exc:
                if self.health:
                    self.health.fail("x", "X", str(exc))
                if exc.status == 400 and self.fields != FIELDS_MIN:
                    self.fields = FIELDS_MIN
                    wait = 2.0
                elif exc.status in (401, 403):
                    self._tell(
                        "auth|%s" % utc_day(),
                        "X につなげません(%s)。鍵(Bearer Token)がちがうか、前払いの残高がない可能性があります" % exc,
                    )
                    wait = 900.0
                elif exc.status == 429:
                    wait = min(900.0, max(60.0, retry_after_seconds(exc, 60.0)))
                else:
                    wait = http_wait
                    http_wait = min(320.0, http_wait * 2)
                log.warning("X: エラー(%s)。%d秒後にやり直します", exc, int(wait))
            except (NetError, OSError, http.client.HTTPException, ValueError) as exc:
                if self.health:
                    self.health.fail("x", "X", str(exc))
                wait = net_wait
                net_wait = min(16.0, net_wait * 2)
                log.info("X: 接続が切れました(%s)。%d秒後につなぎ直します", exc, int(wait))
            except Exception:
                wait = 60.0
                log.exception("X: 思わぬエラー")
            if self.stop_event.wait(wait):
                break
