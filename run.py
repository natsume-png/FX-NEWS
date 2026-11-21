#!/usr/bin/env python3
"""FX見張り番の入口。

使い方(このファイルのあるフォルダで):
  python run.py check            それぞれの情報源につながるかを点検する(通知は送らない)
  python run.py setup-pushover   Pushover(通知アプリ)の番号を入れて、ためしに1通送る
  python run.py setup-claude     Claude(解説をつける AI)の鍵を入れて、つながるか確かめる
  python run.py setup-sec        米国 SEC(会社の届け出)に名乗る連絡先を入れる
  python run.py topic            ntfy(通知アプリ)を使うときの、通知のあて先を作る
  python run.py test-notify      ためしに通知を1通送る
  python run.py run              見張りを始める(この画面で動かす。何も書かずに python run.py でも同じ)
  python run.py start            見張りを、黒い画面なしで始める
  python run.py status           見張りが動いているかを見る
  python run.py stop             動いている見張りを止める
  python run.py autostart-on     止まっても自分で始めなおす仕組みを入れる(Windows だけ)
  python run.py autostart-off    その仕組みを外す
  python run.py version          版の番号を出す
  python run.py report           型ごとの成績(ニュースのあと、ドル円がどう動いたか)と、分類の答え合わせを表示する
"""

import argparse
import datetime as dt
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

if sys.version_info < (3, 9):
    if sys.stderr is not None:
        sys.stderr.write("Python 3.9 以上が必要です(いまは %s)。\n" % sys.version.split()[0])
    sys.exit(2)

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

from fxnews import __version__  # noqa: E402
from fxnews.config import Config, ensure_config_file, load_sources, new_topic, write_value  # noqa: E402
from fxnews.util import JST, jst, mask_secret  # noqa: E402

log = logging.getLogger("fxnews.run")


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
    if sys.stdout is not None:  # 黒い画面なしで動かしたとき(pythonw)は、画面がないので記録のファイルだけに書く
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


def home_dir(explicit=None):
    """config.ini・sources.json・data フォルダを置く場所。ふつうは、このファイルと同じフォルダ。

    --home か、環境変数 FXNEWS_HOME で、別のフォルダにできる(サーバーで動かすときやテスト用)。
    """
    override = explicit or os.environ.get("FXNEWS_HOME")
    return Path(override).resolve() if override else CODE_DIR


def say(text=""):
    """画面に1行出す(黒い画面なしで動かしているときは、何もしない)。"""
    print(text)


def place_console(cfg, cmd):
    """config.ini に黒い画面の置き場所が書いてあれば、そこへ動かす(Windows だけ)。どうなったかの説明を返す。"""
    text = cfg.get("general", "console_rect")
    if not text or os.name != "nt" or sys.stdout is None:
        return ""
    from fxnews.winconsole import parse_rect, place

    rect = parse_rect(text)
    if rect is None:
        note = "config.ini の [general] console_rect の書き方がちがいます。「左,上,幅,高さ」の数字4つで書きます"
        say("(%s)" % note)
        return note
    ok, note = place(rect, title="FX-NEWS %s" % cmd)
    if not ok:
        say("(%s)" % note)
    return note


def note_command(cfg, text):
    """コマンドを動かした記録を、data/commands.log に1行足す(鍵や番号は書かない)。

    黒い画面を閉じたあとでも、いつ・何を動かして・どうなったかを見返せるようにするため。
    """
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        with open(str(cfg.data_dir / "commands.log"), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (jst(None, "%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass


def clear_screen():
    """打ちこんだ鍵が画面に残らないように、画面を消す。"""
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass


def ask_line(ask, prompt, check, hint, tries=5):
    """1行たずねる。check が True を返すまで、tries 回までくり返す。空のまま Enter ならやめる(None)。"""
    for _ in range(tries):
        try:
            value = ask(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not value:
            return None
        if check(value):
            return value
        say("  " + hint)
    return None


# ---- ntfy のあて先 ----
def cmd_topic(cfg):
    current = cfg.get("ntfy", "topic")
    if current:
        say("通知のあて先(topic)は、もう作ってあります:")
        say("")
        say("    %s" % current)
    else:
        ensure_config_file(cfg.base_dir, CODE_DIR)
        current = new_topic()
        write_value(cfg.path, "ntfy", "topic", current)
        say("通知のあて先(topic)を作って、config.ini に書きました:")
        say("")
        say("    %s" % current)
    say("")
    say("スマホの ntfy アプリで、この名前を「購読(subscribe)」すると通知が届きます。")
    say("この名前を知っている人は、だれでも同じ通知を読めます。人に教えたり、ネットに書いたりしないでください。")
    if cfg.get("notify", "service").lower() == "pushover":
        say("")
        say("(いまの設定では、通知は Pushover に送ります。ntfy に送るには、config.ini の [notify] service を ntfy にします)")
    return 0


# ---- ためしの通知 ----
def cmd_test_notify(cfg):
    from fxnews.notifier import Notifier
    from fxnews.store import Store

    store = Store(cfg.data_dir / "fxnews.db")
    try:
        notifier = Notifier(cfg, store)
        if not notifier.available():
            say("まだ送れません: %s。" % notifier.missing())
            say("Pushover を使うなら setup-pushover を、ntfy を使うなら topic を、先に実行してください。")
            return 1
        ok, why = notifier.send(
            "円↑ ドル↓｜FX見張り番のテスト通知",
            "この通知が見えていれば、スマホへの通知はつながっています。\n"
            "円↑ ドル↓ ユーロ→ ポンド→(これは見本です)\n"
            "送った時刻: %s(日本時間)" % jst(None, "%Y-%m-%d %H:%M:%S"),
            priority=4,
        )
        say("テスト通知を %s に送りました。スマホを見てください。" % notifier.name_ja if ok else "送れませんでした: %s" % why)
        return 0 if ok else 1
    finally:
        store.close()


# ---- 鍵や番号を入れる ----
def cmd_setup_pushover(cfg, ask=input, new_user=False):
    """Pushover の番号2つをたずね、ためしに1通送り、送れたら config.ini に書く。

    受け取り番号がもう入っていれば、たずねるのは送り主の番号の1回だけ
    (new_user を True にすると、受け取り番号もたずねなおす)。
    """
    from fxnews.notifier import PUSHOVER_KEY_RE, Notifier
    from fxnews.store import Store

    ensure_config_file(cfg.base_dir, CODE_DIR)
    say("=== Pushover(通知アプリ)の番号を入れます ===")
    say("")
    say("入れる番号は2つです。どちらも、英数字ちょうど30文字です。")
    say("  1. 受け取り番号(User Key): あなたの Pushover のアカウントの番号。")
    say("     Pushover のサイト(pushover.net)にログインしたときの画面か、スマホの Pushover アプリの設定の画面に出ています。")
    say("  2. 送り主の番号(API Token): 「このプログラムから来た通知」と見分けるための番号。")
    say("     Pushover のサイトで、送り主(Application)を1つ登録すると、もらえます。")
    say("")
    say("番号は、このパソコンの config.ini にだけ書きます。やめるときは、何も打たずに Enter を押します。")
    say("")
    hint = "英数字30文字ではないようです。もう一度、貼りつけてください。"
    user = cfg.get("pushover", "user_key")
    if PUSHOVER_KEY_RE.match(user) and not new_user:
        say("受け取り番号(User Key)は、もう入っています(%s)。そのまま使います。" % mask_secret(user))
        say("(受け取り番号を入れなおすときは、setup-pushover --new-user を実行します)")
        say("")
    else:
        user = ask_line(ask, "受け取り番号(User Key)を貼って Enter: ", PUSHOVER_KEY_RE.match, hint)
        if not user:
            say("やめました。何も書きかえていません。")
            return 1
    token = ask_line(ask, "送り主の番号(API Token)を貼って Enter: ", PUSHOVER_KEY_RE.match, hint)
    if not token:
        say("やめました。何も書きかえていません。")
        return 1
    clear_screen()
    store = Store(cfg.data_dir / "fxnews.db")
    try:
        notifier = Notifier(cfg, store)
        notifier.service = "pushover"
        notifier.po_user = user
        notifier.po_token = token
        notifier.daily_limit = notifier.monthly_limit = notifier.reserve = 0  # ためしの1通は、このプログラムの上限にかかわらず送る
        ok, why = notifier.send(
            "FX見張り番のテスト通知",
            "この通知が見えていれば、Pushover への登録はできています。\n"
            "これは、重要度3のニュースと同じ音(%s)です。\n"
            "送った時刻: %s(日本時間)" % (notifier.pushover_sound("news", 3), jst(None, "%Y-%m-%d %H:%M:%S")),
            priority=3,
        )
        if not ok:
            say("送れませんでした: %s" % why)
            say("config.ini は書きかえていません。番号をもう一度確かめてください。")
            return 1
        write_value(cfg.path, "pushover", "user_key", user)
        write_value(cfg.path, "pushover", "api_token", token)
        write_value(cfg.path, "notify", "service", "pushover")
        say("ためしの通知を送れました。スマホの Pushover が鳴ったか、見てください。")
        say("番号を config.ini に書きました(受け取り番号 %s / 送り主の番号 %s)。" % (mask_secret(user), mask_secret(token)))
        say("これからの通知は、Pushover に送ります。")
        if notifier.remaining is not None:
            say("Pushover のアカウントぜんぶの、今月の残り: %d通(上限 %s通)" % (notifier.remaining, notifier.account_limit or "?"))
        return 0
    finally:
        store.close()


def cmd_setup_claude(cfg, ask=input):
    """Claude の鍵をたずね、つながるか確かめて、つながったら config.ini に書く。"""
    from fxnews.analyzer import Analyzer
    from fxnews.store import Store

    ensure_config_file(cfg.base_dir, CODE_DIR)
    say("=== Claude(解説と分類をつける AI)の鍵を入れます ===")
    say("")
    say("鍵(API キー)は、Claude Console(開発者向けの管理画面。platform.claude.com)の「API keys」のページで作ります。")
    say("鍵は、このパソコンの config.ini にだけ書きます。やめるときは、何も打たずに Enter を押します。")
    say("")

    def looks_like_key(value):
        return len(value) >= 20 and not re.search(r"\s", value)

    key = ask_line(ask, "Claude の鍵(API キー)を貼って Enter: ", looks_like_key, "鍵ではないようです(20文字より短いか、空白が入っています)。もう一度、貼りつけてください。")
    if not key:
        say("やめました。何も書きかえていません。")
        return 1
    clear_screen()
    store = Store(":memory:")
    try:
        analyzer = Analyzer(cfg, store)
        analyzer.key = key
        say("つながるか、確かめています…")
        ok, note = analyzer.ping()
        workspace = ""
        if not ok and "workspace" in note.lower():
            say("この鍵は、どのワークスペース(Console の中の作業場)で使うかを決めていない鍵のようです。")
            say("ワークスペースの ID(wrkspc_ ではじまる文字)を入れると、使えます。")
            workspace = ask_line(ask, "ワークスペースの ID を貼って Enter(やめるなら何も打たずに Enter): ", lambda v: v.startswith("wrkspc_"), "wrkspc_ ではじまる文字ではないようです。")
            if workspace:
                analyzer.workspace = workspace
                ok, note = analyzer.ping()
        if not ok:
            say("つながりませんでした: %s" % note)
            say("config.ini は書きかえていません。鍵をもう一度確かめてください。")
            return 1
        write_value(cfg.path, "claude", "api_key", key)
        if workspace:
            write_value(cfg.path, "claude", "workspace_id", workspace)
        say("つながりました(%s)。" % note)
        say("鍵を config.ini に書きました(%s)。" % mask_secret(key))
        say("解説に使うお金の、1か月の上限は %d円 です(config.ini の [claude] monthly_cap_yen で変えられます)。" % cfg.getint("claude", "monthly_cap_yen"))
        return 0
    finally:
        store.close()


def cmd_setup_sec(cfg, ask=input):
    """米国 SEC に名乗る連絡先(名前とメールアドレス)をたずねて、config.ini に書く。"""
    ensure_config_file(cfg.base_dir, CODE_DIR)
    say("=== 米国 SEC(証券取引委員会)に名乗る連絡先を入れます ===")
    say("")
    say("会社の決算の届け出を SEC のサイトから取るときは、SEC の決まりで、名前とメールアドレスを名乗ります。")
    say("連絡先は、このパソコンの config.ini に書き、SEC のサイトにだけ送ります。やめるときは、何も打たずに Enter を押します。")
    say("")
    name = ask_line(ask, "名前(ローマ字。例: Taro Yamada)を打って Enter: ", lambda v: bool(re.match(r"^[A-Za-z][A-Za-z .'-]{1,60}$", v)), "ローマ字(半角の英字)で打ってください。")
    if not name:
        say("やめました。何も書きかえていません。")
        return 1
    mail = ask_line(ask, "メールアドレスを打って Enter: ", lambda v: bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v)), "メールアドレスの形ではないようです。")
    if not mail:
        say("やめました。何も書きかえていません。")
        return 1
    write_value(cfg.path, "sec", "contact", "%s %s" % (name, mail))
    say("config.ini に書きました。check を実行すると、SEC につながるかを確かめられます。")
    return 0


# ---- 動いているかを見る・止める ----
def read_alive(cfg):
    from fxnews.app import ALIVE_FILE

    try:
        return json.loads((cfg.data_dir / ALIVE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


AUTOSTART_TASK = "FX-NEWS mihari"  # Windows の「タスク スケジューラ」(決めた時刻に物を動かす係)に入れる仕事の名前
AUTOSTART_OFF = "autostart.off"  # data フォルダにこのファイルがあるあいだは、自動では始めない(stop コマンドが作る)
AUTOSTART_EVERY_MIN = 5  # 何分おきに「止まっていたら始める」を見るか


def pythonw_path():
    """黒い画面を出さずに動かす Python(pythonw)の場所。見つからなければ None。"""
    exe = Path(sys.executable)
    for name in ("pythonw.exe", "pythonw"):
        cand = exe.with_name(name)
        if cand.exists():
            return cand
    for name in ("pythonw", "pyw"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def spawn_watcher(home):
    """見張りを、黒い画面なしで、このコマンドとは切りはなして始める。"""
    exe = pythonw_path() or Path(sys.executable)
    args = [str(exe), str(CODE_DIR / "run.py"), "--home", str(home), "run"]
    kwargs = {"cwd": str(CODE_DIR), "close_fds": True}
    if os.name == "nt":
        # 0x00000008 = この画面(コンソール)につながない、0x00000200 = 別の組にする(画面を閉じても、道連れにしない)
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs.update({"start_new_session": True, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL})
    subprocess.Popen(args, **kwargs)
    return exe


def schtasks(*args, timeout=30):
    """Windows の schtasks(タスク スケジューラをあつかう、Windows に入っているコマンド)を動かす。(終わりの番号, 出た文字)。"""
    if os.name != "nt":
        return 1, "Windows ではありません"
    try:
        done = subprocess.run(
            ["schtasks"] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, done.stdout.decode("mbcs" if os.name == "nt" else "utf-8", errors="replace").strip()


def autostart_installed():
    """止まっても自分で始めなおす仕組み(タスク スケジューラの仕事)が、入っているか。"""
    return schtasks("/Query", "/TN", AUTOSTART_TASK)[0] == 0


def autostart_paused(cfg):
    return (cfg.data_dir / AUTOSTART_OFF).exists()


def current_user_id():
    """タスク スケジューラに書く、いまの利用者のしるし(SID)。取れなければ「パソコン名\\利用者名」。"""
    try:
        done = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        m = re.search(r"S-1-[0-9-]+", done.stdout.decode("mbcs", errors="replace"))
        if m:
            return m.group(0)
    except (OSError, subprocess.SubprocessError, LookupError):
        pass
    return "%s\\%s" % (os.environ.get("USERDOMAIN", "."), os.environ.get("USERNAME", ""))


def autostart_xml(exe, home, user_id, every_min=AUTOSTART_EVERY_MIN):
    """タスク スケジューラに入れる仕事の中身(XML)。

    - 決めた分おきに「見張りを始める」を動かす。もう動いていれば、タスク スケジューラは何もしない(2つは動かさない)。
      きっかけは2つ書いてある(1回かぎり + くり返し、毎日 + くり返し)。片方が働かない Windows でも、もう片方で動くようにするため。
    - 動かす時間に上限をつけない。電池で動いているときも止めない。
    - この利用者がサインインしているあいだだけ、この利用者として、ふつうの権限で動く。
    """
    from xml.sax.saxutils import escape

    arguments = '"%s" --home "%s" run --auto' % (CODE_DIR / "run.py", home)
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>FX-NEWS: keeps the news watcher running. Starts it again within %(every)d minutes if it has stopped. Remove with: run.py autostart-off</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT%(every)dM</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
    <CalendarTrigger>
      <Repetition>
        <Interval>PT%(every)dM</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:02:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%(user)s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(exe)s</Command>
      <Arguments>%(args)s</Arguments>
      <WorkingDirectory>%(cwd)s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""" % {
        "every": int(every_min),
        "user": escape(str(user_id)),
        "exe": escape(str(exe)),
        "args": escape(arguments),
        "cwd": escape(str(CODE_DIR)),
    }


def cmd_autostart_on(cfg, home):
    """止まっても自分で始めなおす仕組みを入れる(Windows のタスク スケジューラに、仕事を1つ足す)。"""
    if os.name != "nt":
        say("この仕組みは、Windows だけで使えます。")
        return 1
    exe = pythonw_path()
    if exe is None:
        say("黒い画面なしで動かす Python(pythonw)が見つからないので、入れられませんでした。")
        return 1
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    xml_path = cfg.data_dir / "autostart_task.xml"
    xml_path.write_text(autostart_xml(exe, home, current_user_id()), encoding="utf-16")
    code, out = schtasks("/Create", "/TN", AUTOSTART_TASK, "/XML", str(xml_path), "/F")
    if code != 0:
        say("入れられませんでした: %s" % " ".join(out.split())[:300])
        return 1
    try:
        (cfg.data_dir / AUTOSTART_OFF).unlink()
    except OSError:
        pass
    say("止まっても自分で始めなおす仕組みを入れました(タスク スケジューラの「%s」)。" % AUTOSTART_TASK)
    say("・%d分おきに、見張りが動いているかを見て、止まっていたら始めます(動いていれば、何もしません)。" % AUTOSTART_EVERY_MIN)
    say("・パソコンを立ち上げなおしたあとも、サインインして%d分以内に、自分で始まります。" % AUTOSTART_EVERY_MIN)
    say("・「見張りを止める」(stop)で止めたときは、止めたままにします。「見張りを始める」(start)で、また動かします。")
    say("・外すときは autostart-off を実行します。")
    return 0


def cmd_autostart_off(cfg):
    if os.name != "nt":
        say("この仕組みは、Windows だけで使えます。")
        return 1
    if not autostart_installed():
        say("自分で始めなおす仕組みは、入っていません。")
        return 0
    code, out = schtasks("/Delete", "/TN", AUTOSTART_TASK, "/F")
    if code != 0:
        say("外せませんでした: %s" % " ".join(out.split())[:300])
        return 1
    say("自分で始めなおす仕組みを外しました(いま動いている見張りは、そのまま動きつづけます)。")
    return 0


def cmd_status(cfg, home):
    """動いていれば 0、止まっていれば 1 を返す。"""
    from fxnews.single import is_running

    running = is_running(home, cfg.data_dir)
    alive = read_alive(cfg)
    say("FX見張り番 %s の状態(%s 日本時間)" % (__version__, jst(None, "%Y-%m-%d %H:%M:%S")))
    say("・%s" % ("動いています" if running else "止まっています"))
    if alive:
        age = max(0, int(time.time() - float(alive.get("ts", 0))))
        say("・最後のしるし: %s(%d秒前。動いているあいだは30秒ごとに書きます)" % (alive.get("jst", "?"), age))
        if running:
            say("・始めた時刻: %s" % alive.get("started", "?"))
            if alive.get("version") and alive.get("version") != __version__:
                say("・★動いているのは前の版(%s)です。止めて始めなおすと、新しい版(%s)になります" % (alive.get("version"), __version__))
            if age > 120:
                say("・★しるしが2分より古いです。固まっているかもしれません。stop で止めてから、もう一度始めてください")
        say("・%s" % alive.get("summary", ""))
        failing = alive.get("failing") or {}
        for name, why in failing.items():
            say("・つながらない情報源: %s(%s)" % (name, str(why)[:80]))
    if os.name == "nt":
        if not autostart_installed():
            say("・自分で始めなおす仕組み: 入っていません(パソコンを立ち上げなおしたら、手で始めます)")
        elif autostart_paused(cfg):
            say("・自分で始めなおす仕組み: 入っていますが、休ませています(stop で止めたため。start で、また動かします)")
        else:
            say("・自分で始めなおす仕組み: 入っています(止まっても、%d分以内に自分で始めます)" % AUTOSTART_EVERY_MIN)
    log_path = cfg.data_dir / "fxnews.log"
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
    except OSError:
        lines = []
    if lines:
        say("")
        say("記録(%s)の最後の %d行:" % (log_path.name, len(lines)))
        for line in lines:
            say("  " + line[:200])
    return 0 if running else 1


def cmd_start(cfg, home, wait=20.0):
    """見張りを、黒い画面なしで始める。始まったら 0。"""
    from fxnews.single import is_running

    try:  # 「止めたままにする」のしるしを片づける
        (cfg.data_dir / AUTOSTART_OFF).unlink()
    except OSError:
        pass
    if is_running(home, cfg.data_dir):
        say("見張り番は、もう動いています。")
        return 0
    how = ""
    if os.name == "nt" and autostart_installed():
        code, out = schtasks("/Run", "/TN", AUTOSTART_TASK)
        how = "タスク スケジューラから" if code == 0 else ""
        if code != 0:
            say("(タスク スケジューラから始められなかったので、じかに始めます: %s)" % " ".join(out.split())[:200])
    end = time.time() + (8.0 if how else 0.0)
    while how and time.time() < end and not is_running(home, cfg.data_dir):
        time.sleep(0.5)
    if not is_running(home, cfg.data_dir):
        exe = spawn_watcher(home)
        how = "じかに(%s)" % exe.name
    say("見張りを始めました(%s)。動きだすのを待っています…" % how)
    end = time.time() + wait
    while time.time() < end:
        if is_running(home, cfg.data_dir):
            alive = read_alive(cfg) or {}
            if alive.get("state") == "running" and time.time() - float(alive.get("ts", 0)) < 60:
                break
        time.sleep(0.5)
    say("")
    return cmd_status(cfg, home)


def cmd_stop(cfg, home, wait=40.0):
    from fxnews.app import STOP_FILE
    from fxnews.single import is_running

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and autostart_installed():
        # 自分で始めなおす仕組みに、「止めたままにする」と伝える(start で、また動かせる)
        (cfg.data_dir / AUTOSTART_OFF).write_text(jst(None, "%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        say("自分で始めなおす仕組みは、休ませました(start を実行するまで、止めたままにします)。")
    if not is_running(home, cfg.data_dir):
        say("見張り番は、いま動いていません。")
        return 0
    (cfg.data_dir / STOP_FILE).write_text(jst(None, "%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    say("止めるように頼みました。止まるのを待っています(長くて%d秒)…" % int(wait))
    end = time.time() + wait
    while time.time() < end:
        time.sleep(0.5)
        if not is_running(home, cfg.data_dir):
            say("止まりました。")
            return 0
    say("まだ止まっていません。少し待ってから、status で見てください。")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run.py", description="FX見張り番 %s" % __version__)
    parser.add_argument("--home", default=None, help="config.ini と data フォルダを置く場所(ふつうは書かない)")
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="見張りを始める(この画面で動かす)")
    p_run.add_argument("--dry", action="store_true", help="スマホには送らず、画面に出すだけにする")
    p_run.add_argument("--auto", action="store_true", help="自分で始めなおす仕組みから呼ぶとき用。もう動いていれば、何もせずに終わる")
    p_run.add_argument("--replay", type=int, default=0, metavar="N", help="お試し: それぞれの更新一覧のいちばん新しい N 件を、新着としてあつかう")
    p_run.add_argument("--seconds", type=float, default=None, help="この秒数で終わる(お試し用)")
    sub.add_parser("check", help="それぞれの情報源につながるかを点検する")
    p_po = sub.add_parser("setup-pushover", help="Pushover の番号を入れて、ためしに1通送る")
    p_po.add_argument("--new-user", action="store_true", help="受け取り番号(User Key)も、入れなおす")
    sub.add_parser("setup-claude", help="Claude の鍵を入れて、つながるか確かめる")
    sub.add_parser("setup-sec", help="米国 SEC に名乗る連絡先を入れる")
    sub.add_parser("topic", help="通知のあて先(ntfy の topic)を作る")
    sub.add_parser("test-notify", help="ためしに通知を1通送る")
    sub.add_parser("start", help="見張りを、黒い画面なしで始める")
    sub.add_parser("status", help="見張りが動いているかを見る")
    sub.add_parser("stop", help="動いている見張りを止める")
    sub.add_parser("autostart-on", help="止まっても自分で始めなおす仕組みを入れる(Windows だけ)")
    sub.add_parser("autostart-off", help="その仕組みを外す")
    sub.add_parser("version", help="版の番号を出す")
    p_rep = sub.add_parser("report", help="型ごとの成績と、分類の答え合わせを表示する")
    p_rep.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)
    cmd = args.cmd or "run"

    home = home_dir(args.home)
    cfg = Config(home)
    if cmd == "run" and getattr(args, "auto", False):
        # 自分で始めなおす仕組みから呼ばれた。もう動いている・止めたままにする、なら、何も残さずに終わる
        from fxnews.single import is_running

        if autostart_paused(cfg) or is_running(home, cfg.data_dir):
            return 0
    setup_output(cfg.data_dir, to_file=(cmd == "run"))
    placed = place_console(cfg, cmd)
    if cmd == "run":
        how = "(黒い画面なし)" if sys.stdout is None else ""
        note_command(cfg, "run: 始めます%s%s" % (how, "(自分で始めなおす仕組みから)" if getattr(args, "auto", False) else ""))
    code = None
    try:
        code = dispatch(args, cmd, home, cfg)
        return code
    finally:
        note_command(cfg, "%s: 終わりの番号 %s%s" % (cmd, "?" if code is None else code, "｜" + placed if placed else ""))


def dispatch(args, cmd, home, cfg):
    """コマンドを1つ動かして、終わりの番号(0 がふつうの終わり)を返す。"""
    if cmd == "version":
        say("FX見張り番 %s" % __version__)
        return 0
    if cmd == "topic":
        return cmd_topic(cfg)
    if cmd == "test-notify":
        return cmd_test_notify(cfg)
    if cmd == "setup-pushover":
        return cmd_setup_pushover(cfg, new_user=getattr(args, "new_user", False))
    if cmd == "setup-claude":
        return cmd_setup_claude(cfg)
    if cmd == "setup-sec":
        return cmd_setup_sec(cfg)
    if cmd == "status":
        return cmd_status(cfg, home)
    if cmd == "start":
        return cmd_start(cfg, home)
    if cmd == "stop":
        return cmd_stop(cfg, home)
    if cmd == "autostart-on":
        return cmd_autostart_on(cfg, home)
    if cmd == "autostart-off":
        return cmd_autostart_off(cfg)

    sources = load_sources(home if (home / "sources.json").exists() else CODE_DIR)
    if cmd == "check":
        from fxnews.check import run_check

        lines = []

        def out(text=""):
            say(text)
            lines.append(text)

        bad = run_check(cfg, sources, out=out)
        try:  # 点検の結果を、あとから見返せるように残す(鍵は、はじめから伏せ字で出している)
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            (cfg.data_dir / "check_last.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
        return 1 if bad else 0
    if cmd == "report":
        from fxnews.report import run_report
        from fxnews.stats import run_kata_report
        from fxnews.store import Store

        store = Store(cfg.data_dir / "fxnews.db")
        try:
            run_kata_report(store, out=say, min_importance=cfg.getint("notify", "min_importance"))
            say("")
            say("――――――――――――――――")
            run_report(store, days=args.days, out=say)
        finally:
            store.close()
        return 0

    from fxnews.app import App
    from fxnews.single import Lock

    dry = getattr(args, "dry", False)
    if not dry and getattr(args, "seconds", None) is None and not getattr(args, "auto", False):
        try:  # 手で見張りを始めた。「止めたままにする」のしるしが残っていたら、片づける
            (cfg.data_dir / AUTOSTART_OFF).unlink()
        except OSError:
            pass
    lock = None
    if not dry:  # お試しモード(スマホに送らない)は、本番が動いていても試せる
        lock = Lock(home, cfg.data_dir)
        if not lock.acquire():
            log.warning("この見張り番は、もう動いています(同じフォルダで、2つ同時には動かしません)。止めるときは stop を実行します")
            return 3
    # 黒い画面なしで動かしているときは、エラーが画面に出ないので、記録のファイルに残す
    import threading

    def record_thread_error(args_):
        log.error("係の1つが、思わぬエラーで止まりました(%s)", args_.thread.name if args_.thread else "?", exc_info=(args_.exc_type, args_.exc_value, args_.exc_traceback))

    threading.excepthook = record_thread_error
    try:
        app = App(cfg, sources, replay=getattr(args, "replay", 0), dry=dry)
        try:
            app.run(seconds=getattr(args, "seconds", None))
        except Exception:
            log.exception("思わぬエラーで止まりました")
            raise
        finally:
            app.close(timeout=3.0)
    finally:
        if lock is not None:
            lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
