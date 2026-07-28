"""
交通量の平常時ベースラインとの比較による簡易異常検知。

観測点×時刻(hour)ごとに平常時（ベースライン期間）の平均・標準偏差を求め、
地震後の実測値との zスコアを計算する。震度分布そのものが取得しづらいため、
各観測点と震源との距離[km]を「揺れの強さ」の簡易的な代理指標として用いる。
"""
from typing import Optional

import numpy as np
import pandas as pd

from .earthquake_data import haversine_km

POINT_DECIMALS = 6  # 観測点を緯度経度で識別する際の丸め桁数


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


def compute_baseline_stats(baseline_df: pd.DataFrame) -> pd.DataFrame:
    """
    平常時データから、観測点(point_id)×時刻(hour)ごとの
    平均・標準偏差を計算する。

    Returns
    -------
    pd.DataFrame with columns:
        point_id, point_lon, point_lat, hour,
        baseline_mean_up, baseline_std_up, n_up,
        baseline_mean_down, baseline_std_down, n_down
    """
    df = _add_point_key(baseline_df)
    df = _to_numeric(df, ["traffic_up", "traffic_down"])
    df["hour"] = pd.to_datetime(df["datetime"]).dt.hour

    grouped = df.groupby(["point_id", "point_lon", "point_lat", "hour"]).agg(
        baseline_mean_up=("traffic_up", "mean"),
        baseline_std_up=("traffic_up", "std"),
        n_up=("traffic_up", "count"),
        baseline_mean_down=("traffic_down", "mean"),
        baseline_std_down=("traffic_down", "std"),
        n_down=("traffic_down", "count"),
    ).reset_index()

    return grouped


def _zscore(observed: pd.Series, mean: pd.Series, std: pd.Series, min_std_ratio: float = 0.05) -> pd.Series:
    """
    zスコアを計算する。std が 0 または欠測の場合はゼロ除算を避けるため、
    平均値に対する下限（min_std_ratio）でフロアリングする。
    """
    std_floor = np.maximum(std.fillna(0.0), np.maximum(mean.abs() * min_std_ratio, 1.0))
    return (observed - mean) / std_floor


def build_observation_table(
    target_df: pd.DataFrame,
    baseline_df: pd.DataFrame,
    quake_occurred_at: pd.Timestamp,
    epicenter_lat: float,
    epicenter_lon: float,
    anomaly_z_threshold: float = 2.0,
) -> pd.DataFrame:
    """
    対象期間データ・ベースラインデータ・地震情報を結合し、
    観測点×時刻ごとの zスコア／異常フラグ／震源距離を含む1本のテーブルを作る。
    """
    baseline_stats = compute_baseline_stats(baseline_df)

    df = _add_point_key(target_df)
    df = _to_numeric(df, ["traffic_up", "traffic_down"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["hour"] = df["datetime"].dt.hour

    merged = df.merge(
        baseline_stats.drop(columns=["point_lon", "point_lat"]),
        on=["point_id", "hour"],
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

    cols = [
        "point_id", "point_lon", "point_lat", "datetime", "hour",
        "traffic_up", "traffic_down",
        "baseline_mean_up", "baseline_std_up",
        "baseline_mean_down", "baseline_std_down",
        "z_up", "z_down",
        "is_post_quake", "is_anomaly",
        "distance_km_from_epicenter",
    ]
    return merged[cols].sort_values(["point_id", "datetime"]).reset_index(drop=True)


def summarize_by_point(observations: pd.DataFrame) -> pd.DataFrame:
    """
    観測点ごとに、地震発生後の最大異常度（|zスコア|の最大値）などを集計する。
    地図描画・一覧表示に利用する。
    """
    post = observations[observations["is_post_quake"]]
    if post.empty:
        return pd.DataFrame(columns=[
            "point_id", "point_lon", "point_lat",
            "max_abs_z", "max_abs_z_up", "max_abs_z_down",
            "n_anomaly", "distance_km_from_epicenter",
        ])

    def _agg(g: pd.DataFrame) -> pd.Series:
        max_z_up = g["z_up"].abs().max()
        max_z_down = g["z_down"].abs().max()
        return pd.Series({
            "point_lon": g["point_lon"].iloc[0],
            "point_lat": g["point_lat"].iloc[0],
            "max_abs_z_up": max_z_up,
            "max_abs_z_down": max_z_down,
            "max_abs_z": np.nanmax([max_z_up, max_z_down]),
            "n_anomaly": int(g["is_anomaly"].sum()),
            "distance_km_from_epicenter": g["distance_km_from_epicenter"].iloc[0],
        })

    summary = post.groupby("point_id").apply(_agg, include_groups=False).reset_index()
    return summary.sort_values("max_abs_z", ascending=False).reset_index(drop=True)
