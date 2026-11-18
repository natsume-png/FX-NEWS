"""記録の保存(SQLite。Python に最初から入っているデータベース)。

- 一度あつかった記事の目印(二重に知らせないため)
- 知らせた内容と、そのあとの値動き(答え合わせ用)
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
"""

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
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO items(ts, source, source_name, kind, key, title, url, published, title_ja, analysis, "
                "importance, mids) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
    def prune(self, seen_days=400, item_days=400):
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM seen WHERE ts<?", (now - seen_days * 86400,))
            self._db.execute("DELETE FROM items WHERE ts<?", (now - item_days * 86400,))
            self._db.commit()
