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


def regulation_key(reg: Dict[str, Any]) -> str:
    """
    1件の規制を一意に識別するキー。規制内容（全面通行止め→片側交互→解除）や
    終了日時は途中で変わるため、変化しない項目（路線・区間の座標・開始日時）だけで作る。
    """
    return "|".join([
        str(reg.get("route_name") or ""),
        str(reg.get("region") or ""),
        f"{float(reg.get('start_lat') or 0):.6f}",
        f"{float(reg.get('start_lon') or 0):.6f}",
        f"{float(reg.get('end_lat') or 0):.6f}",
        f"{float(reg.get('end_lon') or 0):.6f}",
        str(reg.get("start_timestamp") or ""),
    ])


def build_regulation_paths(
    items: List[Dict[str, Any]],
    request_interval: float = 0.3,
    known_paths: Dict[str, List[List[float]]] = None,
) -> List[Dict[str, Any]]:
    """
    各規制エントリに道路スナップ済みの経路を付与したリストを作る。

    `known_paths`（キー -> 経路）を渡すと、既にスナップ済みの区間はOSRMに
    問い合わせない。定期実行のたびに全件を公開デモサーバーへ投げ直さないための措置。
    """
    known_paths = known_paths or {}
    results = []
    for item in items:
        lat1, lng1 = item["regStartPointLat"], item["regStartPointLng"]
        lat2, lng2 = item["regEndPointLat"], item["regEndPointLng"]
        reg = {
            "region": item.get("regionalPromotion"),
            "route_name": item.get("routeName"),
            "reason_type": item.get("regType"),
            "reason_detail": item.get("regReason") or None,
            "content": item.get("regContent0"),
            "start_timestamp": item.get("regStartTimestamp"),
            "end_timestamp": item.get("regEndTimestamp") or item.get("regPlanedEndTimestamp"),
            "length_km": item.get("regLength"),
            "start_lat": lat1, "start_lon": lng1,
            "end_lat": lat2, "end_lon": lng2,
        }
        key = regulation_key(reg)
        path = known_paths.get(key)
        if path is None:
            path = snap_to_road(lat1, lng1, lat2, lng2)
            if path is None:
                path = [[lat1, lng1], [lat2, lng2]]
            time.sleep(request_interval)
        reg["path"] = path
        results.append(reg)
    return results


# 状態が変わったかどうかを見る項目（これらが変わったら履歴に1行足す）
_MUTABLE_FIELDS = ("content", "end_timestamp", "reason_type", "reason_detail", "length_km")


def merge_regulations_archive(
    archive: Dict[str, Any], current: List[Dict[str, Any]], observed_at: str
) -> Dict[str, Any]:
    """
    ポータルの現在のスナップショットを、追記専用のアーカイブにマージする。

    規制は解除されるとポータルの一覧から消えてしまい、後から取得できない。
    そのためアーカイブからは決して削除せず、
      - 初めて見た日時（first_seen）と最後に見た日時（last_seen）
      - まだ一覧に載っているか（still_listed）
      - 規制内容・終了日時が変わった履歴（history）
    を持たせて、消えた後も「いつからいつまで、どういう規制だったか」を追えるようにする。
    """
    items = dict(archive.get("items") or {})
    current_keys = set()

    for reg in current:
        key = regulation_key(reg)
        current_keys.add(key)
        state = {f: reg.get(f) for f in _MUTABLE_FIELDS}
        existing = items.get(key)
        if existing is None:
            record = {k: v for k, v in reg.items()}
            record.update({
                "first_seen": observed_at,
                "last_seen": observed_at,
                "still_listed": True,
                "history": [dict(observed_at=observed_at, **state)],
            })
            items[key] = record
            continue
        existing["last_seen"] = observed_at
        existing["still_listed"] = True
        # 変化した項目は最新値に更新し、変化があったときだけ履歴を足す
        last = existing["history"][-1] if existing.get("history") else {}
        if any(last.get(f) != state[f] for f in _MUTABLE_FIELDS):
            existing.setdefault("history", []).append(
                dict(observed_at=observed_at, **state)
            )
        existing.update(state)
        # 経路は一度スナップできていればそのまま使う
        if not existing.get("path") and reg.get("path"):
            existing["path"] = reg["path"]

    # 今回の一覧から消えたものは、消えたことだけ記録して残す
    for key, rec in items.items():
        if key not in current_keys:
            rec["still_listed"] = False

    return {"generated_at": observed_at, "items": items}

