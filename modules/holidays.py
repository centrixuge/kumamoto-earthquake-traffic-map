"""
日本の国民の祝日・休日の一覧を取得するモジュール。

出典: 内閣府「国民の祝日について」が公開しているCSV
      https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv
（政府の一次情報。文字コードはShift_JIS、"YYYY/M/D,名称" の形式）

取得できたらローカルにキャッシュし、次回以降は取得に失敗しても
キャッシュで動作を継続できるようにする（定期実行が外部サイトの
一時的な不調で祝日を取りこぼさないようにするため）。
"""
import csv
import io
import json
import os
from datetime import date
from typing import Dict, Set

import requests

HOLIDAY_CSV_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"


def fetch_holidays(cache_path: str = None, timeout: int = 30) -> Dict[str, str]:
    """
    {"2026-07-20": "海の日", ...} という辞書を返す。
    取得に失敗した場合はキャッシュを読む。どちらも駄目なら空辞書を返す。
    """
    try:
        resp = requests.get(HOLIDAY_CSV_URL, timeout=timeout)
        resp.raise_for_status()
        text = resp.content.decode("cp932")
        holidays: Dict[str, str] = {}
        reader = csv.reader(io.StringIO(text))
        next(reader, None)  # ヘッダ行
        for row in reader:
            if len(row) < 2 or not row[0].strip():
                continue
            try:
                y, m, d = (int(v) for v in row[0].strip().split("/"))
            except ValueError:
                continue
            holidays[date(y, m, d).isoformat()] = row[1].strip()
        if holidays and cache_path:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(holidays, f, ensure_ascii=False, indent=2)
        return holidays
    except Exception as e:  # noqa: BLE001 - 祝日取得の失敗で全体を止めない
        print(f"[holidays] fetch failed ({e}); falling back to cache", flush=True)
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
        print("[holidays] no cache available; holidays will NOT be excluded", flush=True)
        return {}


def holiday_dates(holidays: Dict[str, str]) -> Set[date]:
    return {date.fromisoformat(k) for k in holidays}
