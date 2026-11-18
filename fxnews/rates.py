"""為替レートを見張り、急騰・急落を見つける。

対象は、円・ドル・ユーロ・ポンドの4通貨から作れる6つの組合せ(通貨ペア)。
レートの出どころは GMOコインの公開の配信(登録も鍵もいらない)。
"""

import collections
import logging
import threading
import time

from .models import Item
from .net import HttpError, NetError, request

log = logging.getLogger("fxnews.rates")

PAIRS = ("USD_JPY", "EUR_JPY", "GBP_JPY", "EUR_USD", "GBP_USD", "EUR_GBP")
PAIR_JA = {
    "USD_JPY": "ドル円",
    "EUR_JPY": "ユーロ円",
    "GBP_JPY": "ポンド円",
    "EUR_USD": "ユーロドル",
    "GBP_USD": "ポンドドル",
    "EUR_GBP": "ユーロポンド",
}
CCYS = ("JPY", "USD", "EUR", "GBP")
CCY_JA = {"JPY": "円", "USD": "ドル", "EUR": "ユーロ", "GBP": "ポンド"}
UNIT_JA = {"JPY": "円", "USD": "ドル", "GBP": "ポンド"}
WINDOWS = ((60, "1分"), (300, "5分"), (900, "15分"))


def parse_ticker(payload):
    """GMOコインの返事を {通貨ペア: 売値と買値の真ん中} と、取引時間中のペアの集合にする。"""
    if isinstance(payload, dict):
        if payload.get("status") not in (0, "0", None):
            raise ValueError("レートの配信がエラーを返しました: %s" % str(payload)[:200])
        rows = payload.get("data") or []
    else:
        rows = payload or []
    mids = {}
    open_pairs = set()
    for row in rows:
        try:
            sym = row["symbol"]
            if sym not in PAIRS:
                continue
            ask = float(row["ask"])
            bid = float(row["bid"])
        except (KeyError, TypeError, ValueError):
            continue
        if ask <= 0 or bid <= 0:
            continue
        mids[sym] = (ask + bid) / 2.0
        if str(row.get("status", "OPEN")).upper() == "OPEN":
            open_pairs.add(sym)
    return mids, open_pairs


def pct_change(base, now):
    """基準から何%動いたか。"""
    return (now / base - 1.0) * 100.0


def currency_strength(base, now):
    """4通貨それぞれが、ほかの3通貨に対して平均で何%動いたか(プラス = 買われた)。"""
    r = {}
    for pair in PAIRS:
        if pair in base and pair in now and base[pair] > 0:
            r[pair] = pct_change(base[pair], now[pair])

    def avg(values):
        vals = [v for v in values if v is not None]
        return sum(vals) / len(vals) if vals else 0.0

    def pos(pair):
        return r.get(pair)

    def neg(pair):
        v = r.get(pair)
        return None if v is None else -v

    return {
        "JPY": avg([neg("USD_JPY"), neg("EUR_JPY"), neg("GBP_JPY")]),
        "USD": avg([pos("USD_JPY"), neg("EUR_USD"), neg("GBP_USD")]),
        "EUR": avg([pos("EUR_JPY"), pos("EUR_USD"), pos("EUR_GBP")]),
        "GBP": avg([pos("GBP_JPY"), pos("GBP_USD"), neg("EUR_GBP")]),
    }


def strength_tags(strength, floor):
    """強さの数字から、通貨ごとの札(up / down / flat)を決める。

    いちばん大きく動いた通貨の6割以上、かつ floor(%)以上動いた通貨だけに札をつける。
    たとえば円だけが買われたとき、ほかの3通貨は「円に対して下がった」だけなので flat にする。
    """
    biggest = max((abs(v) for v in strength.values()), default=0.0)
    limit = max(floor, biggest * 0.6)
    tags = {}
    for ccy in CCYS:
        v = strength.get(ccy, 0.0)
        if abs(v) >= limit and biggest > 0:
            tags[ccy] = "up" if v > 0 else "down"
        else:
            tags[ccy] = "flat"
    return tags


def fmt_price(pair, value):
    return "%.2f" % value if pair.endswith("JPY") else "%.4f" % value


def fmt_abs_change(pair, diff):
    unit = UNIT_JA[pair.split("_")[1]]
    if pair.endswith("JPY"):
        return "%.2f%s" % (abs(diff), unit)
    return "%.4f%s" % (abs(diff), unit)


class RateBook:
    """直近のレートをためておく入れ物。

    1回きりのおかしな値(配信のまちがいなど)にだまされないよう、
    「直近3回のうちの真ん中の値」をためる。本当の動きなら、2回目の値で反映される。
    """

    def __init__(self, keep_sec=1200):
        self.keep_sec = keep_sec
        self._raw = collections.deque(maxlen=3)  # 受け取ったままの値(直近3回)
        self._samples = collections.deque()  # (時刻, {ペア: ならした値})
        self._lock = threading.Lock()

    def add(self, ts, mids):
        with self._lock:
            self._raw.append(dict(mids))
            if len(self._raw) < 3:
                smooth = dict(self._raw[0])
            else:
                smooth = {}
                for pair in mids:
                    vals = [r[pair] for r in self._raw if pair in r]
                    smooth[pair] = sorted(vals)[1] if len(vals) == 3 else vals[0]
            self._samples.append((ts, smooth))
            limit = ts - self.keep_sec
            while self._samples and self._samples[0][0] < limit:
                self._samples.popleft()

    def latest(self):
        with self._lock:
            return self._samples[-1] if self._samples else (None, {})

    def since(self, ts):
        with self._lock:
            return [s for s in self._samples if s[0] >= ts]

    def __len__(self):
        with self._lock:
            return len(self._samples)


class SpikeDetector:
    """急な動きを見つける。

    「いまのレート」を「決めた時間内のいちばん安い値・高い値」とくらべ、決めた%以上なら急騰・急落とみなす。
    - 1回きりのおかしな値は、RateBook が先にならしてあるので、ここには届かない。
    - 知らせたら、その通貨ペアは cooldown 秒のあいだ休む(さらに大きく動いたときは別)。
    - 知らせたあとは、そこを新しい基準にする(同じ動きを何度も知らせない)。
    """

    def __init__(self, book, thresholds, cooldown_sec=300, confirm=1):
        self.book = book
        self.thresholds = thresholds  # {秒: %}
        self.cooldown = cooldown_sec
        self.confirm = confirm
        self._streak = {}  # (ペア, 向き) -> つづけて条件を満たした回数
        self._last = {}  # ペア -> (知らせた時刻, そのときのレート)

    def _candidate(self, pair, now, price):
        best = None
        last_ts = self._last.get(pair, (0.0, 0.0))[0]
        for sec, label in WINDOWS:
            th = self.thresholds.get(sec)
            if not th:
                continue
            lo = hi = None
            for ts, mids in self.book.since(max(now - sec, last_ts)):
                v = mids.get(pair)
                if v is None:
                    continue
                if lo is None or v < lo[0]:
                    lo = (v, ts, mids)
                if hi is None or v > hi[0]:
                    hi = (v, ts, mids)
            if lo is None:
                continue
            up = pct_change(lo[0], price)
            down = -pct_change(hi[0], price)
            if up >= th and up >= down:
                cand = {"ratio": up / th, "dir": 1, "pct": up, "base": lo}
            elif down >= th:
                cand = {"ratio": down / th, "dir": -1, "pct": -down, "base": hi}
            else:
                continue
            cand.update({"pair": pair, "sec": sec, "label": label, "threshold": th})
            if best is None or cand["ratio"] > best["ratio"]:
                best = cand
        return best

    def check(self, now, open_pairs):
        """急な動きがあれば、その内容(dict)を返す。なければ None。"""
        ts, cur = self.book.latest()
        if ts is None:
            return None
        cands = []
        for pair in PAIRS:
            if pair not in cur or pair not in open_pairs:
                continue
            cand = self._candidate(pair, now, cur[pair])
            if cand:
                cands.append(cand)
        keys = set((c["pair"], c["dir"]) for c in cands)
        for key in list(self._streak):
            if key not in keys:
                del self._streak[key]
        confirmed = []
        for c in cands:
            key = (c["pair"], c["dir"])
            self._streak[key] = self._streak.get(key, 0) + 1
            if self._streak[key] >= self.confirm:
                confirmed.append(c)
        ready = []
        big = self.thresholds.get(300, 0.15) * 2
        for c in confirmed:
            last = self._last.get(c["pair"])
            if last and now - last[0] < self.cooldown:
                if abs(pct_change(last[1], cur[c["pair"]])) < big:
                    continue
            ready.append(c)
        if not ready:
            return None
        main = max(ready, key=lambda c: c["ratio"])
        base_price, base_ts, base_mids = main["base"]
        changes = {}
        for pair in PAIRS:
            if pair in cur and pair in base_mids and base_mids[pair] > 0:
                changes[pair] = pct_change(base_mids[pair], cur[pair])
        # いっしょに動いた通貨ペアも、この通知でまとめて知らせたことにして休ませる
        for pair, chg in changes.items():
            if pair == main["pair"] or abs(chg) >= main["threshold"] * 0.5:
                self._last[pair] = (now, cur[pair])
                self._streak.pop((pair, 1), None)
                self._streak.pop((pair, -1), None)
        strength = currency_strength(base_mids, cur)
        return {
            "ts": now,
            "pair": main["pair"],
            "dir": main["dir"],
            "sec": main["sec"],
            "label": main["label"],
            "pct": main["pct"],
            "ratio": main["ratio"],
            "from": base_price,
            "to": cur[main["pair"]],
            "elapsed": max(0.0, now - base_ts),
            "changes": changes,
            "strength": strength,
            "tags": strength_tags(strength, main["threshold"] * 0.4),
            "mids": dict(cur),
        }


def spike_title(sp):
    pair = sp["pair"]
    word = "急騰" if sp["dir"] > 0 else "急落"
    sign = "+" if sp["dir"] > 0 else "-"
    return "%sが%s %sで%s%s(%s→%s)" % (
        PAIR_JA[pair],
        word,
        sp["label"],
        sign,
        fmt_abs_change(pair, sp["to"] - sp["from"]),
        fmt_price(pair, sp["from"]),
        fmt_price(pair, sp["to"]),
    )


def spike_item(sp):
    return Item(
        source="rates",
        source_name="為替レート(GMOコイン)",
        kind="spike",
        key="spike|%s|%d" % (sp["pair"], int(sp["ts"])),
        title=spike_title(sp),
        published=sp["ts"],
        fetched=sp["ts"],
        extra={"spike": sp},
    )


class RatesPoller(threading.Thread):
    def __init__(self, url, interval, detector, book, emit, stop, health=None):
        super().__init__(name="rates", daemon=True)
        self.url = url
        self.interval = float(interval)
        self.detector = detector
        self.book = book
        self.emit = emit
        self.stop_event = stop
        self.health = health
        self.open_pairs = set()
        self.last_ok = 0.0

    def poll_once(self):
        resp = request(self.url, timeout=8, headers={"Accept": "application/json"})
        mids, open_pairs = parse_ticker(resp.json())
        if not mids:
            raise ValueError("レートが1つも入っていません")
        now = time.time()
        self.open_pairs = open_pairs
        self.last_ok = now
        if open_pairs:
            self.book.add(now, mids)
            return self.detector.check(now, open_pairs)
        return None

    def latest_mids(self, max_age=90):
        """いまのレート。古すぎる(取引時間外など)ときは空の dict。"""
        ts, mids = self.book.latest()
        if ts is None or time.time() - ts > max_age:
            return {}
        return dict(mids)

    def run(self):
        delay = 0.0
        fails = 0
        while not self.stop_event.wait(delay):
            try:
                spike = self.poll_once()
                if spike:
                    self.emit(spike_item(spike))
                fails = 0
                if self.health:
                    self.health.ok("rates")
                delay = self.interval if self.open_pairs else 60.0
            except (NetError, HttpError, ValueError) as exc:
                fails += 1
                delay = min(60.0, self.interval * (2 ** min(fails, 5)))
                if self.health:
                    self.health.fail("rates", "為替レート", str(exc))
                if fails in (1, 5) or fails % 30 == 0:
                    log.warning("為替レート: 取れませんでした(%s)", exc)
            except Exception:
                fails += 1
                delay = min(60.0, self.interval * (2 ** min(fails, 5)))
                log.exception("為替レート: 思わぬエラー")
