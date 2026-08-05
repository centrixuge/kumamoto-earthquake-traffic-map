"""
観測点マスタ（JARTICの「常時観測点コード」と緯度経度の対応）を扱うモジュール。

既存のアーカイブは point_code 列を持たない時期のデータを含むため、
座標からコードを引けるマスタを別に持ち、集計時に付け直す。
座標はAPIのレスポンスによって末尾の桁がわずかに揺れることがあるので、
5桁（約1m）に丸めた値で突き合わせる。
"""
import json
import os
from typing import Dict

import pandas as pd

COORD_DECIMALS = 5


def coord_key(lon: float, lat: float) -> str:
    return f"{round(float(lon), COORD_DECIMALS)}_{round(float(lat), COORD_DECIMALS)}"


def build_station_master(df: pd.DataFrame) -> Dict[str, dict]:
    """
    point_code 列を持つデータフレームから観測点マスタを作る。
    戻り値は {座標キー: {"point_code":…, "lon":…, "lat":…}}。
    """
    if df.empty or "point_code" not in df.columns:
        return {}
    cols = ["point_code", "lon", "lat"]
    if "road_type" in df.columns:
        cols.append("road_type")
    sub = df.dropna(subset=["point_code"])[cols].drop_duplicates()
    master = {}
    for _, r in sub.iterrows():
        entry = {
            "point_code": str(int(r["point_code"])),
            "lon": float(r["lon"]),
            "lat": float(r["lat"]),
        }
        # 道路種別はアーカイブの古い行には入っていないので、マスタ側に持たせて
        # 後から付け直す（point_code と同じ扱い）。
        if "road_type" in sub.columns and pd.notna(r.get("road_type")):
            entry["road_type"] = str(r["road_type"])
        master[coord_key(r["lon"], r["lat"])] = entry
    return master


def merge_station_master(existing: Dict[str, dict], new: Dict[str, dict]) -> Dict[str, dict]:
    """
    既存のマスタに新しい観測点を足す。同じ座標の項目は新しい値で上書きするが、
    新しい側に無いキー（道路種別を持たない時期のデータなど）は消さない。
    """
    merged = dict(existing or {})
    for k, v in (new or {}).items():
        entry = dict(merged.get(k) or {})
        entry.update(v)
        merged[k] = entry
    return merged


def load_station_master(path: str) -> Dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_station_master(master: Dict[str, dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=2, sort_keys=True)


def attach_point_code(df: pd.DataFrame, master: Dict[str, dict]) -> pd.DataFrame:
    """
    lon/lat から point_code を引いて列として付ける（既にあれば欠損だけ埋める）。
    マスタに無い座標は欠損のままにして、黙って別IDを作らないようにする。
    """
    if df.empty or not master:
        return df
    df = df.copy()
    codes = df.apply(
        lambda r: (master.get(coord_key(r["lon"], r["lat"])) or {}).get("point_code"),
        axis=1,
    )
    if "point_code" in df.columns:
        df["point_code"] = df["point_code"].where(df["point_code"].notna(), codes)
        df["point_code"] = df["point_code"].map(
            lambda v: str(int(v)) if isinstance(v, (int, float)) and pd.notna(v) else v
        )
    else:
        df["point_code"] = codes

    # 道路種別も同じマスタから引く（1:高速自動車国道 / 3:一般国道）。
    types = df.apply(
        lambda r: (master.get(coord_key(r["lon"], r["lat"])) or {}).get("road_type"),
        axis=1,
    )
    if "road_type" in df.columns:
        df["road_type"] = df["road_type"].where(df["road_type"].notna(), types)
    else:
        df["road_type"] = types
    df["road_type"] = df["road_type"].map(
        lambda v: str(int(v)) if isinstance(v, (int, float)) and pd.notna(v) else v
    )
    return df
