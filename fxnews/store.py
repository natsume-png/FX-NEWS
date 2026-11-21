"""記録の保存(SQLite。Python に最初から入っているデータベース)。

- 一度あつかった記事の目印(二重に知らせないため)
- 知らせた内容と、そのあとの値動き(答え合わせ用)
- ニュースの型と向き、本文(型ごとに「そのあとドル円がどう動いたか」を数える用)
- 1分ごとの為替の値(ニュースの何分後・何日後の値でも、あとから引けるようにする)
- 使った量(Claude の料金、X の件数、通知の数)
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS source_state(source TEXT PRIMARY KEY, initialized INTEGER DEFAULT 0, updated REAL);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, source TEXT, source_name TEXT, kind TEXT, key TEXT,
  title TEXT, url TEXT, published REAL,
  title_ja TEXT, analysis TEXT, importance INTEGER,
  notified INTEGER DEFAULT 0, delay_sec REAL,
  mids TEXT, out5 TEXT, out15 TEXT, out60 TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_ts ON items(ts);
CREATE TABLE IF NOT EXISTS usage(period TEXT, kind TEXT, amount REAL, PRIMARY KEY(period, kind));
CREATE TABLE IF NOT EXISTS rate_min(
  ts INTEGER NOT NULL, pair TEXT NOT NULL, o REAL, h REAL, l REAL, c REAL, n INTEGER,
  PRIMARY KEY(pair, ts)
) WITHOUT ROWID;
"""
# rate_min は、1分ごとの値。ts はその1分のはじめの時刻(UNIX 秒)。o・h・l・c は、始めの値・いちばん高い値・
# いちばん安い値・終わりの値(売値と買値の真ん中)。n は、その1分に値を取った回数。

# あとから足した列(前の版で作った記録にも、起動のときに足す)
ITEM_COLUMNS_ADDED = (
    ("text", "TEXT"),  # 本文(型の決め方を変えたときに、分け直せるように残す)
    ("kata", "TEXT"),  # 型(例: us_data = 米国の経済指標)
    ("muki", "TEXT"),  # 向き(例: strong = 予想より強い)
    ("usdjpy", "TEXT"),  # Claude の見立て(up / down / flat)
    ("dup", "INTEGER"),  # 前に出た話のくり返しなら 1(数えるときに、二重に数えないため)
)

OUTCOME_STEPS = (("out5", 300), ("out15", 900), ("out60", 3600))


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            have = {r["name"] for r in self._db.execute("PRAGMA table_info(items)").fetchall()}
            for name, kind in ITEM_COLUMNS_ADDED:
                if name not in have:
                    self._db.execute("ALTER TABLE items ADD COLUMN %s %s" % (name, kind))
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_items_kata ON items(kata, muki)")
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    # ---- 一度あつかったものの目印 ----
    def mark_seen(self, key):
        """はじめて見る目印なら記録して True。前に見たものなら False。"""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO seen(key, ts) VALUES(?, ?)", (key, time.time()))
            self._db.commit()
            return cur.rowcount == 1

    def is_seen(self, key):
        with self._lock:
            row = self._db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone()
            return row is not None

    def is_initialized(self, source):
        with self._lock:
            row = self._db.execute("SELECT initialized FROM source_state WHERE source=?", (source,)).fetchone()
            return bool(row and row["initialized"])

    def set_initialized(self, source):
        with self._lock:
            self._db.execute(
                "INSERT INTO source_state(source, initialized, updated) VALUES(?, 1, ?) "
                "ON CONFLICT(source) DO UPDATE SET initialized=1, updated=excluded.updated",
                (source, time.time()),
            )
            self._db.commit()

    # ---- あつかった情報の記録 ----
    def add_item(self, item, analysis=None, importance=None, title_ja="", mids=None):
        """あつかった情報を1件、記録する。

        ts は、このプログラムが受け取った時刻(= 利用者の手もとに届く時刻のめやす)。
        あとから「届いたあと、相場がどう動いたか」を数えるときの起点にする。
        本文(text)も残す。型の決め方を変えて、分け直せるようにするため。
        """
        kata = muki = usdjpy = None
        dup = 0
        if isinstance(analysis, dict):
            kata, muki, usdjpy = analysis.get("kata"), analysis.get("muki"), analysis.get("usdjpy")
            dup = 1 if analysis.get("dup") else 0
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO items(ts, source, source_name, kind, key, title, url, published, title_ja, analysis, "
                "importance, mids, text, kata, muki, usdjpy, dup) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    item.source,
                    item.source_name,
                    item.kind,
                    item.key,
                    item.title,
                    item.url,
                    item.published,
                    title_ja,
                    json.dumps(analysis, ensure_ascii=False) if analysis is not None else None,
                    importance,
                    json.dumps(mids) if mids else None,
                    (getattr(item, "text", "") or "")[:6000] or None,
                    kata,
                    muki,
                    usdjpy,
                    dup,
                ),
            )
            self._db.commit()
            return cur.lastrowid

    def set_notified(self, item_id, ok, delay_sec=None):
        with self._lock:
            self._db.execute(
                "UPDATE items SET notified=?, delay_sec=? WHERE id=?", (1 if ok else 0, delay_sec, item_id)
            )
            self._db.commit()

    def recent_items(self, since_ts, limit=12, notified_only=False, exclude_kind=None):
        sql = "SELECT * FROM items WHERE ts>=?"
        args = [since_ts]
        if notified_only:
            sql += " AND notified=1"
        if exclude_kind:
            sql += " AND kind<>?"
            args.append(exclude_kind)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def count_items(self, since_ts, notified_only=False):
        sql = "SELECT COUNT(*) AS n FROM items WHERE ts>=?"
        if notified_only:
            sql += " AND notified=1"
        with self._lock:
            return self._db.execute(sql, (since_ts,)).fetchone()["n"]

    # ---- 答え合わせ ----
    def pending_outcomes(self, now, give_up_sec=4 * 3600):
        """通知してから 5分・15分・60分たったのに、まだ値動きを記録していないもの。"""
        out = []
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, mids, out5, out15, out60 FROM items "
                "WHERE notified=1 AND mids IS NOT NULL AND out60 IS NULL AND ts>=?",
                (now - give_up_sec,),
            ).fetchall()
        for r in rows:
            for col, sec in OUTCOME_STEPS:
                if r[col] is None and now - r["ts"] >= sec:
                    out.append((r["id"], col, r["mids"]))
        return out

    def set_outcome(self, item_id, col, value):
        if col not in ("out5", "out15", "out60"):
            raise ValueError(col)
        with self._lock:
            self._db.execute("UPDATE items SET %s=? WHERE id=?" % col, (json.dumps(value), item_id))
            self._db.commit()

    def outcome_rows(self, since_ts):
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, kind, title_ja, analysis, out5, out15, out60 FROM items "
                "WHERE notified=1 AND analysis IS NOT NULL AND ts>=? ORDER BY ts",
                (since_ts,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- 1分ごとの値(あとから、何分後・何日後の値でも引けるようにする) ----
    def add_rate_bars(self, rows):
        """rows は (その1分のはじめの時刻, 通貨ペア, 始めの値, 高い値, 安い値, 終わりの値, 回数) の並び。

        同じ1分の記録がもうあるとき(その1分の途中で、見張りを止めて始めなおしたとき)は、1つにまとめる:
        始めの値は前のまま、高い値・安い値は広いほう、終わりの値は新しいほう、回数は足す。
        """
        if not rows:
            return
        with self._lock:
            self._db.executemany(
                "INSERT INTO rate_min(ts, pair, o, h, l, c, n) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(pair, ts) DO UPDATE SET h=MAX(h, excluded.h), l=MIN(l, excluded.l), "
                "c=excluded.c, n=n+excluded.n",
                rows,
            )
            self._db.commit()

    def rate_at(self, pair, ts, max_gap=300):
        """ts の時点の値。なければ None(相場が閉まっている時間や、見張りが止まっていた時間)。

        - ts をふくむ1分の記録があれば、その1分の前半なら始めの値、後半なら終わりの値(ずれは長くて30秒)。
        - なければ、前後 max_gap 秒までで、時刻がいちばん近い記録の値(前の1分の終わりの値か、後の1分の始めの値)。
        """
        start = int(ts // 60) * 60
        with self._lock:
            row = self._db.execute("SELECT o, c FROM rate_min WHERE pair=? AND ts=?", (pair, start)).fetchone()
            if row is not None:
                return row["o"] if ts - start < 30 else row["c"]
            nxt = self._db.execute(
                "SELECT ts, o FROM rate_min WHERE pair=? AND ts>? AND ts<=? ORDER BY ts LIMIT 1",
                (pair, start, int(ts + max_gap)),
            ).fetchone()
            prv = self._db.execute(
                "SELECT ts, c FROM rate_min WHERE pair=? AND ts<? AND ts>=? ORDER BY ts DESC LIMIT 1",
                (pair, start, int(ts - max_gap) - 60),
            ).fetchone()
        gap_next = nxt["ts"] - ts if nxt is not None else None
        gap_prev = ts - (prv["ts"] + 60) if prv is not None else None
        if gap_prev is not None and gap_prev > max_gap:
            gap_prev = None
        if gap_next is None and gap_prev is None:
            return None
        if gap_next is None or (gap_prev is not None and gap_prev <= gap_next):
            return prv["c"]
        return nxt["o"]

    def rate_bars(self, pair, since_ts, until_ts):
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, o, h, l, c, n FROM rate_min WHERE pair=? AND ts>=? AND ts<=? ORDER BY ts",
                (pair, int(since_ts), int(until_ts)),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_rate_bars(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) AS n FROM rate_min").fetchone()["n"]

    def kata_rows(self, since_ts, kata=None, muki=None, min_importance=0):
        """型のついた記録(受け取った時刻と、そのときの値)。古い順。前に出た話のくり返しは入れない。

        kata と muki を指定すると、その型・その向きだけ。
        """
        sql = (
            "SELECT id, ts, published, source, kind, title_ja, importance, kata, muki, usdjpy, mids FROM items "
            "WHERE kata IS NOT NULL AND mids IS NOT NULL AND COALESCE(dup, 0)=0 AND ts>=? AND COALESCE(importance, 0)>=?"
        )
        args = [since_ts, min_importance]
        if kata is not None:
            sql += " AND kata=?"
            args.append(kata)
        if muki is not None:
            sql += " AND muki=?"
            args.append(muki)
        sql += " ORDER BY ts"
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def count_kata_items(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) AS n FROM items WHERE kata IS NOT NULL").fetchone()["n"]

    # ---- 使った量 ----
    def usage_add(self, period, kind, amount):
        with self._lock:
            self._db.execute(
                "INSERT INTO usage(period, kind, amount) VALUES(?,?,?) "
                "ON CONFLICT(period, kind) DO UPDATE SET amount=amount+excluded.amount",
                (period, kind, amount),
            )
            self._db.commit()

    def usage_get(self, period, kind):
        with self._lock:
            row = self._db.execute("SELECT amount FROM usage WHERE period=? AND kind=?", (period, kind)).fetchone()
            return float(row["amount"]) if row else 0.0

    # ---- 古い記録の片づけ ----
    def prune(self, seen_days=400, item_days=800, rate_days=800):
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM seen WHERE ts<?", (now - seen_days * 86400,))
            self._db.execute("DELETE FROM items WHERE ts<?", (now - item_days * 86400,))
            self._db.execute("DELETE FROM rate_min WHERE ts<?", (int(now - rate_days * 86400),))
            self._db.commit()
