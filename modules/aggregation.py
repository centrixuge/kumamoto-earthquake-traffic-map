import geopandas as gpd
from datetime import datetime
from shapely.geometry import Point
from typing import Optional, Tuple, Dict, Any, List

def create_traffic_geodf(
    data: Dict[str, Any]
) -> gpd.GeoDataFrame:
    """
    GeoJSON 形式の traffic data を受け取り、
    欠測値 (None) を保持したまま GeoDataFrame を返す。

    Parameters
    ----------
    data : dict
        WFS から取得した GeoJSON dict（"features" キーを含む）

    Returns
    -------
    gpd.GeoDataFrame
        以下のカラムを持つ GeoDataFrame:
        - lon, lat : float
        - datetime : datetime
        - traffic_up, traffic_down : Optional[int]
        - traffic_up_small, traffic_up_large, traffic_up_unidentified : Optional[int]
        - traffic_down_small, traffic_down_large, traffic_down_unidentified : Optional[int]
        - geometry : Point (EPSG:4326)
    """
    rows: List[Dict[str, Any]] = []

    for feature in data.get("features", []):
        prop = feature["properties"]
        coords = feature["geometry"]["coordinates"][0]

        # 日時のパース（時間帯: 300/2000 または 3/20 の両方に対応）
        time_band = int(prop["時間帯"])
        if 0 <= time_band <= 23:
            time_band *= 100
        dt = datetime.strptime(
            f"{prop['観測年月日']}{time_band:04d}",
            "%Y%m%d%H%M"
        )

        # 各交通量値（None を許容）
        up_small = prop.get('上り・小型交通量')
        up_large = prop.get('上り・大型交通量')
        up_unid  = prop.get('上り・車種判別不能交通量')
        dn_small = prop.get('下り・小型交通量')
        dn_large = prop.get('下り・大型交通量')
        dn_unid  = prop.get('下り・車種判別不能交通量')

        # None が含まれていれば None を保持
        traffic_up = (
            None
            if None in (up_small, up_large, up_unid)
            else up_small + up_large + up_unid
        )
        traffic_down = (
            None
            if None in (dn_small, dn_large, dn_unid)
            else dn_small + dn_large + dn_unid
        )

        rows.append({
            "lon": coords[0],
            "lat": coords[1],
            "datetime": dt,
            "traffic_up": traffic_up,
            "traffic_down": traffic_down,
            "traffic_up_small": up_small,
            "traffic_up_large": up_large,
            "traffic_up_unidentified": up_unid,
            "traffic_down_small": dn_small,
            "traffic_down_large": dn_large,
            "traffic_down_unidentified": dn_unid,
            "geometry": Point(coords[0], coords[1]),
        })

    # GeoDataFrame の作成（featuresが0件のときrowsも空になるため、
    # geometry列を明示しないとGeoDataFrameの生成自体が失敗する）
    if not rows:
        return gpd.GeoDataFrame(
            columns=[
                "lon", "lat", "datetime", "traffic_up", "traffic_down",
                "traffic_up_small", "traffic_up_large", "traffic_up_unidentified",
                "traffic_down_small", "traffic_down_large", "traffic_down_unidentified",
                "geometry",
            ],
            geometry="geometry", crs="EPSG:4326",
        )
    df = gpd.GeoDataFrame(rows, crs="EPSG:4326")

    # オブジェクト型にキャストして None を保持
    cols_to_cast = [
        "traffic_up", "traffic_down",
        "traffic_up_small", "traffic_up_large", "traffic_up_unidentified",
        "traffic_down_small", "traffic_down_large", "traffic_down_unidentified"
    ]
    for col in cols_to_cast:
        df[col] = df[col].astype(object)

    return df


def create_traffic_geodf_img(
    data: Dict[str, Any]
) -> gpd.GeoDataFrame:
    """
    GeoJSON 形式の traffic data を受け取り、
    欠測値 (None) を保持したまま GeoDataFrame を返す。（画像データ用） 

    Parameters
    ----------
    data : dict
        WFS から取得した GeoJSON dict（"features" キーを含む）

    Returns
    -------
    gpd.GeoDataFrame
        以下のカラムを持つ GeoDataFrame:
        - lon, lat : float
        - datetime : datetime
        - traffic_up, traffic_down : Optional[int]
        - traffic_up_small, traffic_up_large, traffic_up_unidentified : Optional[int]
        - traffic_down_small, traffic_down_large, traffic_down_unidentified : Optional[int]
        - geometry : Point (EPSG:4326)
    """
    rows: List[Dict[str, Any]] = []

    for feature in data.get("features", []):
        prop = feature["properties"]
        coords = feature["geometry"]["coordinates"][0]

        # 日時のパース（時間帯: 300/2000 または 3/20 の両方に対応）
        time_band = int(prop["時間帯"])
        if 0 <= time_band <= 23:
            time_band *= 100
        dt = datetime.strptime(
            f"{prop['観測年月日']}{time_band:04d}",
            "%Y%m%d%H%M"
        )

        # 各交通量値（None を許容）
        up_small = prop.get('上り・小型交通量（集計値）')
        up_large = prop.get('上り・大型交通量（集計値）')
        up_unid  = prop.get('上り・小型大型判別不能交通量（集計値）')
        dn_small = prop.get('下り・小型交通量（集計値）')
        dn_large = prop.get('下り・大型交通量（集計値）')
        dn_unid  = prop.get('下り・小型大型判別不能交通量（集計値）')
        traffic_up = prop.get('上り・自動車交通量（集計値）')
        traffic_down = prop.get('下り・自動車交通量（集計値）')

        rows.append({
            "lon": coords[0],
            "lat": coords[1],
            "datetime": dt,
            "traffic_up": traffic_up,
            "traffic_down": traffic_down,
            "traffic_up_small": up_small,
            "traffic_up_large": up_large,
            "traffic_up_unidentified": up_unid,
            "traffic_down_small": dn_small,
            "traffic_down_large": dn_large,
            "traffic_down_unidentified": dn_unid,
            "geometry": Point(coords[0], coords[1]),
        })

    # GeoDataFrame の作成（featuresが0件のときrowsも空になるため、
    # geometry列を明示しないとGeoDataFrameの生成自体が失敗する）
    if not rows:
        return gpd.GeoDataFrame(
            columns=[
                "lon", "lat", "datetime", "traffic_up", "traffic_down",
                "traffic_up_small", "traffic_up_large", "traffic_up_unidentified",
                "traffic_down_small", "traffic_down_large", "traffic_down_unidentified",
                "geometry",
            ],
            geometry="geometry", crs="EPSG:4326",
        )
    df = gpd.GeoDataFrame(rows, crs="EPSG:4326")

    # オブジェクト型にキャストして None を保持
    cols_to_cast = [
        "traffic_up", "traffic_down",
        "traffic_up_small", "traffic_up_large", "traffic_up_unidentified",
        "traffic_down_small", "traffic_down_large", "traffic_down_unidentified"
    ]
    for col in cols_to_cast:
        df[col] = df[col].astype(object)

    return df

# cctvの一時間交通量の様式に対応

def create_traffic_geodf_img_hour(
    data: Dict[str, Any]
) -> gpd.GeoDataFrame:
    """
    GeoJSON 形式の traffic data を受け取り、
    欠測値 (None) を保持したまま GeoDataFrame を返す。（画像データ用） 

    Parameters
    ----------
    data : dict
        WFS から取得した GeoJSON dict（"features" キーを含む）

    Returns
    -------
    gpd.GeoDataFrame
        以下のカラムを持つ GeoDataFrame:
        - lon, lat : float
        - datetime : datetime
        - traffic_up, traffic_down : Optional[int]
        - traffic_up_small, traffic_up_large, traffic_up_unidentified : Optional[int]
        - traffic_down_small, traffic_down_large, traffic_down_unidentified : Optional[int]
        - geometry : Point (EPSG:4326)
    """
    rows: List[Dict[str, Any]] = []

    for feature in data.get("features", []):
        prop = feature["properties"]
        coords = feature["geometry"]["coordinates"][0]

        # 日時のパース（時間帯: 300/2000 または 3/20 の両方に対応）
        time_band = int(prop["時間帯"])
        if 0 <= time_band <= 23:
            time_band *= 100
        dt = datetime.strptime(
            f"{prop['観測年月日']}{time_band:04d}",
            "%Y%m%d%H%M"
        )

        # 各交通量値（None を許容）
        up_small = prop.get('上り・小型交通量')
        up_large = prop.get('上り・大型交通量')
        up_unid  = prop.get('上り・小型大型判別不能交通量')
        dn_small = prop.get('下り・小型交通量')
        dn_large = prop.get('下り・大型交通量')
        dn_unid  = prop.get('下り・小型大型判別不能交通量')
        traffic_up = prop.get('上り・自動車交通量')
        traffic_down = prop.get('下り・自動車交通量')

        rows.append({
            "lon": coords[0],
            "lat": coords[1],
            "datetime": dt,
            "traffic_up": traffic_up,
            "traffic_down": traffic_down,
            "traffic_up_small": up_small,   
            "traffic_up_large": up_large,
            "traffic_up_unidentified": up_unid,
            "traffic_down_small": dn_small,
            "traffic_down_large": dn_large,
            "traffic_down_unidentified": dn_unid,
            "geometry": Point(coords[0], coords[1]),
        })

    # GeoDataFrame の作成（featuresが0件のときrowsも空になるため、
    # geometry列を明示しないとGeoDataFrameの生成自体が失敗する）
    if not rows:
        return gpd.GeoDataFrame(
            columns=[
                "lon", "lat", "datetime", "traffic_up", "traffic_down",
                "traffic_up_small", "traffic_up_large", "traffic_up_unidentified",
                "traffic_down_small", "traffic_down_large", "traffic_down_unidentified",
                "geometry",
            ],
            geometry="geometry", crs="EPSG:4326",
        )
    df = gpd.GeoDataFrame(rows, crs="EPSG:4326")

    # オブジェクト型にキャストして None を保持
    cols_to_cast = [
        "traffic_up", "traffic_down",
        "traffic_up_small", "traffic_up_large", "traffic_up_unidentified",
        "traffic_down_small", "traffic_down_large", "traffic_down_unidentified"
    ]
    for col in cols_to_cast:
        df[col] = df[col].astype(object)

    return df