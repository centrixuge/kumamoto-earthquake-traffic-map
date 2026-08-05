"""
交通量の平常時ベースラインとの比較による簡易異常検知。

観測点×日区分×時刻(hour)ごとに平常時（ベースライン期間）の平均・標準偏差を
求め、実測値との zスコアを計算する。

日区分（月/火/水/木/金/土/日祝）で分けるのは、曜日によって交通量の形が
まったく違うためである。曜日を無視して火曜だけの平常時と比べると、
地震と無関係な平常時の土日でも8割の行が |z|>=2 になることを実測で確認した
（朝7時は火曜比0.68倍、深夜22時は1.99倍）。
各観測点と震源との距離[km]も参考値として持たせるが、判定には使わない。
"""
from typing import Optional

import numpy as np
import pandas as pd

from .earthquake_data import haversine_km
from .holidays import daytype_of
from .stations import attach_point_code

POINT_DECIMALS = 6  # 観測点を緯度経度で識別する際の丸め桁数

# 平常時を求める対象の系列。合計に加えて車種別（小型・大型）も持たせる。
# 判別不能はアーカイブ全期間で1台しかなく、系列として持つ意味がないので扱わない。
TRAFFIC_SERIES = [
    "traffic_up", "traffic_down",
    "traffic_up_small", "traffic_up_large",
    "traffic_down_small", "traffic_down_large",
]


def _baseline_col(series: str, kind: str) -> str:
    """traffic_up_small -> baseline_mean_up_small のように列名を作る。"""
    return f"baseline_{kind}_{series[len('traffic_'):]}"


def _add_point_key(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["point_lon"] = df["lon"].astype(float).round(POINT_DECIMALS)
    df["point_lat"] = df["lat"].astype(float).round(POINT_DECIMALS)
    df["point_id"] = (
        df["point_lon"].astype(str) + "_" + df["point_lat"].astype(str)
    )
    return df


def _to_numeric(df: pd.DataFrame, cols) -> pd.DataFrame:
    df = df.copy()
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def add_daytype(df: pd.DataFrame, holidays: dict) -> pd.DataFrame:
    """日区分の列を足す（03:00起点の日付で判定）。"""
    df = df.copy()
    df["daytype"] = pd.to_datetime(df["datetime"]).map(lambda t: daytype_of(t, holidays))
    return df


def compute_baseline_stats(baseline_df: pd.DataFrame, holidays: dict = None) -> pd.DataFrame:
    """
    平常時データから、観測点(point_id)×日区分×時刻(hour)ごとの
    平均・標準偏差を計算する。

    Returns
    -------
    pd.DataFrame with columns:
        point_id, point_lon, point_lat, daytype, hour,
        baseline_mean_up, baseline_std_up, n_up,
        baseline_mean_down, baseline_std_down, n_down
    """
    df = _add_point_key(baseline_df)
    present = [c for c in TRAFFIC_SERIES if c in df.columns]
    df = _to_numeric(df, present)
    df = add_daytype(df, holidays or {})
    df["hour"] = pd.to_datetime(df["datetime"]).dt.hour

    agg = {}
    for col in present:
        agg[_baseline_col(col, "mean")] = (col, "mean")
        agg[_baseline_col(col, "std")] = (col, "std")
        agg[f"n_{col[len('traffic_'):]}"] = (col, "count")

    grouped = df.groupby(
        ["point_id", "point_lon", "point_lat", "daytype", "hour"]
    ).agg(**agg).reset_index()

    return grouped


def _zscore(observed: pd.Series, mean: pd.Series, std: pd.Series, min_std_ratio: float = 0.05) -> pd.Series:
    """
    zスコアを計算する。std が 0 または欠測の場合はゼロ除算を避けるため、
    平均値に対する下限（min_std_ratio）でフロアリングする。
    """
    std_floor = np.maximum(std.fillna(0.0), np.maximum(mean.abs() * min_std_ratio, 1.0))
    return (observed - mean) / std_floor


def scale_baseline_stats(stats: pd.DataFrame, factor: float) -> pd.DataFrame:
    """
    ベースラインの平均・標準偏差を定数倍する。1時間値から求めた統計量を
    5分間値と同じ単位で扱う（factor=1/12）ときに使う。
    ※ 1時間値のσを1/12した値は「平常時の1時間水準の日々のばらつき」であり、
       5分間値そのもののばらつきではない点に注意（帯は狭くなる）。
    """
    scaled = stats.copy()
    for series in TRAFFIC_SERIES:
        for kind in ("mean", "std"):
            col = _baseline_col(series, kind)
            if col in scaled.columns:
                scaled[col] = scaled[col] * factor
    return scaled


def build_observation_table(
    target_df: pd.DataFrame,
    baseline_df: pd.DataFrame,
    quake_occurred_at: pd.Timestamp,
    epicenter_lat: float,
    epicenter_lon: float,
    anomaly_z_threshold: float = 2.0,
    baseline_stats: pd.DataFrame = None,
    station_master: dict = None,
    holidays: dict = None,
) -> pd.DataFrame:
    """
    対象期間データ・ベースラインデータ・地震情報を結合し、
    観測点×日区分×時刻ごとの zスコア／異常フラグ／震源距離を含む1本のテーブルを作る。

    実測の各行は「同じ日区分の平常時」と比べる。月曜の実測は月曜の平常時、
    土曜は土曜、日曜と祝日は日祝の平常時と突き合わせる。

    `baseline_stats` を渡した場合はそれを平常時の統計量として使い、
    `baseline_df` からの再計算を行わない（1時間値ベースの統計量を
    5分間値に適用する等、母集団を差し替えたいときに使う）。
    """
    if baseline_stats is None:
        baseline_stats = compute_baseline_stats(baseline_df, holidays)

    df = _add_point_key(target_df)
    df = _to_numeric(df, [c for c in TRAFFIC_SERIES if c in df.columns])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = add_daytype(df, holidays or {})
    df["hour"] = df["datetime"].dt.hour

    merged = df.merge(
        baseline_stats.drop(columns=["point_lon", "point_lat"], errors="ignore"),
        on=["point_id", "daytype", "hour"],
        how="left",
    )

    merged["z_up"] = _zscore(merged["traffic_up"], merged["baseline_mean_up"], merged["baseline_std_up"])
    merged["z_down"] = _zscore(merged["traffic_down"], merged["baseline_mean_down"], merged["baseline_std_down"])

    merged["is_post_quake"] = merged["datetime"] >= quake_occurred_at
    merged["is_anomaly"] = merged["is_post_quake"] & (
        (merged["z_up"].abs() >= anomaly_z_threshold) | (merged["z_down"].abs() >= anomaly_z_threshold)
    )

    merged["distance_km_from_epicenter"] = merged.apply(
        lambda r: haversine_km(r["point_lat"], r["point_lon"], epicenter_lat, epicenter_lon),
        axis=1,
    )

    # JARTICの常時観測点コードを付ける（観測点を指す公式のID）
    merged = attach_point_code(merged, station_master or {})

    cols = (
        [
            "point_code", "road_type", "point_id", "point_lon", "point_lat",
            "datetime", "daytype", "hour",
        ]
        # 合計と車種別（小型・大型）の実績、およびそれぞれの平常時。
        # 異常判定は合計（traffic_up / traffic_down）だけで行い、車種別は
        # 時系列図の「車種別」ビュー用に持たせるだけ。
        + [c for c in TRAFFIC_SERIES if c in merged.columns]
        + [
            _baseline_col(c, kind)
            for c in TRAFFIC_SERIES for kind in ("mean", "std")
            if _baseline_col(c, kind) in merged.columns
        ]
        + [
            "z_up", "z_down",
            "is_post_quake", "is_anomaly",
            "distance_km_from_epicenter",
        ]
    )
    cols = [c for c in cols if c in merged.columns]
    return merged[cols].sort_values(["point_id", "datetime"]).reset_index(drop=True)


def summarize_by_point(observations: pd.DataFrame) -> pd.DataFrame:
    """
    観測点ごとに、地震発生後の最大異常度（|zスコア|の最大値）などを集計する。
    地図描画・一覧表示に利用する。
    """
    post = observations[observations["is_post_quake"]]
    if post.empty:
        return pd.DataFrame(columns=[
            "point_id", "point_lon", "point_lat", "road_type",
            "max_abs_z", "max_abs_z_up", "max_abs_z_down",
            "n_anomaly", "distance_km_from_epicenter",
        ])

    def _agg(g: pd.DataFrame) -> pd.Series:
        max_z_up = g["z_up"].abs().max()
        max_z_down = g["z_down"].abs().max()
        return pd.Series({
            "point_lon": g["point_lon"].iloc[0],
            "point_lat": g["point_lat"].iloc[0],
            # 地図のマーカー形状を分けるのに使う（1:高速自動車国道 / 3:一般国道）
            "road_type": g["road_type"].iloc[0] if "road_type" in g.columns else None,
            "max_abs_z_up": max_z_up,
            "max_abs_z_down": max_z_down,
            "max_abs_z": np.nanmax([max_z_up, max_z_down]),
            "n_anomaly": int(g["is_anomaly"].sum()),
            "distance_km_from_epicenter": g["distance_km_from_epicenter"].iloc[0],
        })

    summary = post.groupby("point_id").apply(_agg, include_groups=False).reset_index()
    return summary.sort_values("max_abs_z", ascending=False).reset_index(drop=True)
