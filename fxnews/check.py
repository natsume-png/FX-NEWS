"""点検: それぞれの情報源に本当につながるか、1回ずつ確かめて画面に出す。通知は送らない。"""

import platform
import threading
import time

from .analyzer import Analyzer
from .edgar import EdgarPoller
from .feeds import FeedPoller
from .net import request
from .notifier import Notifier
from .rates import PAIR_JA, PAIRS, Maintenance, fmt_price, parse_ticker
from .store import Store
from .util import jst, mask_secret
from .xstream import XStream


HEADER_BYTES = 500  # 1回のやりとりにつく、中身以外の部分(見出し)のおおよその大きさ


def run_check(cfg, sources, out=print):
    """戻り値は、つながらなかった情報源の数。"""
    bad = 0
    store = Store(":memory:")
    stop = threading.Event()
    out("FX見張り番の点検(%s 日本時間)" % jst(None, "%Y-%m-%d %H:%M:%S"))
    out("Python %s / %s" % (platform.python_version(), platform.platform()))
    out("設定ファイル config.ini: %s" % ("読みこみました" if cfg.loaded else "まだありません(なくても点検はできます)"))
    out("")

    daily_bytes = 0.0  # 24時間つづけて見張ったときの、1日の通信量の目安
    out("■ 更新一覧(ニュース・公式発表)")
    for spec in sources.get("feeds", []):
        if spec.get("enabled", True) is False:
            continue
        poller = FeedPoller(spec, store, None, stop)
        start = time.time()
        try:
            entries = poller.fetch() or []
            took = time.time() - start
            per_day = 86400.0 / poller.interval
            if poller.interval < 120:
                # もう一度たのんで、「前回から変わっていません(304)」という短い返事をもらえるかを見る
                unchanged = poller.fetch() is None
                note = "あり" if unchanged else "なし(毎回ぜんぶ受け取る)"
            else:
                unchanged = False  # 見に行く間隔が長い相手には、ためしの2回目をたのまない
                note = "ためしていない(%d秒おきの相手)" % poller.interval
            daily_bytes += per_day * (HEADER_BYTES + (0 if unchanged else poller.last_wire))
            size = "受け取り %dKB、変更なしの短い返事: %s" % (max(1, poller.last_wire // 1000), note)
            if entries:
                newest = max(entries, key=lambda e: e["published"] or 0)
                when = jst(newest["published"], "%m/%d %H:%M") if newest["published"] else "時刻なし"
                out("  OK  %s: %d件(%.1f秒、%s)" % (poller.name_ja, len(entries), took, size))
                out("        いちばん新しいもの [%s] %s" % (when, newest["title"][:70]))
            else:
                out("  OK? %s: つながりましたが、記事が0件でした(%.1f秒、%s)" % (poller.name_ja, took, size))
        except Exception as exc:
            bad += 1
            out("  NG  %s: %s" % (poller.name_ja, exc))
        finally:
            poller.close()
    out("")

    out("■ 為替レート(GMOコイン)")
    try:
        start = time.time()
        resp = request(cfg.get("rates", "url"), timeout=10, headers={"Accept": "application/json"})
        mids, open_pairs = parse_ticker(resp.json())
        took = time.time() - start
        daily_bytes += 86400.0 / max(0.5, cfg.getfloat("rates", "interval_sec")) * (HEADER_BYTES + resp.wire_len)
        missing = [PAIR_JA[p] for p in PAIRS if p not in mids]
        if missing:
            bad += 1
            out("  NG  次の組合せが入っていません: %s" % "、".join(missing))
        else:
            out("  OK  6つの組合せを取れました(%.1f秒)。取引時間中: %s" % (took, "はい" if open_pairs else "いいえ(週末など)"))
        out("        " + " / ".join("%s %s" % (PAIR_JA[p], fmt_price(p, mids[p])) for p in PAIRS if p in mids))
    except Maintenance:
        out("  --  配信が点検中です(故障ではありません。GMOコインは、毎週土曜の 9:00〜11:00 が定期の点検です)")
    except Exception as exc:
        bad += 1
        out("  NG  %s" % exc)
    out("")

    out("■ 会社の届け出(米国 SEC)")
    contact = cfg.get("sec", "contact")
    spec = sources.get("edgar", {})
    if not contact:
        out("  --  止めています。config.ini の [sec] contact に、名前とメールアドレスを書くと動きます")
        out("        (SEC の決まりで、取りに行くときに連絡先を名乗る必要があるためです)")
    elif not spec.get("tickers"):
        out("  --  sources.json に見張る会社が書かれていません")
    else:
        poller = EdgarPoller(spec, contact, cfg.get("sec", "base"), store, None, stop, cfg.data_dir)
        try:
            ciks = poller.resolve()
            out("  OK  会社の番号を引けました: " + "、".join("%s=%s" % (t, c) for c, (t, _n) in sorted(ciks.items(), key=lambda kv: kv[1][0])))
            for form in poller.forms:
                entries = poller.fetch_form(form)
                mine = [e for e in entries if poller.to_item(e, form) is not None]
                out("  OK  届け出 %s の最新一覧: %d件(うち見張っている会社のもの %d件)" % (form, len(entries), len(mine)))
                if entries:
                    out("        いちばん上の行: %s" % entries[0]["title"][:80])
                    out("        その説明文: %s" % entries[0]["summary"][:120])
        except Exception as exc:
            bad += 1
            out("  NG  %s" % exc)
        finally:
            poller.close()
    out("")

    out("■ 解説(Claude)")
    analyzer = Analyzer(cfg, store)
    if not analyzer.available():
        out("  --  止めています。config.ini の [claude] api_key に鍵を書くと動きます")
    else:
        ok, note = analyzer.ping()
        if not ok:
            bad += 1
        out("  %s  鍵: %s / %s" % ("OK" if ok else "NG", mask_secret(analyzer.key), note))
        if ok:
            # 見本のニュース(作りもの)を1件、型に分けてもらう。型ごとの成績を数える土台が動くかの確かめ
            fine, text = analyzer.selftest()
            if not fine:
                bad += 1
            out("  %s  %s" % ("OK" if fine else "NG", text))
    out("")

    notifier = Notifier(cfg, store)
    out("■ 通知(%s)" % notifier.name_ja)
    if notifier.bad_service:
        out("  NG? config.ini の [notify] service の「%s」は知らない名前です。ntfy としてあつかいます" % notifier.bad_service)
    if not notifier.available():
        if notifier.service == "pushover":
            out("  --  止めています。Pushover の番号がまだありません。setup-pushover を実行すると入れられます")
        else:
            out("  --  止めています。あて先(topic)がまだありません。topic を実行すると作れます")
    elif notifier.service == "pushover":
        try:
            limits = notifier.pushover_get("/1/apps/limits.json")
            out("  OK  番号: 受け取り %s / 送り主 %s" % (mask_secret(notifier.po_user), mask_secret(notifier.po_token)))
            out("        Pushover のアカウントぜんぶの、今月の送れる数: 残り %s通 / %s通(%s 日本時間に元にもどる)" % (
                limits.get("remaining", "?"), limits.get("limit", "?"),
                jst(float(limits["reset"]), "%m/%d %H:%M") if limits.get("reset") else "?"))
            out("        このプログラムの上限: 1日 %d通・1か月 %d通。アカウントの残りが %d通を切ったら、ニュースの通知を止めます" % (
                notifier.daily_limit, notifier.monthly_limit, notifier.reserve))
            known = notifier.pushover_get("/1/sounds.json").get("sounds") or {}
            unknown = sorted({name for name in notifier.po_sounds.values() if name and name not in known})
            if unknown and known:
                out("  NG? Pushover の音の一覧にない名前が、設定に書かれています: %s" % "、".join(unknown))
            else:
                out("        音: ニュース %s / 重要なニュース %s / 急騰急落 %s / プログラムからのお知らせ %s" % (
                    notifier.po_sounds["news"], notifier.po_sounds["important"], notifier.po_sounds["spike"], notifier.po_sounds["system"]))
            out("        ためしに1通送るには、test-notify を実行します")
        except Exception as exc:
            bad += 1
            out("  NG  %s" % exc)
    else:
        out("  OK  あて先: 設定あり。ためしに1通送るには、test-notify を実行します")
    out("")

    out("■ X")
    xs = XStream(sources.get("x", {}).get("rules", []), cfg, store, None, stop)
    if not xs.token:
        out("  --  止めています。config.ini の [x] bearer_token に鍵を書くと動きます")
    else:
        try:
            current = xs.get_rules()
            out("  OK  鍵: %s / X に登録ずみのルール %d本、設定に書いたルール %d本" % (mask_secret(xs.token), len(current), len(xs.rules)))
            out("        上限: 今月 %d円(= %d件)、きょう %d件" % (int(xs.cap_yen), xs.month_cap_posts(), xs.day_cap))
        except Exception as exc:
            bad += 1
            out("  NG  %s" % exc)
    out("")
    out("1日の通信量の目安(更新一覧と為替レートを24時間見張った場合): 約 %d MB" % round(daily_bytes / 1e6))
    out("点検おわり: つながらなかったもの %d件" % bad)
    store.close()
    return bad
