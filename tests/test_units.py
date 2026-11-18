"""部品ごとのテスト(外のネットにはつながない)。"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxnews import analyzer, config, edgar, feeds, notifier, rates, report, util, xstream  # noqa: E402
from fxnews.models import Item  # noqa: E402
from fxnews.store import Store  # noqa: E402


class UtilTest(unittest.TestCase):
    def test_parse_date_rfc822(self):
        self.assertEqual(util.parse_date("Fri, 13 Nov 2026 21:15:00 GMT"), 1794604500.0)
        self.assertEqual(util.parse_date("Fri, 13 Nov 2026 09:00:00 Z"), 1794560400.0)
        self.assertEqual(util.parse_date("Mon, 16 Nov 2026 14:00:00 +0100"), 1794834000.0)

    def test_parse_date_iso(self):
        self.assertEqual(util.parse_date("2026-11-18T17:57:36Z"), 1795024656.0)
        self.assertAlmostEqual(util.parse_date("2026-11-18T17:57:36.59645Z"), 1795024656.59645, places=4)
        self.assertEqual(util.parse_date("2026-11-17T16:20:05-05:00"), 1794950405.0)
        self.assertEqual(util.parse_date("2026-11-19T02:57:36+0900"), 1795024656.0)

    def test_parse_date_unknown(self):
        self.assertIsNone(util.parse_date(""))
        self.assertIsNone(util.parse_date("2026-11-18"))
        self.assertIsNone(util.parse_date("きのう"))

    def test_strip_html(self):
        self.assertEqual(util.strip_html("<p>A &amp; B<br>C</p>"), "A & B C")
        self.assertEqual(util.strip_html("&lt;b&gt;Filed:&lt;/b&gt; 2026"), "Filed: 2026")
        self.assertEqual(util.strip_html("<style>p{}</style>text"), "text")
        self.assertEqual(util.strip_html("abcdef", 3), "abc…")

    def test_cut_bytes(self):
        s = "あ" * 100
        cut = util.cut_bytes(s, 31)
        self.assertLessEqual(len(cut.encode("utf-8")), 31)
        self.assertTrue(cut.endswith("…"))
        self.assertEqual(util.cut_bytes("abc", 10), "abc")

    def test_jst(self):
        self.assertEqual(util.jst(1795024656, "%Y-%m-%d %H:%M:%S"), "2026-11-19 02:57:36")

    def test_normalize(self):
        a = util.normalize_for_dedupe("Fed's Waller: Hike 50bp!", "")
        b = util.normalize_for_dedupe("FED'S WALLER - hike 50bp", "")
        self.assertEqual(a, b)


RSS2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel><title>FRB: Press Release - All Releases</title><link>https://www.federalreserve.gov/</link>
<description>Press Release</description>
<image><title>logo</title><link>https://example.invalid/</link></image>
<item><title><![CDATA[Federal Reserve issues FOMC statement]]></title>
<link><![CDATA[https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm]]></link>
<guid isPermaLink="true">https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm</guid>
<description><![CDATA[<p>Federal Reserve issues <b>FOMC</b> statement</p>]]></description>
<category>Monetary Policy</category><pubDate>Wed, 28 Oct 2026 18:00:00 GMT</pubDate></item>
<item><title>Second &amp; last</title><link>https://example.invalid/2</link><pubDate>Thu, 12 Nov 2026 17:45:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title><updated>2026-11-18T13:55:13-05:00</updated>
<entry>
<title>8-K - NVIDIA CORP (0001045810) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1045810/000104581026000123/0001045810-26-000123-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-11-17 &lt;b&gt;AccNo:&lt;/b&gt; 0001045810-26-000123 &lt;b&gt;Size:&lt;/b&gt; 1 MB
&lt;br&gt;Item 2.02: Results of Operations and Financial Condition
&lt;br&gt;Item 9.01: Financial Statements and Exhibits</summary>
<updated>2026-11-17T16:20:05-05:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
<id>urn:tag:sec.gov,2008:accession-number=0001045810-26-000123</id>
</entry>
<entry>
<title>8-K - Some Other Co (0000999999) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/999999/000099999926000001/0000999999-26-000001-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-11-17 &lt;b&gt;AccNo:&lt;/b&gt; 0000999999-26-000001 &lt;b&gt;Size:&lt;/b&gt; 10 KB</summary>
<updated>2026-11-17T16:21:00-05:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0000999999-26-000001</id>
</entry>
</feed>"""

RDF = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="https://www.boj.or.jp/rss/whatsnew.xml"><title>日本銀行</title><link>https://www.boj.or.jp/</link>
<items><rdf:Seq><rdf:li rdf:resource="https://www.boj.or.jp/mopo/a.htm"/></rdf:Seq></items></channel>
<item rdf:about="https://www.boj.or.jp/mopo/a.htm"><title>金融市場調節方針の変更について</title>
<link>https://www.boj.or.jp/mopo/a.htm</link><dc:date>2026-09-18T12:00:00+09:00</dc:date></item>
</rdf:RDF>"""


class FeedParseTest(unittest.TestCase):
    def test_rss2(self):
        items = feeds.parse_feed(RSS2.encode("utf-8"))
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first["title"], "Federal Reserve issues FOMC statement")
        self.assertTrue(first["link"].endswith("monetary20261028a.htm"))
        self.assertEqual(first["id"], first["link"])
        self.assertEqual(first["summary"], "Federal Reserve issues FOMC statement")
        self.assertEqual(first["published"], 1793210400.0)
        self.assertEqual(items[1]["title"], "Second & last")
        self.assertEqual(items[1]["id"], "https://example.invalid/2")

    def test_atom_latin1(self):
        items = feeds.parse_feed(ATOM.encode("iso-8859-1"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "8-K - NVIDIA CORP (0001045810) (Filer)")
        self.assertIn("-index.htm", items[0]["link"])
        self.assertIn("Item 2.02: Results of Operations", items[0]["summary"])
        self.assertIn("AccNo: 0001045810-26-000123", items[0]["summary"])
        self.assertEqual(items[0]["published"], 1794950405.0)

    def test_rdf_japanese(self):
        items = feeds.parse_feed(RDF.encode("utf-8"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "金融市場調節方針の変更について")
        self.assertEqual(items[0]["id"], "https://www.boj.or.jp/mopo/a.htm")
        self.assertEqual(items[0]["published"], 1789700400.0)

    def test_html_entity_and_bom(self):
        data = b"\xef\xbb\xbf\n" + "<rss><channel><item><title>A&nbsp;B &mdash; C</title><link>http://x.invalid/1</link></item></channel></rss>".encode("utf-8")
        items = feeds.parse_feed(data)
        self.assertEqual(items[0]["title"], "A B — C")

    def test_shift_jis(self):
        text = '<?xml version="1.0" encoding="Shift_JIS"?><rss><channel><item><title>日銀が利上げ</title><link>http://x.invalid/1</link></item></channel></rss>'
        items = feeds.parse_feed(text.encode("shift_jis"))
        self.assertEqual(items[0]["title"], "日銀が利上げ")

    def test_entity_declaration_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss><channel><item><title>&lol;</title></item></channel></rss>'
        with self.assertRaises(feeds.FeedError):
            feeds.parse_feed(bomb)

    def test_not_xml(self):
        with self.assertRaises(feeds.FeedError):
            feeds.parse_feed(b"<html><body>Access denied")


class EdgarTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.tmp = tempfile.TemporaryDirectory()
        self.poller = edgar.EdgarPoller({"tickers": ["NVDA"]}, "Taro taro@example.com", "https://www.sec.gov", self.store, None, None, self.tmp.name)
        self.poller.ciks = {1045810: ("NVDA", "NVIDIA CORP")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_user_agent(self):
        self.assertEqual(self.poller.headers()["User-Agent"], "FX-NEWS Taro taro@example.com")

    def test_parse_tickers(self):
        table = edgar.parse_tickers({"0": {"cik_str": 1045810, "ticker": "nvda", "title": "NVIDIA CORP"}, "1": {"bad": 1}})
        self.assertEqual(table, {"NVDA": (1045810, "NVIDIA CORP")})

    def test_to_item(self):
        entries = feeds.parse_feed(ATOM.encode("iso-8859-1"))
        item = self.poller.to_item(entries[0], "8-K")
        self.assertEqual(item.key, "edgar|0001045810-26-000123")
        self.assertEqual(item.extra["items"], ["2.02", "9.01"])
        self.assertIn("NVIDIA CORP(NVDA)が届け出 8-K を提出", item.title)
        self.assertIn("決算の発表(2.02)", item.title)
        self.assertIsNone(self.poller.to_item(entries[1], "8-K"))

    def test_find_press_release(self):
        from tests.fake_server import INDEX_HTML

        url = edgar.find_press_release_url(INDEX_HTML, "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000123/0001045810-26-000123-index.htm")
        self.assertEqual(url, "https://www.sec.gov/sec/Archives/edgar/data/1045810/000104581026000123/q3fy27pr.htm")
        self.assertEqual(edgar.find_press_release_url("<table><tr><td>8-K</td></tr></table>", "https://x.invalid/"), "")


def mids(**over):
    base = {"USD_JPY": 159.28, "EUR_JPY": 174.82, "GBP_JPY": 209.45, "EUR_USD": 1.09756, "GBP_USD": 1.31499, "EUR_GBP": 0.83467}
    base.update(over)
    return base


class RatesTest(unittest.TestCase):
    def test_parse_ticker(self):
        payload = {"status": 0, "data": [
            {"symbol": "USD_JPY", "ask": "159.284", "bid": "159.279", "status": "OPEN"},
            {"symbol": "EUR_USD", "ask": 1.09759, "bid": 1.09754, "status": "CLOSE"},
            {"symbol": "AUD_JPY", "ask": "107.0", "bid": "106.9", "status": "OPEN"},
            {"symbol": "GBP_JPY", "ask": "x", "bid": "1"},
        ]}
        m, open_pairs = rates.parse_ticker(payload)
        self.assertAlmostEqual(m["USD_JPY"], 159.2815)
        self.assertAlmostEqual(m["EUR_USD"], 1.097565)
        self.assertNotIn("AUD_JPY", m)
        self.assertNotIn("GBP_JPY", m)
        self.assertEqual(open_pairs, {"USD_JPY"})
        with self.assertRaises(ValueError):
            rates.parse_ticker({"status": 5, "messages": [{"message_code": "ERR-5201"}]})

    def test_strength_yen_bought(self):
        base = mids()
        now = mids(USD_JPY=159.28 * 0.997, EUR_JPY=174.82 * 0.997, GBP_JPY=209.45 * 0.997)
        s = rates.currency_strength(base, now)
        self.assertAlmostEqual(s["JPY"], 0.3, places=2)
        self.assertAlmostEqual(s["USD"], -0.1, places=2)
        tags = rates.strength_tags(s, 0.06)
        self.assertEqual(tags, {"JPY": "up", "USD": "flat", "EUR": "flat", "GBP": "flat"})

    def test_strength_dollar_bought(self):
        base = mids()
        now = mids(USD_JPY=159.28 * 1.003, EUR_USD=1.09756 / 1.003, GBP_USD=1.31499 / 1.003)
        s = rates.currency_strength(base, now)
        tags = rates.strength_tags(s, 0.06)
        self.assertEqual(tags, {"JPY": "flat", "USD": "up", "EUR": "flat", "GBP": "flat"})

    def test_strength_small_moves_are_flat(self):
        s = {"JPY": 0.01, "USD": -0.01, "EUR": 0.0, "GBP": 0.0}
        self.assertEqual(set(rates.strength_tags(s, 0.06).values()), {"flat"})

    def test_format(self):
        self.assertEqual(rates.fmt_price("USD_JPY", 159.2815), "159.28")
        self.assertEqual(rates.fmt_price("EUR_USD", 1.097565), "1.0976")
        self.assertEqual(rates.fmt_abs_change("USD_JPY", -0.42), "0.42円")
        self.assertEqual(rates.fmt_abs_change("EUR_USD", 0.0016), "0.0016ドル")
        self.assertEqual(rates.fmt_abs_change("EUR_GBP", 0.0013), "0.0013ポンド")

    def _detector(self, cooldown=300):
        book = rates.RateBook()
        det = rates.SpikeDetector(book, {60: 0.08, 300: 0.15, 900: 0.30}, cooldown_sec=cooldown)
        return book, det

    def test_spike_fires_on_second_sample(self):
        book, det = self._detector()
        t = 1000000.0
        for i in range(10):
            book.add(t + i * 2, mids())
            self.assertIsNone(det.check(t + i * 2, set(rates.PAIRS)))
        drop = mids(USD_JPY=159.28 * 0.9985, EUR_JPY=174.82 * 0.9985, GBP_JPY=209.45 * 0.9985)
        book.add(t + 20, drop)
        self.assertIsNone(det.check(t + 20, set(rates.PAIRS)))  # 1回目はまだ知らせない
        book.add(t + 22, drop)
        sp = det.check(t + 22, set(rates.PAIRS))
        self.assertIsNotNone(sp)
        self.assertEqual(sp["dir"], -1)
        self.assertEqual(sp["label"], "1分")
        self.assertIn(sp["pair"], ("USD_JPY", "EUR_JPY", "GBP_JPY"))
        self.assertAlmostEqual(sp["pct"], -0.15, places=2)
        self.assertEqual(sp["tags"]["JPY"], "up")
        self.assertEqual(sp["tags"]["USD"], "flat")
        title = rates.spike_title(sp)
        self.assertIn("急落", title)
        self.assertIn("1分で-", title)

    def test_single_bad_tick_is_ignored(self):
        book, det = self._detector()
        t = 2000000.0
        for i in range(5):
            book.add(t + i * 2, mids())
            det.check(t + i * 2, set(rates.PAIRS))
        book.add(t + 10, mids(USD_JPY=150.0))
        self.assertIsNone(det.check(t + 10, set(rates.PAIRS)))
        book.add(t + 12, mids())
        self.assertIsNone(det.check(t + 12, set(rates.PAIRS)))
        book.add(t + 14, mids())
        self.assertIsNone(det.check(t + 14, set(rates.PAIRS)))

    def test_no_repeat_for_same_move_and_cooldown(self):
        book, det = self._detector(cooldown=300)
        t = 3000000.0
        book.add(t, mids())
        det.check(t, set(rates.PAIRS))
        up = mids(USD_JPY=159.28 * 1.002)
        fired = []
        for i in range(1, 400):
            now = t + i * 2
            book.add(now, up)
            sp = det.check(now, {"USD_JPY"})
            if sp:
                fired.append((i, sp))
        self.assertEqual(len(fired), 1)  # 同じ動きは1回だけ
        self.assertEqual(fired[0][1]["dir"], 1)
        # 休んでいるあいだでも、さらに大きく(0.3%以上)動けば知らせる
        more = mids(USD_JPY=159.28 * 1.002 * 1.0035)
        now = t + 802
        book.add(now, more)
        det.check(now, {"USD_JPY"})
        book.add(now + 2, more)
        self.assertIsNotNone(det.check(now + 2, {"USD_JPY"}))

    def test_cooldown_blocks_medium_move(self):
        book, det = self._detector(cooldown=300)
        t = 4000000.0
        book.add(t, mids())
        det.check(t, {"USD_JPY"})
        step1 = mids(USD_JPY=159.28 * 1.001)
        book.add(t + 2, step1)
        det.check(t + 2, {"USD_JPY"})
        book.add(t + 4, step1)
        self.assertIsNotNone(det.check(t + 4, {"USD_JPY"}))
        step2 = mids(USD_JPY=159.28 * 1.001 * 1.001)  # 休み中に、さらに 0.1% だけ
        book.add(t + 6, step2)
        det.check(t + 6, {"USD_JPY"})
        book.add(t + 8, step2)
        self.assertIsNone(det.check(t + 8, {"USD_JPY"}))

    def test_closed_market(self):
        book, det = self._detector()
        t = 5000000.0
        book.add(t, mids())
        drop = mids(USD_JPY=150.0)
        book.add(t + 2, drop)
        book.add(t + 4, drop)
        self.assertIsNone(det.check(t + 2, set()))
        self.assertIsNone(det.check(t + 4, set()))


class AnalyzerTest(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(analyzer.extract_json('前置き {"a": 1} 後ろ'), {"a": 1})
        self.assertEqual(analyzer.extract_json('```json\n{"a": {"b": 2}}\n```'), {"a": {"b": 2}})
        self.assertIsNone(analyzer.extract_json("JSON ではない"))
        self.assertIsNone(analyzer.extract_json("{こわれた"))
        self.assertIsNone(analyzer.extract_json(""))

    def test_normalize(self):
        res = analyzer.normalize_news({"title": " 見出し ", "importance": "9", "jpy": "UP", "usd": "sideways", "category": "へんな分類", "dup": 0, "confidence": "HIGH"})
        self.assertEqual(res["title"], "見出し")
        self.assertEqual(res["importance"], 5)
        self.assertEqual(res["jpy"], "up")
        self.assertEqual(res["usd"], "flat")
        self.assertEqual(res["eur"], "flat")
        self.assertEqual(res["category"], "その他")
        self.assertEqual(res["confidence"], "high")
        self.assertFalse(res["dup"])
        self.assertIsNone(analyzer.normalize_news({"title": ""}))
        self.assertIsNone(analyzer.normalize_news(["x"]))
        self.assertEqual(analyzer.normalize_news({"title": "t", "importance": None})["importance"], 2)

    def test_prompt(self):
        item = Item("fed_press", "FRB の発表", "official", "k", "Fed raises rates", text="Body text", published=1795024656.0)
        p = analyzer.build_news_prompt(item, ["前の見出し"], "メモ本文", now=1795024700.0)
        self.assertIn("いまの時刻: 2026-11-19 02:58(日本時間)", p)
        self.assertIn("メモ本文", p)
        self.assertIn("情報源: FRB の発表(種類: 公式発表)", p)
        self.assertIn("発表の時刻: 2026-11-19 02:57(日本時間)", p)
        self.assertIn("見出し: Fed raises rates", p)
        self.assertIn("- 前の見出し", p)
        p2 = analyzer.build_news_prompt(Item("s", "n", "news", "k", "T"), [], "")
        self.assertIn("本文: (なし)", p2)
        self.assertTrue(p2.rstrip().endswith("(なし)"))

    def test_schema_matches_normalizer(self):
        keys = set(analyzer.NEWS_SCHEMA["properties"])
        self.assertEqual(keys, set(analyzer.NEWS_SCHEMA["required"]))
        sample = {k: ("flat" if k in ("jpy", "usd", "eur", "gbp") else "x") for k in keys}
        sample.update({"importance": 3, "dup": False, "title": "t"})
        self.assertEqual(set(analyzer.normalize_news(sample)), keys)


class NotifierTest(unittest.TestCase):
    def test_tag_line(self):
        tags = {"JPY": "up", "USD": "down", "EUR": "flat", "GBP": "flat"}
        self.assertEqual(notifier.tag_line(tags), "円↑ ドル↓ ユーロ→ ポンド→")
        self.assertEqual(notifier.tag_line(tags, only_moves=True), "円↑ ドル↓")
        self.assertEqual(notifier.tag_line({}, only_moves=True), "")

    def test_news_notification(self):
        item = Item("fed_speech", "FRB の講演", "official", "k", "Waller calls for more aggressive action", url="https://x.invalid/a", published=1795024656.0)
        res = {"dup": False, "category": "要人発言", "title": "FRB理事が強い引き締めを主張", "summary": "まとめ", "reason": "りゆう", "jpy": "down", "usd": "up", "eur": "flat", "gbp": "flat", "confidence": "mid", "importance": 4}
        title, body, prio = notifier.build_news_notification(item, res)
        self.assertEqual(title, "円↓ ドル↑｜FRB理事が強い引き締めを主張")
        self.assertEqual(prio, 4)
        lines = body.split("\n")
        self.assertEqual(lines[0], "まとめ")
        self.assertEqual(lines[1], "▶ りゆう")
        self.assertEqual(lines[2], "円↓ ドル↑ ユーロ→ ポンド→(見立て・確からしさ: 中)")
        self.assertEqual(lines[3], "要人発言・重要度 4/5・FRB の講演・02:57")
        self.assertEqual(lines[4], "原文: Waller calls for more aggressive action")

    def test_flat_news_has_plain_title(self):
        item = Item("s", "n", "news", "k", "T")
        res = {"dup": False, "category": "その他", "title": "見出し", "summary": "", "reason": "", "jpy": "flat", "usd": "flat", "eur": "flat", "gbp": "flat", "confidence": "low", "importance": 3}
        title, _body, prio = notifier.build_news_notification(item, res)
        self.assertEqual(title, "見出し")
        self.assertEqual(prio, 3)

    def test_spike_notification(self):
        base = mids()
        now = mids(USD_JPY=158.86, EUR_JPY=174.40, GBP_JPY=208.95)
        strength = rates.currency_strength(base, now)
        sp = {
            "ts": 1795024656.0, "pair": "USD_JPY", "dir": -1, "sec": 300, "label": "5分", "pct": -0.2637, "ratio": 1.76,
            "from": 159.28, "to": 158.86, "elapsed": 212.0,
            "changes": {p: rates.pct_change(base[p], now[p]) for p in rates.PAIRS},
            "strength": strength, "tags": rates.strength_tags(strength, 0.06), "mids": now,
        }
        recent = [{"ts": 1795024400.0, "title_ja": "日銀総裁が利上げに言及", "title": "x"}]
        title, body, prio = notifier.build_spike_notification(sp, "日銀総裁の発言が手がかりの可能性", recent)
        self.assertEqual(title, "円↑｜ドル円が急落 5分で-0.42円(159.28→158.86)")
        self.assertEqual(prio, 4)
        self.assertIn("ドル円 159.28 → 158.86(-0.26%、およそ212秒で)", body)
        self.assertIn("ほかの組合せ: ユーロ円 -0.24%、ポンド円 -0.24%", body)
        self.assertIn("動いた通貨: 円が買われている(+0.25%)", body)
        self.assertIn("▶ 考えられる原因: 日銀総裁の発言が手がかりの可能性", body)
        self.assertIn("・[02:53] 日銀総裁が利上げに言及", body)


class XTest(unittest.TestCase):
    def test_plan_rule_changes(self):
        current = [
            {"id": "1", "value": "from:a -is:retweet", "tag": "A"},
            {"id": "2", "value": "from:old", "tag": "Old"},
            {"id": "3", "value": "from:b", "tag": "名前がちがう"},
            {"id": "4", "value": "from:a -is:retweet", "tag": "A"},
        ]
        desired = [{"tag": "A", "value": "from:a -is:retweet"}, {"tag": "B", "value": "from:b"}, {"tag": "C", "value": "from:c"}, {"tag": "空", "value": " "}]
        delete_ids, add = xstream.plan_rule_changes(current, desired)
        self.assertEqual(sorted(delete_ids), ["2", "3", "4"])
        self.assertEqual(add, [{"value": "from:b", "tag": "B"}, {"value": "from:c", "tag": "C"}])
        self.assertEqual(xstream.plan_rule_changes(None, []), ([], []))

    def test_parse_stream_line(self):
        self.assertEqual(xstream.parse_stream_line(b"\r\n"), (None, None))
        self.assertEqual(xstream.parse_stream_line(b"not json"), (None, None))
        line = json.dumps({
            "data": {"id": "123", "text": "short…", "note_tweet": {"text": "long full text"}, "author_id": "9", "created_at": "2026-11-18T17:57:36.000Z"},
            "matching_rules": [{"id": "1", "tag": "FRB公式"}],
        }).encode("utf-8")
        kind, post = xstream.parse_stream_line(line)
        self.assertEqual(kind, "post")
        self.assertEqual(post["id"], "123")
        self.assertEqual(post["text"], "long full text")
        self.assertEqual(post["tags"], ["FRB公式"])
        self.assertEqual(post["created_at"], 1795024656.0)
        kind, msg = xstream.parse_stream_line(b'{"errors": [{"title": "operational-disconnect"}]}')
        self.assertEqual(kind, "error")
        self.assertIn("operational-disconnect", msg)

    def test_month_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config(tmp)
            store = Store(":memory:")
            xs = xstream.XStream([{"tag": "A", "value": "from:a"}], cfg, store, None, None)
            self.assertEqual(xs.month_cap_posts(), 6250)  # 5000円 ÷ 160円 ÷ 0.005ドル
            self.assertFalse(xs.enabled())  # 鍵がないので動かない
            store.close()


class ConfigTest(unittest.TestCase):
    def test_defaults_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config(tmp)
            self.assertFalse(cfg.loaded)
            self.assertEqual(cfg.get("claude", "model"), "claude-haiku-5-5")
            self.assertEqual(cfg.getint("ntfy", "min_importance"), 3)
            self.assertTrue(cfg.getbool("general", "sleep_block"))
            (Path(tmp) / "config.ini").write_bytes("﻿# 説明\n[claude]\napi_key = sk-%test#1\n[ntfy]\nmin_importance = 4\n".encode("utf-8"))
            cfg = config.Config(tmp)
            self.assertTrue(cfg.loaded)
            self.assertEqual(cfg.get("claude", "api_key"), "sk-%test#1")
            self.assertEqual(cfg.getint("ntfy", "min_importance"), 4)
            self.assertEqual(cfg.getfloat("rates", "threshold_5m"), 0.15)

    def test_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FXNEWS_NTFY_TOPIC"] = "from-env"
            try:
                self.assertEqual(config.Config(tmp).get("ntfy", "topic"), "from-env")
            finally:
                del os.environ["FXNEWS_NTFY_TOPIC"]

    def test_example_file_is_valid_and_matches_defaults(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.example.ini").write_bytes((root / "config.example.ini").read_bytes())
            self.assertTrue(config.ensure_config_file(tmp))
            self.assertFalse(config.ensure_config_file(tmp))
            cfg = config.Config(tmp)
            self.assertTrue(cfg.loaded)
            for section, values in config.DEFAULTS.items():
                for key, default in values.items():
                    self.assertEqual(cfg.get(section, key), default, "%s.%s" % (section, key))
            self.assertEqual(cfg.get("claude", "api_key"), "")

    def test_write_value_keeps_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            path.write_text("# 上の説明\n[ntfy]\n# あて先\ntopic =\nmin_importance = 3\n\n[x]\nbearer_token =\n", encoding="utf-8")
            config.write_value(path, "ntfy", "topic", "fx-abcd")
            text = path.read_text(encoding="utf-8")
            self.assertIn("# 上の説明", text)
            self.assertIn("# あて先\ntopic = fx-abcd\n", text)
            self.assertIn("bearer_token =", text)
            config.write_value(path, "x", "daily_cap_posts", "100")
            config.write_value(path, "new", "k", "v")
            cfg = config.Config(tmp)
            self.assertEqual(cfg.get("ntfy", "topic"), "fx-abcd")
            self.assertEqual(cfg.get("x", "daily_cap_posts"), "100")
            self.assertEqual(cfg._cp.get("new", "k"), "v")

    def test_new_topic(self):
        a, b = config.new_topic(), config.new_topic()
        self.assertRegex(a, r"^fx-[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}$")
        self.assertNotEqual(a, b)

    def test_sources_file(self):
        root = Path(__file__).resolve().parent.parent
        sources = config.load_sources(root)
        ids = [f["id"] for f in sources["feeds"]]
        self.assertEqual(len(ids), len(set(ids)))
        for f in sources["feeds"]:
            self.assertTrue(f["url"].startswith("https://"))
            self.assertIn(f["kind"], ("official", "news", "vip"))
        self.assertIn("NVDA", sources["edgar"]["tickers"])
        self.assertEqual(sources["x"]["rules"], [])


class StoreTest(unittest.TestCase):
    def test_seen_and_usage(self):
        store = Store(":memory:")
        self.assertTrue(store.mark_seen("a"))
        self.assertFalse(store.mark_seen("a"))
        self.assertTrue(store.is_seen("a"))
        self.assertFalse(store.is_initialized("feed"))
        store.set_initialized("feed")
        store.set_initialized("feed")
        self.assertTrue(store.is_initialized("feed"))
        store.usage_add("2026-11", "x_posts", 2)
        store.usage_add("2026-11", "x_posts", 3)
        self.assertEqual(store.usage_get("2026-11", "x_posts"), 5.0)
        self.assertEqual(store.usage_get("2026-12", "x_posts"), 0.0)
        store.close()

    def test_items_and_outcomes(self):
        store = Store(":memory:")
        item = Item("s", "名前", "news", "k1", "Title")
        row = store.add_item(item, analysis={"jpy": "up", "category": "要人発言"}, importance=4, title_ja="見出し", mids={"USD_JPY": 159.0})
        store.set_notified(row, True, 1.2)
        self.assertEqual(store.pending_outcomes(time.time()), [])
        later = time.time() + 1000
        pending = store.pending_outcomes(later)
        self.assertEqual([(p[0], p[1]) for p in pending], [(row, "out5"), (row, "out15")])
        store.set_outcome(row, "out5", {"JPY": 0.1})
        pending = store.pending_outcomes(later)
        self.assertEqual([(p[0], p[1]) for p in pending], [(row, "out15")])
        self.assertEqual(store.pending_outcomes(time.time() + 5 * 3600), [])  # 4時間たったらあきらめる
        with self.assertRaises(ValueError):
            store.set_outcome(row, "bad", {})
        recent = store.recent_items(time.time() - 60, notified_only=True)
        self.assertEqual(recent[0]["title_ja"], "見出し")
        self.assertEqual(store.count_items(time.time() - 60, notified_only=True), 1)
        self.assertEqual(store.recent_items(time.time() - 60, exclude_kind="news"), [])
        store.prune()
        store.close()


class ReportTest(unittest.TestCase):
    def test_judge(self):
        self.assertEqual(report.judge("up", 0.1), "hit")
        self.assertEqual(report.judge("up", -0.1), "miss")
        self.assertEqual(report.judge("down", -0.1), "hit")
        self.assertEqual(report.judge("down", 0.01), "flat")

    def test_tally_and_print(self):
        rows = [
            {"analysis": json.dumps({"category": "要人発言", "jpy": "down", "usd": "up", "eur": "flat", "gbp": "flat"}),
             "out5": json.dumps({"JPY": -0.2, "USD": 0.1, "EUR": 0.0, "GBP": 0.0}), "out15": json.dumps({"JPY": 0.2, "USD": 0.0}), "out60": None},
            {"analysis": json.dumps({"category": "急騰急落", "jpy": "up"}), "out5": json.dumps({"JPY": 0.5}), "out15": None, "out60": None},
            {"analysis": json.dumps({"category": "公式発表", "raw": True, "jpy": "up"}), "out5": json.dumps({"JPY": 0.5}), "out15": None, "out60": None},
            {"analysis": "こわれた", "out5": None, "out15": None, "out60": None},
        ]
        result = report.tally(rows)
        self.assertEqual(result["out5"]["合計"], {"hit": 2, "miss": 0, "flat": 0})
        self.assertEqual(result["out15"]["要人発言"], {"hit": 0, "miss": 1, "flat": 1})
        self.assertNotIn("out60", result)

        class FakeStore:
            def outcome_rows(self, since):
                return rows

        lines = []
        report.run_report(FakeStore(), days=7, out=lines.append)
        text = "\n".join(lines)
        self.assertIn("■ 5分後", text)
        self.assertIn("当たりの割合 100%", text)
        lines = []

        class Empty:
            def outcome_rows(self, since):
                return []

        report.run_report(Empty(), out=lines.append)
        self.assertIn("まだ記録がありません", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
