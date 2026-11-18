"""通し運転のテスト: にせのサーバーを相手に、見張り番を本当に動かして、通知の中身まで確かめる。"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxnews.analyzer import Analyzer  # noqa: E402
from fxnews.app import App  # noqa: E402
from fxnews.check import run_check  # noqa: E402
from fxnews.config import Config  # noqa: E402
from fxnews.feeds import FeedPoller  # noqa: E402
from fxnews.models import Item  # noqa: E402
from fxnews.notifier import Notifier  # noqa: E402
from fxnews.store import Store  # noqa: E402
from tests.fake_server import start_server  # noqa: E402


def wait_for(cond, timeout=15.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def write_config(tmp, state, **over):
    base = state.base()
    values = {
        "claude_key": "sk-test-key",
        "topic": "fx-test-topic",
        "x_token": "x-test-token",
        "contact": "Taro Yamada taro@example.com",
        "day_cap": "300",
        "claude_cap": "2000",
        "min_importance": "3",
    }
    values.update(over)
    text = """
[claude]
api_key = {claude_key}
api_base = {base}/claude
monthly_cap_yen = {claude_cap}
[ntfy]
server = {base}/ntfy
topic = {topic}
min_importance = {min_importance}
[x]
bearer_token = {x_token}
api_base = {base}/x
daily_cap_posts = {day_cap}
[sec]
contact = {contact}
base = {base}/sec
[rates]
url = {base}/ticker
interval_sec = 0.1
[general]
sleep_block = no
heartbeat_hour = -1
""".format(base=base, **values)
    (Path(tmp) / "config.ini").write_text(text, encoding="utf-8")
    return Config(tmp)


def make_sources(state):
    return {
        "context": "テスト用のメモ",
        "feeds": [{"id": "fake", "name": "にせの更新一覧", "url": state.base() + "/feed.xml", "kind": "official", "interval_sec": 0.2}],
        "edgar": {"tickers": ["NVDA"], "forms": ["8-K"], "interval_sec": 0.3},
        "x": {"rules": [{"tag": "テスト速報", "value": "from:test_account -is:retweet"}]},
    }


class EndToEndTest(unittest.TestCase):
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

    def titles(self):
        with self.state.lock:
            return [m.get("title", "") for m in self.state.ntfy]

    def find(self, text):
        with self.state.lock:
            return [m for m in self.state.ntfy if text in m.get("title", "") or text in m.get("message", "")]

    def test_full_pipeline(self):
        st = self.state
        st.x_rules = [{"id": "r1", "value": "from:someone_else", "tag": "古いルール"}]
        cfg = write_config(self.tmp.name, st)
        self.app = App(cfg, make_sources(st))
        self.app.start()

        # はじめの1回: いま載っている記事は「もう見た」ことにして、通知しない
        self.assertTrue(wait_for(lambda: self.app.store.is_initialized("fake") and self.app.store.is_initialized("edgar|8-K")))
        self.assertTrue(wait_for(lambda: len(self.app.book) >= 5))
        time.sleep(0.5)
        self.assertEqual(self.titles(), [])

        # 1) 新しいニュース
        st.add_feed_item("new-1", "Fed's Waller calls for more aggressive action", "Waller said a 50bp hike could be considered.")
        self.assertTrue(wait_for(lambda: self.find("FRB理事が追加の利上げに言及")), "ニュースの通知が来ない")
        news = self.find("FRB理事が追加の利上げに言及")[0]
        self.assertEqual(news["topic"], "fx-test-topic")
        self.assertEqual(news["title"], "円↓ ドル↑｜FRB理事が追加の利上げに言及")
        self.assertEqual(news["priority"], 4)
        self.assertEqual(news["click"], "http://example.invalid/new-1")
        self.assertTrue(news["sequence_id"].startswith("n"))
        self.assertIn("▶ 米国の金利が上がるという見方", news["message"])
        self.assertIn("円↓ ドル↑ ユーロ→ ポンド→(見立て・確からしさ: 中)", news["message"])
        self.assertIn("要人発言・重要度 4/5・にせの更新一覧", news["message"])
        self.assertIn("原文: Fed's Waller calls for more aggressive action", news["message"])

        # Claude への頼み方が、公式説明どおりか
        with st.lock:
            req = [r for r in st.claude_reqs if "Waller" in r["body"]["messages"][0]["content"]][0]
        self.assertEqual(req["headers"]["authorization"], "Bearer sk-test-key")
        self.assertEqual(req["headers"]["anthropic-version"], "2026-10-21")
        self.assertNotIn("x-api-key", req["headers"])
        body = req["body"]
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertEqual(body["output_config"]["effort"], "low")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, body)
        self.assertIn("テスト用のメモ", body["messages"][0]["content"])

        # 2) 同じ話は知らせない / 重要度が低いものは知らせない
        before = len(self.titles())
        st.add_feed_item("new-2", "DUPTEST same story again", "nothing new")
        st.add_feed_item("new-3", "LOWTEST ribbon cutting ceremony", "not about markets")
        self.assertTrue(wait_for(lambda: self.app.counts["dup"] >= 1 and self.app.counts["skipped"] >= 1))
        self.assertEqual(len(self.titles()), before)

        # 3) 決算の届け出(見張っている会社だけ。発表文の冒頭も Claude に渡す)
        st.add_edgar_entry("Some Other Co", 999999, "0000999999-26-000001", items=("8.01",))
        st.add_edgar_entry("NVIDIA CORP", 1045810, "0001045810-26-000123")
        self.assertTrue(wait_for(lambda: any("NVIDIA CORP(NVDA)が届け出 8-K" in r["body"]["messages"][0]["content"] for r in list(st.claude_reqs))), "決算の届け出が Claude に渡らない")
        with st.lock:
            ereq = [r for r in st.claude_reqs if "NVIDIA CORP(NVDA)" in r["body"]["messages"][0]["content"]][0]
            agents = list(st.sec_agents)
        content = ereq["body"]["messages"][0]["content"]
        self.assertIn("決算の発表(2.02)", content)
        self.assertIn("[発表文の冒頭]", content)
        self.assertIn("Record quarterly revenue of $12.3 billion, up 45% from a year ago & up 8%", content)
        self.assertNotIn("color: red", content)
        self.assertTrue(all(a == "FX-NEWS Taro Yamada taro@example.com" for a in agents), agents)
        self.assertFalse(any("Some Other Co" in r["body"]["messages"][0]["content"] for r in list(st.claude_reqs)))

        # 4) X の投稿
        with st.lock:
            st.x_lines = [
                {"data": {"id": "111", "text": "BOJ's Ueda: will raise rates if outlook is realized", "author_id": "9", "created_at": "2026-11-18T17:57:36.000Z"}, "matching_rules": [{"id": "r100", "tag": "テスト速報"}]},
                {"data": {"id": "111", "text": "duplicate delivery", "author_id": "9"}, "matching_rules": [{"id": "r100", "tag": "テスト速報"}]},
            ]
        self.assertTrue(wait_for(lambda: any("BOJ's Ueda" in r["body"]["messages"][0]["content"] for r in list(st.claude_reqs))), "X の投稿が Claude に渡らない")
        with st.lock:
            xreq = [r for r in st.claude_reqs if "BOJ's Ueda" in r["body"]["messages"][0]["content"]][0]
            rule_posts = list(st.x_rule_posts)
            queries = list(st.x_stream_queries)
            auths = list(st.x_auth)
        self.assertIn("情報源: X: テスト速報(種類: Xの投稿)", xreq["body"]["messages"][0]["content"])
        self.assertEqual(rule_posts[0], {"delete": {"ids": ["r1"]}})
        self.assertEqual(rule_posts[1], {"add": [{"value": "from:test_account -is:retweet", "tag": "テスト速報"}]})
        self.assertEqual(queries[0], {"tweet.fields": ["created_at,author_id,lang,note_tweet"]})
        self.assertEqual(auths[0], "Bearer x-test-token")
        self.assertTrue(wait_for(lambda: self.app.xstream.counts() == (1, 1)))  # 同じ投稿は1件として数える
        self.assertTrue(wait_for(lambda: any(m.get("click") == "https://x.com/i/web/status/111" for m in list(st.ntfy))))

        # 5) 急落(円が買われる)
        with st.lock:
            for pair in ("USD_JPY", "EUR_JPY", "GBP_JPY"):
                st.prices[pair] *= 0.997
        self.assertTrue(wait_for(lambda: len(self.find("急落")) >= 2), "急落の通知(先ぶれと原因つき)が来ない")
        spikes = self.find("急落")
        first, second = spikes[0], spikes[1]
        self.assertTrue(first["title"].startswith("円↑｜"), first["title"])
        self.assertIn("が急落 1分で-", first["title"])
        self.assertEqual(first["priority"], 5)
        self.assertEqual(first["sequence_id"], second["sequence_id"])
        self.assertTrue(first["sequence_id"].startswith("s"))
        self.assertNotIn("考えられる原因", first["message"])
        self.assertIn("▶ 考えられる原因: 直前のニュースには、はっきりした原因は見当たりません", second["message"])
        self.assertIn("動いた通貨: 円が買われている", second["message"])
        self.assertIn("直前のニュース:", second["message"])
        time.sleep(1.0)
        self.assertEqual(len(self.find("急落")), 2, "同じ動きを何度も知らせている")

        # 使った量の記録
        self.assertGreater(self.app.analyzer.spent_yen(), 0)
        self.assertGreaterEqual(self.app.notifier.sent_today(), 4)
        self.assertEqual(self.app.health.failing(), {})

    def test_without_claude_and_budget_stops(self):
        st = self.state
        cfg = write_config(self.tmp.name, st, claude_key="", day_cap="2")
        sources = make_sources(st)
        with st.lock:
            st.x_lines = [
                {"data": {"id": "201", "text": "post one", "author_id": "9"}, "matching_rules": [{"id": "r", "tag": "テスト速報"}]},
                {"data": {"id": "202", "text": "post two", "author_id": "9"}, "matching_rules": [{"id": "r", "tag": "テスト速報"}]},
                {"data": {"id": "203", "text": "post three (should not be read)", "author_id": "9"}, "matching_rules": [{"id": "r", "tag": "テスト速報"}]},
            ]
        self.app = App(cfg, sources)
        self.app.start()
        self.assertTrue(wait_for(lambda: self.app.store.is_initialized("fake")))
        st.add_feed_item("raw-1", "Federal Reserve issues FOMC statement", "")
        # Claude がなくても、公式発表は見出しのまま知らせる
        self.assertTrue(wait_for(lambda: self.find("Federal Reserve issues FOMC statement")))
        raw = self.find("Federal Reserve issues FOMC statement")[0]
        self.assertEqual(raw["title"], "Federal Reserve issues FOMC statement")
        self.assertEqual(raw["priority"], 3)
        self.assertIn("(解説なし: Claude の鍵が未設定)", raw["message"])
        # X は、きょうの上限(2件)で止まり、お知らせが届く
        self.assertTrue(wait_for(lambda: self.find("X のきょうの上限(2件)に届きました")), "上限のお知らせが来ない")
        self.assertEqual(self.app.xstream.counts(), (2, 2))
        hits = st.x_stream_hits
        time.sleep(0.8)
        self.assertEqual(st.x_stream_hits, hits, "上限に届いたあとも X につなぎ直している")
        self.assertEqual(len(self.find("X のきょうの上限")), 1)
        with st.lock:
            self.assertEqual(st.claude_reqs, [])

    def test_dry_run_sends_nothing(self):
        st = self.state
        cfg = write_config(self.tmp.name, st, x_token="", contact="")
        self.app = App(cfg, make_sources(st), replay=1, dry=True)
        self.app.start()
        self.assertTrue(wait_for(lambda: self.app.counts["notified"] >= 1))
        time.sleep(0.3)
        self.assertEqual(self.titles(), [])
        self.assertEqual(st.x_stream_hits, 0)
        self.assertEqual(st.sec_agents, [])

    def test_check_command(self):
        st = self.state
        cfg = write_config(self.tmp.name, st)
        lines = []
        bad = run_check(cfg, make_sources(st), out=lines.append)
        text = "\n".join(lines)
        self.assertEqual(bad, 0, text)
        self.assertIn("OK  にせの更新一覧: 2件", text)
        self.assertIn("変更なしの短い返事: なし(毎回ぜんぶ受け取る)", text)
        self.assertRegex(text, r"1日の通信量の目安\(更新一覧と為替レートを24時間見張った場合\): 約 \d+ MB")
        self.assertIn("Old headline two", text)
        self.assertIn("OK  6つの組合せを取れました", text)
        self.assertIn("ドル円 159.28", text)
        self.assertIn("NVDA=1045810", text)
        self.assertIn("返事あり", text)
        self.assertIn("設定あり(sk-t…ey)", text)
        self.assertNotIn("sk-test-key", text)
        self.assertNotIn("x-test-token", text)
        self.assertNotIn("fx-test-topic", text)
        self.assertEqual(st.ntfy, [])
        # つながらないものがあれば、その数を返す
        st.x_status = 401
        cfg2 = write_config(self.tmp.name, st)
        sources = make_sources(st)
        sources["feeds"][0]["url"] = st.base() + "/missing.xml"
        lines = []
        self.assertEqual(run_check(cfg2, sources, out=lines.append), 2)
        self.assertIn("NG  にせの更新一覧: HTTP 404", "\n".join(lines))


class OperationTest(unittest.TestCase):
    """動かしつづけるときの ふるまい(始まりの知らせ、つながらないときの知らせ、毎朝の知らせ、コマンド)。"""

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

    def find(self, text):
        with self.state.lock:
            return [m for m in self.state.ntfy if text in m.get("title", "") or text in m.get("message", "")]

    def test_run_sends_start_notice(self):
        import threading

        cfg = write_config(self.tmp.name, self.state, x_token="", contact="")
        self.app = App(cfg, make_sources(self.state))
        runner = threading.Thread(target=self.app.run, daemon=True)
        runner.start()
        self.assertTrue(wait_for(lambda: self.find("FX見張り番を始めました")), "始まりの知らせが来ない")
        start = self.find("FX見張り番を始めました")[0]
        self.assertEqual(start["priority"], 2)
        self.assertIn("・更新一覧: 1本(にせの更新一覧)", start["message"])
        self.assertIn("・X: 止めています", start["message"])
        self.assertIn("・会社の届け出(SEC): 止めています", start["message"])
        self.assertIn("・解説(Claude): claude-haiku-5-5。今月の上限 2000円", start["message"])
        self.assertIn("・通知(ntfy): 重要度 3 以上を送ります", start["message"])
        self.assertNotIn("fx-test-topic", start["message"])
        self.assertNotIn("sk-test-key", start["message"])
        self.app.stop.set()
        runner.join(timeout=15)
        self.assertFalse(runner.is_alive())

    def test_source_down_and_recovery_are_announced(self):
        st = self.state
        cfg = write_config(self.tmp.name, st, x_token="", contact="")
        self.app = App(cfg, make_sources(st))
        self.app.health.grace = 0.3
        self.app.start()
        self.assertTrue(wait_for(lambda: self.app.store.is_initialized("fake")))
        st.feed_status = 503
        self.assertTrue(wait_for(lambda: self.find("にせの更新一覧 に 0分以上つながりません(HTTP 503")), "つながらない知らせが来ない")
        self.assertEqual(list(self.app.health.failing()), ["にせの更新一覧"])
        st.feed_status = 200
        self.assertTrue(wait_for(lambda: self.find("にせの更新一覧 に、またつながるようになりました"), timeout=30), "直った知らせが来ない")
        self.assertEqual(self.app.health.failing(), {})
        self.assertEqual(len(self.find("つながりません")), 1)

    def test_x_auth_error_is_announced_once(self):
        st = self.state
        st.x_status = 401
        cfg = write_config(self.tmp.name, st, contact="")
        self.app = App(cfg, make_sources(st))
        self.app.start()
        self.assertTrue(wait_for(lambda: self.find("X につなげません(HTTP 401")), "X の鍵のエラーの知らせが来ない")
        time.sleep(0.5)
        self.assertEqual(len(self.find("X につなげません")), 1)
        self.assertEqual(st.x_stream_hits, 0)

    def test_morning_heartbeat(self):
        import calendar

        cfg = write_config(self.tmp.name, self.state, x_token="", contact="")
        (Path(self.tmp.name) / "config.ini").write_text(
            (Path(self.tmp.name) / "config.ini").read_text(encoding="utf-8").replace("heartbeat_hour = -1", "heartbeat_hour = 8"), encoding="utf-8"
        )
        cfg = Config(self.tmp.name)
        self.app = App(cfg, make_sources(self.state))
        eight_jst = calendar.timegm((2026, 11, 19, 23, 5, 0))  # 日本時間 11/20 08:05
        self.app.started = eight_jst - 30 * 60
        self.assertFalse(self.app.housekeeping_tick(eight_jst))  # 起動してすぐは送らない
        self.app.started = eight_jst - 3 * 86400
        self.assertFalse(self.app.housekeeping_tick(eight_jst + 60))  # その日はもう済み
        self.assertFalse(self.app.housekeeping_tick(eight_jst + 5 * 3600))  # 8時台ではない
        self.assertTrue(self.app.housekeeping_tick(eight_jst + 86400))  # 次の日の8時台
        self.assertFalse(self.app.housekeeping_tick(eight_jst + 86400 + 120))
        beats = self.find("FX見張り番は動いています")
        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0]["priority"], 2)
        self.assertIn("この24時間の通知: 0件", beats[0]["message"])

    def test_command_line(self):
        import contextlib
        import io
        import os

        import run

        st = self.state
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        os.environ["FXNEWS_HOME"] = str(home)
        try:
            def call(*argv):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = run.main(list(argv))
                return code, buf.getvalue()

            # topic: 見本から config.ini を作り、あて先を書きこむ
            code, out = call("topic")
            self.assertEqual(code, 0)
            cfg = Config(home)
            topic = cfg.get("ntfy", "topic")
            self.assertRegex(topic, r"^fx-([a-z2-9]{4}-){3}[a-z2-9]{4}$")
            self.assertIn(topic, out)
            self.assertIn("# FX見張り番の設定(見本)", (home / "config.ini").read_text(encoding="utf-8"))
            code, out2 = call("topic")
            self.assertIn("もう作ってあります", out2)
            self.assertEqual(Config(home).get("ntfy", "topic"), topic)

            # ここから先は、にせのサーバーに向ける
            write_config(str(home), st, topic=topic, x_token="", contact="")
            code, out = call("test-notify")
            self.assertEqual(code, 0, out)
            self.assertEqual(st.ntfy[-1]["title"], "円↑ ドル↓｜FX見張り番のテスト通知")
            self.assertEqual(st.ntfy[-1]["topic"], topic)

            (home / "sources.json").write_text(json.dumps(make_sources(st), ensure_ascii=False), encoding="utf-8")
            code, out = call("check")
            self.assertEqual(code, 0, out)
            self.assertIn("点検おわり: つながらなかったもの 0件", out)

            code, out = call("run", "--dry", "--replay", "1", "--seconds", "1.5")
            self.assertEqual(code, 0, out)
            self.assertIn("FX見張り番 0.1.0 を始めます", out)
            self.assertIn("[通知] 円↓ ドル↑｜FRB理事が追加の利上げに言及", out)
            self.assertIn("(お試しモード: 送っていません)", out)
            self.assertIn("[おわり]", out)
            self.assertTrue((home / "data" / "fxnews.db").exists())
            self.assertTrue((home / "data" / "fxnews.log").exists())

            code, out = call("report", "--days", "3")
            self.assertEqual(code, 0)
            self.assertIn("答え合わせ(直近 3日", out)
        finally:
            del os.environ["FXNEWS_HOME"]
            import logging

            for handler in list(logging.getLogger().handlers):
                logging.getLogger().removeHandler(handler)
                handler.close()


class PieceTest(unittest.TestCase):
    """本体を通さずに、部品とにせのサーバーだけで確かめるもの。"""

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

    def test_claude_falls_back_to_legacy_auth(self):
        self.state.claude_mode = "need_legacy_auth"
        an = Analyzer(write_config(self.tmp.name, self.state), self.store)
        res, why = an.analyze_news(self.item(), [])
        self.assertEqual(why, "")
        self.assertEqual(res["importance"], 4)
        heads = [r["headers"] for r in self.state.claude_reqs]
        self.assertIn("authorization", heads[0])
        self.assertEqual(heads[1].get("x-api-key"), "sk-test-key")
        self.assertNotIn("authorization", heads[1])
        an.analyze_news(self.item(), [])
        self.assertEqual(len(self.state.claude_reqs), 3)  # 2回目からは、はじめから古い形で送る

    def test_claude_falls_back_without_format(self):
        self.state.claude_mode = "reject_format"
        an = Analyzer(write_config(self.tmp.name, self.state), self.store)
        res, why = an.analyze_news(self.item(), [])
        self.assertEqual(why, "")
        self.assertEqual(res["usd"], "up")
        bodies = [r["body"] for r in self.state.claude_reqs]
        self.assertIn("format", bodies[0]["output_config"])
        self.assertNotIn("format", bodies[1]["output_config"])
        self.assertEqual(bodies[1]["output_config"]["effort"], "low")

    def test_claude_errors_are_reported_not_raised(self):
        an = Analyzer(write_config(self.tmp.name, self.state), self.store)
        self.state.claude_mode = "garbage"
        res, why = an.analyze_news(self.item(), [])
        self.assertIsNone(res)
        self.assertIn("返事を読めませんでした", why)
        self.state.claude_mode = "error500"
        res, why = an.analyze_news(self.item(), [])
        self.assertIsNone(res)
        self.assertIn("HTTP 500", why)
        res, why = an.analyze_news(self.item(), [])
        self.assertIn("一時停止中", why)

    def test_claude_budget(self):
        cfg = write_config(self.tmp.name, self.state, claude_cap="0.05")
        an = Analyzer(cfg, self.store)
        res, _ = an.analyze_news(self.item(), [])
        self.assertIsNotNone(res)
        # 1回あたり 1500×0.10 + 400×0.50 = 350 → 0.00035ドル = 0.056円
        self.assertAlmostEqual(an.spent_yen(), 0.056, places=4)
        self.assertTrue(an.over_budget())
        res, why = an.analyze_news(self.item(), [])
        self.assertIsNone(res)
        self.assertIn("今月の上限", why)
        self.assertEqual(len(self.state.claude_reqs), 1)

    def test_ntfy_limit_and_errors(self):
        cfg = write_config(self.tmp.name, self.state)
        n = Notifier(cfg, self.store)
        n.daily_limit = 2
        self.assertEqual(n.send("題", "本文", 9), (True, ""))
        self.assertEqual(self.state.ntfy[0]["priority"], 5)
        self.assertTrue(n.send("あ" * 500, "い" * 5000, 3)[0])
        sent = self.state.ntfy[1]
        self.assertLessEqual(len(sent["title"].encode("utf-8")), 240)
        self.assertLessEqual(len(sent["message"].encode("utf-8")), 3800)
        ok, why = n.send("題", "本文")
        self.assertFalse(ok)
        self.assertIn("きょうの通知の上限(2通)", why)
        n.daily_limit = 0
        self.state.ntfy_status = 429
        ok, why = n.send("題", "本文")
        self.assertFalse(ok)
        self.assertIn("HTTP 429", why)
        self.assertEqual(len(self.state.ntfy), 2)

    def test_feed_conditional_get(self):
        self.state.feed_etag = '"v1"'
        poller = FeedPoller({"id": "f", "name": "n", "url": self.state.base() + "/feed.xml"}, self.store, None, None)
        self.assertEqual(poller.poll_once(), [])  # はじめの1回は「もう見た」ことにするだけ
        self.assertEqual(poller.poll_once(), [])
        self.assertEqual(self.state.feed_304, 1)
        self.state.add_feed_item("n1", "New one", "t")
        self.state.add_feed_item("too-old", "Stale", "t", ts=time.time() - 86400)
        self.state.feed_etag = '"v2"'
        fresh = poller.poll_once()
        self.assertEqual([i.title for i in fresh], ["New one"])
        self.assertEqual(fresh[0].key, "f|n1")
        self.assertEqual(poller.poll_once(), [])
        self.assertGreater(poller.last_wire, 100)
        self.assertGreaterEqual(poller.session.reused, 3)  # つないだままの接続を使い回している
        poller.close()

    def test_session_keeps_connection(self):
        from fxnews.net import HttpError, Session

        st = self.state
        session = Session()
        for _ in range(5):
            resp = session.get(st.base() + "/ticker")
            self.assertEqual(resp.status, 200)
            self.assertIn("USD_JPY", resp.text())
        self.assertEqual(len(set(st.ticker_ports)), 1)  # 5回とも同じ接続
        self.assertEqual(session.reused, 4)
        # 相手(にせのサーバー)が、返事のたびにだまって接続を切っても、つなぎ直して成功する
        st.drop_ticker_after = 1
        for _ in range(3):
            self.assertEqual(session.get(st.base() + "/ticker").status, 200)
        self.assertGreaterEqual(len(set(st.ticker_ports)), 3)
        st.drop_ticker_after = 0
        # こちらの接続がこわれていたときも、つなぎ直して成功する
        self.assertEqual(session.get(st.base() + "/ticker").status, 200)
        session._conn.sock.close()
        self.assertEqual(session.get(st.base() + "/ticker").status, 200)
        # エラーの番号は、ふつうの request() と同じ形で伝わる
        with self.assertRaises(HttpError) as ctx:
            session.get(st.base() + "/missing")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(session.get(st.base() + "/ticker").status, 200)
        session.close()

    def test_feed_url_date_placeholder(self):
        from fxnews.util import utc_day

        poller = FeedPoller({"id": "p", "name": "n", "url": "https://x.invalid/feed?start_date={yesterday}"}, self.store, None, None)
        self.assertEqual(poller.current_url(), "https://x.invalid/feed?start_date=" + utc_day(time.time() - 86400))
        self.assertRegex(poller.current_url(), r"start_date=\d{4}-\d{2}-\d{2}$")
        plain = FeedPoller({"id": "q", "name": "n", "url": "https://x.invalid/feed"}, self.store, None, None)
        self.assertEqual(plain.current_url(), "https://x.invalid/feed")


if __name__ == "__main__":
    unittest.main()
