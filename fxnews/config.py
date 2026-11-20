"""設定の読み込み。

- config.ini … 鍵(かぎ)や通知のあて先など、人に見せないもの。GitHub には置かない。
- sources.json … どこを見張るかの一覧。GitHub に置いてよい。
"""

import configparser
import json
import os
import re
import secrets
import shutil
from pathlib import Path

DEFAULTS = {
    "claude": {
        "api_key": "",
        "model": "claude-haiku-5-5",
        "effort": "low",
        "thinking": "off",
        "monthly_cap_yen": "2000",
        "api_base": "https://api.anthropic.com",
        "api_version": "2026-10-21",
        "workspace_id": "",
        "price_in_usd_per_mtok": "0.10",
        "price_out_usd_per_mtok": "0.50",
    },
    "notify": {
        "service": "ntfy",
        "min_importance": "3",
        "spike_fast": "yes",
    },
    "ntfy": {
        "server": "https://ntfy.sh",
        "topic": "",
        "access_token": "",
        "daily_limit": "240",
    },
    "pushover": {
        "user_key": "",
        "api_token": "",
        "device": "",
        "sound_news": "incoming",
        "sound_important": "bugle",
        "sound_spike": "spacealarm",
        "sound_system": "mechanical",
        "daily_limit": "200",
        "monthly_limit": "5000",
        "reserve": "1500",
        "api_base": "https://api.pushover.net",
    },
    "x": {
        "bearer_token": "",
        "monthly_cap_yen": "5000",
        "daily_cap_posts": "300",
        "price_per_post_usd": "0.005",
        "api_base": "https://api.x.com",
    },
    "sec": {
        "contact": "",
        "base": "https://www.sec.gov",
    },
    "rates": {
        "url": "https://forex-api.coin.z.com/public/v1/ticker",
        "interval_sec": "2",
        "threshold_1m": "0.08",
        "threshold_5m": "0.15",
        "threshold_15m": "0.30",
        "cooldown_min": "5",
    },
    "general": {
        "usd_jpy": "160",
        "sleep_block": "yes",
        "heartbeat_hour": "8",
        "max_item_age_hours": "6",
        "workers": "3",
        "console_rect": "",
    },
}

# 前の版(v0.1)では [ntfy] に書いていた項目。古い config.ini に書いてあれば、そちらも読む。
LEGACY = {
    ("notify", "min_importance"): ("ntfy", "min_importance"),
    ("notify", "spike_fast"): ("ntfy", "spike_fast"),
}

_TRUE = ("1", "yes", "true", "on", "はい")


class Config:
    """config.ini の中身。書かれていない項目は DEFAULTS の値になる。"""

    def __init__(self, base_dir, path=None):
        self.base_dir = Path(base_dir)
        self.path = Path(path) if path else self.base_dir / "config.ini"
        self.data_dir = self.base_dir / "data"
        self._cp = configparser.ConfigParser(interpolation=None)  # config.ini に書いてあるものだけ
        self.loaded = False
        if self.path.exists():
            self._read()

    def _read(self):
        raw = self.path.read_bytes()
        text = None
        for enc in ("utf-8-sig", "cp932"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw.decode("utf-8", errors="replace")
        self._cp.read_string(text)
        self.loaded = True

    def _explicit(self, section, key):
        """環境変数か config.ini に書いてあれば、その値。書いてなければ None。"""
        env = os.environ.get("FXNEWS_%s_%s" % (section.upper(), key.upper()))
        if env is not None:
            return env.strip()
        if self._cp.has_option(section, key):
            return self._cp.get(section, key).strip()
        return None

    def get(self, section, key):
        value = self._explicit(section, key)
        if value is None and (section, key) in LEGACY:
            value = self._explicit(*LEGACY[(section, key)])
        if value is None:
            value = DEFAULTS.get(section, {}).get(key, "")
        return value

    def getint(self, section, key):
        try:
            return int(float(self.get(section, key)))
        except ValueError:
            return int(float(DEFAULTS[section][key]))

    def getfloat(self, section, key):
        try:
            return float(self.get(section, key))
        except ValueError:
            return float(DEFAULTS[section][key])

    def getbool(self, section, key):
        return self.get(section, key).lower() in _TRUE


def load_sources(base_dir, path=None):
    p = Path(path) if path else Path(base_dir) / "sources.json"
    with open(p, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def ensure_config_file(base_dir, example_dir=None):
    """config.ini がなければ、見本(config.example.ini)から作る。作ったら True。

    見本は example_dir(指定がなければ base_dir)から探す。
    """
    base = Path(base_dir)
    target = base / "config.ini"
    if target.exists():
        return False
    base.mkdir(parents=True, exist_ok=True)
    example = Path(example_dir or base) / "config.example.ini"
    if example.exists():
        shutil.copyfile(str(example), str(target))
    else:
        target.write_text("", encoding="utf-8")
    return True


def write_value(path, section, key, value):
    """config.ini の1項目だけを書きかえる(説明のコメントは残す)。"""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    lines = text.splitlines()
    sec_re = re.compile(r"^\s*\[(.+?)\]\s*$")
    key_re = re.compile(r"^\s*%s\s*[=:]" % re.escape(key))
    in_sec = False
    sec_line = None
    for i, line in enumerate(lines):
        m = sec_re.match(line)
        if m:
            in_sec = m.group(1).strip().lower() == section.lower()
            if in_sec:
                sec_line = i
            continue
        if in_sec and key_re.match(line):
            lines[i] = "%s = %s" % (key, value)
            break
    else:
        if sec_line is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("[%s]" % section)
            lines.append("%s = %s" % (key, value))
        else:
            lines.insert(sec_line + 1, "%s = %s" % (key, value))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_TOPIC_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # まちがえやすい i l o 0 1 は使わない


def new_topic():
    """ntfy のあて先の名前を作る。人に当てられないよう、でたらめな16文字を入れる。"""
    groups = ["".join(secrets.choice(_TOPIC_ALPHABET) for _ in range(4)) for _ in range(4)]
    return "fx-" + "-".join(groups)
