import requests
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any
def fetch_traffic(
    road_type: str,
    time_code: str,
    type_name: str = "t_travospublic_measure_5m",
    # options ["t_travospublic_measure_5m", "t_travospublic_measure_1h", 
    # "t_travospublic_measure_5m_img", "t_travospublic_measure_1h_img"]
    bbox: Optional[Tuple[float, float, float, float]] = None
) -> Dict[str, Any]:
    """
    単一の time_code に対する WFS リクエスト。
    （既存の実装をそのまま流用してください）
    """
    url = "https://api.jartic-open-traffic.org/geoserver"
    filters = [
        f"道路種別={road_type}",
        f"時間コード={time_code}"
    ]
    if bbox is not None:
        filters.append(
            f"BBOX(ジオメトリ,"
            f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]},"
            f"'EPSG:4326')"
        )
    params = {
        "service":      "WFS",
        "version":      "2.0.0",
        "request":      "GetFeature",
        "typeNames":    type_name,
        "srsName":      "EPSG:4326",
        "outputFormat": "application/json",
        "exceptions":   "application/json",
        "cql_filter":   " AND ".join(filters),
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()

def fetch_traffic_range(
    road_type: str,
    time_code_start: str,
    time_code_end: str,
    type_name: str = "t_travospublic_measure_5m",
    bbox: Optional[Tuple[float, float, float, float]] = None
) -> Dict[str, Any]:
    """
    time_code_start から time_code_end までを５分刻みでフェッチし、
    全 features をまとめた GeoJSON dict を返す。

    Parameters
    ----------
    road_type : str
        道路種別コード（"1" や "3" など）
    time_code_start : str
        開始時間コード (YYYYMMDDhhmm)
    time_code_end : str
        終了時間コード (YYYYMMDDhhmm)
    bbox : tuple or None
        (min_lon, min_lat, max_lon, max_lat) または None

    Returns
    -------
    dict
        結合後の GeoJSON FeatureCollection、
        例: {"type":"FeatureCollection", "features":[...all features...]}
    """
    start_dt = datetime.strptime(time_code_start, "%Y%m%d%H%M")
    end_dt   = datetime.strptime(time_code_end,   "%Y%m%d%H%M")
    dt = start_dt

    all_features = []
    
    # type_name の値によってoffsetを変更
    if type_name == "t_travospublic_measure_5m" or type_name == "t_travospublic_measure_5m_img":
        offset = 5
    elif type_name == "t_travospublic_measure_1h" or type_name == "t_travospublic_measure_1h_img":
        offset = 60
    else:
        raise ValueError("Invalid type_name. Must be 't_travospublic_measure_5m' or 't_travospublic_measure_1h'.")
    
    while dt <= end_dt:
        tc = dt.strftime("%Y%m%d%H%M")
        # tcを文字列に変換
        tc = str(tc)
        data = fetch_traffic(road_type, tc, type_name, bbox)
        feats = data.get("features", [])
        # 取得件数を出力
        print(f"[{tc}] fetched {len(feats)} features")
        if feats:
            all_features.extend(feats)
        dt += timedelta(minutes=offset)

    return {
        "type": "FeatureCollection",
        "features": all_features
    }