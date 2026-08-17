"""
アプリが使っている500mメッシュについて、メッシュと市区町村の対応テーブルを書き出す。

アプリの対応付けは面積被覆（メッシュ面積をいちばん広く占める市区町村）で、
build_mesh_population.py の kumamoto_meshes() がそうしている。ここでは、その割当と
メッシュ面積に占める割合、他の市区町村がどれだけ食い込んでいるかを書き出す。
以前使っていた中心点の点内判定も参考として付け、食い違うメッシュに印を付ける。

出力（data/mss_build/。モバイル空間統計そのものは含まない。中身は
国土数値情報の行政区域とメッシュコードだけから作れるもの）
  mesh_city_table.csv        メッシュごとに1行。割当先と面積割合（参考に中心点判定）
  mesh_city_coverage.csv     メッシュ×市区町村の面積被覆（1メッシュ複数行）

    python scripts/export_mesh_city_table.py
"""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "mss_build"
PREFS = ("43", "40", "44", "45", "46")
SUMMARY = OUT / "mesh_population_summary.parquet"

# 面積を測るための平面直角座標系（第II系：九州北部・熊本を含む）
PLANE = "EPSG:6670"

LAT_STEP = 1 / 240
LON_STEP = 1 / 160

# 熊本市の区名はこの年次のN03の名称欄に入らないので、行政区域コードから補う
# （build_mesh_population.py と同じ。前に「熊本市」が付くので区名だけを足す）
KUMAMOTO_WARDS = {
    "43101": "中央区", "43102": "東区", "43103": "西区",
    "43104": "南区", "43105": "北区",
}


def mesh_corners(codes: np.ndarray):
    """500mメッシュコード（9桁）から南西角の緯度経度を出す。"""
    d = np.array([list(f"{c:09d}") for c in codes], dtype="U1").astype(np.int64)
    lat = (d[:, 0] * 10 + d[:, 1]) / 1.5 + d[:, 4] / 12 + d[:, 6] / 120
    lon = (d[:, 2] * 10 + d[:, 3]) + 100 + d[:, 5] / 8 + d[:, 7] / 80
    q = d[:, 8]
    lat = lat + ((q - 1) // 2) * LAT_STEP
    lon = lon + ((q - 1) % 2) * LON_STEP
    return lat, lon


def load_cities() -> gpd.GeoDataFrame:
    """熊本県＋隣接4県の市区町村（県境をまたぐメッシュも数えられるように）。"""
    frames = []
    for pref in PREFS:
        g = gpd.read_file(f"zip://{OUT / f'n03_{pref}.zip'}!N03-20240101_{pref}.geojson")
        frames.append(g[["N03_003", "N03_004", "N03_007", "geometry"]])
    cities = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs="EPSG:4326")
    cities = cities.dissolve(by="N03_007", as_index=False)
    name = (cities["N03_003"].fillna("") + cities["N03_004"].fillna("")).str.strip()
    name = name + cities["N03_007"].map(KUMAMOTO_WARDS).fillna("")
    cities["city"] = name
    cities["city_code"] = cities["N03_007"].astype(str)
    return cities[["city_code", "city", "geometry"]]


def main() -> None:
    summary = pd.read_parquet(SUMMARY)
    meshes = summary[["mesh", "lat", "lon", "city", "city_code", "n_hours"]].copy()
    meshes = meshes.rename(columns={"city": "city_area",
                                    "city_code": "city_code_area"})
    meshes["city_code_area"] = meshes["city_code_area"].astype(str)
    print(f"対象メッシュ: {len(meshes):,}（うち全336時点そろい "
          f"{int((meshes['n_hours'] == 336).sum()):,}）")

    south, west = mesh_corners(meshes["mesh"].to_numpy())
    geom = [box(w, s, w + LON_STEP, s + LAT_STEP) for w, s in zip(west, south)]
    cells = gpd.GeoDataFrame(meshes, geometry=geom, crs="EPSG:4326")

    cities = load_cities()
    cells_p = cells.to_crs(PLANE)
    cities_p = cities.to_crs(PLANE)
    cells_p["cell_area"] = cells_p.geometry.area

    # メッシュ × 市区町村の重なり面積
    inter = gpd.overlay(cells_p[["mesh", "cell_area", "geometry"]],
                        cities_p, how="intersection", keep_geom_type=True)
    inter["area_m2"] = inter.geometry.area
    inter["share"] = inter["area_m2"] / inter["cell_area"]
    cov = (inter[["mesh", "city_code", "city", "area_m2", "share"]]
           .sort_values(["mesh", "area_m2"], ascending=[True, False])
           .reset_index(drop=True))

    n_cities = cov.groupby("mesh")["city_code"].nunique()
    # メッシュのうち熊本県内の陸域が占める割合（残りは海、または県外）
    inside = (cov[cov["city_code"].str.startswith("43")]
              .groupby("mesh")["share"].sum())

    out = meshes.copy()
    # 割当先の市区町村が、そのメッシュの面積のどれだけを占めるか
    key = pd.MultiIndex.from_frame(cov[["mesh", "city_code"]])
    share_of = pd.Series(cov["share"].values, index=key)
    want = pd.MultiIndex.from_frame(out[["mesh", "city_code_area"]])
    out["share_area"] = pd.Series(share_of.reindex(want).values).fillna(0).round(4)
    out["n_cities"] = out["mesh"].map(n_cities).fillna(0).astype(int)
    out["kumamoto_share"] = out["mesh"].map(inside).round(4)

    # 参考：以前使っていた中心点の点内判定
    pts = gpd.GeoDataFrame(
        out[["mesh"]], geometry=gpd.points_from_xy(out["lon"], out["lat"]),
        crs="EPSG:4326")
    hit = gpd.sjoin(pts, cities, how="left", predicate="within") \
             .drop_duplicates("mesh").set_index("mesh")
    out["city_code_center"] = out["mesh"].map(hit["city_code"]).fillna("")
    out["city_center"] = out["mesh"].map(hit["city"]).fillna("")
    out["straddles"] = out["n_cities"] > 1
    out["differs"] = out["city_code_center"] != out["city_code_area"]

    out["n_hours"] = out["n_hours"].astype(int)
    out = out[["mesh", "lat", "lon", "n_hours",
               "city_code_area", "city_area", "share_area",
               "city_code_center", "city_center",
               "n_cities", "kumamoto_share", "straddles", "differs"]]
    out.to_csv(OUT / "mesh_city_table.csv", index=False, encoding="utf-8-sig")
    cov["share"] = cov["share"].round(4)
    cov["area_m2"] = cov["area_m2"].round(0)
    cov.to_csv(OUT / "mesh_city_coverage.csv", index=False, encoding="utf-8-sig")

    n = len(out)
    print(f"境界をまたぐメッシュ: {int(out['straddles'].sum()):,}"
          f"（{out['straddles'].mean():.1%}）")
    print(f"割当先の面積割合: 中央値 {out['share_area'].median():.3f}"
          f" / 0.5未満 {int((out['share_area'] < 0.5).sum()):,}")
    print(f"中心点判定と食い違うメッシュ: {int(out['differs'].sum()):,}"
          f"（{out['differs'].mean():.1%}。うち中心が海上など県内に入らない "
          f"{int((out['city_code_center'] == '').sum()):,}）")
    print("\n食い違いの多い市区町村（中心点判定 → 面積被覆＝採用）")
    d = out[out["differs"]].groupby(["city_center", "city_area"]).size()
    print(d.sort_values(ascending=False).head(12).to_string())
    print(f"\n→ {OUT / 'mesh_city_table.csv'}（{n:,}行）")
    print(f"→ {OUT / 'mesh_city_coverage.csv'}（{len(cov):,}行）")


if __name__ == "__main__":
    main()
