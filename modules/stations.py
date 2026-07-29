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
    sub = df.dropna(subset=["point_code"])[["point_code", "lon", "lat"]].drop_duplicates()
    master = {}
    for _, r in sub.iterrows():
        master[coord_key(r["lon"], r["lat"])] = {
            "point_code": str(int(r["point_code"])),
            "lon": float(r["lon"]),
            "lat": float(r["lat"]),
        }
    return master


def merge_station_master(existing: Dict[str, dict], new: Dict[str, dict]) -> Dict[str, dict]:
    merged = dict(existing or {})
    merged.update(new or {})
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
    return df
