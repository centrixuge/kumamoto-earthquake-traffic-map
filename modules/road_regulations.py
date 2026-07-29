"""
熊本県の「防災情報くまもと」が公開している道路通行規制情報を取得し、
OSRMの公開ルーティングAPIで実際の道路網に沿った経路にスナップするモジュール。

出典: https://portal.bousai.pref.kumamoto.jp/ （認証不要の公開JSONエンドポイント）
道路スナップ: https://router.project-osrm.org/ （OSRMの公開デモサーバー、認証不要）
"""
import time
from typing import Any, Dict, List, Optional

import requests

TRAFFIC_JSON_URL = "https://portal.bousai.pref.kumamoto.jp/data/traffic/traffic.json"
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/{lng1},{lat1};{lng2},{lat2}"


def fetch_regulations(timeout: int = 30) -> List[Dict[str, Any]]:
    """「防災情報くまもと」の通行規制情報ページのデータから一覧を取得する。"""
    resp = requests.get(
        TRAFFIC_JSON_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def snap_to_road(
    lat1: float, lng1: float, lat2: float, lng2: float, timeout: int = 15
) -> Optional[List[List[float]]]:
    """
    OSRM公開デモサーバーで2点間の道路に沿った経路（[lat, lon]の配列）を取得する。
    失敗した場合や始点・終点が同一の場合はNoneを返す（呼び出し側で直線にフォールバックする）。
    """
    if lat1 == lat2 and lng1 == lng2:
        return None
    url = OSRM_ROUTE_URL.format(lng1=lng1, lat1=lat1, lng2=lng2, lat2=lat2)
    try:
        resp = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson"},
            headers={"User-Agent": "kumamoto-earthquake-traffic-map (research use)"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        coords = data["routes"][0]["geometry"]["coordinates"]  # [[lng, lat], ...]
        return [[lat, lng] for lng, lat in coords]
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
        return None


def build_regulation_paths(
    items: List[Dict[str, Any]], request_interval: float = 0.3
) -> List[Dict[str, Any]]:
    """
    各規制エントリに道路スナップ済みの経路を付与したリストを作る。
    OSRMの公開デモサーバーへの配慮として、リクエスト間に短い間隔を空ける。
    """
    results = []
    for item in items:
        lat1, lng1 = item["regStartPointLat"], item["regStartPointLng"]
        lat2, lng2 = item["regEndPointLat"], item["regEndPointLng"]
        path = snap_to_road(lat1, lng1, lat2, lng2)
        if path is None:
            path = [[lat1, lng1], [lat2, lng2]]
        results.append({
            "region": item.get("regionalPromotion"),
            "route_name": item.get("routeName"),
            "reason_type": item.get("regType"),
            "reason_detail": item.get("regReason") or None,
            "content": item.get("regContent0"),
            "start_timestamp": item.get("regStartTimestamp"),
            "end_timestamp": item.get("regEndTimestamp") or item.get("regPlanedEndTimestamp"),
            "length_km": item.get("regLength"),
            "path": path,
        })
        time.sleep(request_interval)
    return results
