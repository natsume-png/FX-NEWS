"""同じフォルダの見張り番を、2つ同時に動かさないための鍵。

- Windows: 名前つきの鍵(ミューテックス)。プログラムが終われば、Windows が自動で返す。
- Mac・Linux: data フォルダの中の鍵のファイル(fxnews.lock)。プログラムが終われば、自動ではずれる。

鍵を持っているプログラムがいるかどうかは、status コマンドや stop コマンドも見る。
"""

import os

from .util import short_hash

_ERROR_ALREADY_EXISTS = 183


class Lock:
    """鍵。acquire() で取れたら True。release() で返す。"""

    def __init__(self, home_dir, data_dir):
        self.name = "Local\\fxnews_%s" % short_hash(os.path.normcase(os.path.abspath(str(home_dir))))
        self.path = os.path.join(str(data_dir), "fxnews.lock")
        self._handle = None
        self._file = None

    def acquire(self):
        if os.name == "nt":
            return self._acquire_windows()
        return self._acquire_posix()

    def release(self):
        if self._handle is not None:
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def _acquire_windows(self):
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.CreateMutexW(None, False, self.name)
        err = ctypes.get_last_error()
        if not handle:
            return True  # 鍵の仕組みが使えないときは、止めずに動かす
        if err == _ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def _acquire_posix(self):
        import fcntl

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._file = fh
        return True


def is_running(home_dir, data_dir):
    """同じフォルダの見張り番が、いま動いているか。"""
    lock = Lock(home_dir, data_dir)
    if lock.acquire():
        lock.release()
        return False
    return True
