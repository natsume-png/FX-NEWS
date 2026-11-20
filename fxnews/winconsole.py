"""Windows の黒い画面(コマンドの画面)を、決めた場所と大きさに置く。

ほかのアプリの画面を黒い画面でかくしたくないパソコンのための仕組み。
config.ini の [general] console_rect に「左,上,幅,高さ」(画面の点の数)を書いたときだけ動く。

Windows 11 では、黒い画面は「ターミナル」という別のアプリの中に開くことがある。
そのため、いったん目印の題名をつけて、その題名を持つ「見えている窓」を探してから動かす。
動かしたあとに場所を読み直して、本当に動いたときだけ「動いた」と答える。
"""

import os
import random
import time


def parse_rect(text):
    """「左,上,幅,高さ」を数字4つにする。書き方がちがえば None。"""
    try:
        parts = [int(float(p)) for p in str(text).replace(" ", "").split(",")]
    except ValueError:
        return None
    if len(parts) != 4 or parts[2] < 200 or parts[3] < 120:
        return None
    return tuple(parts)


def place(rect, title=None, wait=2.0):
    """黒い画面を rect(左, 上, 幅, 高さ)に動かす。戻り値は (動いたか, 説明)。"""
    if os.name != "nt":
        return False, "Windows ではないので、何もしません"
    if not rect:
        return False, "場所の指定がありません"
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetConsoleWindow():
            return False, "黒い画面がありません"
        try:
            user32.SetProcessDPIAware()  # 場所を、画面の本当の点の数で数える
        except Exception:
            pass

        def rect_of(hwnd):
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            return int(r.left), int(r.top), int(r.right), int(r.bottom)

        def visible_windows():
            found = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
            def callback(hwnd, _lparam):
                try:
                    if user32.IsWindowVisible(hwnd):
                        n = user32.GetWindowTextLengthW(hwnd)
                        if n:
                            buf = ctypes.create_unicode_buffer(n + 1)
                            user32.GetWindowTextW(hwnd, buf, n + 1)
                            found.append((hwnd, buf.value))
                except Exception:
                    pass
                return True

            user32.EnumWindows(callback, 0)
            return found

        old = ctypes.create_unicode_buffer(1024)
        kernel32.GetConsoleTitleW(old, 1024)
        tag = "FXNEWS_%06d" % random.randint(0, 999999)
        hwnd = 0
        try:
            kernel32.SetConsoleTitleW(tag)
            end = time.time() + wait
            while time.time() < end and not hwnd:
                time.sleep(0.2)
                for h, text in visible_windows():
                    if tag in (text or ""):
                        left, top, right, bottom = rect_of(h)
                        if right - left >= 120 and bottom - top >= 80:
                            hwnd = h
                            break
        finally:
            kernel32.SetConsoleTitleW(title or old.value or "FX-NEWS")
        if not hwnd:
            return False, "自分の黒い画面が見つかりませんでした(動かせていません)"
        x, y, w, h = rect
        if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # 最大化・最小化されていたら、ふつうの大きさに戻す
            time.sleep(0.15)
        user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h), 0x0004 | 0x0010)  # 重なりの順番と、手前かどうかは変えない
        time.sleep(0.3)
        left, top, right, bottom = rect_of(hwnd)
        if abs(left - x) <= 24 and abs(top - y) <= 40:
            return True, "黒い画面を 左%d 上%d 幅%d 高さ%d に置きました" % (left, top, right - left, bottom - top)
        return False, "黒い画面を動かせませんでした(いまの場所: 左%d 上%d)" % (left, top)
    except Exception as exc:  # 画面の置き場所は、本体の仕事より大事ではないので、失敗しても止めない
        return False, "黒い画面を動かせませんでした(%s)" % exc
