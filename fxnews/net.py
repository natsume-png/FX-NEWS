"""インターネットとのやりとり(追加の部品を使わず、Python 標準の urllib だけ)。"""

import gzip
import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import __version__

USER_AGENT = "FX-NEWS/%s (+https://github.com/natsume-png/FX-NEWS)" % __version__


class NetError(Exception):
    """つながらない、時間切れ、証明書の確認の失敗など。"""


class HttpError(Exception):
    """相手のサーバーがエラーの番号(4xx、5xx)を返した。"""

    def __init__(self, status, body=b"", headers=None, url=""):
        self.status = status
        self.body = body or b""
        self.headers = headers or {}
        self.url = url
        super().__init__("HTTP %s" % status)

    def text(self, limit=300):
        try:
            return self.body.decode("utf-8", errors="replace")[:limit]
        except Exception:
            return ""

    def __str__(self):
        detail = " ".join(self.text(160).split())
        return "HTTP %s %s" % (self.status, detail) if detail else "HTTP %s" % self.status


class Response:
    __slots__ = ("status", "headers", "body", "url", "wire_len")

    def __init__(self, status, headers, body, url, wire_len=None):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url
        # 回線を通ったときの大きさ(圧縮されていれば、圧縮されたままのバイト数)
        self.wire_len = len(body) if wire_len is None else wire_len

    def text(self, encoding="utf-8"):
        return self.body.decode(encoding, errors="replace")

    def json(self):
        return json.loads(self.body.decode("utf-8", errors="replace"))


def _lower_headers(headers):
    out = {}
    if headers is None:
        return out
    for key in headers.keys():
        out[key.lower()] = headers.get(key)
    return out


def _decode_body(body, headers):
    enc = (headers.get("content-encoding") or "").lower() if headers else ""
    try:
        if "gzip" in enc:
            return gzip.decompress(body)
        if "deflate" in enc:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, EOFError, zlib.error):
        return body
    return body


def _explain(exc):
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
        return (
            "相手の証明書を確認できませんでした(%s)。Mac で python.org 版の Python を使っている場合は、"
            "アプリケーションの Python フォルダにある Install Certificates.command の実行が必要なことがあります"
            % reason
        )
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
        return "時間切れ(相手の返事が遅い、またはネットにつながっていない)"
    if isinstance(reason, socket.gaierror):
        return "相手の名前を引けませんでした(ネットにつながっていない可能性): %s" % reason
    return "つながりませんでした: %s" % reason


def _build(url, method, headers, data):
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if not any(k.lower() == "content-type" for k in hdrs):
            hdrs["Content-Type"] = "application/json; charset=utf-8"
    elif isinstance(data, str):
        data = data.encode("utf-8")
    return urllib.request.Request(url, data=data, headers=hdrs, method=method)


_NET_ERRORS = (
    urllib.error.URLError,
    socket.timeout,
    TimeoutError,
    ssl.SSLError,
    http.client.HTTPException,
    ConnectionError,
    OSError,
)


def request(url, method="GET", headers=None, data=None, timeout=15, max_bytes=8000000):
    """1回のやりとり。成功なら Response。304(変更なし)も Response で返す。

    4xx・5xx は HttpError、つながらないときは NetError を投げる。
    """
    req = _build(url, method, headers, data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise NetError("返ってきたデータが大きすぎます(%d バイト超)" % max_bytes)
            hdrs = _lower_headers(resp.headers)
            return Response(resp.status, hdrs, _decode_body(body, hdrs), resp.geturl(), len(body))
    except urllib.error.HTTPError as exc:
        hdrs = _lower_headers(exc.headers)
        if exc.code == 304:
            return Response(304, hdrs, b"", url)
        try:
            body = _decode_body(exc.read(200000), hdrs)
        except Exception:
            body = b""
        raise HttpError(exc.code, body, hdrs, url) from None
    except NetError:
        raise
    except _NET_ERRORS as exc:
        raise NetError(_explain(exc)) from None


def _uses_proxy(scheme, host):
    """この相手へは、パソコンに設定された中継(プロキシ)を通るか。"""
    try:
        if not urllib.request.getproxies().get(scheme):
            return False
        return not urllib.request.proxy_bypass(host)
    except Exception:
        return True


class Session:
    """同じ相手に、つないだままくり返し問い合わせる係。

    毎回つなぎ直す(暗号のあいさつをやり直す)より速く、通信量も少ない。相手のサーバーの負担もへる。
    1つの係(スレッド)が1つずつ持つ。うまくいかないときは、ふつうの request() と同じ結果になる。
    中継(プロキシ)を通る環境や、行き先が変わる(リダイレクト)相手では、ふつうの request() にまかせる。
    """

    _REDIRECTS = (301, 302, 303, 307, 308)

    def __init__(self):
        self._conn = None
        self._key = None
        self._plain = False  # True なら、つないだままにせず request() を使う
        self.reused = 0  # つないだままの接続を使い回した回数(点検・テスト用)

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._key = None

    def _connect(self, scheme, host, port, timeout):
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def get(self, url, headers=None, timeout=15, max_bytes=8000000):
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname or ""
        if self._plain or scheme not in ("http", "https") or not host or _uses_proxy(scheme, host):
            return request(url, headers=headers, timeout=timeout, max_bytes=max_bytes)
        port = parts.port or (443 if scheme == "https" else 80)
        key = (scheme, host, port)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", "Accept": "*/*"}
        if headers:
            hdrs.update(headers)
        for attempt in (0, 1):
            fresh = self._conn is None or self._key != key
            if fresh:
                self.close()
                self._conn = self._connect(scheme, host, port, timeout)
                self._key = key
            conn = self._conn
            try:
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                conn.request("GET", path, headers=hdrs)
                resp = conn.getresponse()
                body = resp.read(max_bytes + 1)
                status = resp.status
                rhdrs = {k.lower(): v for k, v in resp.getheaders()}
                if resp.will_close or len(body) > max_bytes:
                    self.close()
                elif not fresh:
                    self.reused += 1
            except _NET_ERRORS as exc:
                self.close()
                if attempt == 0 and not fresh:
                    continue  # 相手が、しばらく使っていない接続を切っていた。つなぎ直して、もう一度
                raise NetError(_explain(exc)) from None
            if len(body) > max_bytes:
                raise NetError("返ってきたデータが大きすぎます(%d バイト超)" % max_bytes)
            if status in self._REDIRECTS:
                self._plain = True
                self.close()
                return request(url, headers=headers, timeout=timeout, max_bytes=max_bytes)
            if status == 304:
                return Response(304, rhdrs, b"", url)
            decoded = _decode_body(body, rhdrs)
            if status >= 400:
                raise HttpError(status, decoded[:200000], rhdrs, url)
            return Response(status, rhdrs, decoded, url, len(body))
        raise NetError("つながりませんでした")


def open_stream(url, headers=None, timeout=45):
    """つなぎっぱなしで1行ずつ読むための接続を開く(X の Filtered Stream 用)。

    戻り値は、行を読める接続そのもの。使い終わったら close() する。
    """
    req = _build(url, "GET", headers, None)
    req.remove_header("Accept-encoding")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        hdrs = _lower_headers(exc.headers)
        try:
            body = exc.read(200000)
        except Exception:
            body = b""
        raise HttpError(exc.code, body, hdrs, url) from None
    except _NET_ERRORS as exc:
        raise NetError(_explain(exc)) from None


def retry_after_seconds(err, default=None):
    """HttpError から「何秒待て」の指示(Retry-After など)を読む。なければ default。"""
    hdrs = getattr(err, "headers", {}) or {}
    val = hdrs.get("retry-after")
    if val:
        try:
            return max(0.0, float(val))
        except ValueError:
            pass
    reset = hdrs.get("x-rate-limit-reset")
    if reset:
        try:
            import time

            return max(1.0, float(reset) - time.time())
        except ValueError:
            pass
    return default
