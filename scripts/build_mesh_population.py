"""
モバイル空間統計（リアルタイム国内人口分布）を、熊本県内の500mメッシュ×
1時間の人口推計値に集計する。

入力（いずれも公開repoには置かない。data/mss/ は .gitignore 済み）
  data/mss/過去分/01_total.csv.zip            2026-07-21 00時〜07-28 23時（属性：総数）
  data/mss/realtime/YYYYMMDD/*_00000.csv.zip  1時間ごとの配信分（同上）
  data/mss/過去分/04_residence_city.csv.zip   同じ期間の居住地市区町村別
  data/mss/realtime/YYYYMMDD/*_00003.csv.zip  1時間ごとの配信分（同上）
  ※ *_00001（性年代別）・*_00002（居住地都道府県別）は使わない

出力（data/mss_build/。ここも .gitignore 済み。非公開ストレージへ置くもの）
  mesh_population.parquet          mesh × 時刻 の人口推計値（long）
  mesh_population_summary.parquet  mesh ごとの位置と発災前後の水準
  mesh_population_meta.json        期間・件数・出典・秘匿の扱い

配布データそのものは公開できないため、アプリが描くのに必要な粒度
（熊本県内・500mメッシュ・1時間）まで落としたものだけを出力する。
居住地別は「そのメッシュのある市区町村の居住者か・それ以外か」の2区分に
畳んでから集計し、居住地の市区町村コードは残さない（元データに戻せない）。
性年代別は読み込まない。

メッシュと市区町村の対応は面積被覆で決める。500mメッシュの四角形と国土数値情報の
行政区域（N03）の重なり面積を測り、いちばん広く占める市区町村をそのメッシュの
市区町村とし、それが熊本県内ならメッシュを対象に入れる。メッシュの四角形は
メッシュコードから計算しているので、メッシュのシェープファイルは要らない。

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

# 国土数値情報 行政区域（令和6年）。熊本県=43 と、県境をまたぐメッシュの
# 判定に要る隣接4県（福岡40・大分44・宮崎45・鹿児島46）
N03_URL = ("https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2024/"
           "N03-20240101_{pref}_GML.zip")
N03_PREFS = ("43", "40", "44", "45", "46")
KUMAMOTO = "43"

# 面積を測るための平面直角座標系（第II系：熊本と隣接県を含む）
PLANE_CRS = "EPSG:6670"

# 発災（本震）。この時刻の前を「発災前」とする
QUAKE_AT = pd.Timestamp("2026-07-28 16:27")

# 提供データの秘匿処理。10人未満のメッシュは配信されない
MIN_POPULATION = 10

# 夜間・昼間の代表時刻（国勢調査の夜間人口・昼間人口とは別物なので、
# その語では呼ばない。ここで数えているのはその時刻の滞留人口）
REPRESENTATIVE_HOURS = (3, 14)

# 500mメッシュの大きさ（緯度1/240度・経度1/160度）
LAT_STEP = 1 / 240
LON_STEP = 1 / 160

# 熊本市の行政区。N03の名称欄には「熊本市」としか入らないため、
# 行政区域コードから補う（1,490メッシュが全部「熊本市」では場所が分からない）
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


def _cities():
    """熊本県＋隣接4県の市区町村ポリゴン（行政区域コードで融合したもの）。"""
    import geopandas as gpd

    OUT.mkdir(parents=True, exist_ok=True)
    frames = []
    for pref in N03_PREFS:
        path = OUT / f"n03_{pref}.zip"
        if not path.exists():
            import requests

            url = N03_URL.format(pref=pref)
            print(f"行政区域をダウンロード: {url}")
            r = requests.get(url, timeout=300)
            r.raise_for_status()
            path.write_bytes(r.content)
        g = gpd.read_file(f"zip://{path}!N03-20240101_{pref}.geojson")
        frames.append(g[["N03_003", "N03_004", "N03_007", "geometry"]])

    cities = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True),
                              crs="EPSG:4326")
    cities = cities.dissolve(by="N03_007", as_index=False)
    # 政令市は N03_003 に市名、N03_004 に区名が入る。熊本市の区はこの年次の
    # N03では名称の欄に入らず行政区域コードにしかないので、そこから補う
    name = (cities["N03_003"].fillna("") + cities["N03_004"].fillna("")).str.strip()
    cities["city"] = name + cities["N03_007"].map(KUMAMOTO_WARDS).fillna("")
    cities["city_code"] = cities["N03_007"].astype(str)
    return cities[["city_code", "city", "geometry"]]


def kumamoto_meshes() -> pd.DataFrame:
    """
    熊本県内の500mメッシュの一覧を作る。

    メッシュと市区町村の対応は面積被覆で決める。メッシュの四角形と市区町村
    ポリゴンの重なり面積を測り、いちばん広く占める市区町村をそのメッシュの
    市区町村とする。それが熊本県内なら対象に入れる。市区町村界をまたぐメッシュを
    中心点だけで判定すると、面積では負けている側に寄ることがあるため
    （熊本県内で128メッシュ）、面積で決める。

    海面はどの市区町村にも属さないので比較に入らない。したがって中心が海上でも
    陸が少しでもあるメッシュは、その陸の市区町村に入る。県境をまたぐメッシュを
    正しく落とすため、隣接4県の市区町村も比較対象に入れている。
    """
    import geopandas as gpd  # 集計時だけ使う。アプリ側では使わない
    from shapely.geometry import box

    cities = _cities()
    kuma = cities[cities["city_code"].str.startswith(KUMAMOTO)]
    west, south, east, north = kuma.union_all().bounds

    # 県の外接矩形を500mメッシュで刻む（面積で判定するので端は1メッシュ広く取る）
    lats = np.arange(south - LAT_STEP, north + LAT_STEP, LAT_STEP)
    lons = np.arange(west - LON_STEP, east + LON_STEP, LON_STEP)
    grid_lat, grid_lon = np.meshgrid(lats, lons, indexing="ij")
    codes = np.unique(_mesh_code(grid_lat.ravel(), grid_lon.ravel()))

    lat, lon = _mesh_center(codes)  # 刻みの端のずれを中心に直す
    cells = gpd.GeoDataFrame(
        {"mesh": codes.astype(np.int64), "lat": lat, "lon": lon},
        geometry=[box(x, y, x + LON_STEP, y + LAT_STEP)
                  for x, y in zip(lon - LON_STEP / 2, lat - LAT_STEP / 2)],
        crs="EPSG:4326",
    )

    overlap = gpd.overlay(cells[["mesh", "geometry"]].to_crs(PLANE_CRS),
                          cities.to_crs(PLANE_CRS),
                          how="intersection", keep_geom_type=True)
    overlap["area"] = overlap.geometry.area
    top = (overlap.sort_values("area", ascending=False)
                  .drop_duplicates("mesh")
                  .set_index("mesh"))

    frame = cells.drop(columns="geometry")
    frame["city"] = frame["mesh"].map(top["city"])
    # 居住地市区町村別の集計で「当該市区町村の居住者か」を判定するのに使う。
    # 配布データの居住地コードは5桁で、熊本市は区単位（43101〜43105）まで
    # 入っており、行政区域コードとそのまま突き合わせられる。
    frame["city_code"] = frame["mesh"].map(top["city_code"])
    frame = frame[frame["city_code"].fillna("").str.startswith(KUMAMOTO)]
    frame = frame.reset_index(drop=True)
    print(f"熊本県内の500mメッシュ: {len(frame):,}（面積被覆で判定）")
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


def load_residence(mesh: pd.DataFrame) -> pd.DataFrame:
    """
    居住地市区町村別を、「そのメッシュのある市区町村の居住者か」の2区分に
    畳んで集計する。

    配布データは 居住地の市区町村コード × メッシュ × 時点 の細かい表なので、
    そのまま持つと重い。ここでは居住者（rs）と来訪者（vi、＝それ以外の
    市区町村に住む人）の2つに足し込む。

    総数（01_total）とは足し合わせが一致しない。居住地別は「居住地ごとに
    10人未満なら配信されない」ので、内訳の合計は総数より小さくなる。
    """
    own = mesh.set_index("mesh")["city_code"]
    keep = pd.Index(mesh["mesh"])
    parts: list[pd.DataFrame] = []
    group = ["mesh", "res", "date", "time"]

    def _read(handle) -> None:
        for chunk in pd.read_csv(
            handle,
            usecols=["date", "time", "area", "residence", "population"],
            dtype={"date": "int32", "time": "int32", "area": "int64",
                   "residence": "str", "population": "int32"},
            chunksize=2_000_000,
        ):
            sub = chunk[chunk["area"].isin(keep)]
            if sub.empty:
                continue
            resident = sub["residence"].values == own.reindex(sub["area"]).values
            frame = pd.DataFrame({
                "mesh": sub["area"].values,
                "res": np.where(resident, "rs", "vi"),
                "date": sub["date"].values,
                "time": sub["time"].values,
                "population": sub["population"].values,
            })
            parts.append(
                frame.groupby(group, as_index=False)["population"].sum()
            )

    hist = SRC / "過去分" / "04_residence_city.csv.zip"
    with zipfile.ZipFile(hist) as z:
        name = z.namelist()[0]
        print(f"読み込み: {hist.name} / {name}")
        with z.open(name) as f:
            _read(f)
    # 途中でまとめて行数を抑える（分割読みの境目で同じ時点が分かれるため、
    # 足し合わせは何回に分けても同じ）
    parts = [pd.concat(parts, ignore_index=True)
             .groupby(group, as_index=False)["population"].sum()]

    for day in sorted((SRC / "realtime").iterdir()):
        if not day.is_dir():
            continue
        files = sorted(day.glob("*_00003.csv.zip"))  # 00003 が居住地市区町村別
        print(f"読み込み: realtime/{day.name}（居住地別 {len(files)}時点）")
        for path in files:
            with zipfile.ZipFile(path) as z:
                with z.open(z.namelist()[0]) as f:
                    _read(f)

    df = pd.concat(parts, ignore_index=True).groupby(
        group, as_index=False
    )["population"].sum()
    df["datetime"] = pd.to_datetime(
        df["date"].astype(str) + df["time"].astype(str).str.zfill(4),
        format="%Y%m%d%H%M",
    )
    print(f"居住地別: {len(df):,}行（居住者・来訪者に畳んだ後）")
    return df.drop(columns=["date", "time"])


def is_weekday(index: pd.DatetimeIndex) -> pd.Series:
    """平日か（土日祝でないか）。祝日の扱いは交通量側と揃える。"""
    holidays = set(json.loads(HOLIDAYS.read_text(encoding="utf-8")))
    dates = pd.Series(index.strftime("%Y-%m-%d"), index=range(len(index)))
    dow = pd.Series(index.dayofweek.values)
    return ~(dow.isin([5, 6]) | dates.isin(holidays))


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


def _phase_means(frame: pd.DataFrame, prefix: str) -> list:
    """
    1つの母集団（総数／居住者／来訪者）について、日区分×時間帯ごとの
    発災前・発災後の平均を出す。
    """
    weekday = is_weekday(pd.DatetimeIndex(frame["datetime"]))
    weekday.index = frame.index
    hour = frame["datetime"].dt.hour

    day_masks = {
        "all": pd.Series(True, index=frame.index),
        "wd": weekday,
        "hd": ~weekday,
    }
    hour_masks = {"all": pd.Series(True, index=frame.index)}
    for h in REPRESENTATIVE_HOURS:
        hour_masks[f"h{h}"] = hour == h

    is_pre = frame["datetime"] < QUAKE_AT
    joins = []
    for day, day_mask in day_masks.items():
        for hour_key, hour_mask in hour_masks.items():
            both = day_mask & hour_mask
            for phase, phase_mask in (("pre", is_pre), ("post", ~is_pre)):
                sub = frame[both & phase_mask]
                joins.append(
                    sub.groupby("mesh")["population"].mean()
                    .rename(f"{phase}_{prefix}_{day}_{hour_key}")
                )
    return joins


def build_summary(df: pd.DataFrame, mesh: pd.DataFrame,
                  residence: pd.DataFrame = None) -> pd.DataFrame:
    """
    メッシュごとの発災前後の水準。地図の色分けに使う。

    3つの区分の組み合わせで平均を出す。
      ・母集団（総数／当該市区町村の居住者／それ以外＝来訪者）
      ・日区分（全日／平日／休日）
      ・時間帯（全時間帯／3時／14時）

    3時はほぼ就寝中、14時は通勤通学が済んだ後で、どちらも移動の途中が
    混じりにくい時刻。国勢調査の夜間人口・昼間人口は常住地・従業地で数える
    別の定義なので、その語は使わない。日区分を分けるのは、平日と休日で人の
    居場所が元から違うためで、発災前の平日平均には発災後の平日を、休日平均
    には休日を当てて比べる。

    列の名前は pre_{母集団}_{日区分}_{時間帯} / post_… / ratio_…
    （母集団 all=総数 rs=居住者 vi=来訪者、日区分 all/wd/hd、
    時間帯 all/h3/h14）。
    """
    joins = _phase_means(df, "all")
    if residence is not None:
        for key in ("rs", "vi"):
            joins += _phase_means(residence[residence["res"] == key], key)
    joins += [
        df.groupby("mesh")["population"].max().rename("max_population"),
        df.groupby("mesh")["datetime"].count().rename("n_hours"),
    ]

    out = mesh.set_index("mesh").join(joins, how="left")
    groups = ("all", "rs", "vi") if residence is not None else ("all",)
    for res in groups:
        for day in ("all", "wd", "hd"):
            for hour_key in ("all", *(f"h{h}" for h in REPRESENTATIVE_HOURS)):
                out[f"ratio_{res}_{day}_{hour_key}"] = (
                    out[f"post_{res}_{day}_{hour_key}"]
                    / out[f"pre_{res}_{day}_{hour_key}"]
                )
    return out.reset_index()


def _phase_days(hours: pd.DatetimeIndex) -> dict:
    """発災前後それぞれの平日・休日の日数（日付の数。時点の数ではない）。"""
    weekday = is_weekday(hours).values
    frame = pd.DataFrame({
        "date": hours.strftime("%Y-%m-%d"),
        "weekday": weekday,
        "phase": ["pre" if t < QUAKE_AT else "post" for t in hours],
    }).drop_duplicates(["date", "phase"])
    out = {}
    for phase in ("pre", "post"):
        sub = frame[frame["phase"] == phase]
        out[phase] = {
            "平日": int(sub["weekday"].sum()),
            "休日": int((~sub["weekday"]).sum()),
        }
    return out


def write_gis(summary: pd.DataFrame) -> None:
    """
    QGISで開ける形で、メッシュのポリゴンと集計値を書き出す。

    アプリが地図に描いているものと同じ中身で、こちらは全メッシュを入れる
    （アプリは全時点で配信された分だけを描く。`n_hours` で絞れる）。
    座標系はWGS84（EPSG:4326）。GeoJSONはどこでも開ける代わりに大きく、
    GeoPackageは型が保たれてQGISでは軽い。用途で選べるよう両方出す。
    """
    import geopandas as gpd
    from shapely.geometry import box

    frame = summary.copy()
    # 丸めるのは値の列だけ。緯度経度を丸めると、その値からポリゴンを作った
    # ときに位置が0.1度単位へ寄ってしまう（500mの四角が0.1度おきに点々と
    # 並ぶ状態になっていた）。
    for col in frame.select_dtypes("float").columns:
        if col in ("lat", "lon"):
            frame[col] = frame[col].round(6)
        else:
            frame[col] = frame[col].round(1)
    # ポリゴンはメッシュコードから作る（丸めた列を使わない）。アプリの地図が
    # 使っている modules/mesh_population.mesh_bounds と同じ計算。
    lat, lon = _mesh_center(frame["mesh"].astype(str).values)
    # 角は7桁（約1cm）に丸める。丸めないと 1e-14 度のずれが残り、隣どうしの
    # メッシュの辺がぴったり接しない（GISで境界に髪の毛の隙間が出る）。
    def _r(value: float) -> float:
        return round(value, 7)

    geometry = [
        box(_r(x - LON_STEP / 2), _r(y - LAT_STEP / 2),
            _r(x + LON_STEP / 2), _r(y + LAT_STEP / 2))
        for y, x in zip(lat, lon)
    ]
    gdf = gpd.GeoDataFrame(frame, geometry=geometry, crs="EPSG:4326")
    gdf.to_file(OUT / "mesh_population_summary.gpkg", layer="mesh_population",
                driver="GPKG")
    gdf.to_file(OUT / "mesh_population_summary.geojson", driver="GeoJSON")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    mesh = kumamoto_meshes()
    df = load_population(mesh)

    hours = pd.date_range(df["datetime"].min(), df["datetime"].max(), freq="h")
    print(
        f"人口: {len(df):,}行 / メッシュ {df['mesh'].nunique():,} / "
        f"{df['datetime'].min()}〜{df['datetime'].max()}（{len(hours)}時点）"
    )

    residence = load_residence(mesh)
    summary = build_summary(df, mesh, residence)
    summary = summary[summary["n_hours"].notna()].reset_index(drop=True)

    # 時刻は開始時点からの通し番号で持つ（1時間ごとに欠けなく並ぶため）
    start = df["datetime"].min()
    df["t"] = ((df["datetime"] - start) // pd.Timedelta(hours=1)).astype("int16")
    table = df[["mesh", "t", "population"]].copy()
    # 時系列の図で居住者・来訪者を色分けするため、内訳も同じ表に持たせる。
    # 総数（population）とは一致しない（内訳は居住地ごとに10人未満だと
    # 配信されないので、pop_rs + pop_vi <= population）。
    wide = residence.assign(
        t=((residence["datetime"] - start) // pd.Timedelta(hours=1)).astype("int16")
    ).pivot_table(
        index=["mesh", "t"], columns="res", values="population",
        aggfunc="sum", fill_value=0,
    ).rename(columns={"rs": "pop_rs", "vi": "pop_vi"})
    table = table.merge(wide.reset_index(), on=["mesh", "t"], how="left")
    for col in ("mesh", "population", "pop_rs", "pop_vi"):
        table[col] = table[col].fillna(0).astype("int32")
    table.to_parquet(OUT / "mesh_population.parquet", index=False)
    summary.to_parquet(OUT / "mesh_population_summary.parquet", index=False)
    write_gis(summary)

    types = day_type(pd.DatetimeIndex(hours))
    meta = {
        "source": "モバイル空間統計®（NTTドコモ／ドコモ・インサイトマーケティング）"
                  "リアルタイム国内人口分布",
        "area": "熊本県内（500mメッシュの面積をいちばん広く占める市区町村が"
                "熊本県内のもの。海面は比較に入れない）",
        "city_rule": "面積被覆。メッシュの四角形と行政区域（N03-20240101、"
                     "熊本県と隣接4県）の重なり面積が最大の市区町村を割り当てる",
        "unit": "500mメッシュ・1時間・人口推計値（総数と居住地別）",
        "start": start.isoformat(),
        "hours": int(len(hours)),
        "end": df["datetime"].max().isoformat(),
        "quake_at": QUAKE_AT.isoformat(),
        "meshes": int(df["mesh"].nunique()),
        "rows": int(len(df)),
        "min_population": MIN_POPULATION,
        # 居住地別（居住者＋来訪者）の合計が総数の何割か。居住地ごとに
        # 10人未満だと配信されないので、内訳の合計は総数より小さくなる。
        "residence_coverage": round(
            float(residence["population"].sum() / df["population"].sum()), 4
        ),
        "representative_hours": list(REPRESENTATIVE_HOURS),
        "suppressed_note": f"{MIN_POPULATION}人未満のメッシュは配信されないため、"
                           "値が無い時間帯は0ではなく「10人未満または欠測」",
        "day_type_days": {
            k: int(v) for k, v in
            types.groupby(types).size().items()
        },
        # 平日・休日それぞれ何日分あるか（地図の比の厚みを画面に出すため）。
        # 本震のあった日は前後にまたがるので、両方で1日と数える。
        "phase_days": _phase_days(hours),
        "boundary": "国土数値情報 行政区域（N03-20240101。熊本県43と隣接4県"
                    "40・44・45・46）",
    }
    (OUT / "mesh_population_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    for path in ("mesh_population.parquet", "mesh_population_summary.parquet",
                 "mesh_population_meta.json",
                 "mesh_population_summary.gpkg",
                 "mesh_population_summary.geojson"):
        size = (OUT / path).stat().st_size
        print(f"  {path}: {size/1e6:.1f} MB")


if __name__ == "__main__":
    main()
