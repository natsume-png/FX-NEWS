"""プログラムの中を流れる「1件の情報」の形。"""

import time
from dataclasses import dataclass, field
from typing import Optional

KIND_JA = {
    "official": "公式発表",
    "news": "ニュース",
    "vip": "要人の投稿",
    "x": "Xの投稿",
    "edgar": "決算などの届け出",
    "spike": "急騰急落",
}


@dataclass
class Item:
    source: str  # 情報源のID(例: fed_press)
    source_name: str  # 情報源の日本語の名前
    kind: str  # official / news / vip / x / edgar / spike
    key: str  # 同じものを二度あつかわないための目印
    title: str
    url: str = ""
    text: str = ""
    published: Optional[float] = None  # 発表の時刻(UNIX 秒)。分からなければ None
    fetched: float = field(default_factory=time.time)  # このプログラムが受け取った時刻
    extra: dict = field(default_factory=dict)
