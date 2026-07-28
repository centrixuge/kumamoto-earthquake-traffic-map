"""
気象庁が公開している地震情報JSON（防災情報ページの裏で使われている非公式フィード）から
震源・マグニチュード・震度情報を取得するモジュール。

出典: https://www.jma.go.jp/bosai/quake/data/list.json
※ 正式なAPI仕様として公開されているものではなく、気象庁ウェブサイトが配信している
   公開JSONを利用している（多くの防災系アプリ・サイトで実利用されている形式）。
"""
import math
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

JMA_LIST_URL = "https://www.jma.go.jp/bosai/quake/data/list.json"

# JMA震度（10段階）を数値の順序尺度に変換するための対応表
INTENSITY_ORDER = {
    "1": 1, "2": 2, "3": 3, "4": 4,
    "5-": 5, "5+": 6, "6-": 7, "6+": 8, "7": 9,
}


def fetch_earthquake_list(timeout: int = 30) -> List[Dict[str, Any]]:
    """JMAの地震一覧（直近1か月分）を取得する。"""
    resp = requests.get(
        JMA_LIST_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def parse_epicenter(cod: str) -> Tuple[float, float, float]:
    """
    JMAの'cod'フィールド（例: "+32.6+130.7-10000/"）をパースして
    (lat, lon, depth_m) を返す。
    """
    m = re.match(r"([+-]\d+\.?\d*)([+-]\d+\.?\d*)([+-]\d+)", cod)
    if not m:
        raise ValueError(f"Unexpected cod format: {cod!r}")
    lat, lon, depth = m.groups()
    return float(lat), float(lon), float(depth)


def intensity_to_numeric(maxi: Optional[str]) -> Optional[int]:
    """震度表記（"5-", "6+", "7" など）を数値順序に変換する。"""
    if maxi is None:
        return None
    return INTENSITY_ORDER.get(maxi)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の大圏距離[km]を返す。"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _build_quake_info(event: Dict[str, Any]) -> Dict[str, Any]:
    lat, lon, depth_m = parse_epicenter(event["cod"])

    municipalities = []
    for pref in event.get("int", []):
        for city in pref.get("city", []):
            municipalities.append({
                "code": city.get("code"),
                "maxi": city.get("maxi"),
                "maxi_numeric": intensity_to_numeric(city.get("maxi")),
            })

    mag_raw = event.get("mag")
    magnitude = float(mag_raw) if mag_raw not in (None, "") else None

    return {
        "eid": event.get("eid"),
        "occurred_at": event.get("at"),
        "epicenter_name": event.get("anm"),
        "epicenter_lat": lat,
        "epicenter_lon": lon,
        "depth_km": abs(depth_m) / 1000.0,
        "magnitude": magnitude,
        "max_intensity": event.get("maxi"),
        "max_intensity_numeric": intensity_to_numeric(event.get("maxi")),
        "municipalities": municipalities,
    }


def get_quake_info(eid: str, events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """指定したeidの地震について、震源・震度情報を取得する。"""
    if events is None:
        events = fetch_earthquake_list()
    for event in events:
        if event.get("eid") == eid:
            return _build_quake_info(event)
    raise ValueError(f"eid {eid} not found in JMA list.json")


def get_significant_events(
    bbox: Tuple[float, float, float, float],
    start_dt: datetime,
    end_dt: datetime,
    min_magnitude: float = 4.0,
    bbox_margin_deg: float = 1.0,
) -> List[Dict[str, Any]]:
    """
    指定した bbox・期間内で発生した、マグニチュード min_magnitude 以上の地震
    （本震＋主要な余震）を取得する。同一eidが複数回更新されている場合は
    最初に見つかったものを採用する。

    Parameters
    ----------
    bbox : (min_lon, min_lat, max_lon, max_lat)
    start_dt, end_dt : タイムゾーンなしの datetime（ローカル時刻=JST想定）
    """
    events = fetch_earthquake_list()
    min_x, min_y, max_x, max_y = bbox

    seen_eids = set()
    results = []
    for event in events:
        eid = event.get("eid")
        if eid is None or eid in seen_eids:
            continue

        mag_raw = event.get("mag")
        if mag_raw in (None, ""):
            continue
        try:
            mag = float(mag_raw)
        except ValueError:
            continue
        if mag < min_magnitude:
            continue

        at_raw = event.get("at")
        if not at_raw:
            continue
        try:
            at_dt = datetime.fromisoformat(at_raw).replace(tzinfo=None)
        except ValueError:
            continue
        if not (start_dt <= at_dt <= end_dt):
            continue

        try:
            lat, lon, _ = parse_epicenter(event.get("cod", ""))
        except ValueError:
            continue
        if not (min_x - bbox_margin_deg <= lon <= max_x + bbox_margin_deg
                and min_y - bbox_margin_deg <= lat <= max_y + bbox_margin_deg):
            continue

        seen_eids.add(eid)
        results.append(_build_quake_info(event))

    results.sort(key=lambda e: e["occurred_at"])
    return results
