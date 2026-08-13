"""
モバイル空間統計（リアルタイム国内人口分布）を、熊本県内の500mメッシュ×
1時間の人口推計値に集計する。

入力（いずれも公開repoには置かない。data/mss/ は .gitignore 済み）
  data/mss/過去分/01_total.csv.zip            2026-07-21 00時〜07-28 23時（属性：総数）
  data/mss/realtime/YYYYMMDD/*_00000.csv.zip  1時間ごとの配信分（同上）
  ※ *_00001〜00003 は性年代別・居住地別。今回は使わない

出力（data/mss_build/。ここも .gitignore 済み。非公開ストレージへ置くもの）
  mesh_population.parquet          mesh × 時刻 の人口推計値（long）
  mesh_population_summary.parquet  mesh ごとの位置と発災前後の水準
  mesh_population_meta.json        期間・件数・出典・秘匿の扱い

配布データそのものは公開できないため、アプリが描くのに必要な粒度
（熊本県内・500mメッシュ・1時間・総数のみ）まで落としたものだけを出力する。
元データに戻せる情報（性年代・居住地）は含めない。

熊本県内の判定は、500mメッシュの中心が国土数値情報の行政区域（N03）の
熊本県のポリゴンに入るかどうか。メッシュの緯度経度はメッシュコードから
計算しているので、メッシュのシェープファイルは要らない。

    python scripts/build_mesh_population.py
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "mss"
OUT = ROOT / "data" / "mss_build"
HOLIDAYS = ROOT / "data" / "holidays.json"

# 国土数値情報 行政区域（令和6年）。熊本県=43
N03_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2024/N03-20240101_43_GML.zip"
N03_ZIP = OUT / "n03_43.zip"
N03_NAME = "N03-20240101_43.geojson"

# 発災（本震）。この時刻の前を「発災前」とする
QUAKE_AT = pd.Timestamp("2026-07-28 16:27")

# 提供データの秘匿処理。10人未満のメッシュは配信されない
MIN_POPULATION = 10

# 熊本市の行政区。N03の名称欄には「熊本市」としか入らないため、
# 行政区域コードから補う（2,030メッシュが全部「熊本市」では場所が分からない）
KUMAMOTO_WARDS = {
    "43101": "中央区", "43102": "東区", "43103": "西区",
    "43104": "南区", "43105": "北区",
}


def _mesh_center(codes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    500mメッシュコード（9桁）から中心の緯度経度を出す。

    1〜4桁目が1次メッシュ（緯度は1.5倍して整数部、経度は+100）、
    5・6桁目が2次（1次を8分割）、7・8桁目が3次（2次を10分割＝1km）、
    9桁目が3次の4分割（1:南西 2:南東 3:北西 4:北東）。
    """
    d = np.array([list(s) for s in codes], dtype="U1").astype(np.int64)
    lat = (d[:, 0] * 10 + d[:, 1]) / 1.5 + d[:, 4] / 12 + d[:, 6] / 120
    lon = (d[:, 2] * 10 + d[:, 3]) + 100 + d[:, 5] / 8 + d[:, 7] / 80
    q = d[:, 8]
    lat = lat + ((q - 1) // 2) / 240 + 1 / 480
    lon = lon + ((q - 1) % 2) / 160 + 1 / 320
    return lat, lon


def kumamoto_meshes() -> pd.DataFrame:
    """熊本県内（メッシュ中心で判定）の500mメッシュの一覧を作る。"""
    import geopandas as gpd  # 集計時だけ使う。アプリ側では使わない

    OUT.mkdir(parents=True, exist_ok=True)
    if not N03_ZIP.exists():
        import requests

        print(f"行政区域をダウンロード: {N03_URL}")
        r = requests.get(N03_URL, timeout=180)
        r.raise_for_status()
        N03_ZIP.write_bytes(r.content)

    cities = gpd.read_file(f"zip://{N03_ZIP}!{N03_NAME}")
    pref = cities.union_all()
    west, south, east, north = pref.bounds

    # 県の外接矩形を500mメッシュで刻み、中心が県内のものだけ残す
    lat_step, lon_step = 1 / 240, 1 / 160
    lats = np.arange(south - lat_step, north + lat_step, lat_step)
    lons = np.arange(west - lon_step, east + lon_step, lon_step)
    grid_lat, grid_lon = np.meshgrid(lats, lons, indexing="ij")
    grid_lat, grid_lon = grid_lat.ravel(), grid_lon.ravel()

    codes = _mesh_code(grid_lat, grid_lon)
    lat, lon = _mesh_center(codes)  # 刻みの端のずれを中心に直す
    frame = pd.DataFrame({"mesh": codes.astype(np.int64), "lat": lat, "lon": lon})
    frame = frame.drop_duplicates("mesh").reset_index(drop=True)

    pts = gpd.points_from_xy(frame["lon"], frame["lat"], crs="EPSG:4326")
    frame = frame[gpd.GeoSeries(pts).within(pref).values].reset_index(drop=True)

    # メッシュコードだけでは場所が分からないので、市区町村名を持たせる
    # （政令市は N03_003 に市名、N03_004 に区名が入る）
    points = gpd.GeoDataFrame(
        frame,
        geometry=gpd.points_from_xy(frame["lon"], frame["lat"]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(
        points, cities[["N03_003", "N03_004", "N03_007", "geometry"]],
        how="left", predicate="within",
    ).drop_duplicates("mesh")
    name = (joined["N03_003"].fillna("") + joined["N03_004"].fillna("")).str.strip()
    # 熊本市の区は、この年次のN03では名称の欄に入らず行政区域コードにしかない
    name = name + joined["N03_007"].map(KUMAMOTO_WARDS).fillna("")
    frame["city"] = name.values
    print(f"熊本県内の500mメッシュ: {len(frame):,}"
          f"（市区町村名なし {int((frame['city'] == '').sum())}）")
    return frame


def _mesh_code(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """緯度経度から500mメッシュコード（9桁）を作る。"""
    p_lat, r_lat = np.divmod(lat * 1.5, 1)
    p_lon, r_lon = np.divmod(lon - 100, 1)
    s_lat, r_lat = np.divmod(r_lat * 8, 1)
    s_lon, r_lon = np.divmod(r_lon * 8, 1)
    t_lat, r_lat = np.divmod(r_lat * 10, 1)
    t_lon, r_lon = np.divmod(r_lon * 10, 1)
    q = (r_lat >= 0.5) * 2 + (r_lon >= 0.5) + 1
    return np.char.add(
        np.char.add(
            np.char.add(
                np.array([f"{a:02d}{b:02d}" for a, b in
                          zip(p_lat.astype(int), p_lon.astype(int))]),
                np.array([f"{a:d}{b:d}" for a, b in
                          zip(s_lat.astype(int), s_lon.astype(int))]),
            ),
            np.array([f"{a:d}{b:d}" for a, b in
                      zip(t_lat.astype(int), t_lon.astype(int))]),
        ),
        q.astype(int).astype(str),
    )


def _read_csv(handle, keep: np.ndarray) -> list[pd.DataFrame]:
    """総数ファイルを分割読みし、熊本県内のメッシュだけ残す。"""
    parts = []
    for chunk in pd.read_csv(
        handle,
        usecols=["date", "time", "area", "population"],
        dtype={"date": "int32", "time": "int32",
               "area": "int64", "population": "int32"},
        chunksize=2_000_000,
    ):
        parts.append(chunk[chunk["area"].isin(keep)])
    return parts


def load_population(mesh: pd.DataFrame) -> pd.DataFrame:
    keep = pd.Index(mesh["mesh"])
    parts: list[pd.DataFrame] = []

    hist = SRC / "過去分" / "01_total.csv.zip"
    with zipfile.ZipFile(hist) as z:
        name = z.namelist()[0]
        print(f"読み込み: {hist.name} / {name}")
        with z.open(name) as f:
            parts += _read_csv(f, keep)

    for day in sorted((SRC / "realtime").iterdir()):
        if not day.is_dir():
            continue
        files = sorted(day.glob("*_00000.csv.zip"))  # 00000 が総数
        print(f"読み込み: realtime/{day.name}（{len(files)}時点）")
        for path in files:
            with zipfile.ZipFile(path) as z:
                with z.open(z.namelist()[0]) as f:
                    parts += _read_csv(f, keep)

    df = pd.concat(parts, ignore_index=True)
    df["datetime"] = pd.to_datetime(
        df["date"].astype(str) + df["time"].astype(str).str.zfill(4),
        format="%Y%m%d%H%M",
    )
    df = df.drop(columns=["date", "time"]).rename(
        columns={"area": "mesh", "population": "population"}
    )
    df = df.drop_duplicates(["mesh", "datetime"]).sort_values(["mesh", "datetime"])
    return df.reset_index(drop=True)


def day_type(index: pd.DatetimeIndex) -> pd.Series:
    """平常時の平均を取る単位。祝日は日曜と同じ扱い（交通量側と揃える）。"""
    holidays = set(json.loads(HOLIDAYS.read_text(encoding="utf-8")))
    is_holiday = pd.Series(index.strftime("%Y-%m-%d"), index=range(len(index))).isin(
        holidays
    )
    dow = pd.Series(index.dayofweek.values)
    out = pd.Series("平日", index=range(len(index)))
    out[dow == 5] = "土"
    out[(dow == 6) | is_holiday] = "日祝"
    return out


def build_summary(df: pd.DataFrame, mesh: pd.DataFrame) -> pd.DataFrame:
    """
    メッシュごとの発災前後の水準。地図の色分けに使う。

    深夜（2〜4時）を分けて出すのは、その時間帯がほぼ滞在（就寝）人口で、
    昼夜の移動に左右されずに「そこに残っているか」を見られるため。
    """
    pre = df[df["datetime"] < QUAKE_AT]
    post = df[df["datetime"] >= QUAKE_AT]
    night = df["datetime"].dt.hour.isin([2, 3, 4])

    def _mean(frame: pd.DataFrame, name: str) -> pd.Series:
        return frame.groupby("mesh")["population"].mean().rename(name)

    out = mesh.set_index("mesh").join(
        [
            _mean(pre, "pre_mean"),
            _mean(post, "post_mean"),
            _mean(pre[night.loc[pre.index]], "pre_night"),
            _mean(post[night.loc[post.index]], "post_night"),
            df.groupby("mesh")["population"].max().rename("max_population"),
            df.groupby("mesh")["datetime"].count().rename("n_hours"),
        ],
        how="left",
    )
    out["ratio"] = out["post_mean"] / out["pre_mean"]
    out["ratio_night"] = out["post_night"] / out["pre_night"]
    return out.reset_index()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    mesh = kumamoto_meshes()
    df = load_population(mesh)

    hours = pd.date_range(df["datetime"].min(), df["datetime"].max(), freq="h")
    print(
        f"人口: {len(df):,}行 / メッシュ {df['mesh'].nunique():,} / "
        f"{df['datetime'].min()}〜{df['datetime'].max()}（{len(hours)}時点）"
    )

    summary = build_summary(df, mesh)
    summary = summary[summary["n_hours"].notna()].reset_index(drop=True)

    # 時刻は開始時点からの通し番号で持つ（1時間ごとに欠けなく並ぶため）
    start = df["datetime"].min()
    df["t"] = ((df["datetime"] - start) // pd.Timedelta(hours=1)).astype("int16")
    table = df[["mesh", "t", "population"]].copy()
    table["mesh"] = table["mesh"].astype("int32")
    table["population"] = table["population"].astype("int32")
    table.to_parquet(OUT / "mesh_population.parquet", index=False)
    summary.to_parquet(OUT / "mesh_population_summary.parquet", index=False)

    types = day_type(pd.DatetimeIndex(hours))
    meta = {
        "source": "モバイル空間統計®（NTTドコモ／ドコモ・インサイトマーケティング）"
                  "リアルタイム国内人口分布",
        "area": "熊本県内（500mメッシュの中心が県域に入るもの）",
        "unit": "500mメッシュ・1時間・人口推計値（総数）",
        "start": start.isoformat(),
        "hours": int(len(hours)),
        "end": df["datetime"].max().isoformat(),
        "quake_at": QUAKE_AT.isoformat(),
        "meshes": int(df["mesh"].nunique()),
        "rows": int(len(df)),
        "min_population": MIN_POPULATION,
        "suppressed_note": f"{MIN_POPULATION}人未満のメッシュは配信されないため、"
                           "値が無い時間帯は0ではなく「10人未満または欠測」",
        "day_type_days": {
            k: int(v) for k, v in
            types.groupby(types).size().items()
        },
        "boundary": "国土数値情報 行政区域（N03-20240101_43）",
    }
    (OUT / "mesh_population_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    for path in ("mesh_population.parquet", "mesh_population_summary.parquet",
                 "mesh_population_meta.json"):
        size = (OUT / path).stat().st_size
        print(f"  {path}: {size/1e6:.1f} MB")


if __name__ == "__main__":
    main()
