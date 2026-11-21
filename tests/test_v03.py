"""v0.3 で足したものの確かめ。

- ニュースを「型」と「向き」に分ける(Claude への頼み方と、返事のそろえ方)
- 1分ごとの為替の値を記録する
- 型ごとの成績(ニュースが届いたあと、ドル円がどう動いたか)を数える
- 通知に「ドル円の見立て」と「この型の過去の成績」をつける
- 為替レートの配信が点検中・週末のときは、「つながりません」と知らせない
- Claude の鍵や前払いの残りがだめなときは、すぐ知らせる
- 黒い画面なしで始める命令と、止まっても自分で始めなおす仕組み
外のネットにはつながず、にせのサーバーを相手にする。
"""

import datetime as dt
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxnews import analyzer, notifier, rates, stats  # noqa: E402
from fxnews.analyzer import Analyzer  # noqa: E402
from fxnews.app import REPORT_FILE, App, Health  # noqa: E402
from fxnews.config import Config  # noqa: E402
from fxnews.models import Item  # noqa: E402
from fxnews.net import HttpError  # noqa: E402
from fxnews.store import Store  # noqa: E402
from fxnews.util import JST  # noqa: E402
from tests.fake_server import start_server  # noqa: E402
from tests.test_e2e import PO_TOKEN, PO_USER, make_sources, wait_for, write_config  # noqa: E402
from tests.test_v02 import answers, call  # noqa: E402

KATA_ANSWER = {
    "usdjpy": "up",
    "kata": "fed_talk",
    "muki": "hawk",
    "tag": "ウォラー理事",
}


def at(year, month, day, hour, minute=0, second=0):
    """日本時間の日時を UNIX 秒にする。"""
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=JST).timestamp()


def mids(usd_jpy=159.20, **over):
    base = {"USD_JPY": usd_jpy, "EUR_JPY": 174.80, "GBP_JPY": 209.40, "EUR_USD": 1.0980, "GBP_USD": 1.3150, "EUR_GBP": 0.8350}
    base.update(over)
    return base


def fill_bars(store, pair, start, minutes, price, step=0.0):
    """start から minutes 本、1分ごとの値を入れる。1本ごとに step ずつ値を動かす。"""
    rows = []
    for i in range(minutes):
        p = price + step * i
        rows.append((int(start) + 60 * i, pair, p, p + 0.002, p - 0.002, p + step, 30))
    store.add_rate_bars(rows)


def add_news(store, ts, kata="us_data", muki="strong", usdjpy="up", importance=4, price=159.20, dup=False, published=None, title="t"):
    """記録に、ニュースを1件足す(受け取った時刻を ts にする)。"""
    item = Item("src", "名前", "news", "k%s" % ts, title, text="本文", published=published)
    analysis = {"dup": dup, "category": "経済指標", "title": title, "kata": kata, "muki": muki, "usdjpy": usdjpy}
    row_id = store.add_item(item, analysis=analysis, importance=importance, title_ja=title, mids=mids(price))
    with store._lock:
        store._db.execute("UPDATE items SET ts=? WHERE id=?", (ts, row_id))
        store._db.commit()
    return row_id


class KataTest(unittest.TestCase):
    """型と向きの、そろえ方。"""

    BASE = {"dup": False, "category": "経済指標", "title": "米雇用統計", "importance": 4}

    def norm(self, **over):
        obj = dict(self.BASE)
        obj.update(over)
        return analyzer.normalize_news(obj)

    def test_good_values_pass(self):
        res = self.norm(kata="us_data", muki="strong", usdjpy="up", tag="雇用統計")
        self.assertEqual((res["kata"], res["muki"], res["usdjpy"], res["tag"]), ("us_data", "strong", "up", "雇用統計"))
        self.assertEqual(res["kata_ver"], analyzer.KATA_VERSION)

    def test_wrong_values_become_safe_ones(self):
        self.assertEqual(self.norm(kata="us_data", muki="hawk")["muki"], "inline")  # その型で使えない向き
        self.assertEqual(self.norm(kata="fed_talk", muki="strong")["muki"], "neutral")
        res = self.norm(kata="知らない型", muki="dove", usdjpy="sideways")
        self.assertEqual((res["kata"], res["muki"], res["usdjpy"]), ("other", "neutral", "flat"))
        res = self.norm()  # 前の版の形の返事(型なし)
        self.assertEqual((res["kata"], res["muki"], res["usdjpy"], res["tag"]), ("other", "neutral", "flat", ""))

    def test_tables_agree(self):
        self.assertEqual(set(analyzer.KATA_JA), set(analyzer.KATA_MUKI))
        for kata, words in analyzer.KATA_MUKI.items():
            for word in words:
                self.assertIn(word, analyzer.MUKI_JA, kata)
            self.assertIn(kata, analyzer.NEWS_SYSTEM, "頼み方の表に、型 %s がない" % kata)
        self.assertEqual(set(analyzer.NEWS_SCHEMA["properties"]["kata"]["enum"]), set(analyzer.KATA_JA))
        self.assertEqual(set(analyzer.NEWS_SCHEMA["properties"]["muki"]["enum"]), set(analyzer.MUKI_JA))

    def test_label(self):
        self.assertEqual(analyzer.kata_label("us_data", "strong"), "米国の経済指標・予想より強い")
        self.assertEqual(analyzer.kata_label("fed_talk", "neutral"), "FRB の人の発言")
        self.assertEqual(analyzer.kata_label(None, None), "")


class BarTest(unittest.TestCase):
    """1分ごとの値。"""

    def test_bars_are_made_per_minute(self):
        saved = []
        maker = rates.BarMaker(saved.extend)
        t0 = 1795000020.0  # ちょうど分のはじめ
        self.assertEqual(t0 % 60, 0)
        maker.add(t0 + 1, {"USD_JPY": 159.20, "EUR_JPY": 174.80, "AUD_JPY": 107.0}, {"USD_JPY", "EUR_JPY"})
        maker.add(t0 + 20, {"USD_JPY": 159.30, "EUR_JPY": 174.70}, {"USD_JPY", "EUR_JPY"})
        maker.add(t0 + 50, {"USD_JPY": 159.10, "EUR_JPY": 174.75}, {"USD_JPY", "EUR_JPY"})
        self.assertEqual(saved, [])  # その1分が終わるまでは、記録しない
        maker.add(t0 + 61, {"USD_JPY": 159.15, "EUR_JPY": 174.76}, {"USD_JPY", "EUR_JPY"})
        self.assertEqual(saved, [(int(t0), "EUR_JPY", 174.80, 174.80, 174.70, 174.75, 3), (int(t0), "USD_JPY", 159.20, 159.30, 159.10, 159.10, 3)])
        maker.flush()  # 止めるときは、作りかけの1分も記録する
        self.assertEqual(saved[-1], (int(t0) + 60, "USD_JPY", 159.15, 159.15, 159.15, 159.15, 1))
        self.assertEqual(maker.saved, 4)
        maker.flush()
        self.assertEqual(len(saved), 4)

    def test_broken_sink_does_not_stop_the_watcher(self):
        def broken(rows):
            raise RuntimeError("記録できない")

        maker = rates.BarMaker(broken)
        with self.assertLogs("fxnews.rates", level="ERROR"):
            maker.add(60.0, {"USD_JPY": 159.0})
            maker.add(121.0, {"USD_JPY": 159.1})

    def test_store_keeps_and_merges_bars(self):
        store = Store(":memory:")
        try:
            store.add_rate_bars([(600, "USD_JPY", 159.00, 159.50, 158.90, 159.40, 20)])
            # 同じ1分の途中で始めなおしたとき: 始めの値は前のまま、高い・安いは広いほう、終わりの値は新しいほう
            store.add_rate_bars([(600, "USD_JPY", 159.45, 159.60, 159.30, 159.55, 9)])
            self.assertEqual(store.rate_bars("USD_JPY", 0, 9999), [{"ts": 600, "o": 159.00, "h": 159.60, "l": 158.90, "c": 159.55, "n": 29}])
            store.add_rate_bars([(660, "USD_JPY", 159.55, 159.60, 159.50, 159.58, 30), (900, "USD_JPY", 160.00, 160.10, 159.90, 160.05, 30)])
            self.assertEqual(store.count_rate_bars(), 3)
            self.assertEqual(store.rate_at("USD_JPY", 600), 159.00)  # その1分の前半は、始めの値
            self.assertEqual(store.rate_at("USD_JPY", 640), 159.55)  # 後半は、終わりの値
            self.assertEqual(store.rate_at("USD_JPY", 730), 159.58)  # 記録のない1分は、時刻が近いほう(前の終わりの値)
            self.assertEqual(store.rate_at("USD_JPY", 880), 160.00)  # 後のほうが近ければ、後の始めの値
            self.assertIsNone(store.rate_at("USD_JPY", 2000))  # 前後5分に記録がなければ、値なし
            self.assertIsNone(store.rate_at("EUR_JPY", 600))
            store.prune(rate_days=0)
            self.assertEqual(store.count_rate_bars(), 0)
        finally:
            store.close()

    def test_old_database_is_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE seen(key TEXT PRIMARY KEY, ts REAL);
                CREATE TABLE source_state(source TEXT PRIMARY KEY, initialized INTEGER DEFAULT 0, updated REAL);
                CREATE TABLE items(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts REAL, source TEXT, source_name TEXT, kind TEXT, key TEXT,
                  title TEXT, url TEXT, published REAL,
                  title_ja TEXT, analysis TEXT, importance INTEGER,
                  notified INTEGER DEFAULT 0, delay_sec REAL,
                  mids TEXT, out5 TEXT, out15 TEXT, out60 TEXT
                );
                CREATE TABLE usage(period TEXT, kind TEXT, amount REAL, PRIMARY KEY(period, kind));
                INSERT INTO items(ts, source, kind, title, importance, notified) VALUES(1.0, 's', 'news', '前の版の記録', 3, 1);
                INSERT INTO usage VALUES('2026-11', 'pushover', 9);
                """
            )
            db.commit()
            db.close()
            for _ in range(2):  # 2回開いても、こわれない
                store = Store(path)
                try:
                    self.assertEqual(store.recent_items(0)[0]["title"], "前の版の記録")
                    self.assertEqual(store.usage_get("2026-11", "pushover"), 9)
                    self.assertEqual(store.count_kata_items(), 0)
                    self.assertEqual(store.kata_rows(0), [])
                finally:
                    store.close()
            store = Store(path)
            try:
                item = Item("s", "n", "news", "k", "新しい記録", text="本文" * 5000)
                row = store.add_item(item, analysis={"kata": "risk", "muki": "worse", "usdjpy": "down", "dup": True}, importance=4, mids=mids())
                saved = [r for r in store.recent_items(0) if r["id"] == row][0]
                self.assertEqual((saved["kata"], saved["muki"], saved["usdjpy"], saved["dup"]), ("risk", "worse", "down", 1))
                self.assertEqual(len(saved["text"]), 6000)  # 本文は長さを切って残す
                self.assertEqual(store.kata_rows(0), [])  # 前に出た話のくり返しは、数える側に出さない
            finally:
                store.close()


class StatsTest(unittest.TestCase):
    """型ごとの成績の数え方。"""

    def setUp(self):
        self.store = Store(":memory:")
        # 2026-11-18(水)の 22:00 から6時間ぶん、ドル円が1分に 0.5銭ずつ上がっていく値を入れる
        self.t0 = at(2026, 11, 18, 22, 0)
        fill_bars(self.store, "USD_JPY", self.t0, 360, 159.00, step=0.005)
        self.now = at(2026, 11, 25, 12, 0)

    def tearDown(self):
        self.store.close()

    def measured(self, **kwargs):
        rows = stats.collapse(self.store.kata_rows(0, **kwargs))
        return stats.measure(self.store, rows, now=self.now)

    def test_moves_are_measured_from_the_time_of_arrival(self):
        # 22:30 ちょうどに届いたニュース。そのときの値は 159.20(記録の値とは別に、届いた瞬間の値を起点にする)
        add_news(self.store, self.t0 + 1800, price=159.20)
        m = self.measured()
        self.assertEqual(len(m), 1)
        # 22:35 の値は 159.00 + 0.005×35 = 159.175 → 159.20 から −2.5銭
        self.assertAlmostEqual(m[0]["diff"][300], -2.5, places=3)
        self.assertAlmostEqual(m[0]["diff"][900], 2.5, places=3)  # 22:45 は 159.225
        self.assertAlmostEqual(m[0]["diff"][3600], 25.0, places=3)  # 23:30 は 159.45
        self.assertIsNone(m[0]["diff"][86400])  # 1日後の値は、記録がない

    def test_dawn_is_not_counted(self):
        self.assertFalse(stats.in_dawn(at(2026, 11, 19, 5, 59)))
        self.assertTrue(stats.in_dawn(at(2026, 11, 19, 6, 0)))
        self.assertTrue(stats.in_dawn(at(2026, 11, 19, 7, 9)))
        self.assertFalse(stats.in_dawn(at(2026, 11, 19, 7, 10)))
        fill_bars(self.store, "USD_JPY", at(2026, 11, 19, 4, 0), 300, 160.00)
        add_news(self.store, at(2026, 11, 19, 6, 30), price=160.00)  # 朝の数えない時間に届いた → 数えない
        add_news(self.store, at(2026, 11, 19, 5, 30), kata="fed_talk", muki="hawk", price=160.00)
        m = self.measured()
        self.assertEqual([x["kata"] for x in m], ["fed_talk"])
        self.assertIsNotNone(m[0]["diff"][300])  # 5:35 は数える
        self.assertIsNotNone(m[0]["diff"][900])  # 5:45 も数える
        self.assertIsNone(m[0]["diff"][3600])  # 6:30 に答えが出るものは、数えない

    def test_repeats_late_and_duplicate_items_are_not_counted(self):
        t = self.t0 + 600
        add_news(self.store, t)
        add_news(self.store, t + 900)  # 同じ型・同じ向きが30分以内 → 最初の1回だけ
        add_news(self.store, t + 1900)  # 30分より後 → 数える
        add_news(self.store, t + 2000, kata="us_data", muki="weak")  # 向きがちがえば、別に数える
        add_news(self.store, t + 5000, dup=True)  # 前に出た話のくり返し
        add_news(self.store, t + 6000, published=t + 6000 - 3600)  # 発表から1時間おくれて届いた
        add_news(self.store, t + 7000, published=t + 7000 - 60)  # 1分おくれは、ふつう
        add_news(self.store, t + 9000, importance=2)  # 重要度が低い
        counted = [(x["ts"] - t, x["muki"]) for x in self.measured(min_importance=3)]
        self.assertEqual(counted, [(0, "strong"), (1900, "strong"), (2000, "weak"), (7000, "strong")])
        self.assertEqual(len(self.measured(min_importance=0)), 5)

    def test_one_day_later_skips_the_weekend(self):
        store = Store(":memory:")
        try:
            fri = at(2026, 11, 20, 22, 30)  # 金曜の夜
            thu = at(2026, 11, 19, 22, 30)  # 木曜の夜
            fill_bars(store, "USD_JPY", at(2026, 11, 20, 22, 0), 120, 159.50)  # 金曜の夜
            fill_bars(store, "USD_JPY", at(2026, 11, 23, 22, 0), 120, 160.00)  # 月曜の夜
            add_news(store, fri, price=159.00)
            add_news(store, thu, kata="fed_talk", muki="dove", price=159.00)
            m = {x["kata"]: x for x in stats.measure(store, stats.collapse(store.kata_rows(0)), now=at(2026, 11, 25, 0, 0))}
            self.assertAlmostEqual(m["us_data"]["diff"][86400], 100.0, places=3)  # 金曜の分は、月曜の同じ時刻(160.00)で見る
            self.assertAlmostEqual(m["fed_talk"]["diff"][86400], 50.0, places=3)  # 木曜の分は、金曜の同じ時刻(159.50)
            # まだ時間がたっていないものは、答えなし
            early = stats.measure(store, stats.collapse(store.kata_rows(0)), now=fri + 600)
            self.assertIsNone([x for x in early if x["kata"] == "us_data"][0]["diff"][900])
        finally:
            store.close()

    def test_summary_and_chance(self):
        s = stats.summarize([3.0, -1.0, 2.0, None, 0.0])
        self.assertEqual((s["n"], s["up"], s["down"], s["same"]), (4, 2, 1, 1))
        self.assertAlmostEqual(s["mean"], 1.0)
        self.assertAlmostEqual(s["median"], 1.0)
        self.assertEqual(stats.summarize([])["n"], 0)
        self.assertAlmostEqual(stats.chance_of_tilt(5, 5), 1.0)
        self.assertAlmostEqual(stats.chance_of_tilt(10, 0), 2.0 / 1024)
        self.assertAlmostEqual(stats.chance_of_tilt(15, 5), 0.0414, places=3)  # 20回のうち15回以上どちらかに偏る確率
        self.assertEqual(stats.chance_of_tilt(0, 0), 1.0)

    def test_line_for_notification(self):
        self.assertEqual(stats.kata_line(self.store, "other", "neutral"), "")
        self.assertEqual(stats.kata_line(self.store, None, None), "")
        self.assertEqual(
            stats.kata_line(self.store, "us_data", "strong", now=self.now),
            "型: 米国の経済指標・予想より強い(この型のあとのドル円の動きは、これから数えます)",
        )
        for i in range(3):
            add_news(self.store, self.t0 + 600 + i * 2400, price=159.00)
        line = stats.kata_line(self.store, "us_data", "strong", now=self.now)
        self.assertEqual(line, "型: 米国の経済指標・予想より強い｜この型のあとのドル円(15分後): 上げ3回・下げ0回。まだ3回なので、目安になりません(20回から)")
        # 20回たまったら、回数と平均を出す
        store = Store(":memory:")
        try:
            for day in range(22):
                start = at(2026, 10, 1 + day, 21, 0)
                fill_bars(store, "USD_JPY", start, 120, 159.00, step=0.004 if day % 4 else -0.004)
                add_news(store, start + 600, price=159.04 if day % 4 else 158.96)
            line = stats.kata_line(store, "us_data", "strong", now=at(2026, 11, 1, 0, 0))
            # 22回のうち、上げた日は 16回(4日に1日は下げ)。15分で 0.4銭×15 = 6銭
            self.assertEqual(line, "型: 米国の経済指標・予想より強い｜この型のあとのドル円: 15分後は上げ16回・下げ6回(平均+2.7銭)、60分後は上げ16回・下げ6回(平均+10.9銭)")
        finally:
            store.close()

    def test_report(self):
        store = Store(":memory:")
        try:
            lines = []
            stats.run_kata_report(store, out=lines.append, now=self.now)
            self.assertIn("まだ、数えられる記録がありません", "\n".join(lines))
            # 24日ぶん。どの通貨ペアも、1分ごとに少しずつ上がっていく値にする(円のからむペアは 0.4銭、ほかは 0.2pips)。
            # ニュースは、値を入れはじめてから 310分後に届く。そのときの値が、ちょうど mids() の値になるようにしてある。
            for day in range(24):
                start = at(2026, 10, 1 + day, 21, 0) if day % 2 else at(2026, 10, 1 + day, 9, 0)
                for pair, price in mids().items():
                    step = 0.004 if pair.endswith("JPY") else 0.00002
                    fill_bars(store, pair, start - 5 * 3600, 420, price - step * 310, step=step)
                add_news(store, start + 600, usdjpy="up" if day % 3 else "down")
            add_news(store, at(2026, 10, 30, 21, 10), kata="boj_talk", muki="hawk", published=at(2026, 10, 30, 12, 0))
            lines = []
            result = stats.run_kata_report(store, out=lines.append, now=at(2026, 11, 1, 0, 0))
            text = "\n".join(lines)
            self.assertEqual(result[("us_data", "strong")][900]["n"], 24)
            self.assertIn("■ 米国の経済指標・予想より強い(24件)", text)
            self.assertIn("15分後: 24回 上げ24回・下げ0回(平均+6.0銭)｜1万ドルを持っていたとすると +600円｜たまたまでも、100回に0回は出る偏り", text)
            self.assertIn("[確かめA・別の通貨ペア(15分後)]", text)
            self.assertIn("ユーロドル: 24回 上げ24回・下げ0回(平均+3.0pips)", text)
            self.assertIn("[確かめB・届いた時間帯(15分後)]", text)
            self.assertIn("東京の時間(7:10〜15:00): 12回", text)
            self.assertIn("ニューヨークの時間(21:00〜翌6:00): 12回", text)
            self.assertIn("[確かめC・大きな流れ(届く4時間前より、ドル円が上か下か。15分後)]", text)
            self.assertIn("上向きのとき: 24回", text)
            self.assertIn("■ Claude の見立て(通知の「ドル円↑」「ドル円↓」)の当たり外れ", text)
            self.assertIn("15分後: 当たり16回・外れ8回", text)
            self.assertIn("届くのが遅くて数えなかった記事: src 1件", text)
            self.assertIn("ふだんの揺れ(この30日): ドル円は、15分のあいだに平均で 6.0銭 動いています", text)
            self.assertNotIn("boj", text.lower())
        finally:
            store.close()

    def test_sessions(self):
        self.assertTrue(stats.session_of(at(2026, 11, 19, 9, 0)).startswith("東京"))
        self.assertTrue(stats.session_of(at(2026, 11, 19, 16, 0)).startswith("欧州"))
        self.assertTrue(stats.session_of(at(2026, 11, 19, 23, 0)).startswith("ニューヨーク"))
        self.assertTrue(stats.session_of(at(2026, 11, 19, 3, 0)).startswith("ニューヨーク"))
        self.assertEqual(stats.session_of(at(2026, 11, 19, 6, 30)), "")


class NotificationTest(unittest.TestCase):
    RES = {"dup": False, "category": "経済指標", "title": "米雇用統計は予想を上回る", "summary": "まとめ", "reason": "りゆう", "jpy": "down", "usd": "up", "eur": "flat", "gbp": "flat", "confidence": "mid", "importance": 5}

    def test_title_leads_with_usdjpy(self):
        item = Item("s", "investingLive", "news", "k", "US NFP beats", published=1795024656.0)
        title, body, prio = notifier.build_news_notification(item, dict(self.RES, usdjpy="up"), "型: 米国の経済指標・予想より強い(…)")
        self.assertEqual(title, "ドル円↑｜米雇用統計は予想を上回る")
        lines = body.split("\n")
        self.assertEqual(lines[0], "型: 米国の経済指標・予想より強い(…)")
        self.assertEqual(lines[1], "まとめ")
        self.assertEqual(lines[3], "ドル円は上がりやすい｜円↓ ドル↑ ユーロ→ ポンド→(見立て・確からしさ: 中)")
        self.assertEqual(prio, 5)
        title, body, _ = notifier.build_news_notification(item, dict(self.RES, usdjpy="down"))
        self.assertEqual(title, "ドル円↓｜米雇用統計は予想を上回る")
        self.assertIn("ドル円は下がりやすい｜", body)
        # ドル円の向きが読めないときは、動きそうな通貨の札を出す(前の版と同じ)
        title, body, _ = notifier.build_news_notification(item, dict(self.RES, usdjpy="flat"))
        self.assertEqual(title, "円↓ ドル↑｜米雇用統計は予想を上回る")
        self.assertEqual(body.split("\n")[0], "まとめ")
        self.assertNotIn("ドル円は", body)


class RatesClosedTest(unittest.TestCase):
    """為替レートの配信が点検中・週末のときは、「つながりません」と知らせない。"""

    def setUp(self):
        self.state, self.server = start_server()
        self.alerts = []
        self.health = Health(self.alerts.append, grace_sec=0.3)
        import threading

        self.stop = threading.Event()
        book = rates.RateBook()
        self.poller = rates.RatesPoller(self.state.base() + "/ticker", 0.05, rates.SpikeDetector(book, {60: 0.08}), book, lambda item: None, self.stop, self.health)

    def tearDown(self):
        self.stop.set()
        if self.poller.is_alive():
            self.poller.join(timeout=5)
        self.server.shutdown()
        self.server.server_close()

    def test_weekend_window(self):
        self.assertFalse(rates.weekend_closed(at(2026, 11, 21, 4, 59)))  # 土曜 4:59
        self.assertTrue(rates.weekend_closed(at(2026, 11, 21, 5, 0)))  # 土曜 5:00
        self.assertTrue(rates.weekend_closed(at(2026, 11, 22, 12, 0)))  # 日曜
        self.assertTrue(rates.weekend_closed(at(2026, 11, 23, 7, 9)))  # 月曜 7:09
        self.assertFalse(rates.weekend_closed(at(2026, 11, 23, 7, 10)))  # 月曜 7:10
        self.assertFalse(rates.weekend_closed(at(2026, 11, 25, 12, 0)))  # 水曜

    def test_check_does_not_call_maintenance_a_failure(self):
        from fxnews.check import run_check

        with tempfile.TemporaryDirectory() as tmp:
            cfg = write_config(tmp, self.state, claude_key="", x_token="", contact="", topic="")
            self.state.ticker_mode = "maintenance"
            lines = []
            bad = run_check(cfg, {"feeds": []}, out=lines.append)
            self.assertEqual(bad, 0, lines)
            self.assertIn("配信が点検中です(故障ではありません", "\n".join(lines))

    def test_maintenance_answer_is_recognised(self):
        payload = {"status": 5, "messages": [{"message_code": "ERR-5201", "message_string": "MAINTENANCE. Please wait for a while"}]}
        with self.assertRaises(rates.Maintenance):
            rates.parse_ticker(payload)
        with self.assertRaises(ValueError) as ctx:
            rates.parse_ticker({"status": 1, "messages": [{"message_code": "ERR-5003", "message_string": "Requests are too many."}]})
        self.assertNotIsInstance(ctx.exception, rates.Maintenance)

    def run_for(self, seconds):
        with mock.patch.object(rates, "weekend_closed", return_value=self.weekend):
            if not self.poller.is_alive():
                self.poller.start()
            time.sleep(seconds)

    def test_maintenance_is_not_announced(self):
        self.weekend = False
        for mode in ("maintenance", "maintenance503"):
            self.state.ticker_mode = mode
            with self.assertLogs("fxnews.rates", level="INFO"):
                self.run_for(0.8)
            self.assertEqual(self.alerts, [], mode)
            self.assertTrue(self.poller.maintenance)
            self.assertEqual(self.poller.open_pairs, set())
            self.assertEqual(self.health.failing(), {})
            break  # 点検中は1分おきにしか見に行かないので、2つめは別の係で確かめる

    def test_maintenance_with_http_error_is_not_announced(self):
        self.weekend = False
        self.state.ticker_mode = "maintenance503"
        self.run_for(0.8)
        self.assertEqual(self.alerts, [])
        self.assertTrue(self.poller.maintenance)

    def test_other_errors_are_announced_on_weekdays_only(self):
        self.state.ticker_mode = "error"
        self.weekend = True
        self.run_for(1.0)
        self.assertEqual(self.alerts, [])  # 週末は、知らせない
        self.assertEqual(self.health.failing(), {})

    def test_other_errors_are_announced_on_weekdays(self):
        self.state.ticker_mode = "error"
        self.weekend = False
        self.run_for(1.5)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("為替レート に", self.alerts[0])


class ClaudeTroubleTest(unittest.TestCase):
    """Claude の鍵や、前払いの残りがだめなとき。"""

    def setUp(self):
        self.state, self.server = start_server()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def item(self):
        return Item("s", "名前", "news", "k", "Some headline", text="Body")

    def test_error_kinds(self):
        def err(status, message):
            return HttpError(status, json.dumps({"type": "error", "error": {"message": message}}).encode("utf-8"))

        self.assertTrue(analyzer.is_credit_error(err(400, "Your credit balance is too low to access the Anthropic API.")))
        self.assertFalse(analyzer.is_credit_error(err(500, "credit")))
        self.assertFalse(analyzer.is_request_shape_error(err(400, "Your credit balance is too low")))
        self.assertTrue(analyzer.is_request_shape_error(err(400, "output_config.format: unsupported")))
        self.assertTrue(analyzer.is_request_shape_error(err(400, "thinking: Extra inputs are not permitted")))
        self.assertTrue(analyzer.is_request_shape_error(err(400, "something we have never seen")))
        self.assertFalse(analyzer.is_request_shape_error(err(400, "anthropic-workspace-id header is required (workspace not specified)")))
        self.assertFalse(analyzer.is_request_shape_error(err(401, "format")))

    def test_credit_error_does_not_change_how_we_ask(self):
        self.state.claude_mode = "credit"
        an = Analyzer(write_config(self.tmp.name, self.state), self.store)
        res, why = an.analyze_news(self.item(), [])
        self.assertIsNone(res)
        self.assertIn("前払いの残り(クレジット)が足りないようです", why)
        self.assertEqual(an.blocked, why)
        self.assertEqual(len(self.state.claude_reqs), 1)  # 送り方を変えて、何度も送ったりしない
        self.assertTrue(an._use_format and an._use_tuning)
        res, why = an.analyze_news(self.item(), [])
        self.assertIn("一時停止中", why)  # 10分は、送らずに待つ
        # 残りが元にもどったら、前と同じ送り方で、また使える
        self.state.claude_mode = "ok"
        an._paused_until = 0
        res, why = an.analyze_news(self.item(), [])
        self.assertEqual(why, "")
        self.assertEqual(an.blocked, "")
        body = self.state.claude_reqs[-1]["body"]
        self.assertIn("format", body["output_config"])
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_tokens"], 800)

    def test_bad_key(self):
        self.state.claude_mode = "bad_key"
        an = Analyzer(write_config(self.tmp.name, self.state), self.store)
        res, why = an.analyze_news(self.item(), [])
        self.assertIsNone(res)
        self.assertIn("Claude の鍵が受けつけられません", why)
        self.assertEqual(len(self.state.claude_reqs), 2)  # 新しい形の見出しと、古い形の見出しで、1回ずつ

    def test_selftest(self):
        an = Analyzer(write_config(self.tmp.name, self.state), self.store)
        ok, text = an.selftest()
        self.assertTrue(ok, text)
        self.assertEqual(text, "型分けの試し: 米国の経済指標・予想より強い｜ドル円の見立て up｜米雇用統計は予想を大きく上回る(返事の形の指定あり)")
        self.assertIn("nonfarm payrolls +250K", self.state.claude_reqs[-1]["body"]["messages"][0]["content"])
        self.state.claude_mode = "selftest_wrong"
        ok, text = an.selftest()
        self.assertFalse(ok)
        self.assertIn("思った型(米国の経済指標・予想より強い)になりませんでした", text)
        self.state.claude_mode = "error500"
        ok, text = an.selftest()
        self.assertFalse(ok)
        self.assertIn("できませんでした", text)

    def test_room_for_thinking_is_added_when_it_cannot_be_turned_off(self):
        an = Analyzer(write_config(self.tmp.name, self.state), self.store)
        self.assertEqual(an._body("s", "u", None, 800)["max_tokens"], 800)
        self.assertEqual(an._body("s", "u", None, 800, use_tuning=False)["max_tokens"], 2000)
        self.assertNotIn("thinking", an._body("s", "u", None, 800, use_tuning=False))


class PipelineTest(unittest.TestCase):
    """本体を通して: 型つきの通知、記録、Claude が止まったときの知らせ。"""

    def setUp(self):
        self.state, self.server = start_server()
        self.tmp = tempfile.TemporaryDirectory()
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.close()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def sent(self, text):
        with self.state.lock:
            return [m for m in self.state.pushover if text in m.get("title", "") or text in m.get("message", "")]

    def start(self, **over):
        st = self.state
        cfg = write_config(self.tmp.name, st, service="pushover", x_token="", contact="", **over)
        sources = make_sources(st)
        sources["feeds"][0]["kind"] = "news"
        self.app = App(cfg, sources)
        self.app.start()
        self.assertTrue(wait_for(lambda: self.app.store.is_initialized("fake")))
        self.assertTrue(wait_for(lambda: len(self.app.book) >= 5))
        return cfg

    def test_news_gets_type_record_and_is_saved(self):
        self.state.news_answer.update(KATA_ANSWER)
        self.start()
        self.state.add_feed_item("new-1", "Fed's Waller calls for more aggressive action", "Waller said a 50bp hike could be considered.")
        self.assertTrue(wait_for(lambda: self.sent("FRB理事が追加の利上げに言及")), "ニュースの通知が来ない")
        news = self.sent("FRB理事が追加の利上げに言及")[0]
        self.assertEqual(news["title"], "ドル円↑｜FRB理事が追加の利上げに言及")
        lines = news["message"].split("\n")
        self.assertEqual(lines[0], "型: FRB の人の発言・引き締め寄り(この型のあとのドル円の動きは、これから数えます)")
        self.assertIn("ドル円は上がりやすい｜円↓ ドル↑ ユーロ→ ポンド→(見立て・確からしさ: 中)", news["message"])
        # Claude への頼み方に、型の表が入っている
        with self.state.lock:
            req = [r for r in self.state.claude_reqs if "Waller" in r["body"]["messages"][0]["content"]][0]
        self.assertIn("fed_talk", req["body"]["system"])
        self.assertEqual(req["body"]["output_config"]["format"]["schema"]["properties"]["kata"]["enum"], list(analyzer.KATA_JA))
        self.assertEqual(req["body"]["max_tokens"], 800)
        # 記録に、型・向き・見立て・本文・そのときの値が残っている
        row = [r for r in self.app.store.recent_items(0, limit=50) if r["kind"] == "news"][0]
        self.assertEqual((row["kata"], row["muki"], row["usdjpy"], row["dup"]), ("fed_talk", "hawk", "up", 0))
        self.assertEqual(row["text"], "Waller said a 50bp hike could be considered.")
        self.assertAlmostEqual(json.loads(row["mids"])["USD_JPY"], 159.28, places=2)
        self.assertEqual(json.loads(row["analysis"])["tag"], "ウォラー理事")
        self.assertEqual(len(self.app.store.kata_rows(0, kata="fed_talk", muki="hawk")), 1)
        # 重要度が低くて知らせなかったニュースも、型をつけて記録する(あとで数えられるように)
        self.state.add_feed_item("low-1", "LOWTEST minor note", "Nothing much.")
        self.assertTrue(wait_for(lambda: len(self.app.store.kata_rows(0)) == 2))
        self.assertEqual(len(self.sent("LOWTEST")), 0)
        # 止めると、作りかけの1分の値も記録に残る
        store = self.app.store
        self.app.close()
        self.app = None
        check = Store(str(Path(self.tmp.name) / "data" / "fxnews.db"))
        try:
            self.assertGreaterEqual(check.count_rate_bars(), 6)
            bar = check.rate_bars("USD_JPY", 0, time.time() + 60)[-1]
            self.assertAlmostEqual(bar["c"], 159.28, places=2)
            self.assertGreaterEqual(bar["n"], 1)
        finally:
            check.close()
        del store

    def test_trial_items_are_not_counted(self):
        st = self.state
        st.news_answer.update(KATA_ANSWER)
        cfg = write_config(self.tmp.name, st, service="pushover", x_token="", contact="")
        sources = make_sources(st)
        self.app = App(cfg, sources, replay=1)
        self.app.start()
        self.assertTrue(wait_for(lambda: self.sent("お試し")), "お試しの通知が来ない")
        rows = [r for r in self.app.store.recent_items(0, limit=50) if r["kata"]]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["mids"])  # お試しの記事には、値を残さない
        self.assertEqual(self.app.store.kata_rows(0), [])  # だから、成績には数えない

    def test_blocked_claude_is_announced_at_once_and_only_once(self):
        self.state.claude_mode = "credit"
        self.start()
        self.state.add_feed_item("new-1", "Headline one", "Text one")
        self.assertTrue(wait_for(lambda: self.sent("前払いの残り(クレジット)が足りないようです")), "知らせが来ない")
        self.state.add_feed_item("new-2", "Headline two", "Text two")
        self.assertTrue(wait_for(lambda: self.app.counts["seen"] >= 2))
        time.sleep(0.3)
        alerts = self.sent("前払いの残り")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["title"], "FX見張り番からのお知らせ")
        self.assertIn("型ごとの成績の記録も止まります", alerts[0]["message"])
        self.assertIn("Claude Console(platform.claude.com)", alerts[0]["message"])
        # 型なしでも、本文は残す(あとで型をつけなおせるように)
        rows = self.app.store.recent_items(0, limit=50)
        self.assertEqual({r["kata"] for r in rows}, {None})
        self.assertEqual({r["text"] for r in rows}, {"Text one", "Text two"})

    def test_selftest_runs_once_at_real_start_and_leaves_no_record(self):
        import threading

        st = self.state
        cfg = write_config(self.tmp.name, st, service="pushover", x_token="", contact="")
        self.app = App(cfg, make_sources(st))
        runner = threading.Thread(target=self.app.run, daemon=True)
        with self.assertLogs("fxnews.app", level="INFO") as logs:
            runner.start()
            self.assertTrue(wait_for(lambda: any("self-test" in r["body"]["messages"][0]["content"] for r in list(st.claude_reqs))))
            self.assertTrue(wait_for(lambda: any("[型分けの試し: 米国の経済指標・予想より強い" in line for line in logs.output)))
            self.app.stop.set()
            runner.join(timeout=15)
        self.assertEqual(len([r for r in st.claude_reqs if "self-test" in r["body"]["messages"][0]["content"]]), 1)
        self.assertEqual(self.app.store.count_kata_items(), 0)  # 試しは、記録に残さない
        self.assertEqual(self.sent("雇用統計"), [])  # 通知もしない

    def test_report_file(self):
        self.state.news_answer.update(KATA_ANSWER)
        self.start()
        self.app.write_report()
        path = Path(self.tmp.name) / "data" / REPORT_FILE
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn("型ごとの成績(ニュースが届いたあと、ドル円がどう動いたか)", text)
        self.assertIn("まだ、数えられる記録がありません", text)
        described = "\n".join(self.app.describe())
        self.assertIn("・型ごとの成績: ニュースを型に分けて、そのあとのドル円の動きを数えています(型のついた記録 0件", described)


class CommandTest(unittest.TestCase):
    """命令: 番号入れの聞き方、成績の表示、黒い画面なしで始める、自分で始めなおす仕組み。"""

    def setUp(self):
        import run

        self.run = run
        self.state, self.server = start_server()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        os.environ["FXNEWS_HOME"] = str(self.home)

    def tearDown(self):
        try:
            call(self.run.main, ["stop"])
        finally:
            del os.environ["FXNEWS_HOME"]
            for handler in list(logging.getLogger().handlers):
                logging.getLogger().removeHandler(handler)
                handler.close()
            self.server.shutdown()
            self.server.server_close()
            self.tmp.cleanup()

    def config(self, **over):
        return write_config(str(self.home), self.state, **over)

    def test_pushover_asks_only_for_the_app_token_when_user_key_is_saved(self):
        cfg = self.config(po_token="")
        ask = answers(PO_TOKEN)
        code, out = call(self.run.cmd_setup_pushover, cfg, ask)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(ask.asked), 1)  # 聞くのは1回だけ
        self.assertIn("送り主の番号(API Token)", ask.asked[0])
        self.assertIn("受け取り番号(User Key)は、もう入っています", out)
        self.assertEqual(Config(self.home).get("pushover", "api_token"), PO_TOKEN)
        # まちがえて何も打たずに Enter を押したら、何も変えずにやめる
        code, out = call(self.run.cmd_setup_pushover, Config(self.home), answers())
        self.assertEqual(code, 1)
        self.assertIn("やめました", out)
        # 受け取り番号も入れなおすとき
        other_user = "v" + PO_USER[1:]
        ask = answers(other_user, PO_TOKEN)
        code, out = call(self.run.cmd_setup_pushover, Config(self.home), ask, True)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(ask.asked), 2)
        self.assertEqual(Config(self.home).get("pushover", "user_key"), other_user)

    def test_report_command_shows_type_records(self):
        self.config()
        code, out = call(self.run.main, ["report", "--days", "3"])
        self.assertEqual(code, 0, out)
        self.assertIn("型ごとの成績(ニュースが届いたあと、ドル円がどう動いたか)", out)
        self.assertIn("答え合わせ(直近 3日", out)

    def test_task_definition(self):
        xml = self.run.autostart_xml(Path(r"C:\Python\pythonw.exe"), Path(r"C:\Users\someone\FX & NEWS"), "S-1-5-21-1-2-3-1001")
        root = ET.fromstring(xml.replace('encoding="UTF-16"', 'encoding="utf-8"').encode("utf-8"))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find("t:Actions/t:Exec/t:Command", ns).text, r"C:\Python\pythonw.exe")
        args = root.find("t:Actions/t:Exec/t:Arguments", ns).text
        self.assertTrue(args.endswith('--home "C:\\Users\\someone\\FX & NEWS" run --auto'), args)
        self.assertIn("run.py", args)
        self.assertEqual(root.find("t:Principals/t:Principal/t:UserId", ns).text, "S-1-5-21-1-2-3-1001")
        self.assertEqual(root.find("t:Principals/t:Principal/t:LogonType", ns).text, "InteractiveToken")
        self.assertEqual(root.find("t:Principals/t:Principal/t:RunLevel", ns).text, "LeastPrivilege")
        settings = {child.tag.split("}")[1]: child.text for child in root.find("t:Settings", ns)}
        self.assertEqual(settings["MultipleInstancesPolicy"], "IgnoreNew")  # 2つ同時には動かさない
        self.assertEqual(settings["ExecutionTimeLimit"], "PT0S")  # 動かす時間に上限をつけない
        self.assertEqual(settings["DisallowStartIfOnBatteries"], "false")
        self.assertEqual(settings["StopIfGoingOnBatteries"], "false")
        self.assertEqual(settings["WakeToRun"], "false")
        intervals = [e.text for e in root.iter("{%s}Interval" % ns["t"])]
        self.assertEqual(intervals, ["PT5M", "PT5M"])

    @unittest.skipIf(os.name == "nt", "Windows では、GitHub の自動テスト(check.yml)の中で、本物のファイルを使って確かめる")
    def test_start_in_background_and_stop(self):
        import warnings

        warnings.simplefilter("ignore", ResourceWarning)  # 切りはなして始めた見張りは、このテストが終わるまで動いている
        self.config(service="pushover", x_token="", contact="")
        (self.home / "sources.json").write_text(json.dumps(make_sources(self.state), ensure_ascii=False), encoding="utf-8")
        self.assertIn("この仕組みは、Windows だけ", call(self.run.main, ["autostart-on"])[1])
        code, out = call(self.run.main, ["start"])
        self.assertEqual(code, 0, out)
        self.assertIn("見張りを始めました(じかに", out)
        self.assertIn("・動いています", out)
        code, out = call(self.run.main, ["start"])  # もう一度押しても、2つは動かない
        self.assertEqual(code, 0, out)
        self.assertIn("もう動いています", out)
        # 自分で始めなおす仕組みから呼ばれたとき: もう動いていれば、何も残さずに終わる
        before = (self.home / "data" / "commands.log").read_text(encoding="utf-8")
        self.assertEqual(self.run.main(["run", "--auto"]), 0)
        self.assertEqual((self.home / "data" / "commands.log").read_text(encoding="utf-8"), before)
        code, out = call(self.run.main, ["stop"])
        self.assertEqual(code, 0, out)
        self.assertIn("止まりました", out)
        # 「止めたままにする」のしるしがあるあいだは、自分では始めない
        (self.home / "data" / self.run.AUTOSTART_OFF).write_text("x", encoding="utf-8")
        self.assertEqual(self.run.main(["run", "--auto"]), 0)
        self.assertEqual(call(self.run.main, ["status"])[0], 1)
        # start を押すと、しるしを片づけて、また始める
        code, out = call(self.run.main, ["start"])
        self.assertEqual(code, 0, out)
        self.assertFalse((self.home / "data" / self.run.AUTOSTART_OFF).exists())
        record = (self.home / "data" / "commands.log").read_text(encoding="utf-8")
        self.assertIn("start: 終わりの番号 0", record)
        self.assertIn("run: 始めます", record)


if __name__ == "__main__":
    unittest.main()
