"""見張り番の本体。集める係・解説の係・通知の係をつなぐ。"""

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time

from . import __version__
from .analyzer import Analyzer
from .edgar import EdgarPoller
from .feeds import FeedPoller
from .models import KIND_JA
from .notifier import Notifier, build_news_notification, build_spike_notification, news_tags, tag_line
from .rates import BarMaker, RateBook, RatesPoller, SpikeDetector, currency_strength, spike_title
from .stats import kata_line, run_kata_report
from .store import Store
from .util import jst, normalize_for_dedupe, short_hash, utc_day, utc_month
from .xstream import XStream

log = logging.getLogger("fxnews.app")

RAW_NOTIFY_KINDS = ("official", "edgar")  # Claude が使えないときでも、見出しのまま知らせる種類
STOP_FILE = "stop.request"  # data フォルダにこの名前のファイルができたら、見張りを止める(stop コマンドが作る)
ALIVE_FILE = "alive.json"  # 動いているあいだ、30秒ごとに書きなおす「元気です」のしるし(status コマンドが読む)
REPORT_FILE = "seiseki.txt"  # 型ごとの成績の表。始めたときと、1時間ごとに書きなおす


class Health:
    """情報源ごとの「つながらない時間」を数え、10分つづいたら利用者に知らせる。"""

    def __init__(self, alert, grace_sec=600):
        self.alert = alert
        self.grace = grace_sec
        self._since = {}
        self._names = {}
        self._errors = {}
        self._alerted = set()
        self._lock = threading.Lock()

    def ok(self, sid):
        with self._lock:
            was_alerted = sid in self._alerted
            self._since.pop(sid, None)
            self._errors.pop(sid, None)
            self._alerted.discard(sid)
            name = self._names.get(sid, sid)
        if was_alerted:
            self.alert("%s に、またつながるようになりました" % name)

    def fail(self, sid, name, why):
        now = time.time()
        tell = False
        with self._lock:
            first = self._since.setdefault(sid, now)
            self._names[sid] = name
            self._errors[sid] = why
            if now - first >= self.grace and sid not in self._alerted:
                self._alerted.add(sid)
                tell = True
        if tell:
            self.alert("%s に %d分以上つながりません(%s)" % (name, int(self.grace // 60), why[:120]))

    def failing(self):
        with self._lock:
            return {self._names.get(s, s): self._errors.get(s, "") for s in self._since}


def block_sleep():
    """このプログラムが動いているあいだ、パソコンが自動でスリープしないようにたのむ。

    パソコンの設定は書きかえない。プログラムを閉じれば元どおりになる。ふたを閉じたときのスリープは止めない。
    """
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            return True
    except Exception:
        log.info("スリープを止める指定はできませんでした(動作には影響しません)")
    return False


class App:
    def __init__(self, cfg, sources, replay=0, dry=False):
        self.cfg = cfg
        self.sources = sources
        self.replay = replay
        self.dry = dry
        self.store = Store(cfg.data_dir / "fxnews.db")
        self.stop = threading.Event()
        self.queue = queue.Queue()
        self.analyzer = Analyzer(cfg, self.store, context=sources.get("context", ""))
        self.notifier = Notifier(cfg, self.store)
        self.min_importance = cfg.getint("notify", "min_importance")
        self.spike_fast = cfg.getbool("notify", "spike_fast")
        self.health = Health(self.system_alert)
        self.book = RateBook(keep_sec=1200)
        self.detector = SpikeDetector(
            self.book,
            {
                60: cfg.getfloat("rates", "threshold_1m"),
                300: cfg.getfloat("rates", "threshold_5m"),
                900: cfg.getfloat("rates", "threshold_15m"),
            },
            cooldown_sec=cfg.getfloat("rates", "cooldown_min") * 60.0,
        )
        self.bars = BarMaker(self.store.add_rate_bars)  # 1分ごとの値を、記録にためる係
        self.rates = RatesPoller(
            cfg.get("rates", "url"),
            cfg.getfloat("rates", "interval_sec"),
            self.detector,
            self.book,
            self.emit,
            self.stop,
            self.health,
            bars=self.bars,
        )
        self.xstream = None
        self.threads = []
        self.counts = {"seen": 0, "notified": 0, "skipped": 0, "dup": 0}
        self._count_lock = threading.Lock()
        self._alerted_budget = set()
        self._last_heartbeat_day = ""
        self.started = time.time()
        self._last_status = self.started
        self._last_report = 0.0
        self.run_selftest = False  # 始めたときに、型分けの試しをするか(本番の run のときだけ True にする)

    # ---- 入口 ----
    def emit(self, item):
        self.queue.put(item)

    def _bump(self, key):
        with self._count_lock:
            self.counts[key] = self.counts.get(key, 0) + 1

    # ---- 通知 ----
    def _send(self, title, body, priority, click=None, seq=None, kind="news", followup=False):
        if self.dry:
            return True, "(お試しモード: 送っていません)"
        if not self.notifier.available():
            return False, self.notifier.missing()
        ok, why = self.notifier.send(title, body, priority, click=click, sequence_id=seq, kind=kind, followup=followup)
        if not ok and self.notifier.limit_reason:
            self._alert_limit(self.notifier.limit_reason)
        return ok, why

    def _alert_limit(self, reason):
        """通知の上限で送れなかったことを、1日に1回だけ知らせる。"""
        key = "notify|%s|%s" % (utc_day(), reason[:10])
        if key in self._alerted_budget:
            return
        self._alerted_budget.add(key)
        self.system_alert("%s。ニュースと急騰急落の通知を止めています(上限の数は config.ini で変えられます)" % reason)

    def system_alert(self, message):
        """プログラムそのものについてのお知らせ(つながらない、上限に届いた、など)。"""
        log.warning("[お知らせ] %s", message)
        if not self.dry and self.notifier.available():
            self.notifier.send("FX見張り番からのお知らせ", message, priority=3, kind="system")

    # ---- ニュース ----
    def _raw_result(self, item, why):
        return {
            "dup": False,
            "category": KIND_JA.get(item.kind, "未分類"),
            "title": item.title[:70],
            "summary": "",
            "reason": "",
            "jpy": "flat",
            "usd": "flat",
            "eur": "flat",
            "gbp": "flat",
            "confidence": "low",
            "importance": 3 if item.kind in RAW_NOTIFY_KINDS else 2,
            "raw": True,
            "raw_reason": why,
        }

    def _note_claude_trouble(self, why):
        """Claude が使えなかったときの知らせ。型分けも止まるので、理由ごとに、きちんと知らせる。"""
        if not self.analyzer.available():
            return  # 鍵を入れていない(はじめから、解説なしで動かしている)
        if self.analyzer.over_budget():
            key = "claude|%s" % utc_month()
            if key not in self._alerted_budget:
                self._alerted_budget.add(key)
                self.system_alert("%s。来月まで、解説なし(見出しだけ)で知らせます。型ごとの成績の記録も止まります" % why)
            return
        if self.analyzer.blocked:
            # 鍵が受けつけられない・前払いの残りが足りない。待っても直らないので、1日に1回、すぐ知らせる
            key = "claude-blocked|%s" % utc_day()
            if key not in self._alerted_budget:
                self._alerted_budget.add(key)
                self.system_alert(
                    "%s。直るまで、解説なし(見出しだけ)になり、型ごとの成績の記録も止まります。"
                    "Claude Console(platform.claude.com)で、鍵と、前払いの残り(クレジット)を確かめてください" % self.analyzer.blocked
                )
            return
        if "一時停止中" in why or "つながりません" in why or "エラーを返しました" in why:
            self.health.fail("claude", "Claude(解説と型分け)", why)

    def handle_news(self, item):
        self._bump("seen")
        replay = bool(item.extra.get("replay"))
        fingerprint = "t|" + short_hash(normalize_for_dedupe(item.title, item.text))
        if not self.store.mark_seen(fingerprint) and not replay:
            self._bump("dup")
            log.info("[同じ記事] %s", item.title[:60])
            return
        now = time.time()
        recent = self.store.recent_items(now - 45 * 60, limit=12, notified_only=True, exclude_kind="spike")
        recent_titles = [r.get("title_ja") or r.get("title") or "" for r in recent]
        res, why = self.analyzer.analyze_news(item, recent_titles)
        if res is None:
            self._note_claude_trouble(why)
            res = self._raw_result(item, why)
        elif self.analyzer.available():
            self.health.ok("claude")
        # お試し(古い記事を新着としてあつかう)のときは、値を残さない(成績に数えないようにするため)
        mids = {} if replay else self.rates.latest_mids()
        row_id = self.store.add_item(item, analysis=res, importance=res["importance"], title_ja=res["title"], mids=mids)
        tags = tag_line(news_tags(res), only_moves=True) or "→"
        if res["dup"] and not replay:
            self._bump("dup")
            log.info("[同じ話] %s", res["title"])
            return
        if res["importance"] < self.min_importance and not replay:
            self._bump("skipped")
            log.info("[見送り] 重要度%d %s ← %s", res["importance"], res["title"], item.source_name)
            return
        record = kata_line(self.store, res.get("kata"), res.get("muki"), min_importance=self.min_importance)
        title, body, priority = build_news_notification(item, res, record)
        if replay:
            body = "(お試し: いちばん新しい過去の記事を、新着としてあつかっています)\n" + body
        ok, note = self._send(title, body, priority, click=item.url or None, seq="n%d" % row_id)
        delay = time.time() - item.fetched
        self.store.set_notified(row_id, ok, delay)
        if ok:
            self._bump("notified")
            age = ""
            if item.published:
                age = "、発表から%d秒" % max(0, int(time.time() - item.published))
            log.info("[通知] %s｜%s(重要度%d、受け取ってから%.1f秒%s)← %s %s", tags, res["title"], res["importance"], delay, age, item.source_name, note)
        else:
            log.warning("[通知できず] %s(%s)", res["title"], note)

    # ---- 急騰急落 ----
    def handle_spike(self, item):
        sp = item.extra["spike"]
        now = time.time()
        recent = self.store.recent_items(now - 20 * 60, limit=8, exclude_kind="spike")
        analysis = {
            "category": "急騰急落",
            "title": item.title,
            "jpy": sp["tags"]["JPY"],
            "usd": sp["tags"]["USD"],
            "eur": sp["tags"]["EUR"],
            "gbp": sp["tags"]["GBP"],
            "pair": sp["pair"],
            "pct": round(sp["pct"], 4),
            "sec": sp["sec"],
        }
        row_id = self.store.add_item(item, analysis=analysis, importance=5, title_ja=item.title, mids=sp["mids"])
        seq = "s%d" % row_id
        sent = False
        if self.spike_fast:
            title, body, priority = build_spike_notification(sp, "", recent)
            sent, note = self._send(title, body, priority, seq=seq, kind="spike")
            log.info("[急騰急落] %s %s", title, note)
        cause, why = self.analyzer.explain_spike(sp, recent)
        if cause or not self.spike_fast:
            second = sent and not self.notifier.can_replace and not self.dry  # 差しかえられないので、2通目になる
            if second and "見当たりません" in cause:
                # 手がかりがないと分かっただけなら、2通目は送らない(送れる数を大事にする)
                log.info("[急騰急落・原因つき] %s(2通目は送りません)", cause)
            else:
                title, body, priority = build_spike_notification(sp, cause, recent)
                if second:
                    title = "原因の見立て｜%s" % spike_title(sp)
                ok, note = self._send(title, body, priority, seq=seq, kind="spike", followup=sent)
                sent = sent or ok
                log.info("[急騰急落・原因つき] %s %s", cause or "(原因の説明なし: %s)" % why, note)
        self.store.set_notified(row_id, sent, time.time() - item.fetched)
        if sent:
            self._bump("notified")

    # ---- 係たち ----
    def _worker(self):
        while not self.stop.is_set():
            try:
                item = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                if item.kind == "spike":
                    self.handle_spike(item)
                else:
                    self.handle_news(item)
            except Exception:
                log.exception("1件の処理で思わぬエラー: %s", getattr(item, "title", "")[:60])
            finally:
                self.queue.task_done()

    def _outcome_loop(self):
        """答え合わせ: 通知の5分後・15分後・60分後に、4通貨がどう動いたかを記録する。"""
        while not self.stop.wait(20.0):
            try:
                cur = self.rates.latest_mids()
                if not cur:
                    continue
                for item_id, col, mids_json in self.store.pending_outcomes(time.time()):
                    base = json.loads(mids_json)
                    strength = currency_strength(base, cur)
                    self.store.set_outcome(item_id, col, {k: round(v, 4) for k, v in strength.items()})
            except Exception:
                log.exception("答え合わせの記録で思わぬエラー")

    def summary_line(self):
        day_x, month_x = (0, 0)
        if self.xstream is not None:
            day_x, month_x = self.xstream.counts()
        failing = self.health.failing()
        return "あつかった情報 %d件・通知 %d件・見送り %d件・同じ話 %d件｜Claude 今月 約%d円｜X 今月 %d件｜通知 きょう %d通｜つながらない情報源: %s" % (
            self.counts["seen"],
            self.counts["notified"],
            self.counts["skipped"],
            self.counts["dup"],
            int(round(self.analyzer.spent_yen())),
            month_x,
            self.notifier.sent_today(),
            "、".join(failing.keys()) if failing else "なし",
        )

    def housekeeping_tick(self, now):
        """定期の仕事。10分ごとに状況を画面に出し、1日1回「動いています」と静かに知らせる。

        「動いています」を送ったときだけ True を返す。
        """
        if now - self._last_status >= 600:
            self._last_status = now
            log.info("[状況] %s", self.summary_line())
        hour = self.cfg.getint("general", "heartbeat_hour")
        today = jst(now, "%Y-%m-%d")
        if not (0 <= hour <= 23) or int(jst(now, "%H")) != hour or self._last_heartbeat_day == today:
            return False
        self._last_heartbeat_day = today
        self.store.prune()
        if now - self.started < 3600 or self.dry or not self.notifier.available():
            return False  # 起動してすぐなら、「始めました」の通知が出たばかりなので送らない
        n = self.store.count_items(now - 86400, notified_only=True)
        ok, _why = self.notifier.send(
            "FX見張り番は動いています",
            "この24時間の通知: %d件\n%s" % (n, self.summary_line()),
            priority=2,
            kind="system",
        )
        return ok

    def _housekeeping_loop(self):
        while not self.stop.wait(30.0):
            try:
                self.write_alive("running")
                self.housekeeping_tick(time.time())
                if time.time() - self._last_report >= 3600:
                    self.write_report()
            except Exception:
                log.exception("定期の仕事で思わぬエラー")

    def selftest(self):
        """始めたときに1回、Claude が型を分けられるかを、見本のニュースで試す(記録や通知には残さない)。"""
        if self.dry or not self.analyzer.available() or self.analyzer.over_budget():
            return
        try:
            ok, text = self.analyzer.selftest()
        except Exception:
            log.exception("型分けの試しで、思わぬエラー")
            return
        if ok:
            log.info("[%s]", text)
        elif self.analyzer.blocked:
            self._note_claude_trouble(text)
        else:
            log.warning("[%s]", text)

    def write_report(self):
        """型ごとの成績の表を、data/seiseki.txt に書く(いつでも開いて読めるようにする)。"""
        self._last_report = time.time()
        if self.dry or self.store.path == ":memory:":
            return
        lines = ["%s(日本時間)に書きました。FX見張り番 %s" % (jst(None, "%Y-%m-%d %H:%M"), __version__), ""]
        try:
            run_kata_report(self.store, out=lines.append, min_importance=self.min_importance)
            self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cfg.data_dir / (REPORT_FILE + ".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            os.replace(str(tmp), str(self.cfg.data_dir / REPORT_FILE))
        except Exception:
            log.exception("成績の表を書けませんでした")

    # ---- 動いているしるしと、止めてほしいという合図 ----
    def write_alive(self, state):
        """「元気です」のしるしを data/alive.json に書く(status コマンドや、外からの点検で読む)。"""
        if self.dry or self.store.path == ":memory:":
            return  # お試しモードは、本番のしるしを書きかえない
        now = time.time()
        info = {
            "ts": now,
            "jst": jst(now, "%Y-%m-%d %H:%M:%S"),
            "state": state,
            "pid": os.getpid(),
            "version": __version__,
            "started": jst(self.started, "%Y-%m-%d %H:%M:%S"),
            "service": self.notifier.service,
            "dry": self.dry,
            "summary": self.summary_line(),
            "failing": self.health.failing(),
        }
        path = self.cfg.data_dir / ALIVE_FILE
        tmp = self.cfg.data_dir / (ALIVE_FILE + ".tmp")
        try:
            self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(str(tmp), str(path))
        except OSError:
            pass

    def stop_requested(self):
        """止めてほしいという合図(data/stop.request)が来ていたら、合図を片づけて True。

        合図のファイルは、この見張りを始めたあとに置かれたものだけを、合図としてあつかう。
        前の回の合図が消せずに残っていても(Windows では、ウイルス対策のソフトなどがファイルを
        つかんでいて、すぐに消せないことがある)、それで止まってしまわないようにするため。
        """
        if self.dry:
            return False  # お試しモードは、本番あての合図を横取りしない
        path = self.cfg.data_dir / STOP_FILE
        try:
            placed = path.stat().st_mtime
        except OSError:
            return False  # 合図はない
        for _ in range(5):
            try:
                path.unlink()
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.2)  # だれかがファイルをつかんでいる。少し待って、もう一度
        return placed >= self.started - 1.0

    # ---- 起動と終了 ----
    def build_threads(self):
        max_age = self.cfg.getfloat("general", "max_item_age_hours") * 3600.0
        threads = []
        for spec in self.sources.get("feeds", []):
            if spec.get("enabled", True) is False:
                continue
            threads.append(FeedPoller(spec, self.store, self.emit, self.stop, max_age, self.health, self.replay))
        contact = self.cfg.get("sec", "contact")
        edgar_spec = self.sources.get("edgar", {})
        if contact and edgar_spec.get("tickers"):
            threads.append(
                EdgarPoller(
                    edgar_spec,
                    contact,
                    self.cfg.get("sec", "base"),
                    self.store,
                    self.emit,
                    self.stop,
                    self.cfg.data_dir,
                    self.health,
                    max_age,
                )
            )
        threads.append(self.rates)
        x_rules = self.sources.get("x", {}).get("rules", [])
        self.xstream = XStream(x_rules, self.cfg, self.store, self.emit, self.stop, self.system_alert, self.health)
        if self.xstream.enabled():
            threads.append(self.xstream)
        for i in range(max(1, self.cfg.getint("general", "workers"))):
            threads.append(threading.Thread(target=self._worker, name="worker-%d" % i, daemon=True))
        threads.append(threading.Thread(target=self._outcome_loop, name="outcome", daemon=True))
        threads.append(threading.Thread(target=self._housekeeping_loop, name="housekeeping", daemon=True))
        if self.run_selftest:
            threads.append(threading.Thread(target=self.selftest, name="selftest", daemon=True))
        return threads

    def describe(self):
        """起動したときに画面に出す、いまの設定のまとめ。"""
        lines = []
        feeds = [s for s in self.sources.get("feeds", []) if s.get("enabled", True) is not False]
        lines.append("FX見張り番 %s を始めます(%s 日本時間)" % (__version__, jst(None, "%Y-%m-%d %H:%M")))
        lines.append("・更新一覧: %d本(%s)" % (len(feeds), "、".join(s.get("name", s["id"]) for s in feeds)))
        if self.cfg.get("sec", "contact") and self.sources.get("edgar", {}).get("tickers"):
            lines.append("・会社の届け出(SEC): %s" % "、".join(self.sources["edgar"]["tickers"]))
        else:
            lines.append("・会社の届け出(SEC): 止めています(config.ini の [sec] contact が未設定)")
        lines.append("・為替レート: %s秒おき。急騰急落の目安 1分 %s%% / 5分 %s%% / 15分 %s%%" % (
            self.cfg.get("rates", "interval_sec"),
            self.cfg.get("rates", "threshold_1m"),
            self.cfg.get("rates", "threshold_5m"),
            self.cfg.get("rates", "threshold_15m"),
        ))
        if self.xstream is not None and self.xstream.enabled():
            lines.append("・X: 受け取ります(ルール %d本、今月の上限 %d円 = %d件、きょうの上限 %d件)" % (
                len(self.xstream.rules), int(self.xstream.cap_yen), self.xstream.month_cap_posts(), self.xstream.day_cap))
        else:
            lines.append("・X: 止めています(鍵が未設定、または sources.json のルールが空)")
        if self.analyzer.available():
            lines.append("・解説(Claude): %s。今月の上限 %d円(いま 約%d円)" % (self.analyzer.model, int(self.analyzer.cap_yen), int(round(self.analyzer.spent_yen()))))
            lines.append("・型ごとの成績: ニュースを型に分けて、そのあとのドル円の動きを数えています(型のついた記録 %d件、1分ごとの値 %d本)" % (
                self.store.count_kata_items(), self.store.count_rate_bars()))
        else:
            lines.append("・解説(Claude): 止めています(鍵が未設定)。公式発表と決算だけ、見出しのまま知らせます")
            lines.append("・型ごとの成績: 止めています(型に分けるのに、Claude の鍵がいります)。1分ごとの値は、記録しています")
        if self.dry:
            lines.append("・通知: お試しモード(スマホには送らず、この画面に出すだけ)")
        elif self.notifier.available():
            lines.append("・通知(%s): 重要度 %d 以上を送ります(%s)" % (self.notifier.name_ja, self.min_importance, self.notifier.limits_text()))
        else:
            lines.append("・通知(%s): 止めています(%s)。この画面に出すだけです" % (self.notifier.name_ja, self.notifier.missing()))
        if self.notifier.bad_service:
            lines.append("・[notify] service の「%s」は知らない名前なので、ntfy としてあつかいます(ntfy か pushover と書きます)" % self.notifier.bad_service)
        return lines

    def start(self):
        self.threads = self.build_threads()
        for line in self.describe():
            log.info(line)
        for t in self.threads:
            t.start()

    def run(self, seconds=None):
        """見張りを始める。seconds を指定すると、その秒数で終わる(お試し用)。"""
        if self.cfg.getbool("general", "sleep_block") and seconds is None:
            if block_sleep():
                log.info("・このプログラムが動いているあいだ、パソコンが自動でスリープしないようにしています(config.ini の sleep_block = no で止められます)")
        self.stop_requested()  # 前の回の合図が残っていたら、片づける
        self.run_selftest = seconds is None
        self.start()
        self.write_alive("running")
        if seconds is None:
            self.write_report()
        if not self.dry and self.notifier.available() and seconds is None:
            self.notifier.send("FX見張り番を始めました", "\n".join(self.describe()[1:]), priority=2, kind="system")
        log.info("止めるときは、この画面で Ctrl キーを押しながら C を押すか、stop を実行します")
        end = time.time() + seconds if seconds else None
        try:
            while not self.stop.is_set():
                if end and time.time() >= end:
                    break
                if self.stop_requested():
                    log.info("止めるように頼まれたので、止めます…")
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("止めます…")
        finally:
            self.drain(8.0)
            self.stop.set()
            self.write_alive("stopped")
            log.info("[おわり] %s", self.summary_line())
            log.info(
                "[接続] 為替レートを取った回数 %d(うち、つないだままの接続を使い回した回数 %d)",
                self.rates.polls,
                self.rates.session.reused,
            )

    def drain(self, timeout):
        """受け取りずみの情報を、処理しおわるまで少し待つ。"""
        deadline = time.time() + timeout
        while self.queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.1)

    def close(self, timeout=5.0):
        """係をすべて止めて、記録を閉じる。

        通信の途中で止まれない係が残っているときは、記録を閉じずにそのままにする
        (プログラムが終わるときに自動で片づく)。
        """
        self.stop.set()
        deadline = time.time() + timeout
        for t in self.threads:
            remaining = deadline - time.time()
            if t.is_alive() and remaining > 0:
                t.join(timeout=remaining)
        if not self.rates.is_alive():
            self.bars.flush()  # 作りかけの1分も、記録に残す(レートの係が止まってから)
        if not any(t.is_alive() for t in self.threads):
            self.store.close()
