#!/usr/bin/env python
# coding: utf-8
"""
熊本地震（2026-07-28）による交通行動変容分析用のデータ取得・前処理スクリプト。

実行すると以下を `data/` 配下に生成する:
- archive/traffic_raw.parquet    : 5分間値の生データを丸ごと保持する恒久アーカイブ
                                （JARTIC側の5分値は過去1ヶ月分しか遡れないため、削除・上書きせず追記していく）
- archive/traffic_hourly.parquet : 1時間値の生データの恒久アーカイブ（1時間値は3ヶ月分遡れる）
- observations_hourly.parquet    : 1時間値どうしの比較。異常検知（zスコア・地図の色分け・
                                   異常検知一覧）はこちらの定義を使う
- holidays.json                  : 内閣府の祝日CSVのキャッシュ（平常時から祝日を除くために使用）
- target.parquet       : 地震前後を含む対象期間の交通量データ（アーカイブから毎回再生成するビュー）
- baseline.parquet     : 平常時（2週間前の同曜日ペア）の交通量データ（同上）
- observations.parquet : 5分間値の実績に、1時間値ベース（同曜日8週分・祝日除く）の平常時を
                         1/12して適用したテーブル（時系列図の既定ビュー）
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
from modules.anomaly import (
    build_observation_table, compute_baseline_stats, scale_baseline_stats,
)
from modules.road_regulations import fetch_regulations, build_regulation_paths
from modules.holidays import fetch_holidays

ROAD_TYPE = "3"
TYPE_NAME = "t_travospublic_measure_5m"
HOURLY_TYPE_NAME = "t_travospublic_measure_1h"
BBOX = (130.450, 32.400, 130.900, 32.900)  # 熊本県
MAINSHOCK_EID = "20260728162718"
MIN_AFTERSHOCK_INTENSITY = 5  # 震度5弱以上（INTENSITY_ORDERの数値尺度）

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ARCHIVE_PATH = os.path.join(DATA_DIR, "archive", "traffic_raw.parquet")
HOURLY_ARCHIVE_PATH = os.path.join(DATA_DIR, "archive", "traffic_hourly.parquet")
FETCH_STEP = timedelta(minutes=5)
HOURLY_FETCH_STEP = timedelta(hours=1)

TARGET_START = datetime(2026, 7, 27, 3, 0)
# 復旧期（被災後72時間はもちろん、その後の交通パターンが平常に戻る過程まで）を
# 動的取得の対象に含めるため、本震発生時刻からの経過期間で取得終了日時を決める。
RECOVERY_PERIOD = timedelta(days=14)

# 5分間値は過去1ヶ月しか遡れないため、これまでの平常時は直前2回の火曜だけだった。
BASELINE_WINDOWS = [
    (datetime(2026, 7, 14, 3, 0), datetime(2026, 7, 15, 3, 0)),
    (datetime(2026, 7, 21, 3, 0), datetime(2026, 7, 22, 3, 0)),
]

# 1時間値は過去3ヶ月遡れるので、本震と同じ曜日（火）を8週分さかのぼって
# 平常時の平均・標準偏差の母集団にする。1日の区切りは5分間値側と揃えて03:00起点。
# 祝日は交通量の傾向が平日と異なるため、母集団から除外する（下で実施）。
HOURLY_BASELINE_WEEKS = 8
_HOURLY_BASELINE_CANDIDATES = [
    (
        datetime(2026, 7, 21, 3, 0) - timedelta(weeks=i),
        datetime(2026, 7, 22, 3, 0) - timedelta(weeks=i),
    )
    for i in range(HOURLY_BASELINE_WEEKS)
]

HOLIDAY_CACHE_PATH = os.path.join(DATA_DIR, "holidays.json")


def _baseline_windows_excluding_holidays(candidates, holidays):
    """祝日にあたる日を平常時の母集団から外す。除外内容は必ずログに出す。"""
    kept, dropped = [], []
    for start_dt, end_dt in candidates:
        name = holidays.get(start_dt.date().isoformat())
        if name:
            dropped.append((start_dt.date(), name))
        else:
            kept.append((start_dt, end_dt))
    if dropped:
        for d, name in dropped:
            print(f"[baseline-1h] excluding {d} ({name}) as a public holiday", flush=True)
    else:
        print("[baseline-1h] no public holidays fell in the baseline window", flush=True)
    print(f"[baseline-1h] using {len(kept)} of {len(candidates)} candidate days", flush=True)
    return kept


def _fetch_period(
    start_dt: datetime, end_dt: datetime, label: str, type_name: str = TYPE_NAME
) -> pd.DataFrame:
    start_s = start_dt.strftime("%Y%m%d%H%M")
    end_s = end_dt.strftime("%Y%m%d%H%M")
    print(f"[{label}] fetching {start_s} -> {end_s}", flush=True)
    combined = fetch_traffic_range(
        road_type=ROAD_TYPE,
        time_code_start=start_s,
        time_code_end=end_s,
        type_name=type_name,
        bbox=BBOX,
    )
    print(f"[{label}] total features: {len(combined['features'])}", flush=True)
    # 常設トラカンの1時間値は5分間値と同じプロパティ名なので、変換関数を共用できる。
    gdf = create_traffic_geodf(combined)
    return pd.DataFrame(gdf.drop(columns="geometry"))


def _load_archive(path: str = ARCHIVE_PATH) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df


def _save_archive(archive: pd.DataFrame, path: str = ARCHIVE_PATH) -> pd.DataFrame:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    archive = archive.drop_duplicates(subset=["lon", "lat", "datetime"]).sort_values("datetime")
    archive.to_parquet(path, index=False)
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


def _fetch_missing_target(
    archive: pd.DataFrame, target_end: datetime,
    type_name: str = TYPE_NAME, step: timedelta = FETCH_STEP, label: str = "target",
) -> pd.DataFrame:
    """アーカイブ済みのtarget範囲より先だけを新規取得する。"""
    existing = _slice(archive, TARGET_START, target_end)
    next_start = TARGET_START
    if not existing.empty:
        next_start = existing["datetime"].max() + step
    if next_start > target_end:
        print(f"[{label}] already up to date (archived through {existing['datetime'].max()})", flush=True)
        return pd.DataFrame()
    return _fetch_period(next_start, target_end, label, type_name)


def _fetch_missing_baseline(
    archive: pd.DataFrame, windows=BASELINE_WINDOWS,
    type_name: str = TYPE_NAME, label: str = "baseline",
) -> pd.DataFrame:
    """まだアーカイブにない平常時ウィンドウだけを取得する（固定の過去データなので一度取れば十分）。"""
    new_dfs = []
    for start_dt, end_dt in windows:
        if not _slice(archive, start_dt, end_dt).empty:
            print(f"[{label}({start_dt.date()})] already archived, skipping", flush=True)
            continue
        new_dfs.append(
            _fetch_period(start_dt, end_dt, f"{label}({start_dt.date()})", type_name)
        )
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

    # --- 1時間値（平常時の母集団＋異常検知の基準） -----------------------------
    holidays = fetch_holidays(HOLIDAY_CACHE_PATH)
    hourly_baseline_windows = _baseline_windows_excluding_holidays(
        _HOURLY_BASELINE_CANDIDATES, holidays
    )

    hourly_archive = _load_archive(HOURLY_ARCHIVE_PATH)
    new_hourly_target = _fetch_missing_target(
        hourly_archive, target_end,
        type_name=HOURLY_TYPE_NAME, step=HOURLY_FETCH_STEP, label="target-1h",
    )
    new_hourly_baseline = _fetch_missing_baseline(
        hourly_archive, windows=hourly_baseline_windows,
        type_name=HOURLY_TYPE_NAME, label="baseline-1h",
    )
    hourly_archive = _merge_into_archive(
        hourly_archive, new_hourly_target, new_hourly_baseline
    )
    hourly_archive = _save_archive(hourly_archive, HOURLY_ARCHIVE_PATH)

    hourly_target_df = _slice(hourly_archive, TARGET_START, target_end)
    hourly_baseline_df = pd.concat(
        [_slice(hourly_archive, s, e) for s, e in hourly_baseline_windows],
        ignore_index=True,
    )
    hourly_baseline_stats = compute_baseline_stats(hourly_baseline_df)

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

    # 実績の既定は5分間値。平常時は1時間値（同曜日8週分・祝日除く）から求め、
    # 単位を合わせるため1/12にスケールする。
    observations = build_observation_table(
        target_df=target_df,
        baseline_df=baseline_df,
        quake_occurred_at=quake_occurred_at,
        epicenter_lat=mainshock["epicenter_lat"],
        epicenter_lon=mainshock["epicenter_lon"],
        baseline_stats=scale_baseline_stats(hourly_baseline_stats, 1.0 / 12.0),
    )
    observations.to_parquet(os.path.join(DATA_DIR, "observations.parquet"))

    # 異常検知（zスコア・地図の色分け・異常検知一覧）は1時間値どうしの比較で定義する。
    # 5分間値どうしだと短時間の揺らぎを拾いやすく、1時間値のσを1/12した帯と組み合わせると
    # 過検知になるため。
    observations_hourly = build_observation_table(
        target_df=hourly_target_df,
        baseline_df=hourly_baseline_df,
        quake_occurred_at=quake_occurred_at,
        epicenter_lat=mainshock["epicenter_lat"],
        epicenter_lon=mainshock["epicenter_lon"],
        baseline_stats=hourly_baseline_stats,
    )
    observations_hourly.to_parquet(os.path.join(DATA_DIR, "observations_hourly.parquet"))

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
        # ダッシュボード側で「平常時」の定義を正確に説明できるように、
        # 実際に使ったベースライン期間もそのまま書き出しておく。
        "baseline_windows": [
            {"start": s.isoformat(), "end": e.isoformat()} for s, e in BASELINE_WINDOWS
        ],
        "hourly_baseline_windows": [
            {"start": s.isoformat(), "end": e.isoformat()}
            for s, e in sorted(hourly_baseline_windows)
        ],
        "hourly_baseline_excludes_holidays": True,
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

    # 異常検知の正式な定義は1時間値ベース。5分間値テーブルにも
    # build_observation_table 由来の is_anomaly 列が入るが、そちらは
    # 1時間値のσを1/12した狭い帯に対する判定なので参考値にすぎない。
    n_anomaly = int(observations_hourly["is_anomaly"].sum())
    n_points = observations_hourly["point_id"].nunique()
    print(
        f"done. archive rows={len(archive)} (5m) / {len(hourly_archive)} (1h), "
        f"observations rows={len(observations)} (5m) / {len(observations_hourly)} (1h), "
        f"points={n_points}, anomalies flagged={n_anomaly} (1h basis; "
        f"5m table's own flag would be {int(observations['is_anomaly'].sum())} and is not used)",
        flush=True,
    )


if __name__ == "__main__":
    main()
