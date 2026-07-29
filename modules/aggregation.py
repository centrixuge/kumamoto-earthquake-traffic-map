import geopandas as gpd
from datetime import datetime
from shapely.geometry import Point
from typing import Optional, Tuple, Dict, Any, List


def parse_observation_datetime(prop: Dict[str, Any]) -> datetime:
    """
    「観測年月日」と「時間帯」から観測時刻を作る。

    「時間帯」はHHMM形式だが、APIはゼロ埋めせず整数として返してくる
    （00:05 -> 5、00:20 -> 20、01:00 -> 100、12:35 -> 1235）。
    そのため「0〜23なら時(hour)」と解釈すると真夜中の4コマが
    00:05 -> 05:00 / 00:10 -> 10:00 / 00:15 -> 15:00 / 00:20 -> 20:00
    と昼以降の時刻にすり替わってしまう（5分間値・1時間値の両方で確認済み。
    1時間値はHHMMが必ず100の倍数なので実害は00:00のみで、そこは結果が一致する）。
    必ず4桁ゼロ埋めのHHMMとして扱う。
    """
    time_band = int(prop["時間帯"])
    return datetime.strptime(f"{prop['観測年月日']}{time_band:04d}", "%Y%m%d%H%M")


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

        dt = parse_observation_datetime(prop)

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
            # JARTICの「常時観測点コード」。観測点を一意に識別する公式のIDなので、
            # lon/latの文字列連結ではなくこれを観測点の識別子として使う。
            "point_code": prop.get("常時観測点コード"),
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
                "point_code", "lon", "lat", "datetime", "traffic_up", "traffic_down",
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

        dt = parse_observation_datetime(prop)

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

        dt = parse_observation_datetime(prop)

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