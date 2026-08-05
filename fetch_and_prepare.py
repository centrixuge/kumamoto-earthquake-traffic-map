#!/usr/bin/env python
# coding: utf-8
"""
熊本地震（2026-07-28）前後の交通量分析用のデータ取得・前処理スクリプト。

実行すると以下を `data/` 配下に生成する:
- archive/traffic_raw.parquet    : 5分間値の生データを丸ごと保持する恒久アーカイブ
                                （JARTIC側の5分値は過去1ヶ月分しか遡れないため、削除・上書きせず追記していく）
- archive/traffic_hourly.parquet : 1時間値の生データの恒久アーカイブ（1時間値は3ヶ月分遡れる）
- observations_hourly.parquet    : 1時間値どうしの比較。異常検知（zスコア・地図の色分け・
                                   異常検知一覧）はこちらの定義を使う
- holidays.json                  : 内閣府の祝日CSVのキャッシュ（平常時から祝日を除くために使用）
- archive/regulations_archive.json
                                 : 通行規制の追記専用アーカイブ。規制は解除されるとポータルの
                                   一覧から消えて後から取得できないため、初出/最終確認日時・
                                   規制内容の変化履歴つきで残し続ける
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

from modules.api_request_func import fetch_traffic_codes, fetch_traffic_range
from modules.aggregation import create_traffic_geodf
from modules.earthquake_data import get_quake_info, get_significant_events
from modules.anomaly import (
    build_observation_table, compute_baseline_stats, scale_baseline_stats,
)
from modules.road_regulations import (
    fetch_regulations, build_regulation_paths, merge_regulations_archive,
    regulation_key,
)
from modules.holidays import (
    fetch_holidays, daytype_of_date, DAYTYPES, DAYTYPE_SUNDAY_HOLIDAY,
    TRAFFIC_DAY_START_HOUR,
)
from modules.stations import (
    build_station_master, merge_station_master, load_station_master,
    save_station_master,
)

# 取得する道路種別。仕様書では 1:高速自動車国道 / 3:一般国道 の2値のみ。
# APIは種別ごとに別リクエストなので、両方取ってから結合する。
# 以前は "3" だけで、BBOX内にある九州中央自動車道の3点を取りこぼしていた。
ROAD_TYPES = ("3", "1")
TYPE_NAME = "t_travospublic_measure_5m"
HOURLY_TYPE_NAME = "t_travospublic_measure_1h"
BBOX = (130.450, 32.400, 131.000, 33.000)  # 熊本県
MAINSHOCK_EID = "20260728162718"
MIN_AFTERSHOCK_INTENSITY = 5  # 震度5弱以上（INTENSITY_ORDERの数値尺度）

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ARCHIVE_PATH = os.path.join(DATA_DIR, "archive", "traffic_raw.parquet")
HOURLY_ARCHIVE_PATH = os.path.join(DATA_DIR, "archive", "traffic_hourly.parquet")
REGULATIONS_ARCHIVE_PATH = os.path.join(DATA_DIR, "archive", "regulations_archive.json")
STATIONS_PATH = os.path.join(DATA_DIR, "stations.json")
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
# 平常時は日区分ごとに別々に持つ（月/火/水/木/金/土/日祝）。曜日で交通量の形が
# まったく違うため、火曜だけの平常時で土日を評価すると、地震と無関係な平常時の
# 土日でも8割の行が |z|>=2 になる（実測: 朝7時は火曜比0.68倍、深夜22時は1.99倍）。
# 各区分で「祝日でない同曜日」を8日集める。足りなければさらに前の週へ遡る。
HOURLY_BASELINE_DAYS_PER_TYPE = 8
# 平常時に使えるのは分析対象期間より前の日だけ（対象期間の実測を平常時に
# 混ぜると自分自身と比べることになる）。
BASELINE_LATEST_DAY = TARGET_START.date() - timedelta(days=1)

HOLIDAY_CACHE_PATH = os.path.join(DATA_DIR, "holidays.json")


def _daytype_baseline_windows(holidays, days_per_type=HOURLY_BASELINE_DAYS_PER_TYPE):
    """
    日区分ごとの平常時ウィンドウを作る。

    月〜金・土は「その曜日で祝日でない日」を、日祝は「日曜、または平日・土曜に
    あたる祝日」を、それぞれ新しい順に days_per_type 日集める。
    1日のウィンドウは 03:00 〜 翌03:00（深夜帯を日付でまたいで切らないため）。

    Returns
    -------
    dict: {日区分: [(start_dt, end_dt), ...]}
    """
    picked = {dt: [] for dt in DAYTYPES}
    d = BASELINE_LATEST_DAY
    # 十分な期間だけ遡る（1時間値は3ヶ月＝約13週まで取得できる）
    limit = BASELINE_LATEST_DAY - timedelta(days=7 * 13)
    while d >= limit and any(len(v) < days_per_type for v in picked.values()):
        dt = daytype_of_date(d, holidays)
        if len(picked[dt]) < days_per_type:
            start = datetime(d.year, d.month, d.day, TRAFFIC_DAY_START_HOUR)
            picked[dt].append((start, start + timedelta(days=1)))
        d -= timedelta(days=1)

    for dt in DAYTYPES:
        days = [s.date().isoformat() for s, _ in picked[dt]]
        print(f"[baseline-1h] {dt}: {len(days)}日 {days}", flush=True)
    hol_in = [
        s.date().isoformat() for s, _ in picked[DAYTYPE_SUNDAY_HOLIDAY]
        if holidays.get(s.date().isoformat())
    ]
    print(
        f"[baseline-1h] 日祝の母集団に含めた祝日: {hol_in or 'なし'}"
        "（日曜は祝日と重なっても除外しない。平日・土曜の祝日は日祝側に入れる）",
        flush=True,
    )
    return picked


def _fetch_period(
    start_dt: datetime, end_dt: datetime, label: str, type_name: str = TYPE_NAME
) -> pd.DataFrame:
    start_s = start_dt.strftime("%Y%m%d%H%M")
    end_s = end_dt.strftime("%Y%m%d%H%M")
    print(f"[{label}] fetching {start_s} -> {end_s}", flush=True)
    feats = []
    for rt in ROAD_TYPES:
        part = fetch_traffic_range(
            road_type=rt,
            time_code_start=start_s,
            time_code_end=end_s,
            type_name=type_name,
            bbox=BBOX,
        )
        got = part.get("features", [])
        print(f"[{label}] 道路種別{rt}: {len(got)} features", flush=True)
        feats.extend(got)
    combined = {"type": "FeatureCollection", "features": feats}
    print(f"[{label}] total features: {len(feats)}", flush=True)
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


def _archived_frames(archive: pd.DataFrame, road_type: str) -> set:
    """
    その道路種別で取得済みのコマ。

    既取得の判定は道路種別ごとに行う。まとめて「そのコマが1行でもあるか」で
    見ていたときは、種別3を取り終えたコマは種別1が未取得でもスキップされ、
    後から種別1を足しても過去分が永久に埋まらなかった。
    """
    if archive.empty or "road_type" not in archive.columns:
        return set()
    return set(archive.loc[archive["road_type"] == road_type, "datetime"])


def _fetch_frames(
    frames, label: str, type_name: str = TYPE_NAME, road_type: str = "3",
) -> pd.DataFrame:
    """指定した道路種別・コマだけを取得する（飛び飛びの欠けを埋めるため）。"""
    codes = [pd.Timestamp(t).strftime("%Y%m%d%H%M") for t in frames]
    print(
        f"[{label}] 道路種別{road_type}: fetching {len(codes)} frames "
        f"({codes[0]} ... {codes[-1]})", flush=True,
    )
    combined = fetch_traffic_codes(
        road_type=road_type, time_codes=codes, type_name=type_name, bbox=BBOX,
    )
    print(f"[{label}] total features: {len(combined['features'])}", flush=True)
    gdf = create_traffic_geodf(combined)
    return pd.DataFrame(gdf.drop(columns="geometry"))


# 1回の実行で取り直すコマ数の上限。恒久的に配信されないコマが残っていても
# リクエストが無限に増えないようにするための歯止め。
MAX_FRAMES_PER_RUN = 900


def _fetch_missing_target(
    archive: pd.DataFrame, target_end: datetime,
    type_name: str = TYPE_NAME, step: timedelta = FETCH_STEP, label: str = "target",
) -> pd.DataFrame:
    """
    target範囲のうち、アーカイブに無いコマだけを取得する。

    以前は「アーカイブ済みの最大時刻＋step」から取っていたが、それだと
    何らかの理由で先の時刻のレコードが1件混ざるだけで、その手前の未取得区間が
    毎回スキップされ、穴が永久に埋まらない（実際に時間帯パースの不具合で
    真夜中のコマが昼以降の時刻として入り込み、7/29 12:40〜14:55 が丸ごと
    飛ばされた）。そのため、期待されるコマの並びと突き合わせて
    「無いコマだけ」を取りにいく。1コマ=1リクエストなので範囲取得より無駄がない。
    """
    grid = pd.date_range(TARGET_START, target_end, freq=step)
    sliced = _slice(archive, TARGET_START, target_end)
    new_dfs = []
    for rt in ROAD_TYPES:
        existing = _archived_frames(sliced, rt)
        missing = [t for t in grid if t not in existing]
        if not missing:
            print(
                f"[{label}] 道路種別{rt}: already up to date "
                f"({len(grid)} frames archived)", flush=True,
            )
            continue
        if len(missing) > MAX_FRAMES_PER_RUN:
            # 新しい方を優先して取る（古い穴は次回以降に回す）
            skipped = len(missing) - MAX_FRAMES_PER_RUN
            missing = missing[-MAX_FRAMES_PER_RUN:]
            print(
                f"[{label}] 道路種別{rt}: {skipped} older missing frames deferred "
                f"to a later run (cap {MAX_FRAMES_PER_RUN})", flush=True,
            )
        new_dfs.append(_fetch_frames(missing, label, type_name, road_type=rt))
    if not new_dfs:
        return pd.DataFrame()
    return pd.concat(new_dfs, ignore_index=True)


def _fetch_missing_baseline(
    archive: pd.DataFrame, windows=BASELINE_WINDOWS,
    type_name: str = TYPE_NAME, step: timedelta = FETCH_STEP, label: str = "baseline",
) -> pd.DataFrame:
    """
    平常時ウィンドウのうち、アーカイブに無いコマだけを取得する。

    以前は「ウィンドウ内に1件でもあればスキップ」だったため、途中に穴が
    空いたまま埋まらなかった。ウィンドウごとに期待されるコマと突き合わせる。
    """
    new_dfs = []
    for rt in ROAD_TYPES:
        existing = _archived_frames(archive, rt)
        for start_dt, end_dt in windows:
            grid = pd.date_range(start_dt, end_dt, freq=step)
            missing = [t for t in grid if t not in existing]
            if not missing:
                print(
                    f"[{label}({start_dt.date()})] 道路種別{rt}: already archived, "
                    f"skipping", flush=True,
                )
                continue
            new_dfs.append(
                _fetch_frames(
                    missing, f"{label}({start_dt.date()})", type_name, road_type=rt
                )
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
    daytype_windows = _daytype_baseline_windows(holidays)
    # 取得は日区分をまとめて1つのリストで扱う（アーカイブは日区分を持たない）
    hourly_baseline_windows = sorted(
        w for windows in daytype_windows.values() for w in windows
    )

    hourly_archive = _load_archive(HOURLY_ARCHIVE_PATH)
    new_hourly_target = _fetch_missing_target(
        hourly_archive, target_end,
        type_name=HOURLY_TYPE_NAME, step=HOURLY_FETCH_STEP, label="target-1h",
    )
    new_hourly_baseline = _fetch_missing_baseline(
        hourly_archive, windows=hourly_baseline_windows,
        type_name=HOURLY_TYPE_NAME, step=HOURLY_FETCH_STEP, label="baseline-1h",
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
    hourly_baseline_stats = compute_baseline_stats(hourly_baseline_df, holidays)

    # --- 観測点マスタ（常時観測点コードと緯度経度の対応） ----------------------
    # アーカイブにはコード列を持たない時期のデータが含まれるため、座標から
    # コードを引けるマスタを別に保持し、集計時に付け直す。
    station_master = load_station_master(STATIONS_PATH)
    for df in (new_target_df, new_baseline_df, new_hourly_target, new_hourly_baseline):
        station_master = merge_station_master(station_master, build_station_master(df))
    if not station_master:
        # 今回の実行で新規取得が無かった場合は、1時点だけ取得してマスタを作る
        probe_at = datetime(2026, 7, 28, 16, 0)
        probe = _fetch_period(probe_at, probe_at, "stations-probe", HOURLY_TYPE_NAME)
        station_master = build_station_master(probe)
    save_station_master(station_master, STATIONS_PATH)
    print(f"[stations] {len(station_master)} observation points in master", flush=True)

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

    # 実績の既定は5分間値。平常時は同じ日区分の1時間値（各区分8日分）から求め、
    # 単位を合わせるため1/12にスケールする。
    observations = build_observation_table(
        target_df=target_df,
        baseline_df=baseline_df,
        quake_occurred_at=quake_occurred_at,
        epicenter_lat=mainshock["epicenter_lat"],
        epicenter_lon=mainshock["epicenter_lon"],
        baseline_stats=scale_baseline_stats(hourly_baseline_stats, 1.0 / 12.0),
        station_master=station_master,
        holidays=holidays,
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
        station_master=station_master,
        holidays=holidays,
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
        "hourly_baseline_daytypes": {
            dt: [s2.date().isoformat() for s2, _ in sorted(w)]
            for dt, w in daytype_windows.items()
        },
        "hourly_baseline_windows": [
            {"start": s.isoformat(), "end": e.isoformat()}
            for s, e in sorted(hourly_baseline_windows)
        ],
        "hourly_baseline_excludes_holidays": True,
    }
    with open(os.path.join(DATA_DIR, "quake_info.json"), "w", encoding="utf-8") as f:
        json.dump(quake_info, f, ensure_ascii=False, indent=2)

    # 規制情報は解除されるとポータルの一覧から消え、後から取得できない。
    # スナップ済み経路を再利用しつつ（OSRMへの再問い合わせを避ける）、
    # 取得したスナップショットは必ず追記専用アーカイブに残す。
    reg_archive = {}
    if os.path.exists(REGULATIONS_ARCHIVE_PATH):
        with open(REGULATIONS_ARCHIVE_PATH, encoding="utf-8") as f:
            reg_archive = json.load(f)
    known_paths = {
        key: rec.get("path")
        for key, rec in (reg_archive.get("items") or {}).items()
        if rec.get("path")
    }

    regulations = None
    try:
        reg_items = fetch_regulations()
        regulations = build_regulation_paths(reg_items, known_paths=known_paths)
    except Exception as e:  # noqa: BLE001 - 規制情報の取得失敗でパイプライン全体は止めない
        print(f"[regulations] fetch failed, skipping: {e}", flush=True)

    if regulations is None:
        # 取得できなかった回は「一覧から消えた」と誤判定しないよう、
        # アーカイブには一切手を触れず、前回のスナップショットも残す。
        print("[regulations] archive left untouched for this run", flush=True)
    else:
        reg_archive = merge_regulations_archive(
            reg_archive, regulations, now.isoformat()
        )
        os.makedirs(os.path.dirname(REGULATIONS_ARCHIVE_PATH), exist_ok=True)
        with open(REGULATIONS_ARCHIVE_PATH, "w", encoding="utf-8") as f:
            json.dump(reg_archive, f, ensure_ascii=False, indent=2)

        with open(os.path.join(DATA_DIR, "regulations.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"generated_at": now.isoformat(), "items": regulations},
                f, ensure_ascii=False, indent=2,
            )
        archived = reg_archive.get("items") or {}
        n_gone = sum(1 for r in archived.values() if not r.get("still_listed"))
        reused = sum(1 for r in regulations if regulation_key(r) in known_paths)
        print(
            f"[regulations] snapshot {len(regulations)} entries "
            f"(paths reused from archive: {reused}); "
            f"archive now holds {len(archived)} "
            f"({n_gone} no longer listed on the portal)",
            flush=True,
        )

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
