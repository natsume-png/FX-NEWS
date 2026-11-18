"""テスト用の「にせのサーバー」。

本物の相手(ニュースサイト、SEC、GMOコイン、Claude、ntfy、X)のふりをして、
決めた返事を返す。外のネットにはつながない。
"""

import json
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

NEWS_ANSWER = {
    "dup": False,
    "category": "要人発言",
    "title": "FRB理事が追加の利上げに言及",
    "summary": "FRBの理事が、物価の上昇が続けば追加の利上げが必要だと述べた。",
    "reason": "米国の金利が上がるという見方が強まると、金利の高いドルが買われやすい。",
    "jpy": "down",
    "usd": "up",
    "eur": "flat",
    "gbp": "flat",
    "confidence": "mid",
    "importance": 4,
}


def iso_now(offset=0.0):
    t = time.gmtime(time.time() + offset)
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + "+00:00"


class FakeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.port = 0
        # 更新一覧
        self.feed_items = [
            ("old-1", "Old headline one", "Older text", time.time() - 7200),
            ("old-2", "Old headline two", "Older text two", time.time() - 3600),
        ]
        self.feed_etag = None  # 文字を入れると ETag つきで返す
        self.feed_hits = 0
        self.feed_status = 200  # 200 以外にすると、その番号のエラーを返す
        self.feed_304 = 0
        # SEC
        self.edgar_entries = []
        self.sec_agents = []
        # 為替レート
        self.prices = {
            "USD_JPY": 159.280,
            "EUR_JPY": 174.820,
            "GBP_JPY": 209.450,
            "EUR_USD": 1.09756,
            "GBP_USD": 1.31499,
            "EUR_GBP": 0.83467,
            "AUD_JPY": 107.020,
        }
        self.market_status = "OPEN"
        self.ticker_ports = []  # 為替レートを取りに来た接続の番号(同じ番号なら、つないだまま使い回している)
        self.ticker_hits = 0
        self.drop_ticker_after = 0  # 0 以外なら、その回数ごとに相手(にせのサーバー)から接続を切る
        # ntfy
        self.ntfy = []
        self.ntfy_status = 200
        # Claude
        self.claude_reqs = []
        self.claude_mode = "ok"  # ok / need_legacy_auth / reject_format / error500 / garbage
        self.news_answer = dict(NEWS_ANSWER)
        # X
        self.x_rules = []
        self.x_rule_posts = []
        self.x_lines = []
        self.x_stream_hits = 0
        self.x_stream_queries = []
        self.x_auth = []
        self.x_status = 200

    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def add_feed_item(self, guid, title, text, ts=None):
        with self.lock:
            self.feed_items.append((guid, title, text, ts if ts is not None else time.time()))

    def add_edgar_entry(self, company, cik, accession, items=("2.02", "9.01"), form="8-K", role="Filer"):
        with self.lock:
            self.edgar_entries.insert(0, (company, cik, accession, tuple(items), form, role, iso_now()))


def _rss(state):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>']
    parts.append("<title>Fake feed</title><link>http://example.invalid/</link><description>test</description>")
    with state.lock:
        items = list(reversed(state.feed_items))
    for guid, title, text, ts in items:
        parts.append(
            "<item><title>%s</title><link>http://example.invalid/%s</link><guid>%s</guid>"
            "<description>%s</description><pubDate>%s</pubDate></item>"
            % (escape(title), guid, guid, escape(text), formatdate(ts, usegmt=True))
        )
    parts.append("</channel></rss>")
    return "\n".join(parts).encode("utf-8")


ITEM_TEXT = {
    "2.02": "Results of Operations and Financial Condition",
    "9.01": "Financial Statements and Exhibits",
    "5.02": "Departure of Directors or Certain Officers",
    "8.01": "Other Events",
}


def _edgar_atom(state):
    parts = ['<?xml version="1.0" encoding="ISO-8859-1" ?>', '<feed xmlns="http://www.w3.org/2005/Atom">']
    parts.append("<title>Latest Filings - Wed, 18 Nov 2026 13:55:13 EST</title>")
    parts.append('<link rel="alternate" href="/cgi-bin/browse-edgar?action=getcurrent"/>')
    parts.append("<id>https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent</id>")
    parts.append("<author><name>Webmaster</name><email>webmaster@sec.gov</email></author>")
    parts.append("<updated>%s</updated>" % iso_now())
    with state.lock:
        entries = list(state.edgar_entries)
    for company, cik, acc, items, form, role, updated in entries:
        nodash = acc.replace("-", "")
        href = "%s/sec/Archives/edgar/data/%d/%s/%s-index.htm" % (state.base(), cik, nodash, acc)
        summary = " <b>Filed:</b> 2026-11-17 <b>AccNo:</b> %s <b>Size:</b> 1 MB" % acc
        for num in items:
            summary += "\n<br>Item %s: %s" % (num, ITEM_TEXT.get(num, "Other"))
        parts.append("<entry>")
        parts.append("<title>%s - %s (%010d) (%s)</title>" % (form, escape(company), cik, role))
        parts.append('<link rel="alternate" type="text/html" href="%s"/>' % href)
        parts.append('<summary type="html">%s</summary>' % escape(summary))
        parts.append("<updated>%s</updated>" % updated)
        parts.append('<category scheme="https://www.sec.gov/" label="form type" term="%s"/>' % form)
        parts.append("<id>urn:tag:sec.gov,2008:accession-number=%s</id>" % acc)
        parts.append("</entry>")
    parts.append("</feed>")
    return "\n".join(parts).encode("iso-8859-1")


INDEX_HTML = """<html><body>
<table class="tableFile" summary="Document Format Files">
<tr><th scope="col">Seq</th><th scope="col">Description</th><th scope="col">Document</th><th scope="col">Type</th><th scope="col">Size</th></tr>
<tr><td scope="row">1</td><td scope="row">8-K</td><td scope="row"><a href="/ix?doc=/sec/Archives/edgar/data/1045810/000104581026000123/nvda-20261117.htm">nvda-20261117.htm</a></td><td scope="row">8-K</td><td scope="row">30000</td></tr>
<tr class="evenRow"><td scope="row">2</td><td scope="row">EX-99.1</td><td scope="row"><a href="/sec/Archives/edgar/data/1045810/000104581026000123/q3fy27pr.htm">q3fy27pr.htm</a></td><td scope="row">EX-99.1</td><td scope="row">250000</td></tr>
</table></body></html>"""

PRESS_HTML = """<html><head><style>p {color: red}</style></head><body>
<p><b>Example Corp Announces Financial Results for Third Quarter</b></p>
<p>Record quarterly revenue of $12.3 billion, up 45% from a year ago &amp; up 8% from the previous quarter.</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state = None

    def log_message(self, fmt, *args):
        return

    def _send(self, status, body=b"", ctype="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status, obj, extra=None):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json", extra)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None

    def do_GET(self):
        st = self.state
        url = urlparse(self.path)
        path = url.path
        if path == "/feed.xml":
            with st.lock:
                st.feed_hits += 1
                etag = st.feed_etag
                feed_status = st.feed_status
            if feed_status != 200:
                self._send(feed_status, b"temporarily broken", "text/plain")
                return
            if etag and self.headers.get("If-None-Match") == etag:
                with st.lock:
                    st.feed_304 += 1
                self._send(304)
                return
            self._send(200, _rss(st), "application/rss+xml", {"ETag": etag} if etag else None)
        elif path == "/ticker":
            with st.lock:
                st.ticker_ports.append(self.client_address[1])
                st.ticker_hits += 1
                if st.drop_ticker_after and st.ticker_hits % st.drop_ticker_after == 0:
                    self.close_connection = True
                rows = [
                    {"symbol": s, "ask": "%.5f" % (p * 1.00002), "bid": "%.5f" % (p * 0.99998), "timestamp": iso_now(), "status": st.market_status}
                    for s, p in st.prices.items()
                ]
            self._json(200, {"status": 0, "data": rows, "responsetime": iso_now()})
        elif path == "/sec/files/company_tickers.json":
            with st.lock:
                st.sec_agents.append(self.headers.get("User-Agent"))
            self._json(200, {
                "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
                "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            })
        elif path == "/sec/cgi-bin/browse-edgar":
            with st.lock:
                st.sec_agents.append(self.headers.get("User-Agent"))
            self._send(200, _edgar_atom(st), "application/atom+xml")
        elif path.startswith("/sec/Archives/") and path.endswith("-index.htm"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html")
        elif path.startswith("/sec/Archives/") and path.endswith("q3fy27pr.htm"):
            self._send(200, PRESS_HTML.encode("utf-8"), "text/html")
        elif path == "/x/2/tweets/search/stream/rules":
            with st.lock:
                st.x_auth.append(self.headers.get("Authorization"))
                status = st.x_status
                rules = list(st.x_rules)
            if status != 200:
                self._json(status, {"title": "Unauthorized"})
                return
            self._json(200, {"data": rules, "meta": {"result_count": len(rules)}} if rules else {"meta": {"result_count": 0}})
        elif path == "/x/2/tweets/search/stream":
            with st.lock:
                st.x_stream_hits += 1
                st.x_stream_queries.append(parse_qs(url.query))
                status = st.x_status
                lines = list(st.x_lines)
                st.x_lines = []
            if status != 200:
                self._json(status, {"title": "Unauthorized"})
                return
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(b"\r\n")
                for line in lines:
                    self.wfile.write(json.dumps(line, ensure_ascii=False).encode("utf-8") + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.05)
                time.sleep(0.3)
            except OSError:
                pass
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        st = self.state
        path = urlparse(self.path).path
        body = self._read_json()
        if path in ("/ntfy/", "/ntfy"):
            with st.lock:
                status = st.ntfy_status
                if status == 200:
                    st.ntfy.append(body)
            if status != 200:
                self._json(status, {"code": 42901, "error": "limit reached"})
                return
            self._json(200, {"id": "msg%d" % len(st.ntfy), "event": "message"})
        elif path == "/claude/v1/messages":
            headers = {k.lower(): v for k, v in self.headers.items()}
            with st.lock:
                st.claude_reqs.append({"headers": headers, "body": body})
                mode = st.claude_mode
                answer = dict(st.news_answer)
            if mode == "need_legacy_auth" and "x-api-key" not in headers:
                self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid bearer"}})
                return
            if mode == "reject_format" and "format" in (body or {}).get("output_config", {}):
                self._json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "output_config.format: unsupported"}})
                return
            if mode == "error500":
                self._json(500, {"type": "error", "error": {"type": "api_error", "message": "boom"}})
                return
            system = (body or {}).get("system", "")
            if mode == "garbage":
                text = "すみません、JSON にできませんでした"
            elif "急な動き" in system:
                text = json.dumps({"cause": "直前のニュースには、はっきりした原因は見当たりません"}, ensure_ascii=False)
            elif system.startswith("「OK」"):
                text = "OK"
            else:
                user = (body or {}).get("messages", [{}])[0].get("content", "")
                if "DUPTEST" in user:
                    answer["dup"] = True
                if "LOWTEST" in user:
                    answer["importance"] = 1
                text = json.dumps(answer, ensure_ascii=False)
            self._json(200, {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": (body or {}).get("model"),
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1500, "output_tokens": 400},
            })
        elif path == "/x/2/tweets/search/stream/rules":
            with st.lock:
                st.x_rule_posts.append(body)
                if body and "delete" in body:
                    ids = set(body["delete"].get("ids", []))
                    st.x_rules = [r for r in st.x_rules if r["id"] not in ids]
                if body and "add" in body:
                    for rule in body["add"]:
                        st.x_rules.append({"id": "r%d" % (len(st.x_rules) + 100), "value": rule["value"], "tag": rule.get("tag", "")})
            self._json(201, {"meta": {"summary": {"created": len((body or {}).get("add", []))}}})
        else:
            self._send(404, b"not found", "text/plain")


def start_server():
    """にせのサーバーを起動して (状態, サーバー) を返す。使い終わったら server.shutdown()。"""
    state = FakeState()
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, name="fake-server", daemon=True)
    thread.start()
    return state, server
