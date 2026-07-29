#!/usr/bin/env python
# coding: utf-8
"""
熊本地震（2026-07-28）による交通行動変容分析用のデータ取得・前処理スクリプト。

実行すると以下を `data/` 配下に生成する:
- archive/traffic_raw.parquet : これまでに取得した生データを丸ごと保持する恒久アーカイブ
                                （JARTIC側の5分値は過去1ヶ月分しか遡れないため、削除・上書きせず追記していく）
- target.parquet       : 地震前後を含む対象期間の交通量データ（アーカイブから毎回再生成するビュー）
- baseline.parquet     : 平常時（2週間前の同曜日ペア）の交通量データ（同上）
- observations.parquet : 異常検知結果（zスコア・震源距離など）を結合した観測点×時刻のテーブル
- quake_info.json      : 本震・主要余震（M4.0以上）の震源・震度情報
- regulations.json     : 「防災情報くまもと」の道路通行規制情報（OSRMで道路網にスナップ済み）

Streamlitアプリ（app.py）はtarget/baseline/observations/quake_info/regulationsだけを
読み込むため、GDAL依存のgeopandas/shapelyはこのスクリプト側でのみ使用する。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 全ての日時定数（TARGET_START, BASELINE_WINDOWS, 地震発生時刻など）は
# タイムゾーン情報を持たないJSTの壁時計時刻として扱っている。GitHub Actionsの
# ランナーはUTCで動くため、datetime.now()をそのまま使うと「今」がJSTより
# 9時間早く扱われ、地震発生前後の判定が環境によって変わってしまう。
JST = timezone(timedelta(hours=9))


def _now_jst() -> datetime:
    return datetime.now(JST).replace(tzinfo=None)

from modules.api_request_func import fetch_traffic_range
from modules.aggregation import create_traffic_geodf
from modules.earthquake_data import get_quake_info, get_significant_events
from modules.anomaly import build_observation_table
from modules.road_regulations import fetch_regulations, build_regulation_paths

ROAD_TYPE = "3"
TYPE_NAME = "t_travospublic_measure_5m"
BBOX = (130.450, 32.400, 130.900, 32.900)  # 熊本県
MAINSHOCK_EID = "20260728162718"
MIN_AFTERSHOCK_INTENSITY = 5  # 震度5弱以上（INTENSITY_ORDERの数値尺度）

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ARCHIVE_PATH = os.path.join(DATA_DIR, "archive", "traffic_raw.parquet")
FETCH_STEP = timedelta(minutes=5)

TARGET_START = datetime(2026, 7, 27, 3, 0)
# 復旧期（被災後72時間はもちろん、その後の交通パターンが平常に戻る過程まで）を
# 動的取得の対象に含めるため、本震発生時刻からの経過期間で取得終了日時を決める。
RECOVERY_PERIOD = timedelta(days=14)

BASELINE_WINDOWS = [
    (datetime(2026, 7, 14, 3, 0), datetime(2026, 7, 15, 3, 0)),
    (datetime(2026, 7, 21, 3, 0), datetime(2026, 7, 22, 3, 0)),
]


def _fetch_period(start_dt: datetime, end_dt: datetime, label: str) -> pd.DataFrame:
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


def _load_archive() -> pd.DataFrame:
    if not os.path.exists(ARCHIVE_PATH):
        return pd.DataFrame()
    df = pd.read_parquet(ARCHIVE_PATH)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df


def _save_archive(archive: pd.DataFrame) -> pd.DataFrame:
    os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
    archive = archive.drop_duplicates(subset=["lon", "lat", "datetime"]).sort_values("datetime")
    archive.to_parquet(ARCHIVE_PATH, index=False)
    return archive


def _merge_into_archive(archive: pd.DataFrame, *new_dfs: pd.DataFrame) -> pd.DataFrame:
    parts = [df for df in (archive, *new_dfs) if not df.empty]
    if not parts:
        return archive
    return pd.concat(parts, ignore_index=True)


def _slice(archive: pd.DataFrame, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    if archive.empty:
        return archive
    mask = (archive["datetime"] >= start_dt) & (archive["datetime"] <= end_dt)
    return archive.loc[mask].reset_index(drop=True)


def _fetch_missing_target(archive: pd.DataFrame, target_end: datetime) -> pd.DataFrame:
    """アーカイブ済みのtarget範囲より先だけを新規取得する。"""
    existing = _slice(archive, TARGET_START, target_end)
    next_start = TARGET_START
    if not existing.empty:
        next_start = existing["datetime"].max() + FETCH_STEP
    if next_start > target_end:
        print(f"[target] already up to date (archived through {existing['datetime'].max()})", flush=True)
        return pd.DataFrame()
    return _fetch_period(next_start, target_end, "target")


def _fetch_missing_baseline(archive: pd.DataFrame) -> pd.DataFrame:
    """まだアーカイブにない平常時ウィンドウだけを取得する（固定の過去データなので一度取れば十分）。"""
    new_dfs = []
    for start_dt, end_dt in BASELINE_WINDOWS:
        if not _slice(archive, start_dt, end_dt).empty:
            print(f"[baseline({start_dt.date()})] already archived, skipping", flush=True)
            continue
        new_dfs.append(_fetch_period(start_dt, end_dt, f"baseline({start_dt.date()})"))
    if not new_dfs:
        return pd.DataFrame()
    return pd.concat(new_dfs, ignore_index=True)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = _now_jst()

    mainshock = get_quake_info(MAINSHOCK_EID)
    quake_occurred_at = pd.Timestamp(
        datetime.fromisoformat(mainshock["occurred_at"]).replace(tzinfo=None)
    )
    target_end_cap = quake_occurred_at.to_pydatetime() + RECOVERY_PERIOD
    target_end = min(now, target_end_cap)

    archive = _load_archive()
    new_target_df = _fetch_missing_target(archive, target_end)
    new_baseline_df = _fetch_missing_baseline(archive)
    archive = _merge_into_archive(archive, new_target_df, new_baseline_df)
    archive = _save_archive(archive)

    target_df = _slice(archive, TARGET_START, target_end)
    baseline_df = pd.concat(
        [_slice(archive, s, e) for s, e in BASELINE_WINDOWS], ignore_index=True
    )

    target_df.to_parquet(os.path.join(DATA_DIR, "target.parquet"))
    baseline_df.to_parquet(os.path.join(DATA_DIR, "baseline.parquet"))

    # 「対象期間内の地震」は本震発生時刻から現在（もしくは復旧期の終端）までの
    # 期間で数える。TARGET_STARTは交通量データの取得開始日（本震の前日）であり、
    # 地震の集計期間とは意味が異なるため別に定義する。
    events_period_start = quake_occurred_at.to_pydatetime()
    aftershocks = get_significant_events(
        bbox=BBOX,
        start_dt=events_period_start,
        end_dt=target_end,
        min_intensity=MIN_AFTERSHOCK_INTENSITY,
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
        "events_min_intensity": MIN_AFTERSHOCK_INTENSITY,
        "events_period_start": events_period_start.isoformat(),
        "events_period_end": target_end.isoformat(),
        "generated_at": now.isoformat(),
        "target_start": TARGET_START.isoformat(),
        "target_end": target_end.isoformat(),
        "target_end_cap": target_end_cap.isoformat(),
    }
    with open(os.path.join(DATA_DIR, "quake_info.json"), "w", encoding="utf-8") as f:
        json.dump(quake_info, f, ensure_ascii=False, indent=2)

    try:
        reg_items = fetch_regulations()
        regulations = build_regulation_paths(reg_items)
    except Exception as e:  # noqa: BLE001 - 規制情報の取得失敗でパイプライン全体は止めない
        print(f"[regulations] fetch failed, skipping: {e}", flush=True)
        regulations = []
    with open(os.path.join(DATA_DIR, "regulations.json"), "w", encoding="utf-8") as f:
        json.dump(
            {"generated_at": now.isoformat(), "items": regulations},
            f, ensure_ascii=False, indent=2,
        )
    print(f"[regulations] saved {len(regulations)} entries", flush=True)

    n_anomaly = int(observations["is_anomaly"].sum())
    n_points = observations["point_id"].nunique()
    print(
        f"done. archive rows={len(archive)}, observations rows={len(observations)}, "
        f"points={n_points}, anomalies flagged={n_anomaly}",
        flush=True,
    )


if __name__ == "__main__":
    main()
