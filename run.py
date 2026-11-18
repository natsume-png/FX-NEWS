#!/usr/bin/env python3
"""FX見張り番の入口。

使い方(このファイルのあるフォルダで):
  python run.py check         それぞれの情報源につながるかを点検する(通知は送らない)
  python run.py topic         通知のあて先(ntfy の topic)を作って、config.ini に書く
  python run.py test-notify   ためしに通知を1通送る
  python run.py run           見張りを始める(何も書かずに python run.py でも同じ)
  python run.py report        分類の答え合わせを表示する
"""

import argparse
import datetime as dt
import logging
import logging.handlers
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    sys.stderr.write("Python 3.9 以上が必要です(いまは %s)。\n" % sys.version.split()[0])
    sys.exit(2)

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

from fxnews import __version__  # noqa: E402
from fxnews.config import Config, ensure_config_file, load_sources, new_topic, write_value  # noqa: E402
from fxnews.util import JST, jst  # noqa: E402


class JstFormatter(logging.Formatter):
    """記録の時刻を、パソコンの設定にかかわらず日本時間で出す。"""

    def formatTime(self, record, datefmt=None):
        return dt.datetime.fromtimestamp(record.created, JST).strftime(datefmt or "%H:%M:%S")


def setup_output(data_dir, to_file=True):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(JstFormatter("%(asctime)s %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    if to_file:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                str(data_dir / "fxnews.log"), maxBytes=2000000, backupCount=3, encoding="utf-8"
            )
            fh.setFormatter(JstFormatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"))
            root.addHandler(fh)
        except OSError:
            pass


def home_dir():
    """config.ini・sources.json・data フォルダを置く場所。ふつうは、このファイルと同じフォルダ。

    環境変数 FXNEWS_HOME で、別のフォルダにできる(サーバーで動かすときやテスト用)。
    """
    import os

    override = os.environ.get("FXNEWS_HOME")
    return Path(override).resolve() if override else CODE_DIR


def cmd_topic(cfg):
    current = cfg.get("ntfy", "topic")
    if current:
        print("通知のあて先(topic)は、もう作ってあります:")
        print("")
        print("    %s" % current)
    else:
        ensure_config_file(cfg.base_dir, CODE_DIR)
        current = new_topic()
        write_value(cfg.path, "ntfy", "topic", current)
        print("通知のあて先(topic)を作って、config.ini に書きました:")
        print("")
        print("    %s" % current)
    print("")
    print("スマホの ntfy アプリで、この名前を「購読(subscribe)」すると通知が届きます。")
    print("この名前を知っている人は、だれでも同じ通知を読めます。人に教えたり、ネットに書いたりしないでください。")
    return 0


def cmd_test_notify(cfg):
    from fxnews.notifier import Notifier
    from fxnews.store import Store

    store = Store(cfg.data_dir / "fxnews.db")
    try:
        notifier = Notifier(cfg, store)
        if not notifier.available():
            print("通知のあて先(topic)がまだありません。先に topic を実行してください。")
            return 1
        ok, why = notifier.send(
            "円↑ ドル↓｜FX見張り番のテスト通知",
            "この通知が見えていれば、スマホへの通知はつながっています。\n"
            "円↑ ドル↓ ユーロ→ ポンド→(これは見本です)\n"
            "送った時刻: %s(日本時間)" % jst(None, "%Y-%m-%d %H:%M:%S"),
            priority=4,
        )
        print("テスト通知を送りました。スマホを見てください。" if ok else "送れませんでした: %s" % why)
        return 0 if ok else 1
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run.py", description="FX見張り番 %s" % __version__)
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="見張りを始める")
    p_run.add_argument("--dry", action="store_true", help="スマホには送らず、画面に出すだけにする")
    p_run.add_argument("--replay", type=int, default=0, metavar="N", help="お試し: それぞれの更新一覧のいちばん新しい N 件を、新着としてあつかう")
    p_run.add_argument("--seconds", type=float, default=None, help="この秒数で終わる(お試し用)")
    sub.add_parser("check", help="それぞれの情報源につながるかを点検する")
    sub.add_parser("topic", help="通知のあて先(ntfy の topic)を作る")
    sub.add_parser("test-notify", help="ためしに通知を1通送る")
    p_rep = sub.add_parser("report", help="分類の答え合わせを表示する")
    p_rep.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)
    cmd = args.cmd or "run"

    home = home_dir()
    cfg = Config(home)
    setup_output(cfg.data_dir, to_file=(cmd == "run"))

    if cmd == "topic":
        return cmd_topic(cfg)
    if cmd == "test-notify":
        return cmd_test_notify(cfg)

    sources = load_sources(home if (home / "sources.json").exists() else CODE_DIR)
    if cmd == "check":
        from fxnews.check import run_check

        return 1 if run_check(cfg, sources) else 0
    if cmd == "report":
        from fxnews.report import run_report
        from fxnews.store import Store

        store = Store(cfg.data_dir / "fxnews.db")
        try:
            run_report(store, days=args.days)
        finally:
            store.close()
        return 0

    from fxnews.app import App

    app = App(cfg, sources, replay=getattr(args, "replay", 0), dry=getattr(args, "dry", False))
    try:
        app.run(seconds=getattr(args, "seconds", None))
    finally:
        app.close(timeout=3.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
