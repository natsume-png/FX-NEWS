"""サイトの「更新一覧」(RSS・Atom)を見張る。

更新一覧とは、サイトが「新しい記事を出した」と機械向けに公開している一覧のこと。
"""

import html
import logging
import random
import re
import threading
import time
import xml.etree.ElementTree as ET

from .models import Item
from .net import HttpError, NetError, request, retry_after_seconds
from .util import parse_date, strip_html, utc_day

log = logging.getLogger("fxnews.feeds")

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_XML_DECL_RE = re.compile(r"^<\?xml[^>]*\?>")
_ENC_RE = re.compile(rb"<\?xml[^>]*encoding=[\"']([A-Za-z0-9_.\-]+)[\"']")


class FeedError(Exception):
    """更新一覧として読めなかった。"""


def _local(tag):
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _fix_entities(text):
    """XML にない実体(&nbsp; など)を、ふつうの文字に直す。"""

    def rep(m):
        name = m.group(1)
        if name in ("amp", "lt", "gt", "quot", "apos"):
            return m.group(0)
        ch = html.unescape("&%s;" % name)
        return "" if ch.startswith("&") else ch

    return _ENTITY_RE.sub(rep, text)


def _parse_xml(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    data = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if re.search(rb"<!ENTITY", data, re.I):
        raise FeedError("実体宣言(ENTITY)をふくむ XML は、安全のため読みません")
    try:
        return ET.fromstring(data)
    except (ET.ParseError, ValueError, LookupError):
        pass
    m = _ENC_RE.match(data[:300])
    enc = m.group(1).decode("ascii", errors="replace") if m else "utf-8"
    try:
        text = data.decode(enc, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    text = _XML_DECL_RE.sub("", text.lstrip())
    text = _CTRL_RE.sub("", text)
    text = _fix_entities(text)
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise FeedError("XML として読めません: %s" % exc) from None


def _text(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def _entry(el):
    d = {"title": "", "link": "", "id": "", "summary": "", "published": None}
    alt = ""
    other_link = ""
    pub = None
    upd = None
    for ch in el:
        name = _local(ch.tag)
        if name == "title":
            if not d["title"]:
                d["title"] = strip_html(_text(ch), 400)
        elif name == "link":
            href = ch.get("href")
            if href:
                rel = (ch.get("rel") or "alternate").lower()
                if rel == "alternate" and not alt:
                    alt = href.strip()
                elif not other_link:
                    other_link = href.strip()
            else:
                t = _text(ch)
                if t and not alt:
                    alt = t
        elif name in ("guid", "id"):
            if not d["id"]:
                d["id"] = _text(ch)
        elif name in ("description", "summary", "content", "encoded"):
            t = strip_html(_text(ch), 3000)
            if len(t) > len(d["summary"]):
                d["summary"] = t
        elif name in ("pubdate", "published", "date", "issued"):
            if pub is None:
                pub = parse_date(_text(ch))
        elif name in ("updated", "modified"):
            if upd is None:
                upd = parse_date(_text(ch))
    d["link"] = alt or other_link
    d["published"] = pub if pub is not None else upd
    if not d["id"]:
        d["id"] = d["link"] or d["title"]
    return d


def parse_feed(data):
    """RSS 2.0 / RSS 1.0(RDF) / Atom のどれでも、記事の一覧(dict のリスト)にする。"""
    root = _parse_xml(data)
    out = []
    for el in root.iter():
        if _local(el.tag) in ("item", "entry"):
            d = _entry(el)
            if d["title"] or d["link"]:
                out.append(d)
    return out


class FeedPoller(threading.Thread):
    """1つの更新一覧を、決めた秒数おきに見に行く係。"""

    def __init__(self, spec, store, emit, stop, max_age_sec=6 * 3600, health=None, replay=0):
        super().__init__(name="feed-%s" % spec["id"], daemon=True)
        self.spec = spec
        self.id = spec["id"]
        self.name_ja = spec.get("name", spec["id"])
        self.url = spec["url"]
        self.kind = spec.get("kind", "news")
        self.interval = float(spec.get("interval_sec", 30))
        self.store = store
        self.emit = emit
        self.stop_event = stop
        self.max_age = max_age_sec
        self.health = health
        self.replay = replay
        self._replayed = False
        self.etag = None
        self.last_modified = None
        self.last_count = 0
        self.last_wire = 0
        self.last_size = 0

    def fetch(self):
        """いまの一覧を取ってくる。前回から変わっていなければ None。"""
        headers = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        resp = request(self.current_url(), headers=headers, timeout=15)
        if resp.status == 304:
            return None
        entries = parse_feed(resp.body)
        self.etag = resp.headers.get("etag")
        self.last_modified = resp.headers.get("last-modified")
        self.last_count = len(entries)
        self.last_wire = resp.wire_len
        self.last_size = len(resp.body)
        return entries

    def current_url(self):
        """住所の中の {yesterday} を、きのうの日付(協定世界時)に置きかえる。

        「この日より後の記事だけ」と指定できる一覧で、毎回の受け取りを小さくするために使う。
        """
        if "{yesterday}" not in self.url:
            return self.url
        return self.url.replace("{yesterday}", utc_day(time.time() - 86400))

    def to_item(self, entry):
        return Item(
            source=self.id,
            source_name=self.name_ja,
            kind=self.kind,
            key="%s|%s" % (self.id, entry["id"]),
            title=entry["title"] or "(見出しなし)",
            url=entry["link"],
            text=entry["summary"],
            published=entry["published"],
        )

    def poll_once(self):
        """新しく出た記事だけを Item にして返す。

        はじめて見る一覧は、いま載っている記事をすべて「もう見た」ことにして、何も返さない
        (起動したとたんに古い記事が大量に通知されるのをふせぐ)。
        """
        entries = self.fetch()
        if entries is None:
            return []
        first = not self.store.is_initialized(self.id)
        now = time.time()
        fresh = []
        for entry in entries:
            key = "%s|%s" % (self.id, entry["id"])
            if not self.store.mark_seen(key):
                continue
            if first:
                continue
            if entry["published"] is not None and now - entry["published"] > self.max_age:
                continue
            fresh.append(self.to_item(entry))
        if first:
            self.store.set_initialized(self.id)
        if self.replay > 0 and not self._replayed:
            # お試し用: 起動して最初の1回だけ、いちばん新しい記事を「新着」としてあつかう
            self._replayed = True
            dated = sorted(entries, key=lambda e: e["published"] or 0, reverse=True)
            fresh = [self.to_item(e) for e in dated[: self.replay]]
            for item in fresh:
                item.extra["replay"] = True
        fresh.sort(key=lambda it: it.published or it.fetched)
        return fresh

    def run(self):
        delay = random.uniform(0.0, min(3.0, self.interval))
        fails = 0
        while not self.stop_event.wait(delay):
            try:
                for item in self.poll_once():
                    self.emit(item)
                fails = 0
                if self.health:
                    self.health.ok(self.id)
                delay = self.interval * random.uniform(1.0, 1.1)
            except (NetError, HttpError, FeedError) as exc:
                fails += 1
                delay = min(600.0, self.interval * (2 ** min(fails, 6)))
                if isinstance(exc, HttpError) and exc.status in (403, 429):
                    delay = max(delay, retry_after_seconds(exc, 120.0))
                if self.health:
                    self.health.fail(self.id, self.name_ja, str(exc))
                log.warning("%s: 取れませんでした(%s)。%d秒後にやり直します", self.name_ja, exc, int(delay))
            except Exception:
                fails += 1
                delay = min(600.0, self.interval * (2 ** min(fails, 6)))
                log.exception("%s: 思わぬエラー", self.name_ja)
