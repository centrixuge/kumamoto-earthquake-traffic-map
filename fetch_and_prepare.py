#!/usr/bin/env python
# coding: utf-8
"""
熊本地震（2026-07-28）による交通行動変容分析用のデータ取得・前処理スクリプト。

実行すると以下を `data/` 配下に生成する:
- target.parquet       : 地震前後を含む対象期間の交通量データ（平置きテーブル、geometryなし）
- baseline.parquet     : 平常時（2週間前の同曜日ペア）の交通量データ
- observations.parquet : 異常検知結果（zスコア・震源距離など）を結合した観測点×時刻のテーブル
- quake_info.json      : 本震・主要余震（M4.0以上）の震源・震度情報
- shelters.parquet     : 避難所（国土数値情報 P20、H24時点）のうちbbox内のもの

Streamlitアプリ（app.py）はこの出力（parquet/json）だけを読み込むため、
GDAL依存のgeopandas/shapelyはこのスクリプト側でのみ使用する。
"""
import json
import os
import sys
from datetime import datetime

import geopandas as gpd
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.api_request_func import fetch_traffic_range
from modules.aggregation import create_traffic_geodf
from modules.earthquake_data import get_quake_info, get_significant_events
from modules.anomaly import build_observation_table

ROAD_TYPE = "3"
TYPE_NAME = "t_travospublic_measure_5m"
BBOX = (130.450, 32.400, 130.900, 32.900)  # 熊本県
MAINSHOCK_EID = "20260728162718"
MIN_AFTERSHOCK_MAGNITUDE = 4.0

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SHELTER_SHP_PATH = os.path.join(DATA_DIR, "shelter", "P20-12_43.shp")

TARGET_START = datetime(2026, 7, 27, 3, 0)
TARGET_END_CAP = datetime(2026, 7, 29, 3, 0)  # 安全のための上限（この時刻以降は取得しない）

BASELINE_WINDOWS = [
    (datetime(2026, 7, 14, 3, 0), datetime(2026, 7, 15, 3, 0)),
    (datetime(2026, 7, 21, 3, 0), datetime(2026, 7, 22, 3, 0)),
]


def _fetch_period(start_dt: datetime, end_dt: datetime, label: str) -> "pd.DataFrame":
    start_s = start_dt.strftime("%Y%m%d%H%M")
    end_s = end_dt.strftime("%Y%m%d%H%M")
    print(f"[{label}] fetching {start_s} -> {end_s}", flush=True)
    combined = fetch_traffic_range(
        road_type=ROAD_TYPE,
        time_code_start=start_s,
        time_code_end=end_s,
        type_name=TYPE_NAME,
        bbox=BBOX,
    )
    print(f"[{label}] total features: {len(combined['features'])}", flush=True)
    gdf = create_traffic_geodf(combined)
    return pd.DataFrame(gdf.drop(columns="geometry"))


def _load_shelters(bbox) -> pd.DataFrame:
    """
    国土数値情報 避難所データ（P20、H24時点）を読み込み、bbox内のものだけを
    軽量なテーブル（名称・所在地・種別・緯度経度）として返す。
    """
    gdf = gpd.read_file(SHELTER_SHP_PATH, encoding="cp932")
    gdf = gdf.to_crs(epsg=4326)
    min_x, min_y, max_x, max_y = bbox
    gdf = gdf.cx[min_x:max_x, min_y:max_y]
    return pd.DataFrame({
        "name": gdf["P20_002"],
        "address": gdf["P20_003"],
        "shelter_type": gdf["P20_004"],
        "lon": gdf.geometry.x,
        "lat": gdf.geometry.y,
    }).reset_index(drop=True)


def main(target_start: datetime = TARGET_START, target_end: datetime = None):
    os.makedirs(DATA_DIR, exist_ok=True)

    now = datetime.now()
    if target_end is None:
        target_end = min(now, TARGET_END_CAP)

    target_df = _fetch_period(target_start, target_end, "target")
    baseline_dfs = [
        _fetch_period(s, e, f"baseline({s.date()})") for s, e in BASELINE_WINDOWS
    ]
    baseline_df = pd.concat(baseline_dfs, ignore_index=True)

    target_df.to_parquet(os.path.join(DATA_DIR, "target.parquet"))
    baseline_df.to_parquet(os.path.join(DATA_DIR, "baseline.parquet"))

    shelters_df = _load_shelters(BBOX)
    shelters_df.to_parquet(os.path.join(DATA_DIR, "shelters.parquet"))
    print(f"[shelters] saved {len(shelters_df)} shelters within bbox", flush=True)

    mainshock = get_quake_info(MAINSHOCK_EID)
    quake_occurred_at = pd.Timestamp(
        datetime.fromisoformat(mainshock["occurred_at"]).replace(tzinfo=None)
    )

    aftershocks = get_significant_events(
        bbox=BBOX,
        start_dt=target_start,
        end_dt=target_end,
        min_magnitude=MIN_AFTERSHOCK_MAGNITUDE,
    )

    observations = build_observation_table(
        target_df=target_df,
        baseline_df=baseline_df,
        quake_occurred_at=quake_occurred_at,
        epicenter_lat=mainshock["epicenter_lat"],
        epicenter_lon=mainshock["epicenter_lon"],
    )
    observations.to_parquet(os.path.join(DATA_DIR, "observations.parquet"))

    quake_info = {
        "mainshock": mainshock,
        "events": aftershocks,
        "generated_at": now.isoformat(),
        "target_start": target_start.isoformat(),
        "target_end": target_end.isoformat(),
    }
    with open(os.path.join(DATA_DIR, "quake_info.json"), "w", encoding="utf-8") as f:
        json.dump(quake_info, f, ensure_ascii=False, indent=2)

    n_anomaly = int(observations["is_anomaly"].sum())
    n_points = observations["point_id"].nunique()
    print(
        f"done. observations rows={len(observations)}, points={n_points}, "
        f"anomalies flagged={n_anomaly}",
        flush=True,
    )


if __name__ == "__main__":
    main()
