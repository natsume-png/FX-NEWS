"""こまごました道具(日時、文字の整理など)。"""

import datetime as dt
import hashlib
import html
import re
import time
from email.utils import parsedate_to_datetime

JST = dt.timezone(dt.timedelta(hours=9), "JST")
UTC = dt.timezone.utc

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:[.,](\d+))?\s*"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


def strip_html(text, limit=None):
    """HTML のタグを取りのぞき、空白をそろえる。limit があればその文字数で切る。"""
    if not text:
        return ""
    s = html.unescape(str(text))
    s = _SCRIPT_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def parse_date(text):
    """RSS(RFC 822)や Atom(ISO 8601)の日時を UNIX 秒にする。読めなければ None。

    日付だけ(時刻なし)のものは、何時のことか分からないので None にする。
    """
    if not text:
        return None
    s = str(text).strip()
    m = _ISO_RE.match(s)
    if m:
        year, mon, day, hh, mm, ss, frac, tz = m.groups()
        micro = int((frac or "0")[:6].ljust(6, "0"))
        if tz in (None, "Z"):
            tzinfo = UTC
        else:
            sign = -1 if tz[0] == "-" else 1
            digits = tz[1:].replace(":", "")
            tzinfo = dt.timezone(sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4])))
        try:
            d = dt.datetime(int(year), int(mon), int(day), int(hh), int(mm), int(ss or 0), micro, tzinfo)
            return d.timestamp()
        except ValueError:
            return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    try:
        return d.timestamp()
    except (OverflowError, ValueError):
        return None


def jst(ts=None, fmt="%m/%d %H:%M:%S"):
    """UNIX 秒を日本時間の文字にする。"""
    if ts is None:
        ts = time.time()
    return dt.datetime.fromtimestamp(ts, JST).strftime(fmt)


def utc_day(ts=None):
    if ts is None:
        ts = time.time()
    return dt.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def utc_month(ts=None):
    if ts is None:
        ts = time.time()
    return dt.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def seconds_until_next_utc_day(ts=None):
    if ts is None:
        ts = time.time()
    d = dt.datetime.fromtimestamp(ts, UTC)
    nxt = (d + dt.timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return max(1.0, (nxt - d).total_seconds())


def cut_bytes(text, max_bytes):
    """UTF-8 で max_bytes を超えないように、文字の途中で切らずに短くする。"""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    cut = raw[: max(0, max_bytes - 3)]
    return cut.decode("utf-8", errors="ignore").rstrip() + "…"


def cut_chars(text, max_chars):
    """max_chars 文字を超えないように短くする(Pushover は、バイト数ではなく文字数で数える)。"""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def next_utc_month_start(ts=None):
    """次の月の1日 0時(協定世界時)の UNIX 秒。"""
    if ts is None:
        ts = time.time()
    d = dt.datetime.fromtimestamp(ts, UTC)
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return dt.datetime(year, month, 1, tzinfo=UTC).timestamp()


def short_hash(text):
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


_NORM_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize_for_dedupe(title, text=""):
    """同じ記事かどうかを見分けるための、記号や大文字小文字をならした文字。"""
    base = "%s|%s" % (title or "", (text or "")[:120])
    return _NORM_RE.sub("", base.lower())


def mask_secret(value):
    """鍵などを、先頭と末尾だけ残してかくす(点検の表示用)。"""
    if not value:
        return "(未設定)"
    if len(value) <= 8:
        return "設定あり"
    return "設定あり(%s…%s)" % (value[:4], value[-2:])
