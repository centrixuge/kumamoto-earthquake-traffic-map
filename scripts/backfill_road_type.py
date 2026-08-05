"""
アーカイブの `road_type` 列を、観測点マスタ（data/stations.json）から埋める。

道路種別をレスポンスから保存するようにしたのは後からなので、それ以前に
取得した行は `road_type` が欠損している。道路種別は観測点に固定の属性で
あり、座標からマスタを引けば決まるため、後から付け直せる。

この列は「そのコマをその道路種別で取得済みか」の判定に使う。欠損のままだと
すべてのコマが未取得と見なされ、取り直しが際限なく走る。

行の削除・値の書き換えは行わない（欠損を埋めるだけ）。

    python scripts/backfill_road_type.py --dry-run
    python scripts/backfill_road_type.py
"""
import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.stations import coord_key, load_station_master  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STATIONS_PATH = os.path.join(DATA_DIR, "stations.json")
# この列を持たなかった時期は ROAD_TYPE="3" 固定で取得していたため、
# マスタから引けない座標はこの値で埋めてよい。
LEGACY_ROAD_TYPE = "3"

ARCHIVES = [
    os.path.join(DATA_DIR, "archive", "traffic_raw.parquet"),
    os.path.join(DATA_DIR, "archive", "traffic_hourly.parquet"),
]


def backfill(path: str, master: dict, dry_run: bool) -> None:
    if not os.path.exists(path):
        print(f"  {path}: ファイルなし、スキップ")
        return
    df = pd.read_parquet(path)
    before_rows = len(df)
    if "road_type" not in df.columns:
        df["road_type"] = None

    missing = df["road_type"].isna()
    filled = df.loc[missing].apply(
        lambda r: (master.get(coord_key(r["lon"], r["lat"])) or {}).get("road_type"),
        axis=1,
    ) if missing.any() else pd.Series(dtype=object)

    n_missing = int(missing.sum())
    n_master = int(filled.notna().sum()) if len(filled) else 0
    if n_missing and n_master < n_missing:
        # 配信が止まってマスタに残っていない観測点（9310183 など）。
        # この列を持たなかった時期のアーカイブは ROAD_TYPE="3" 固定で
        # 取得していたので、種別3であることが取得方法から確定する。
        rest = df.loc[missing][filled.isna()][["lon", "lat"]].drop_duplicates()
        print(f"    マスタに無い座標 {len(rest)}地点は、取得時のフィルタから種別3と確定:")
        print(rest.to_string(index=False))
        filled = filled.fillna(LEGACY_ROAD_TYPE)
    n_filled = int(filled.notna().sum()) if len(filled) else 0
    print(f"  {os.path.basename(path)}: {before_rows}行 / 欠損 {n_missing}行 → 埋まる {n_filled}行")
    if dry_run or not n_filled:
        return

    df.loc[missing, "road_type"] = filled
    assert len(df) == before_rows, "行数が変わってはいけない"
    df.to_parquet(path, index=False)
    print(f"    書き込み完了: {df['road_type'].value_counts(dropna=False).to_dict()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="書き込まずに件数だけ表示する")
    args = ap.parse_args()

    master = load_station_master(STATIONS_PATH)
    typed = sum(1 for v in master.values() if v.get("road_type"))
    print(f"観測点マスタ: {len(master)}地点（うち道路種別あり {typed}地点）")
    for path in ARCHIVES:
        backfill(path, master, args.dry_run)


if __name__ == "__main__":
    main()
