"""インターネットとのやりとり(追加の部品を使わず、Python 標準の urllib だけ)。"""

import gzip
import http.client
import json
import socket
import ssl
import urllib.error
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
