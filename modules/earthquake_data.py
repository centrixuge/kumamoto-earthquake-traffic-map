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


def _to_decimal_degrees(raw: str, is_longitude: bool) -> float:
    """
    JMAの座標値を10進度に変換する。通常の続報（例: "震源・震度情報"）は
    "130.7" のような10進度だが、「顕著な地震の震源要素更新のお知らせ」等では
    "13040.7"（130度40.7分）のような度分（DDMM.m）形式で来ることがある。
    整数部の桁数（緯度は2桁、経度は3桁を超えるかどうか）で自動判別する。
    """
    value = float(raw)
    int_len = len(str(int(abs(value))))
    threshold = 4 if is_longitude else 3
    if int_len < threshold:
        return value
    sign = -1.0 if value < 0 else 1.0
    abs_value = abs(value)
    minutes = abs_value % 100
    degrees = (abs_value - minutes) / 100
    return sign * (degrees + minutes / 60.0)


def parse_epicenter(cod: str) -> Tuple[float, float, float]:
    """
    JMAの'cod'フィールド（例: "+32.6+130.7-10000/" や度分形式の
    "+3237.5+13040.7-16000/"）をパースして (lat, lon, depth_m) を返す。
    """
    m = re.match(r"([+-]\d+\.?\d*)([+-]\d+\.?\d*)([+-]\d+)", cod)
    if not m:
        raise ValueError(f"Unexpected cod format: {cod!r}")
    lat_raw, lon_raw, depth = m.groups()
    lat = _to_decimal_degrees(lat_raw, is_longitude=False)
    lon = _to_decimal_degrees(lon_raw, is_longitude=True)
    return lat, lon, float(depth)


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


def _merge_revisions(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    同一eid（地震ID）について複数回配信される続報をeidごとにマージする。

    JMAは大きな地震の後、「顕著な地震の震源要素更新のお知らせ」のような、
    震度情報（maxi/int）を含まない後続レポートを別途配信することがある。
    単純に一覧の先頭（最新）や末尾（最古）の1件だけを採用すると、
    そのレポートには載っていない最大震度などの項目が欠落してしまう。
    そのため配信時刻の古い順に重ね書きし、各フィールドについて
    「それまでに得られた最後の非空の値」を採用する。
    """
    ordered = sorted(events, key=lambda e: e.get("rdt") or e.get("ctt") or "")
    merged: Dict[str, Dict[str, Any]] = {}
    for event in ordered:
        eid = event.get("eid")
        if eid is None:
            continue
        current = merged.setdefault(eid, {})
        for k, v in event.items():
            if v:
                current[k] = v
    return merged


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
    """指定したeidの地震について、震源・震度情報を取得する（複数回の続報をマージ済み）。"""
    if events is None:
        events = fetch_earthquake_list()
    merged = _merge_revisions(events)
    if eid not in merged:
        raise ValueError(f"eid {eid} not found in JMA list.json")
    return _build_quake_info(merged[eid])


def get_significant_events(
    bbox: Tuple[float, float, float, float],
    start_dt: datetime,
    end_dt: datetime,
    min_intensity: Optional[int] = None,
    min_magnitude: Optional[float] = None,
    bbox_margin_deg: float = 1.0,
) -> List[Dict[str, Any]]:
    """
    指定した bbox・期間内で発生した地震（本震＋主要な余震）を取得する。
    `min_intensity`（INTENSITY_ORDERの数値尺度、例: 5=震度5弱）を指定すると
    最大震度で絞り込み、`min_magnitude` を指定するとマグニチュードで絞り込む。
    両方指定した場合は min_intensity を優先する。同一eidの複数続報は
    マージ済みの値を使うため、震度情報を含まない後続レポートに引きずられない。

    Parameters
    ----------
    bbox : (min_lon, min_lat, max_lon, max_lat)
    start_dt, end_dt : タイムゾーンなしの datetime（ローカル時刻=JST想定）
    """
    events = fetch_earthquake_list()
    merged = _merge_revisions(events)
    min_x, min_y, max_x, max_y = bbox

    results = []
    for event in merged.values():
        if min_intensity is not None:
            maxi_num = intensity_to_numeric(event.get("maxi"))
            if maxi_num is None or maxi_num < min_intensity:
                continue
        elif min_magnitude is not None:
            mag_raw = event.get("mag")
            if mag_raw in (None, ""):
                continue
            try:
                mag = float(mag_raw)
            except ValueError:
                continue
            if mag < min_magnitude:
                continue
        else:
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

        results.append(_build_quake_info(event))

    results.sort(key=lambda e: e["occurred_at"])
    return results
