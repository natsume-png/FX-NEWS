"""v0.2 で足したものの確かめ。

- Pushover(通知ごとに音を選べる通知アプリ)への通知と、送る数の上限
- 黒い画面なしで動かすための、始め方・止め方・動いているかの見方
- 鍵や番号を入れるコマンド
外のネットにはつながず、にせのサーバーを相手にする。
"""

import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxnews import notifier, single, util, winconsole  # noqa: E402
from fxnews.app import ALIVE_FILE, STOP_FILE, App  # noqa: E402
from fxnews.check import run_check  # noqa: E402
from fxnews.config import Config  # noqa: E402
from fxnews.notifier import Notifier  # noqa: E402
from fxnews.store import Store  # noqa: E402
from tests.fake_server import start_server  # noqa: E402
from tests.test_e2e import PO_TOKEN, PO_USER, make_sources, wait_for, write_config  # noqa: E402


def answers(*lines):
    """たずねられるたびに、決めた答えを順番に返す(キーボードのかわり)。答えがつきたら、空の行を返す。"""
    queue = list(lines)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return queue.pop(0) if queue else ""

    ask.asked = asked
    return ask


def call(func, *args, **kwargs):
    """画面に出るはずの文字をつかまえて、(戻り値, 文字) を返す。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(*args, **kwargs)
    return code, buf.getvalue()


class SmallPiecesTest(unittest.TestCase):
    def test_pushover_priority(self):
        self.assertEqual([notifier.pushover_priority(p) for p in (1, 2, 3, 4, 5)], [-1, -1, 0, 1, 1])
        self.assertEqual(notifier.pushover_priority(5, followup=True), -1)

    def test_cut_chars(self):
        self.assertEqual(util.cut_chars("あいう", 3), "あいう")
        cut = util.cut_chars("あ" * 2000, 1024)
        self.assertEqual(len(cut), 1024)
        self.assertTrue(cut.endswith("…"))

    def test_next_utc_month_start(self):
        import calendar

        nov = calendar.timegm((2026, 11, 20, 2, 0, 0))
        self.assertEqual(util.next_utc_month_start(nov), calendar.timegm((2026, 12, 1, 0, 0, 0)))
        dec = calendar.timegm((2026, 12, 31, 23, 59, 59))
        self.assertEqual(util.next_utc_month_start(dec), calendar.timegm((2027, 1, 1, 0, 0, 0)))

    def test_console_rect(self):
        self.assertEqual(winconsole.parse_rect("0,0,598,392"), (0, 0, 598, 392))
        self.assertEqual(winconsole.parse_rect(" 10 , 20 , 600 , 400 "), (10, 20, 600, 400))
        for bad in ("", "0,0,598", "a,b,c,d", "0,0,50,50"):
            self.assertIsNone(winconsole.parse_rect(bad), bad)
        ok, note = winconsole.place(None)
        self.assertFalse(ok)
        # 黒い画面がない場所(テストの機械)でも、こけずに答えを返す
        ok, note = winconsole.place((0, 0, 598, 392), wait=0.3)
        self.assertIn(ok, (True, False))
        self.assertTrue(note)
        if os.name != "nt":
            self.assertFalse(ok)

    def test_only_one_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            first = single.Lock(tmp, data)
            self.assertFalse(single.is_running(tmp, data))
            self.assertTrue(first.acquire())
            self.assertTrue(single.is_running(tmp, data))
            second = single.Lock(tmp, data)
            self.assertFalse(second.acquire())
            # 別のフォルダの見張り番とは、ぶつからない
            with tempfile.TemporaryDirectory() as other:
                self.assertFalse(single.is_running(other, Path(other) / "data"))
            first.release()
            self.assertFalse(single.is_running(tmp, data))
            self.assertTrue(second.acquire())
            second.release()


class PushoverTest(unittest.TestCase):
    """Pushover への送り方が、公式説明どおりか。上限を守るか。"""

    def setUp(self):
        self.state, self.server = start_server()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def notifier(self, **over):
        over.setdefault("service", "pushover")
        n = Notifier(write_config(self.tmp.name, self.state, **over), self.store)
        n.retry_wait = 0.01
        return n

    def test_send_fields(self):
        n = self.notifier()
        self.assertTrue(n.available())
        self.assertEqual(n.name_ja, "Pushover")
        self.assertFalse(n.can_replace)
        self.assertEqual(n.send("円↓ ドル↑｜見出し", "本文です", 4, click="https://example.invalid/a"), (True, ""))
        sent = self.state.pushover[0]
        self.assertEqual(sent["token"], PO_TOKEN)
        self.assertEqual(sent["user"], PO_USER)
        self.assertEqual(sent["title"], "円↓ ドル↑｜見出し")
        self.assertEqual(sent["message"], "本文です")
        self.assertEqual(sent["priority"], "1")
        self.assertEqual(sent["sound"], "bugle")
        self.assertEqual(sent["url"], "https://example.invalid/a")
        self.assertEqual(sent["url_title"], "もとの記事を開く")
        self.assertNotIn("device", sent)
        # 送った数と、アカウントぜんぶの残り(返事の見出しから読む)
        self.assertEqual((n.sent_today(), n.sent_month()), (1, 1))
        self.assertEqual((n.remaining, n.account_limit), (8999, 10000))
        self.assertIn("今月 1通 / 上限 5000通、アカウントぜんぶの今月の残り 8999通", n.limits_text())
        self.assertEqual(self.state.ntfy, [])

    def test_sound_and_priority_by_kind(self):
        n = self.notifier()
        n.send("ふつうのニュース", "本文", 3)
        n.send("重要なニュース", "本文", 5)
        n.send("急騰急落", "本文", 4, kind="spike")
        n.send("急騰急落のつづき", "本文", 4, kind="spike", followup=True)
        n.send("お知らせ", "本文", 3, kind="system")
        n.send("静かなお知らせ", "本文", 2, kind="system")
        got = [(m["title"], m["priority"], m.get("sound")) for m in self.state.pushover]
        self.assertEqual(got, [
            ("ふつうのニュース", "0", "incoming"),
            ("重要なニュース", "1", "bugle"),
            ("急騰急落", "1", "spacealarm"),
            ("急騰急落のつづき", "-1", None),
            ("お知らせ", "0", "mechanical"),
            ("静かなお知らせ", "-1", None),
        ])
        for name in n.po_sounds.values():
            self.assertIn(name, notifier.PUSHOVER_SOUNDS)

    def test_long_text_is_cut_to_pushover_limits(self):
        n = self.notifier()
        self.assertTrue(n.send("あ" * 500, "い" * 5000, 3, click="https://example.invalid/" + "x" * 600)[0])
        sent = self.state.pushover[0]
        self.assertEqual(len(sent["title"]), 250)
        self.assertEqual(len(sent["message"]), 1024)
        self.assertNotIn("url", sent)  # 512文字を超えるリンクは、つけない
        self.assertTrue(n.send("題", "", 3)[0])
        self.assertEqual(self.state.pushover[1]["message"], "-")  # 本文なしでは送れないので、線を入れる

    def test_own_daily_and_monthly_limits(self):
        n = self.notifier(po_day="2", po_month="3")
        self.assertTrue(n.send("1", "本文")[0])
        self.assertTrue(n.send("2", "本文")[0])
        ok, why = n.send("3", "本文")
        self.assertFalse(ok)
        self.assertIn("きょうの通知の上限(2通)", why)
        self.assertEqual(n.limit_reason, why)
        # プログラムからのお知らせ(上限に届いた、など)は、上限にかかわらず送る
        self.assertTrue(n.send("お知らせ", "本文", kind="system")[0])
        n.daily_limit = 0
        ok, why = n.send("4", "本文")
        self.assertFalse(ok)
        self.assertIn("今月の通知の上限(3通)", why)
        self.assertEqual(len(self.state.pushover), 3)

    def test_reserve_keeps_room_for_other_senders(self):
        self.state.pushover_remaining = 1502
        n = self.notifier()
        self.assertTrue(n.send("1", "本文")[0])  # 残り 1501
        self.assertTrue(n.send("2", "本文")[0])  # 残り 1500
        ok, why = n.send("3", "本文")
        self.assertFalse(ok)
        self.assertIn("今月の残りが 1500通になった(ほかの通知のために残す数は 1500通)", why)
        self.assertEqual(n.limit_reason, why)
        self.assertTrue(n.send("お知らせ", "本文", kind="system")[0])
        self.assertEqual(self.state.pushover_hits, 3)
        zero = self.notifier(po_reserve="0")
        zero.remaining = 5
        self.assertTrue(zero.send("残りを見ない設定", "本文")[0])

    def test_account_limit_reached_stops_until_reset(self):
        n = self.notifier()
        self.state.pushover_status = 429
        self.state.pushover_remaining = 0
        ok, why = n.send("題", "本文")
        self.assertFalse(ok)
        self.assertIn("今月の送れる数を使い切りました", why)
        self.assertEqual(self.state.pushover_hits, 1)  # 送り直さない
        self.assertAlmostEqual(n._blocked_until, self.state.pushover_reset, delta=1)
        self.state.pushover_status = 200
        ok, why = n.send("題", "本文", kind="system")
        self.assertFalse(ok)
        self.assertIn("まで送れません", why)
        self.assertEqual(self.state.pushover_hits, 1)  # 元にもどる時刻までは、たのみにも行かない
        # 元にもどる時刻をすぎたら、前に聞いた「残り 0通」を忘れて、また送る
        self.assertEqual(n.remaining, 0)
        n._blocked_until = n._limit_reset = time.time() - 1
        self.state.pushover_remaining = 10000
        self.state.pushover_reset = int(time.time()) + 30 * 86400
        self.assertTrue(n.send("月が変わった", "本文")[0])
        self.assertEqual(n.remaining, 9999)

    def test_stale_remaining_is_forgotten_after_reset(self):
        """残りが少なくて止めていても、月が変わったら、たのみに行って残りを聞きなおす。"""
        self.state.pushover_remaining = 1501
        n = self.notifier()
        self.assertTrue(n.send("1", "本文")[0])
        self.assertFalse(n.send("2", "本文")[0])
        n._limit_reset = time.time() - 1
        self.state.pushover_remaining = 10000
        self.assertTrue(n.send("3", "本文")[0])
        self.assertEqual(n.remaining, 9999)

    def test_refused_is_not_retried_but_server_error_is(self):
        n = self.notifier()
        self.state.pushover_status = 400
        ok, why = n.send("題", "本文")
        self.assertFalse(ok)
        self.assertIn("Pushover が受けつけませんでした(HTTP 400 something went wrong)", why)
        self.assertEqual(n.limit_reason, "")
        self.assertEqual(self.state.pushover_hits, 1)
        self.assertNotIn(PO_TOKEN, why)
        self.state.pushover_status = 200
        self.state.pushover_fail_first = 1
        self.assertEqual(n.send("題", "本文"), (True, ""))
        self.assertEqual(self.state.pushover_hits, 3)  # 500 のあと、1回だけやり直した
        self.state.pushover_fail_first = 5
        ok, why = n.send("題", "本文")
        self.assertFalse(ok)
        self.assertIn("HTTP 500", why)
        self.assertEqual(self.state.pushover_hits, 5)

    def test_missing_or_broken_keys(self):
        n = self.notifier(po_user="too-short")
        self.assertFalse(n.available())
        self.assertIn("[pushover] user_key と api_token", n.missing())
        self.assertEqual(n.send("題", "本文"), (False, n.missing()))
        self.assertEqual(self.state.pushover_hits, 0)
        odd = self.notifier(service="line")
        self.assertEqual((odd.service, odd.bad_service), ("ntfy", "line"))

    def test_check_shows_pushover_state_without_leaking_keys(self):
        cfg = write_config(self.tmp.name, self.state, service="pushover")
        lines = []
        bad = run_check(cfg, make_sources(self.state), out=lines.append)
        text = "\n".join(lines)
        self.assertEqual(bad, 0, text)
        self.assertIn("■ 通知(Pushover)", text)
        self.assertIn("今月の送れる数: 残り 9000通 / 10000通", text)
        self.assertIn("このプログラムの上限: 1日 200通・1か月 5000通。アカウントの残りが 1500通を切ったら", text)
        self.assertIn("音: ニュース incoming / 重要なニュース bugle / 急騰急落 spacealarm / プログラムからのお知らせ mechanical", text)
        self.assertNotIn(PO_TOKEN, text)
        self.assertNotIn(PO_USER, text)
        self.assertEqual(self.state.pushover, [])  # 点検では、通知を送らない
        self.assertEqual([q[1] for q in self.state.pushover_queries], [PO_TOKEN, PO_TOKEN])
        # 番号がまだなければ、入れ方を案内する
        cfg = write_config(self.tmp.name, self.state, service="pushover", po_token="")
        lines = []
        run_check(cfg, make_sources(self.state), out=lines.append)
        self.assertIn("setup-pushover を実行すると入れられます", "\n".join(lines))


class PushoverPipelineTest(unittest.TestCase):
    """見張り番を本当に動かして、Pushover に届く通知の中身を確かめる。"""

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
            return [m for m in self.state.pushover if text in m.get("title", "") or text in m.get("message", "")]

    def start(self, **over):
        over.setdefault("service", "pushover")
        over.setdefault("x_token", "")
        over.setdefault("contact", "")
        cfg = write_config(self.tmp.name, self.state, **over)
        self.app = App(cfg, make_sources(self.state))
        self.app.notifier.retry_wait = 0.01
        self.app.start()
        self.assertTrue(wait_for(lambda: self.app.store.is_initialized("fake")))
        self.assertTrue(wait_for(lambda: len(self.app.book) >= 5))

    def drop_yen_pairs(self):
        with self.state.lock:
            for pair in ("USD_JPY", "EUR_JPY", "GBP_JPY"):
                self.state.prices[pair] *= 0.997

    def test_news_and_spike_without_clue(self):
        self.start()
        self.state.add_feed_item("new-1", "Fed's Waller calls for more aggressive action", "Waller said a 50bp hike could be considered.")
        self.assertTrue(wait_for(lambda: self.find("FRB理事が追加の利上げに言及")), "ニュースの通知が来ない")
        news = self.find("FRB理事が追加の利上げに言及")[0]
        self.assertEqual(news["title"], "円↓ ドル↑｜FRB理事が追加の利上げに言及")
        self.assertEqual((news["priority"], news["sound"]), ("1", "bugle"))
        self.assertEqual(news["url"], "http://example.invalid/new-1")
        self.assertIn("円↓ ドル↑ ユーロ→ ポンド→(見立て・確からしさ: 中)", news["message"])
        self.assertLessEqual(len(news["message"]), 1024)
        # 急落: 先に値動きだけを鳴らす。原因の手がかりがないときは、2通目を送らない
        self.drop_yen_pairs()
        self.assertTrue(wait_for(lambda: self.find("急落")), "急落の通知が来ない")
        self.assertTrue(wait_for(lambda: any("急な動き" in r["body"].get("system", "") for r in list(self.state.claude_reqs))))
        time.sleep(1.0)
        spikes = self.find("急落")
        self.assertEqual(len(spikes), 1, [m["title"] for m in spikes])
        self.assertTrue(spikes[0]["title"].startswith("円↑｜"), spikes[0]["title"])
        self.assertEqual((spikes[0]["priority"], spikes[0]["sound"]), ("1", "spacealarm"))
        self.assertNotIn("考えられる原因", spikes[0]["message"])
        self.assertEqual(self.state.ntfy, [])

    def test_spike_cause_arrives_as_quiet_second_message(self):
        self.state.spike_cause = "日銀総裁の発言が手がかりの可能性があります"
        self.start()
        self.drop_yen_pairs()
        self.assertTrue(wait_for(lambda: len(self.find("急落")) >= 2), "原因つきの2通目が来ない")
        time.sleep(0.8)
        first, second = self.find("急落")
        self.assertEqual((first["priority"], first["sound"]), ("1", "spacealarm"))
        self.assertTrue(second["title"].startswith("原因の見立て｜"), second["title"])
        self.assertIn("が急落", second["title"])
        self.assertEqual(second["priority"], "-1")  # 2回鳴らさない
        self.assertNotIn("sound", second)
        self.assertIn("▶ 考えられる原因: 日銀総裁の発言が手がかりの可能性があります", second["message"])

    def test_limit_is_announced_once(self):
        self.start(po_day="1")
        self.state.add_feed_item("n-1", "First headline", "text one")
        self.assertTrue(wait_for(lambda: self.find("FRB理事が追加の利上げに言及")))
        self.state.add_feed_item("n-2", "Second headline", "text two")
        self.assertTrue(wait_for(lambda: self.find("きょうの通知の上限(1通)に達した")), "上限のお知らせが来ない")
        notice = self.find("きょうの通知の上限(1通)に達した")[0]
        self.assertEqual(notice["title"], "FX見張り番からのお知らせ")
        self.assertEqual((notice["priority"], notice["sound"]), ("0", "mechanical"))
        self.state.add_feed_item("n-3", "Third headline", "text three")
        self.assertTrue(wait_for(lambda: self.app.counts["seen"] >= 3))
        time.sleep(0.6)
        self.assertEqual(len(self.find("きょうの通知の上限")), 1)
        self.assertEqual(len(self.find("FRB理事が追加の利上げに言及")), 1)

    def test_start_notice_and_alive_file(self):
        cfg = write_config(self.tmp.name, self.state, service="pushover", x_token="", contact="")
        self.app = App(cfg, make_sources(self.state))
        runner = threading.Thread(target=self.app.run, daemon=True)
        runner.start()
        self.assertTrue(wait_for(lambda: self.find("FX見張り番を始めました")), "始まりの知らせが来ない")
        start = self.find("FX見張り番を始めました")[0]
        self.assertEqual(start["priority"], "-1")
        self.assertNotIn("sound", start)
        self.assertIn("・通知(Pushover): 重要度 3 以上を送ります(きょう 0通 / 上限 200通、今月 0通 / 上限 5000通", start["message"])
        self.assertNotIn(PO_TOKEN, start["message"])
        alive_path = Path(self.tmp.name) / "data" / ALIVE_FILE
        self.assertTrue(wait_for(alive_path.exists))
        alive = json.loads(alive_path.read_text(encoding="utf-8"))
        self.assertEqual((alive["state"], alive["service"], alive["version"]), ("running", "pushover", "0.2.0"))
        # 止めてほしいという合図のファイルを置くと、止まる
        (Path(self.tmp.name) / "data" / STOP_FILE).write_text("stop", encoding="utf-8")
        runner.join(timeout=20)
        self.assertFalse(runner.is_alive(), "合図を置いても止まらない")
        self.assertFalse((Path(self.tmp.name) / "data" / STOP_FILE).exists())
        self.assertEqual(json.loads(alive_path.read_text(encoding="utf-8"))["state"], "stopped")


class CommandTest(unittest.TestCase):
    """鍵や番号を入れるコマンドと、始める・見る・止めるコマンド。"""

    def setUp(self):
        import run

        self.run = run
        self.state, self.server = start_server()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        os.environ["FXNEWS_HOME"] = str(self.home)

    def tearDown(self):
        del os.environ["FXNEWS_HOME"]
        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)
            handler.close()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def config(self, **over):
        return write_config(str(self.home), self.state, **over)

    def test_setup_pushover(self):
        cfg = self.config(po_user="", po_token="")
        # 形のちがう番号は受けつけず、もう一度たずねる
        ask = answers("too-short", PO_USER, "also bad", PO_TOKEN)
        code, out = call(self.run.cmd_setup_pushover, cfg, ask)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(ask.asked), 4)
        self.assertIn("英数字30文字ではないようです", out)
        self.assertIn("ためしの通知を送れました", out)
        self.assertIn("今月の残り: 8999通(上限 10000通)", out)
        self.assertNotIn(PO_TOKEN, out)
        self.assertNotIn(PO_USER, out)
        sent = self.state.pushover[0]
        self.assertEqual((sent["title"], sent["priority"], sent["sound"]), ("FX見張り番のテスト通知", "0", "incoming"))
        saved = Config(self.home)
        self.assertEqual(saved.get("pushover", "user_key"), PO_USER)
        self.assertEqual(saved.get("pushover", "api_token"), PO_TOKEN)
        self.assertEqual(saved.get("notify", "service"), "pushover")
        # そのあとのテスト通知は、Pushover に行く
        code, out = call(self.run.main, ["test-notify"])
        self.assertEqual(code, 0, out)
        self.assertIn("テスト通知を Pushover に送りました", out)
        self.assertEqual(self.state.pushover[1]["sound"], "bugle")
        self.assertEqual(self.state.ntfy, [])
        # 受け取り番号がもう入っていれば、何も打たずに Enter でそのまま使える
        ask = answers("", PO_TOKEN)
        code, out = call(self.run.cmd_setup_pushover, Config(self.home), ask)
        self.assertEqual(code, 0, out)
        self.assertIn("もう入っています", out)

    def test_setup_pushover_keeps_config_when_it_fails(self):
        cfg = self.config(po_user="", po_token="")
        before = (self.home / "config.ini").read_text(encoding="utf-8")
        self.state.pushover_status = 400
        code, out = call(self.run.cmd_setup_pushover, cfg, answers(PO_USER, PO_TOKEN))
        self.assertEqual(code, 1)
        self.assertIn("config.ini は書きかえていません", out)
        self.assertEqual((self.home / "config.ini").read_text(encoding="utf-8"), before)
        code, out = call(self.run.cmd_setup_pushover, cfg, answers())
        self.assertEqual(code, 1)
        self.assertIn("やめました", out)
        self.assertEqual((self.home / "config.ini").read_text(encoding="utf-8"), before)

    def test_setup_claude(self):
        cfg = self.config(claude_key="")
        code, out = call(self.run.cmd_setup_claude, cfg, answers("short", "sk-test-0123456789abcdefghij"))
        self.assertEqual(code, 0, out)
        self.assertIn("つながりました", out)
        self.assertNotIn("sk-test-0123456789abcdefghij", out)
        self.assertEqual(Config(self.home).get("claude", "api_key"), "sk-test-0123456789abcdefghij")
        self.assertEqual(self.state.claude_reqs[-1]["headers"]["authorization"], "Bearer sk-test-0123456789abcdefghij")
        # ワークスペース(Console の中の作業場)を決めていない鍵は、ID をたずねてからつなぐ
        self.state.claude_mode = "need_workspace"
        cfg = self.config(claude_key="")
        code, out = call(self.run.cmd_setup_claude, cfg, answers("sk-test-another-key-0123456789", "wrkspc_01TEST"))
        self.assertEqual(code, 0, out)
        saved = Config(self.home)
        self.assertEqual(saved.get("claude", "workspace_id"), "wrkspc_01TEST")
        self.assertEqual(self.state.claude_reqs[-1]["headers"]["anthropic-workspace-id"], "wrkspc_01TEST")
        # つながらなければ、書きかえない
        self.state.claude_mode = "error500"
        cfg = self.config(claude_key="")
        code, out = call(self.run.cmd_setup_claude, cfg, answers("sk-test-bad-key-0123456789abc"))
        self.assertEqual(code, 1)
        self.assertEqual(Config(self.home).get("claude", "api_key"), "")

    def test_commands_leave_a_record_without_secrets(self):
        self.config(service="pushover", x_token="", contact="")
        (self.home / "sources.json").write_text(json.dumps(make_sources(self.state), ensure_ascii=False), encoding="utf-8")
        code, out = call(self.run.main, ["check"])
        self.assertEqual(code, 0, out)
        saved = (self.home / "data" / "check_last.txt").read_text(encoding="utf-8")
        self.assertIn("■ 通知(Pushover)", saved)
        self.assertIn("点検おわり: つながらなかったもの 0件", saved)
        self.assertEqual(saved.strip(), out.strip())
        call(self.run.main, ["version"])
        call(self.run.main, ["test-notify"])
        record = (self.home / "data" / "commands.log").read_text(encoding="utf-8")
        self.assertRegex(record, r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d check: 終わりの番号 0")
        self.assertIn("version: 終わりの番号 0", record)
        self.assertIn("test-notify: 終わりの番号 0", record)
        for secret in (PO_TOKEN, PO_USER, "sk-test-key"):
            self.assertNotIn(secret, saved)
            self.assertNotIn(secret, record)

    def test_setup_sec(self):
        cfg = self.config(contact="")
        code, out = call(self.run.cmd_setup_sec, cfg, answers("山田", "Taro Yamada", "not-a-mail", "taro@example.com"))
        self.assertEqual(code, 0, out)
        self.assertEqual(Config(self.home).get("sec", "contact"), "Taro Yamada taro@example.com")

    def test_start_status_stop(self):
        self.config(service="pushover", x_token="", contact="")
        (self.home / "sources.json").write_text(json.dumps(make_sources(self.state), ensure_ascii=False), encoding="utf-8")
        code, out = call(self.run.main, ["status"])
        self.assertEqual(code, 1)
        self.assertIn("・止まっています", out)
        code, out = call(self.run.main, ["stop"])
        self.assertEqual(code, 0)
        self.assertIn("いま動いていません", out)

        result = {}
        runner = threading.Thread(target=lambda: result.setdefault("code", self.run.main(["run"])), daemon=True)
        runner.start()
        self.assertTrue(wait_for(lambda: any("FX見張り番を始めました" in m.get("title", "") for m in list(self.state.pushover))))
        self.assertTrue(wait_for((self.home / "data" / ALIVE_FILE).exists))
        code, out = call(self.run.main, ["status"])
        self.assertEqual(code, 0, out)
        self.assertIn("・動いています", out)
        self.assertIn("・最後のしるし:", out)
        self.assertIn("あつかった情報", out)
        # 同じフォルダで2つめを始めようとしても、始まらない
        code, out = call(self.run.main, ["run"])
        self.assertEqual(code, 3)
        self.assertEqual(len([m for m in self.state.pushover if m["title"] == "FX見張り番を始めました"]), 1)
        # お試しモード(スマホに送らない)は、本番が動いていても試せる
        code, out = call(self.run.main, ["run", "--dry", "--seconds", "0.5"])
        self.assertEqual(code, 0, out)
        code, out = call(self.run.main, ["stop"])
        self.assertEqual(code, 0, out)
        self.assertIn("止まりました", out)
        runner.join(timeout=10)
        self.assertFalse(runner.is_alive())
        self.assertEqual(result.get("code"), 0)
        code, out = call(self.run.main, ["status"])
        self.assertEqual(code, 1)
        self.assertIn("・止まっています", out)
        record = (self.home / "data" / "commands.log").read_text(encoding="utf-8")
        self.assertIn("run: 始めます", record)
        self.assertIn("run: 終わりの番号 3", record)
        self.assertIn("run: 終わりの番号 0", record)
        self.assertIn("stop: 終わりの番号 0", record)


if __name__ == "__main__":
    unittest.main()
