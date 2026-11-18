"""米国の会社の届け出(決算など)を見張る。

出どころは、米国の証券取引委員会(SEC)の EDGAR という公開の仕組み。
SEC の決まり: 取りに行くときは連絡先(名前とメールアドレス)を名乗る。1秒に10回まで。
"""

import json
import logging
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

from .feeds import FeedError, parse_feed
from .models import Item
from .net import HttpError, NetError, Session, retry_after_seconds
from .util import strip_html

log = logging.getLogger("fxnews.edgar")

ATOM_PATH = "/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb=&owner=include&start=0&count=100&output=atom"
TICKERS_PATH = "/files/company_tickers.json"

_CIK_RE = re.compile(r"\((\d{10})\)")
_ACC_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")
_ROW_RE = re.compile(r"(?is)<tr[^>]*>(.*?)</tr>")
_EX99_RE = re.compile(r">\s*EX-99(\.\d+)?\s*<", re.I)
_HREF_RE = re.compile(r'href="([^"]+\.html?)"', re.I)

# 8-K という届け出の「項目番号」のうち、よく出るものの日本語
ITEM_JA = {
    "2.02": "決算の発表",
    "7.01": "投資家向けの情報開示",
    "8.01": "そのほかの重要な出来事",
    "5.02": "役員の交代",
    "1.01": "重要な契約の締結",
    "2.05": "事業の整理・人員削減",
    "9.01": "添付資料",
}


def user_agent(contact):
    return "FX-NEWS %s" % contact.strip()


def parse_tickers(payload):
    """SEC の対応表(company_tickers.json)を {ティッカー: (CIK番号, 会社名)} にする。"""
    out = {}
    rows = payload.values() if isinstance(payload, dict) else payload
    for row in rows:
        try:
            out[str(row["ticker"]).upper()] = (int(row["cik_str"]), str(row.get("title", "")))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def describe_items(summary):
    """届け出の説明文から「Item 2.02」などを拾い、日本語をそえる。"""
    found = []
    for num in re.findall(r"Item\s+(\d\.\d{2})", summary or ""):
        if num not in found:
            found.append(num)
    return ["%s(%s)" % (ITEM_JA.get(n, "項目"), n) for n in found], found


def find_press_release_url(index_html, index_url):
    """届け出の目次ページから、添付の発表文(EX-99)の場所を探す。なければ空文字。"""
    for row in _ROW_RE.findall(index_html or ""):
        if _EX99_RE.search(row):
            m = _HREF_RE.search(row)
            if m:
                href = m.group(1)
                if "/ix?doc=" in href:
                    href = href.split("/ix?doc=", 1)[1]
                return urljoin(index_url, href)
    return ""


class EdgarPoller(threading.Thread):
    def __init__(self, spec, contact, base, store, emit, stop, data_dir, health=None, max_age_sec=6 * 3600):
        super().__init__(name="edgar", daemon=True)
        self.tickers = [t.upper() for t in spec.get("tickers", [])]
        self.forms = spec.get("forms", ["8-K"])
        self.interval = float(spec.get("interval_sec", 15))
        self.contact = contact
        self.base = base.rstrip("/")
        self.store = store
        self.emit = emit
        self.stop_event = stop
        self.cache_path = Path(data_dir) / "edgar_ciks.json"
        self.health = health
        self.max_age = max_age_sec
        self.ciks = {}  # CIK番号 -> (ティッカー, 会社名)
        self.session = Session()
        self.last_count = 0

    def headers(self):
        return {"User-Agent": user_agent(self.contact), "Accept-Encoding": "gzip, deflate"}

    def resolve(self, force=False):
        """ティッカー(NVDA など)を、SEC の会社番号(CIK)に直す。結果は7日間おぼえておく。"""
        cached = {}
        if self.cache_path.exists() and not force:
            try:
                blob = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if time.time() - blob.get("ts", 0) < 7 * 86400:
                    cached = blob.get("map", {})
            except (ValueError, OSError):
                cached = {}
        if not all(t in cached for t in self.tickers):
            resp = self.session.get(self.base + TICKERS_PATH, headers=self.headers(), timeout=30, max_bytes=20000000)
            table = parse_tickers(resp.json())
            cached = {t: list(table[t]) for t in self.tickers if t in table}
            missing = [t for t in self.tickers if t not in table]
            if missing:
                log.warning("SEC の対応表に見つからないティッカー: %s", ", ".join(missing))
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps({"ts": time.time(), "map": cached}), encoding="utf-8")
            except OSError:
                pass
        self.ciks = {int(v[0]): (t, v[1]) for t, v in cached.items()}
        return self.ciks

    def fetch_form(self, form):
        resp = self.session.get(self.base + ATOM_PATH.format(form=form), headers=self.headers(), timeout=20)
        return parse_feed(resp.body)

    def fetch_press_release(self, index_url):
        """決算の届け出なら、添付の発表文の冒頭を取ってくる(できなければ空文字)。"""
        try:
            page = self.session.get(index_url, headers=self.headers(), timeout=15)
            url = find_press_release_url(page.text(), index_url)
            if not url:
                return ""
            doc = self.session.get(url, headers=self.headers(), timeout=20)
            return strip_html(doc.text(), 6000)
        except (NetError, HttpError) as exc:
            log.info("発表文を取れませんでした: %s", exc)
            return ""

    def to_item(self, entry, form):
        m = _CIK_RE.search(entry["title"])
        if not m:
            return None
        cik = int(m.group(1))
        if cik not in self.ciks:
            return None
        ticker, company = self.ciks[cik]
        acc = _ACC_RE.search(" ".join((entry["id"], entry["summary"], entry["link"])))
        key = "edgar|%s" % (acc.group(1) if acc else entry["id"])
        labels, nums = describe_items(entry["summary"])
        title = "%s(%s)が届け出 %s を提出" % (company, ticker, form)
        if labels:
            title += ": " + "、".join(labels[:3])
        return Item(
            source="edgar",
            source_name="SEC の届け出(EDGAR)",
            kind="edgar",
            key=key,
            title=title,
            url=entry["link"],
            text=entry["summary"],
            published=entry["published"],
            extra={"ticker": ticker, "company": company, "form": form, "items": nums},
        )

    def poll_once(self):
        if not self.ciks:
            self.resolve()
        fresh = []
        now = time.time()
        total = 0
        for form in self.forms:
            entries = self.fetch_form(form)
            total += len(entries)
            state = "edgar|%s" % form
            first = not self.store.is_initialized(state)
            for entry in entries:
                item = self.to_item(entry, form)
                if item is None:
                    continue  # 見張っていない会社の届け出
                if not self.store.mark_seen(item.key):
                    continue
                if first:
                    continue
                if item.published is not None and now - item.published > self.max_age:
                    continue
                nums = item.extra.get("items", [])
                if item.url and form.upper().startswith("8-K") and (not nums or "2.02" in nums):
                    body = self.fetch_press_release(item.url)
                    if body:
                        item.text = "%s\n\n[発表文の冒頭]\n%s" % (item.text, body)
                fresh.append(item)
            if first:
                self.store.set_initialized(state)
        self.last_count = total
        return fresh

    def close(self):
        self.session.close()

    def run(self):
        try:
            self._loop()
        finally:
            self.close()

    def _loop(self):
        delay = random.uniform(0.0, 3.0)
        fails = 0
        while not self.stop_event.wait(delay):
            try:
                for item in self.poll_once():
                    self.emit(item)
                fails = 0
                if self.health:
                    self.health.ok("edgar")
                delay = self.interval * random.uniform(1.0, 1.1)
            except (NetError, HttpError, FeedError, ValueError) as exc:
                fails += 1
                delay = min(900.0, self.interval * (2 ** min(fails, 6)))
                if isinstance(exc, HttpError) and exc.status in (403, 429):
                    delay = max(delay, retry_after_seconds(exc, 600.0))
                if self.health:
                    self.health.fail("edgar", "SEC の届け出(EDGAR)", str(exc))
                log.warning("SEC の届け出: 取れませんでした(%s)。%d秒後にやり直します", exc, int(delay))
            except Exception:
                fails += 1
                delay = min(900.0, self.interval * (2 ** min(fails, 6)))
                log.exception("SEC の届け出: 思わぬエラー")
