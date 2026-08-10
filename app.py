#!/usr/bin/env python
# coding: utf-8
"""
熊本地震（2026-07-28）前後の交通量を平常時と比べるダッシュボード。

`fetch_and_prepare.py` が生成した data/*.parquet, data/quake_info.json を
読み込んで表示するだけのビュー層。GDAL依存のgeopandas/shapelyはここでは使わない
（Streamlit Community Cloud等の軽量環境でも動かせるようにするため）。
"""
import io
import itertools
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import branca.element as branca_element
import folium
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_folium import st_folium

from modules.holidays import WEEKDAY_LABELS
from modules.stations import attach_point_code, load_station_master

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MAX_SELECTED_POINTS = 2
SELECTION_COLORS = ["red", "green"]
# 車種別ビューで大型車に使う色。選択色（赤・緑）と同系統のまま明るくして、
# 「同じ観測点の別の車種」だと分かるようにする。大型車は小型車の1割程度の
# 水準なので同じ縦軸でも下側に分かれて描かれる。
# 線種で分ける案は、平常時の点線と紛らわしくなるため採らなかった。
SELECTION_ALT_COLORS = ["#ff5fa2", "#7ac70c"]  # ピンク / ライムグリーン
# 観測点マーカーのツールチップに必ず入れる定型句。クリックされた図形が
# 観測点マーカーかどうかを、この文字列で判定する（point_id_from_tooltip）。
# 表示と判定でずれないよう1か所に置く。
POINT_TOOLTIP_HINT = "（クリックで時系列に表示/解除）"
# 交通量API利用規約（2025-05-12施行）が求める記載。
#   第5条1項: サービス提供時、エンドユーザーが利用の度に確認できる位置に明示
#   第5条2項: 加工して利用する場合の出典
#   第8条2項: 利用者が一切の責任を負う旨の明記
# 文言は規約の記載例に合わせ、3つを1文にまとめて常時表示する。
JARTIC_TERMS_NOTICE = (
    "このサービスは、交通量API 機能を使用していますが、"
    "サービスの内容は国土交通省によって保証されたものではありません。"
    "掲載している交通量は「国土交通省API 機能による交通量(参考値)を加工して作成」"
    "したものです。本サービスの作成・運営について、"
    '<a href="https://github.com/centrixuge" target="_blank">作成者</a>'
    "が一切の責任を負います。"
)

# 時系列図の表示開始時刻（データ保持期間の先頭より後ろにしている）
TIMESERIES_DISPLAY_START = pd.Timestamp("2026-07-27 12:00")
# 表示期間の選択肢。データは復旧期（本震+2週間）まで伸び続けるので、
# 常に最新まで出すと発災直後の変化が横に潰れて読めなくなる。
# 区切りは本震発生時刻を起点にした経過時間で置く（日付で書くと
# 何日目なのかが読み取れないため）。開始はどれも発災前からで
# 自明なので、名前には終端だけを書いている。
# 値は (本震時刻, データの最終時刻) を受け取って (開始, 終了) を返す。
# 本震時刻は quake_info.json から読んだものを渡す（ここで日時を
# 直書きすると二重管理になる）。終了がデータより後ろでも Plotly 側で
# 右側が空くだけなので、データがそこまで伸びるまでの間も同じ窓を出せる。
def _day_ticks(x_range):
    """x軸の目盛りを日の変わり目に置き、8/4(火) の形で曜日まで出す。

    plotly の tickformat は d3-time-format なので、日本語の曜日を
    出せない（%a は Tue になる）。目盛りの位置と文字列を自前で作る。
    平常時が日区分（曜日）ごとに違うため、どの曜日と比べているのかを
    軸から読めるようにしておきたい。
    """
    start, end = pd.Timestamp(x_range[0]), pd.Timestamp(x_range[1])
    # 表示幅が広いときは1日おきにして、目盛りが詰まりすぎないようにする
    step = 1 if (end - start) <= pd.Timedelta(days=9) else 2
    first = start.normalize()
    if first < start:
        first += pd.Timedelta(days=1)
    days = pd.date_range(first, end, freq=f"{step}D")
    text = [f"{d.month}/{d.day}({WEEKDAY_LABELS[d.weekday()]})" for d in days]
    return list(days), text


def _since_quake(days: int):
    """本震から days 日後を含む日の24時までを返す。

    本震は16:27なので厳密に days 日後で切ると日の途中で終わり、
    右端が半端な位置になる。その時点を含む日の終わり（翌日0時）まで
    伸ばして、日の区切りで終わるようにしている。
    """
    return lambda quake, last: (
        TIMESERIES_DISPLAY_START,
        (quake + pd.Timedelta(days=days)).normalize() + pd.Timedelta(days=1),
    )


TIMESERIES_RANGES = {
    "発災後3日間": _since_quake(3),
    "発災後1週間": _since_quake(7),
    "発災後2週間": _since_quake(14),
    "最新3日間": lambda quake, last: (last - pd.Timedelta(days=3), last),
}
# 既定は発災後1週間。発災直後の落ち込みと戻り始めが1枚で読める。
TIMESERIES_DEFAULT_RANGE = "発災後1週間"

# 時系列ビューの切り替え。実績の既定は5分間値、平常時はいずれも1時間値
# （同じ日区分の各8日分）から求めている。
TIMESERIES_VIEWS = {
    "5分間値": {
        "file": "observations.parquet",
        "unit_label": "5分間値",
        "note": (
            "平常時は1時間値から求め、単位を合わせるため1/12しています。"
            "そのため帯は「平常時の1時間あたり交通量の日々のばらつき÷12」であり、"
            "5分間値そのもののばらつきではありません（帯は狭めに出ます）。"
            "異常検知の判定は1時間値ベースで行っています。"
        ),
    },
    "1時間値": {
        "file": "observations_hourly.parquet",
        "unit_label": "1時間値",
        "note": "実績・平常時とも1時間値どうしの比較で、異常検知の判定もこの粒度で行っています。",
    },
    "車種別・5分間値": {
        "file": "observations.parquet",
        "unit_label": "5分間値",
        "series": "vehicle",
        "note": (
            "同じ図の中に小型車（濃い色）と大型車（明るい色）を5分間値で並べています。"
            "**大型車は5分あたり中央値4台（0台が14%）**"
            "と粒度が粗く、線がぎざぎざになります。形の変化を細かく追いたいとき向けで、"
            "水準の比較は「車種別・1時間値」のほうが読みやすいです。"
            "平常時は1時間値から求めた値を1/12しているため、帯は実際のばらつきより狭く出ます"
            "（台数の小さい大型車ではこの影響がより大きくなります）。"
            "異常検知の判定は車種を合計した1時間値で行っており、この図の判定は変わりません。"
        ),
    },
    "車種別・1時間値": {
        "file": "observations_hourly.parquet",
        "unit_label": "1時間値",
        "series": "vehicle",
        "note": (
            "同じ図の中に小型車（濃い色）と大型車（明るい色）を並べています。"
            "大型車は小型車の1割程度の水準なので、同じ縦軸でも下側に分かれて描かれます。"
            "線種で分ける案は平常時の点線と紛らわしくなるため色で分けています。"
            "異常検知の判定は車種を合計した1時間値で行っており、この図の判定は変わりません。"
            "APIは車種判別不能の台数も返しますが、アーカイブ全期間で1台しかないため扱っていません。"
        ),
    },
}

# 時系列図に描く系列。既定は上り・下りの合計、「車種別」は小型・大型に分ける。
# 大型車は小型車の1割程度なので、同じ軸に載せず図を分ける。
# 1図＝1方向。各図に描く系列を (列のサフィックス, 凡例に足す語, 色の種別) で持つ。
# 色の種別 "main" は選択色そのもの、"alt" は同系統の明るい色（大型車用）。
SERIES_TOTAL = [
    ("上り", [("up", "", "main")]),
    ("下り", [("down", "", "main")]),
]
SERIES_VEHICLE = [
    ("上り", [("up_small", " 小型", "main"), ("up_large", " 大型", "alt")]),
    ("下り", [("down_small", " 小型", "main"), ("down_large", " 大型", "alt")]),
]

# 通行規制の日時はJSTの壁時計時刻（naive）で保存されている。Streamlit Cloud等の
# UTCサーバーでdatetime.now()をそのまま使うと「終了済みか」の判定がずれるため、
# fetch_and_prepare.py と同様にJST固定の「今」を使う。
JST = timezone(timedelta(hours=9))


def _now_jst() -> datetime:
    return datetime.now(JST).replace(tzinfo=None)

st.set_page_config(
    page_title="熊本地震・交通量変化ダッシュボード",
    layout="wide",
)


@st.cache_data(ttl=300)
def load_observations(filename: str = "observations.parquet") -> pd.DataFrame:
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df


@st.cache_data(ttl=300)
def load_quake_info() -> dict:
    with open(os.path.join(DATA_DIR, "quake_info.json"), encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(ttl=300)
def load_regulations() -> list:
    path = os.path.join(DATA_DIR, "regulations.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("items", [])


@st.cache_data(ttl=300)
def load_mlit_regulations() -> dict:
    """
    直轄国道（国が管理する国道）の規制。熊本県「防災情報くまもと」の
    通行規制情報は県・市町村が管理する道路が対象で、国道57号のような
    直轄国道は載らない。そのため熊本河川国道事務所の公表PDFから
    手作業で転記したものを別ファイルで持つ。
    """
    path = os.path.join(DATA_DIR, "mlit_regulations.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(ttl=300)
def regulation_archive_start() -> str:
    """
    通行規制のアーカイブに記録が残っている最も古い時点（first_seen の最小値）。

    これより前に解除された規制は、県管理道路であってもアーカイブに存在しない。
    本震（7/28 16:27）より後に収集を始めたため、発災直後だけ規制されて
    すぐ解除された区間は取りこぼしている。その境目を画面に出すために使う。
    """
    path = os.path.join(DATA_DIR, "archive", "regulations_archive.json")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        items = json.load(f).get("items", {})
    seen = [v.get("first_seen") for v in items.values() if v.get("first_seen")]
    if not seen:
        return ""
    return pd.Timestamp(min(seen)).strftime("%Y-%m-%d %H:%M")


def mlit_regulations_for_point(mlit: dict, point_code) -> list:
    """指定した観測点コードに掛かっている直轄国道の規制を返す。"""
    if not mlit or point_code is None or pd.isna(point_code):
        return []
    code = str(point_code)
    return [
        item for item in mlit.get("items", [])
        if code in (item.get("affected_point_codes") or [])
    ]


@st.cache_data(ttl=300)
def load_station_master_cached() -> dict:
    """常時観測点コードと緯度経度の対応（fetch_and_prepare.py が生成）。"""
    return load_station_master(os.path.join(DATA_DIR, "stations.json"))


@st.cache_data(ttl=300)
def load_traffic_archive(filename: str) -> pd.DataFrame:
    """
    交通量の恒久アーカイブ（5分値／1時間値）を読む。
    アーカイブにはコード列を持たない時期のデータも含まれるので、
    観測点マスタからJARTICの常時観測点コードを付け直して先頭列に置く。
    """
    path = os.path.join(DATA_DIR, "archive", filename)
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = attach_point_code(df, load_station_master_cached())
    cols = ["point_code"] + [c for c in df.columns if c != "point_code"]
    return df[cols].sort_values(["datetime", "point_code"]).reset_index(drop=True)


@st.cache_data(ttl=300)
def load_regulations_archive() -> tuple:
    """
    通行規制の恒久アーカイブを、CSVにしやすい2つの表に開く。
      - 規制一覧: 1件1行（経路の座標列は列数が膨大になるので含めない）
      - 状態変化履歴: 規制内容・終了日時などが変わった時点ごとに1行
    """
    path = os.path.join(DATA_DIR, "archive", "regulations_archive.json")
    if not os.path.exists(path):
        return pd.DataFrame(), pd.DataFrame()
    with open(path, encoding="utf-8") as f:
        items = (json.load(f).get("items") or {})

    rows, hist_rows = [], []
    for key, rec in items.items():
        rows.append({
            "regulation_key": key,
            "route_name": rec.get("route_name"),
            "region": rec.get("region"),
            "content": rec.get("content"),
            "reason_type": rec.get("reason_type"),
            "reason_detail": rec.get("reason_detail"),
            "start_timestamp": rec.get("start_timestamp"),
            "end_timestamp": rec.get("end_timestamp"),
            "length_km": rec.get("length_km"),
            "start_lat": rec.get("start_lat"), "start_lon": rec.get("start_lon"),
            "end_lat": rec.get("end_lat"), "end_lon": rec.get("end_lon"),
            "first_seen": rec.get("first_seen"),
            "last_seen": rec.get("last_seen"),
            "still_listed": rec.get("still_listed"),
            "path_points": len(rec.get("path") or []),
        })
        for h in rec.get("history") or []:
            hist_rows.append({
                "regulation_key": key,
                "route_name": rec.get("route_name"),
                "observed_at": h.get("observed_at"),
                "content": h.get("content"),
                "end_timestamp": h.get("end_timestamp"),
                "reason_type": h.get("reason_type"),
                "reason_detail": h.get("reason_detail"),
                "length_km": h.get("length_km"),
            })

    regs = pd.DataFrame(rows).sort_values("start_timestamp", ascending=False)
    hist = pd.DataFrame(hist_rows)
    if not hist.empty:
        hist = hist.sort_values(["observed_at", "route_name"])
    return regs.reset_index(drop=True), hist.reset_index(drop=True)


@st.cache_data(ttl=300)
def to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Excelでそのまま開けるようBOM付きUTF-8にする。"""
    return df.to_csv(index=False).encode("utf-8-sig")


# ダウンロードするCSVの列定義。「データセット名」はダウンロードするCSVのファイル名と
# 対応させている。実データの列と食い違っていないかは build_data_dictionary で検査する。
_TRAFFIC_COLUMNS = [
    ("point_code", "文字列", "JARTICの常時観測点コード。観測点を一意に識別する公式のID"),
    ("road_type", "文字列", "「1」＝高速自動車国道、「3」＝一般国道（JARTICの道路種別。この2値のみ）。常時観測点コードからは判別できないためAPIの値を保持している。地図では高速自動車国道を■、一般国道を●で描き分けている"),
    ("lon", "度（EPSG:4326）", "観測点の経度"),
    ("lat", "度（EPSG:4326）", "観測点の緯度"),
    ("datetime", "日時（JST）", "観測時刻。5分間値は5分刻み、1時間値は毎時0分（その時刻から始まる区間の集計値）"),
    ("traffic_up", "台", "上り方向の合計交通量（小型＋大型＋車種判別不能）。いずれかが欠測なら空欄"),
    ("traffic_down", "台", "下り方向の合計交通量（同上）"),
    ("traffic_up_small", "台", "上り・小型車の交通量"),
    ("traffic_up_large", "台", "上り・大型車の交通量"),
    ("traffic_up_unidentified", "台", "上り・車種を判別できなかった交通量"),
    ("traffic_down_small", "台", "下り・小型車の交通量"),
    ("traffic_down_large", "台", "下り・大型車の交通量"),
    ("traffic_down_unidentified", "台", "下り・車種を判別できなかった交通量"),
]

_OBSERVATION_COLUMNS = [
    ("point_code", "文字列", "JARTICの常時観測点コード"),
    ("road_type", "文字列", "「1」＝高速自動車国道、「3」＝一般国道（JARTICの道路種別。この2値のみ）。常時観測点コードからは判別できないためAPIの値を保持している。地図では高速自動車国道を■、一般国道を●で描き分けている"),
    ("point_id", "文字列", "内部の結合キー（経度_緯度を6桁に丸めた文字列）。観測点の識別にはpoint_codeを使ってください"),
    ("point_lon", "度（EPSG:4326）", "観測点の経度（6桁に丸め）"),
    ("point_lat", "度（EPSG:4326）", "観測点の緯度（6桁に丸め）"),
    ("datetime", "日時（JST）", "観測時刻（1時間値なので毎時0分）"),
    ("daytype", "文字列", "日区分（月/火/水/木/金/土/日祝）。03:00起点の日付で判定し、祝日は曜日によらず日祝に入れる。平常時は同じ日区分どうしで比べる"),
    ("hour", "0〜23", "時刻の「時」。平常時の平均・標準偏差は日区分×この単位で求めている"),
    ("traffic_up", "台/時", "上り方向の実績交通量（小型＋大型）"),
    ("traffic_down", "台/時", "下り方向の実績交通量（小型＋大型）"),
    ("traffic_up_small", "台/時", "上り・小型車の実績交通量"),
    ("traffic_up_large", "台/時", "上り・大型車の実績交通量"),
    ("traffic_down_small", "台/時", "下り・小型車の実績交通量"),
    ("traffic_down_large", "台/時", "下り・大型車の実績交通量"),
    ("baseline_mean_up", "台/時", "平常時（同じ日区分の8日分）の上り交通量の平均"),
    ("baseline_std_up", "台/時", "同じ母集団での上り交通量の標準偏差"),
    ("baseline_mean_down", "台/時", "平常時の下り交通量の平均"),
    ("baseline_std_down", "台/時", "平常時の下り交通量の標準偏差"),
    ("baseline_mean_up_small", "台/時", "平常時の上り・小型車の平均"),
    ("baseline_std_up_small", "台/時", "同じ母集団での上り・小型車の標準偏差"),
    ("baseline_mean_up_large", "台/時", "平常時の上り・大型車の平均"),
    ("baseline_std_up_large", "台/時", "同じ母集団での上り・大型車の標準偏差"),
    ("baseline_mean_down_small", "台/時", "平常時の下り・小型車の平均"),
    ("baseline_std_down_small", "台/時", "同じ母集団での下り・小型車の標準偏差"),
    ("baseline_mean_down_large", "台/時", "平常時の下り・大型車の平均"),
    ("baseline_std_down_large", "台/時", "同じ母集団での下り・大型車の標準偏差"),
    ("z_up", "無次元", "上りのzスコア =（実績 − 平常時平均）÷ 標準偏差。車種を合計した値で計算する。標準偏差が0や極小のときは下限でクリップ"),
    ("z_down", "無次元", "下りのzスコア（同上）"),
    ("is_post_quake", "真偽値", "本震（2026-07-28 16:27）以降の時刻かどうか"),
    ("is_anomaly", "真偽値", "異常と判定したか。is_post_quakeがTrueかつ|z_up|または|z_down|が2以上"),
    ("distance_km_from_epicenter", "km", "観測点と本震の震源との大圏距離（参考値。異常検知の判定には使っていない）"),
]

_REGULATION_COLUMNS = [
    ("regulation_key", "文字列", "規制の識別キー。路線名・地域・区間の始終点座標・規制開始日時を連結したもの（規制内容や終了日時は途中で変わるため含めない）"),
    ("route_name", "文字列", "路線名"),
    ("region", "文字列", "振興局などの地域区分"),
    ("content", "文字列", "最新の規制内容（全面通行止め／片側交互通行止め／車両通行止め／解除 など）"),
    ("reason_type", "文字列", "規制の原因区分（災害／工事／事故／その他）"),
    ("reason_detail", "文字列", "原因の詳細（道路損壊など）。空欄のことも多い"),
    ("start_timestamp", "日時（JST）", "規制の開始日時。本震（2026-07-28 16:27）以降かどうかで地震起因かを切り分けている"),
    ("end_timestamp", "日時（JST）", "規制の終了日時（実績または予定）。未定なら空欄"),
    ("length_km", "km", "規制区間の延長（ポータルの申告値）"),
    ("start_lat", "度（EPSG:4326）", "規制区間の始点の緯度"),
    ("start_lon", "度（EPSG:4326）", "規制区間の始点の経度"),
    ("end_lat", "度（EPSG:4326）", "規制区間の終点の緯度"),
    ("end_lon", "度（EPSG:4326）", "規制区間の終点の経度"),
    ("first_seen", "日時（JST）", "この規制をアーカイブで最初に確認した日時"),
    ("last_seen", "日時（JST）", "最後に確認した日時。still_listedがFalseならこの時点以降に一覧から消えた"),
    ("still_listed", "真偽値", "最新の取得時点でポータルの一覧に載っていたか。Falseは解除等で削除されたことを示す"),
    ("path_points", "個数", "地図描画用にOSRMで道路網へスナップした経路の座標点数（経路そのものはCSVには含めない）"),
]

_REGULATION_HISTORY_COLUMNS = [
    ("regulation_key", "文字列", "規制の識別キー（一覧CSVのregulation_keyと対応）"),
    ("route_name", "文字列", "路線名"),
    ("observed_at", "日時（JST）", "この状態を観測した日時"),
    ("content", "文字列", "その時点の規制内容"),
    ("end_timestamp", "日時（JST）", "その時点で示されていた終了日時"),
    ("reason_type", "文字列", "その時点の原因区分"),
    ("reason_detail", "文字列", "その時点の原因詳細"),
    ("length_km", "km", "その時点の規制区間延長"),
]

# (CSVファイル名, Excelのシート名, データセット名, 列定義)
# シート名はExcelの制約（31文字以内・ : \ / ? * [ ] を含まない）に収まるよう短くしている。
DATA_DICTIONARY = [
    ("kumamoto_traffic_5min_archive.csv", "5分間交通量", "5分間交通量（アーカイブ全期間）", _TRAFFIC_COLUMNS),
    ("kumamoto_traffic_hourly_archive.csv", "1時間交通量", "1時間交通量（アーカイブ全期間）", _TRAFFIC_COLUMNS),
    ("kumamoto_road_regulations_archive.csv", "通行規制_一覧", "通行規制 一覧", _REGULATION_COLUMNS),
    ("kumamoto_road_regulations_history.csv", "通行規制_履歴", "通行規制 状態変化の履歴", _REGULATION_HISTORY_COLUMNS),
    ("kumamoto_observations_hourly.csv", "異常検知_入力データ", "異常検知の入力データ（1時間値）", _OBSERVATION_COLUMNS),
]


def build_data_dictionary(actual_columns: dict) -> tuple:
    """
    列定義書を1枚の表にする。あわせて、実際のCSVの列と定義が食い違っていないかを
    検査し、その内容を「読んで意味が分かる備考文」として返す。

    食い違いは異常ではなく、次の仕組みで一時的に起こりうる:
      - 列定義書はアプリのコード内にあり、デプロイした時点で最新になる
      - CSVの中身は「最後にデータを生成した時点」のもの（6時間ごとの自動実行 or 手動実行）
    そのため列を追加した直後は、次のデータ生成までの間だけ両者がずれる。
    """
    rows = []
    only_in_dict, only_in_csv = {}, {}
    for fname, _sheet, dataset, columns in DATA_DICTIONARY:
        documented = [c for c, _, _ in columns]
        actual = actual_columns.get(fname)
        if actual is not None:
            stale = [c for c in documented if c not in actual]
            extra = [c for c in actual if c not in documented]
            if stale:
                only_in_dict[fname] = stale
            if extra:
                only_in_csv[fname] = extra
        for order, (col, unit, desc) in enumerate(columns, start=1):
            rows.append({
                "csv_file": fname,
                "dataset": dataset,
                "column_order": order,
                "column": col,
                "unit_or_type": unit,
                "description": desc,
                "in_actual_csv": (col in actual) if actual is not None else None,
            })

    notes = []
    if only_in_dict:
        detail = "、".join(
            f"`{f}` の {', '.join(f'`{c}`' for c in cols)}"
            for f, cols in only_in_dict.items()
        )
        notes.append(
            f"**備考: 定義書に載っているのに、いま配布中のCSVにまだ入っていない列があります**（{detail}）。"
            "エラーではありません。列定義書はアプリのコードに書かれていてデプロイと同時に新しくなる一方、"
            "CSVの中身は「最後にデータを生成した時点」のものなので、列を増やした直後は"
            "次のデータ生成（6時間ごとの自動実行、または `python fetch_and_prepare.py`）が走るまでの間だけ"
            "両者がずれます。このずれがある間だけ、列定義書に「配布中CSVに存在」列が追加され、"
            "該当する列が `×` になります（全て揃っているときはこの列自体を省いています）。"
        )
        if any("point_code" in cols for cols in only_in_dict.values()):
            notes.append(
                "**`point_code` について**: この列は観測点マスタ（`data/stations.json`：常時観測点コードと"
                "緯度経度の対応表）を使って付けています。マスタが用意される前に生成されたデータには"
                "この列自体が存在しないため、いったん定義書だけに載る状態になります。"
                "次のデータ生成でマスタから付与され、CSVにも入ります。"
                "それまでは `lon` / `lat` の組が観測点の識別子として使えます。"
            )
    if only_in_csv:
        detail = "、".join(
            f"`{f}` の {', '.join(f'`{c}`' for c in cols)}"
            for f, cols in only_in_csv.items()
        )
        notes.append(
            f"**備考: CSVに入っているのに、定義書でまだ説明していない列があります**（{detail}）。"
            "データ側に新しい列が増えて、定義書の更新が追いついていない状態です。"
        )
    return pd.DataFrame(rows), notes


def _plain_text(text: str) -> str:
    """備考文のMarkdown記法（**強調**・`コード`）を落としてExcelに書ける素の文にする。"""
    return text.replace("**", "").replace("`", "")


# 列定義シートのヘッダ。Excelで人が読む資料なので日本語にする。
_DICT_SHEET_HEADERS = [
    ("列番号", 8),
    ("列名", 30),
    ("単位・型", 18),
    ("説明", 90),
]
_DICT_PRESENCE_HEADER = ("配布中CSVに存在", 20)


def dictionary_has_missing_column(dict_df: pd.DataFrame) -> bool:
    """
    定義書にあるのに配布中CSVに無い列があるか。全て揃っているときは
    「配布中CSVに存在」列が全て○になって情報量がないので、この判定で列自体を省く。
    """
    if dict_df.empty or "in_actual_csv" not in dict_df.columns:
        return False
    return bool(dict_df["in_actual_csv"].eq(False).any())


@st.cache_data(ttl=300)
def to_dictionary_xlsx_bytes(dict_df: pd.DataFrame, notes: tuple) -> bytes:
    """
    列定義書をExcelブックにする。1シート＝1データセット（＝1CSVファイル）とし、
    先頭に全体の目次と備考をまとめた「はじめに」シートを置く。
    「配布中CSVに存在」列は食い違いがあるときだけ追加する。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    header_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    wrap_top = Alignment(vertical="top", wrap_text=True)
    top = Alignment(vertical="top")

    show_presence = dictionary_has_missing_column(dict_df)
    headers = _DICT_SHEET_HEADERS + ([_DICT_PRESENCE_HEADER] if show_presence else [])

    wb = Workbook()
    intro = wb.active
    intro.title = "はじめに"

    intro["A1"] = "熊本地震・交通量変化ダッシュボード CSV列定義書"
    intro["A1"].font = Font(bold=True, size=14)
    intro["A2"] = f"作成日時: {_now_jst():%Y-%m-%d %H:%M} (JST)"
    intro["A3"] = "公開URL: https://kumamoto-earthquake-traffic-map.streamlit.app/"
    intro["A4"] = "データセットごとにシートを分けています。下の表のシート名から移動してください。"

    row = 6
    for i, label in enumerate(["シート名", "CSVファイル名", "データセット", "列数"]):
        cell = intro.cell(row=row, column=i + 1, value=label)
        cell.font = header_font
        cell.fill = header_fill
    for fname, sheet, dataset, columns in DATA_DICTIONARY:
        row += 1
        intro.cell(row=row, column=1, value=sheet)
        intro.cell(row=row, column=2, value=fname)
        intro.cell(row=row, column=3, value=dataset)
        intro.cell(row=row, column=4, value=len(columns))

    if notes:
        row += 2
        cell = intro.cell(row=row, column=1, value="備考")
        cell.font = header_font
        for note in notes:
            row += 1
            c = intro.cell(row=row, column=1, value=_plain_text(note))
            c.alignment = wrap_top
    for col, width in zip("ABCD", (22, 42, 34, 8)):
        intro.column_dimensions[col].width = width

    for fname, sheet, dataset, _columns in DATA_DICTIONARY:
        ws = wb.create_sheet(sheet)
        ws["A1"] = dataset
        ws["A1"].font = Font(bold=True, size=12)
        ws["A2"] = f"CSVファイル名: {fname}"
        for i, (label, width) in enumerate(headers):
            cell = ws.cell(row=4, column=i + 1, value=label)
            cell.font = header_font
            cell.fill = header_fill
            ws.column_dimensions[get_column_letter(i + 1)].width = width

        sub = dict_df[dict_df["csv_file"] == fname]
        for offset, (_, r) in enumerate(sub.iterrows()):
            out = 5 + offset
            values = [r["column_order"], r["column"], r["unit_or_type"], r["description"]]
            if show_presence:
                in_csv = r["in_actual_csv"]
                values.append(
                    "-" if in_csv is None else ("○" if in_csv else "×（次回生成時に追加）")
                )
            for i, v in enumerate(values):
                c = ws.cell(row=out, column=i + 1, value=v)
                c.alignment = wrap_top if i == 3 else top
        # ヘッダ行を固定して、列数の多いデータセットでもスクロールしやすくする
        ws.freeze_panes = "A5"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@st.cache_data(ttl=300)
def build_point_summary(post: pd.DataFrame, observations: pd.DataFrame = None) -> pd.DataFrame:
    """
    観測点ごとの異常度をまとめる。

    地震後の交通量が全て欠測の観測点は max_abs_z が NaN になる（zスコアが
    定義できない）。これは「異常なし」ではなく「地震後のデータがない」という
    別の状態なので、地図側で区別して描けるよう last_observed_at（最後に値が
    あった時刻）も持たせる。実際に地震発生と同時に配信が止まった観測点がある。
    """
    if post.empty:
        return pd.DataFrame(columns=[
            "point_id", "point_code", "road_type", "point_lon", "point_lat",
            "max_abs_z", "n_anomaly", "distance_km", "last_observed_at",
        ])
    summary = (
        post.groupby(["point_id", "point_lon", "point_lat"])
        .apply(
            lambda g: pd.Series({
                # 常時観測点コードは観測点ごとに一意なので、先頭の値をそのまま採る
                "point_code": (
                    g["point_code"].dropna().iloc[0]
                    if "point_code" in g.columns and g["point_code"].notna().any()
                    else None
                ),
                # 道路種別も観測点ごとに固定（1:高速自動車国道 / 3:一般国道）。
                # 地図のマーカー形状を分けるのに使う。
                "road_type": (
                    g["road_type"].dropna().iloc[0]
                    if "road_type" in g.columns and g["road_type"].notna().any()
                    else None
                ),
                "max_abs_z": max(g["z_up"].abs().max(), g["z_down"].abs().max()),
                "n_anomaly": int(g["is_anomaly"].sum()),
                "distance_km": g["distance_km_from_epicenter"].iloc[0],
            }),
            include_groups=False,
        )
        .reset_index()
    )

    # 最後に値があった時刻は地震前も含めて見る必要があるので observations 全体から求める
    src = observations if observations is not None and not observations.empty else post
    has_value = src["traffic_up"].notna() | src["traffic_down"].notna()
    last_seen = (
        src.loc[has_value].groupby("point_id")["datetime"].max().rename("last_observed_at")
    )
    summary = summary.merge(last_seen, on="point_id", how="left")

    return summary.sort_values("max_abs_z", ascending=False).reset_index(drop=True)


# 初期表示で選んでおく観測点（JARTICの常時観測点コード）。
# 異常度の最上位を出すと日によって入れ替わってしまうので、
# 見せたい地点を固定しておく。データに無ければ最上位にフォールバックする。
DEFAULT_POINT_CODE = "9110040"


def _default_selection(point_summary: pd.DataFrame) -> list:
    """初期選択の観測点IDを返す。"""
    if point_summary.empty:
        return []
    if "point_code" in point_summary.columns:
        hit = point_summary[point_summary["point_code"].astype(str) == DEFAULT_POINT_CODE]
        if not hit.empty:
            return [hit["point_id"].iloc[0]]
    return [point_summary["point_id"].iloc[0]]


FULL_CLOSURE_CONTENTS = {"全面通行止め", "車両通行止め"}


def _point_radius(max_abs_z, max_z: float, selected: bool = False) -> float:
    """
    観測点マーカーの半径。異常度に比例させる。欠測（NaN）は最小サイズ。
    ▲の表示位置を丸の外側に置くためにも使うので、計算を1か所にまとめている。
    """
    frac = 0.0 if pd.isna(max_abs_z) else max_abs_z / max_z
    return (14 if selected else 11) + 12 * frac


# 高速自動車国道は■、一般国道は●で描く。色は異常度に使っているので、
# 道路の種別は形で分ける（色を増やすと異常度のグラデーションが読めなくなる）。
ROAD_TYPE_SQUARE = "1"    # 高速自動車国道
ROAD_TYPE_GENERAL = "3"   # 一般国道
ROAD_TYPE_LABELS = {"1": "高速自動車国道", "3": "一般国道"}


def _is_square_point(road_type) -> bool:
    return str(road_type) == ROAD_TYPE_SQUARE


def _road_type_counts(point_summary: pd.DataFrame) -> tuple:
    """(一般国道, 高速自動車国道, 種別不明) の点数を返す。

    種別が取れていない観測点を一般国道に混ぜないよう別に数える。
    アーカイブには道路種別の列を持たない時期のデータがあり、
    配信が止まって観測点マスタにも残っていない地点（9310183 など）は
    種別が引けない。そういう点まで「一般国道」と表示すると、
    根拠のない分類を出すことになる。
    """
    if "road_type" not in point_summary.columns:
        return 0, 0, len(point_summary)
    rt = point_summary["road_type"].astype("string")
    n_express = int((rt == ROAD_TYPE_SQUARE).sum())
    n_general = int((rt == ROAD_TYPE_GENERAL).sum())
    return n_general, n_express, len(point_summary) - n_general - n_express


def _rgba(hex_color: str, alpha: float) -> str:
    """#rrggbb を rgba() にする。■はdivで描くため、塗りだけ透過させたい。"""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _severity_color(frac: float) -> str:
    """
    0(平常)〜1(最大異常)のfracを寒色系（薄い水色〜濃い紺）の16進カラーに変換する。
    通行規制の色（赤系）と見分けやすいよう、観測点側はあえて寒色にしている。
    """
    frac = max(0.0, min(1.0, frac))
    low = (222, 235, 247)
    high = (8, 48, 107)
    rgb = [int(low[i] + (high[i] - low[i]) * frac) for i in range(3)]
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _parse_reg_time(value) -> datetime:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _regulation_is_ended(reg: dict, now: datetime) -> bool:
    """規制が既に解除済みかどうかを判定する。"""
    if reg.get("content") == "解除":
        return True
    end_dt = _parse_reg_time(reg.get("end_timestamp"))
    if end_dt is None:
        return False
    return end_dt < now


def _regulation_is_post_quake(reg: dict, quake_at: datetime) -> bool:
    """
    規制の開始日時が本震発生以降かどうか。データには2020年7月豪雨のように
    今回の地震と無関係な長期規制も多数含まれるため、これで切り分ける。
    開始日時が読めないものは、地震起因と誤認させない側（=発災前）に寄せる。
    """
    start_dt = _parse_reg_time(reg.get("start_timestamp"))
    if start_dt is None:
        return False
    return start_dt >= quake_at


def _x_circle_icon(color: str, ended: bool, size: int = 18) -> folium.DivIcon:
    """
    PDFの別添図と同じ ⊗（丸の中に×）の印を作る。

    観測点マーカーが塗りつぶしの丸なので、規制の地点まで丸にすると見分けが
    つかない。PDFの凡例で使われている記号に合わせて、丸の中に×を描く。
    解除済みは外周を点線にする（線の実線／破線と同じ意味）。

    クリックはこの印を透過させて、下にある観測点マーカーに通す
    （markerPane は overlayPane より上にあるため、透過させないと
     観測点の選択を横取りしてしまう）。
    """
    # 線の太さと余白はサイズに比例させる（小さくしても潰れないように）
    sw = round(size * 0.11, 2)
    r = size / 2 - sw
    c = size / 2
    d = r * 0.62
    dash = f' stroke-dasharray="{round(sw * 1.25, 2)},{round(sw * 1.1, 2)}"' if ended else ""
    html = (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
        f'<circle cx="{c}" cy="{c}" r="{r}" fill="#ffffff" fill-opacity="0.9" '
        f'stroke="{color}" stroke-width="{sw}"{dash}/>'
        f'<path d="M{c - d} {c - d} L{c + d} {c + d} M{c + d} {c - d} L{c - d} {c + d}" '
        f'stroke="{color}" stroke-width="{sw}" stroke-linecap="round"/>'
        "</svg>"
    )
    return folium.DivIcon(
        html=html, icon_size=(size, size), icon_anchor=(size // 2, size // 2),
        class_name="reg-x-mark",
    )


def _regulation_style(reg: dict, now: datetime, quake_at: datetime) -> dict:
    """
    線のスタイルを決める。色＝規制の区分、破線＝解除済み、で意味を分けている。
      赤     : 今回の地震以降に始まった全面/車両通行止め（最重要。×印つき）
      橙     : 今回の地震以降に始まったその他の規制（片側交互通行止めなど）
      青灰色 : 地震前から続く規制（工事・過去の災害など。今回の地震とは無関係）
    """
    ended = _regulation_is_ended(reg, now)
    dash = "6,8" if ended else None

    if not _regulation_is_post_quake(reg, quake_at):
        return {
            "color": "#5b7c99", "dash_array": dash, "show_x": False,
            "weight": 3, "opacity": 0.35 if ended else 0.55,
        }
    if reg.get("content") in FULL_CLOSURE_CONTENTS:
        return {
            "color": "#e60000", "dash_array": dash, "show_x": True,
            "weight": 6, "opacity": 0.5 if ended else 0.95,
        }
    return {
        "color": "#e67e22", "dash_array": dash, "show_x": True,
        "weight": 5, "opacity": 0.5 if ended else 0.9,
    }


@contextmanager
def _deterministic_branca_ids():
    """
    branca(folium)の各要素は既定でランダムなDOM idを振るため、内容が全く同じ
    地図でもPython側で再構築するたびにHTMLが毎回変わってしまい、streamlit_foliumが
    毎クリックごとに地図全体を再マウントしてしまう（＝選択が鈍く感じる主因）。
    要素生成中だけid生成を連番の決定的な値に置き換えて、選択状態が変わらない限り
    生成HTMLが完全に同一になるようにする。
    """
    counter = itertools.count()
    original = branca_element.Element._generate_id

    def _next_id(cls):
        return format(next(counter), "032x")

    branca_element.Element._generate_id = classmethod(_next_id)
    try:
        yield
    finally:
        branca_element.Element._generate_id = original


def build_base_map(
    point_summary: pd.DataFrame,
    mainshock: dict,
    regulations: list = None,
    mlit: dict = None,
    point_labels: dict = None,
) -> folium.Map:
    """
    選択状態に依存しない「ベース地図」（背景タイル・通行規制・震源）を作る。

    観測点マーカーは選択状態で見た目が変わるため、この地図には含めず
    st_folium の feature_group_to_add で別途渡す。streamlit_folium が
    コンポーネントの同一性判定に使うハッシュはベース地図のJSだけから
    作られる（feature_group は含まれない）ため、こう分けておくと
    選択のたびに地図全体が再マウント（タイル再読込・全要素再構築）されず、
    観測点レイヤーだけが差し替わる。

    folium.Map.render()は副作用を持ち複数回呼ぶと壊れるため、Mapオブジェクト
    自体はキャッシュしない（毎回作り直す）。

    避難所（748件）は、個別表示だとクリック応答が重く、クラスタ化すると
    個々の場所が分からず意味が薄いため、地図には表示しない。
    """
    with _deterministic_branca_ids():
        center = [point_summary["point_lat"].mean(), point_summary["point_lon"].mean()]
        fmap = folium.Map(location=center, tiles=None)
        # 通行規制などの重ね合わせ情報が見やすいよう、背景地図は半透明にする。
        # 背景地図は1種類しかなく、切り替えられないものをレイヤ一覧に出しても
        # 幅を取るだけなので control=False で一覧から外す。
        folium.TileLayer(
            "OpenStreetMap", name="OpenStreetMap", opacity=0.55, control=False,
        ).add_to(fmap)
        # st_folium(width=550) は地図divに width:550px を焼き込む。iframe側は
        # CSSで100%にしているため、列が550pxより狭くなると地図だけが
        # iframeからあふれ、右端に貼り付くレイヤ一覧が画面外にはみ出す。
        # 列幅はブラウザの表示倍率で変わる（100%は90%より狭い）ので、
        # 地図div自体をiframe幅に追従させる。
        #
        # あわせてレイヤ一覧の幅にも上限を掛ける。%指定は親が leaflet-top/right の
        # 隅要素になって地図幅に対して解決しないため、iframeのビューポート幅である
        # vw を使う（iframe幅＝地図幅）。折り返しはラベルのテキスト部分
        # （一番内側のspan）だけに許す。labelや外側のspanに white-space:normal を
        # 当てると、チェックボックスとテキストの間で改行され切れて見えてしまう。
        fmap.get_root().header.add_child(folium.Element(
            "<style>"
            "#map_div{width:100% !important;}"
            # 通行止めの×は目印だけなのでクリックを透過させ、下にある
            # 観測点マーカーを確実にクリックできるようにする。
            ".reg-x-mark{pointer-events:none !important;}"
            # 地図内の観測点凡例（左下）。レイヤ一覧(bottomright)と重ならない
            # 位置に置き、クリックは下の地図に通す。
            ".pt-shape-legend{position:absolute;right:8px;top:8px;z-index:650;"
            "background:rgba(255,255,255,0.88);border:1px solid #bbb;border-radius:4px;"
            "padding:4px 7px;font-size:11.5px;line-height:1.6;color:#222;"
            "pointer-events:none;font-family:sans-serif;}"
            ".pt-shape-legend span{display:block;white-space:nowrap;}"
            ".pt-mark{display:inline-block;width:10px;height:10px;margin-right:5px;"
            "background:#4a7ab5;border:1px solid #333;vertical-align:middle;}"
            ".pt-circle{border-radius:50%;}"
            ".pt-legend-head{font-size:10.5px;color:#555;letter-spacing:0.02em;"
            "border-bottom:1px solid #ddd;margin-bottom:2px;padding-bottom:1px;}"
            # ツールチップは既定（white-space:nowrap）だと横に伸びて地図の外まで
            # はみ出す。かといって max-width だけを付けると、絶対配置の
            # 幅が「その位置から地図の端までの残り幅」に縮められ、
            # 地図の端に近い規制では数文字ごとに折り返す細長い箱になる。
            # width:max-content で内容なりの幅を確保し、その上で上限を掛ける。
            ".leaflet-tooltip{width:max-content;max-width:260px;"
            "white-space:normal;font-size:11.5px;line-height:1.5;padding:5px 8px;}"
            ".leaflet-control-layers{font-size:12px;max-width:calc(100vw - 28px);}"
            ".leaflet-control-layers-overlays label,"
            ".leaflet-control-layers-overlays label>span{white-space:nowrap;}"
            ".leaflet-control-layers-overlays label>span>span"
            "{white-space:normal;overflow-wrap:anywhere;}"
            "</style>"
        ))

        bounds = [
            [point_summary["point_lat"].min(), point_summary["point_lon"].min()],
            [point_summary["point_lat"].max(), point_summary["point_lon"].max()],
        ]
        fmap.fit_bounds(bounds, padding=(40, 40))

        # ((状態, データソース), レイヤ)。状態は 0=規制中 / 1=解除済み /
        # 2=地震前からの規制、データソースは 0=直轄国道 / 1=県・市町村道。
        # 規制中を上、解除済みをその下、地震前を一番下に並べ、
        # 同じ状態のなかでは直轄国道を先に置く。
        overlays = []

        if regulations:
            now = _now_jst()
            quake_at = datetime.fromisoformat(mainshock["occurred_at"]).replace(tzinfo=None)
            # レイヤは「どこから来たデータか（道路の管理者）」×「いまどの状態か」
            # で分ける。記録している規制の多くはすでに解除済みなので、
            # 既定では規制中だけを出し、解除済みと地震前からの規制は
            # 地図の右下のレイヤ一覧でオンにしてもらう。
            post_active_layer = folium.FeatureGroup(name="県・市町村道：規制中")
            post_ended_layer = folium.FeatureGroup(
                name="県・市町村道：解除済み", show=False
            )
            pre_layer = folium.FeatureGroup(
                name="県・市町村道：地震前からの規制（工事・過去の災害等）", show=False
            )
            for reg in regulations:
                is_post = _regulation_is_post_quake(reg, quake_at)
                style = _regulation_style(reg, now, quake_at)
                ended = _regulation_is_ended(reg, now)
                period = reg["start_timestamp"] or "?"
                period += f" 〜 {reg['end_timestamp']}" if ended and reg["end_timestamp"] else " 〜 (継続中)"
                if not is_post:
                    target = pre_layer
                else:
                    target = post_ended_layer if ended else post_active_layer
                folium.PolyLine(
                    locations=reg["path"],
                    color=style["color"],
                    weight=style["weight"],
                    opacity=style["opacity"],
                    dash_array=style["dash_array"],
                    tooltip=(
                        f"{reg['route_name']}（{reg['region']}）<br>"
                        f"<b>{'今回の地震以降に開始' if is_post else '地震前からの規制'}"
                        f"／{'解除済み' if ended else '規制中'}</b><br>"
                        f"{reg['content']}｜{reg['reason_type']}"
                        f"{('・' + reg['reason_detail']) if reg['reason_detail'] else ''}<br>"
                        f"{period}"
                    ),
                ).add_to(target)
                if style["show_x"]:
                    mid = reg["path"][len(reg["path"]) // 2]
                    folium.Marker(
                        location=mid,
                        # ×はただの目印で、ツールチップもポップアップも持たない。
                        # マーカーはmarkerPane(z600)にあり観測点の円(overlayPane z400)より
                        # 上に来るため、そのままだと観測点クリックを横取りしてしまう
                        # （クリックが×に当たると、その座標が返るだけで観測点が選ばれない）。
                        # クリックを透過させるクラスを付ける。
                        icon=_x_circle_icon(style["color"], ended),
                    ).add_to(target)
            # 直轄国道のレイヤも作ってから、まとめて決まった順に足す
            # （足した順がそのままレイヤ一覧の並びになるため）。
            overlays += [
                ((0, 1), post_active_layer), ((1, 1), post_ended_layer),
                ((2, 1), pre_layer),
            ]

        # 直轄国道の規制。PDFは区間を「○○IC〜○○IC」と名前で示すだけで座標が無いが、
        # 実在する道路区間なので端点のIC座標から線形を復元してある
        # （scripts/build_mlit_paths.py が path として書き込む）。
        # 線形を作れなかった規制（キロポストで示された地点など）は、
        # 掛かっていた観測点が分かる場合だけ▲を置いてフォールバックする。
        mlit_items = (mlit or {}).get("items", [])
        mlit_drawn = 0
        if mlit_items:
            mlit_active_layer = folium.FeatureGroup(name="直轄国道：規制中")
            mlit_ended_layer = folium.FeatureGroup(
                name="直轄国道：解除済み", show=False
            )

            def mlit_layer_for(is_ended: bool):
                return mlit_ended_layer if is_ended else mlit_active_layer
            # 同じ区間に別の日時の規制が複数あることがある（阿蘇西IC〜車帰IC は
            # 本震直後の通行止めと、8月の夜間工事の通行止めの2件）。
            # 区間ごとに1本にまとめないと線が完全に重なり、下になった方は
            # ツールチップを出せず、その規制の存在に気づけない。
            by_section = {}
            for item in mlit_items:
                if not item.get("path"):
                    continue
                by_section.setdefault(
                    (item["route_name"], item["section"]), []
                ).append(item)

            for (route_name, section), items in by_section.items():
                # 見た目は「いま規制中のものがあるか」「全面通行止めが
                # 含まれるか」で決める（重い方に寄せる）。
                ended = all(i.get("end_timestamp") for i in items)
                full = any(i.get("content") in FULL_CLOSURE_CONTENTS for i in items)
                periods = "".join(
                    f"<b>{i['content']}／{'解除済み' if i.get('end_timestamp') else '規制中'}</b><br>"
                    f"{i['start_timestamp']} 〜 {i['end_timestamp'] or '(継続中)'}<br>"
                    for i in sorted(items, key=lambda x: x["start_timestamp"])
                )
                count = f"（{len(items)}件）" if len(items) > 1 else ""
                folium.PolyLine(
                    locations=items[0]["path"],
                    color="#e60000" if full else "#e67e22",
                    weight=6 if full else 5,
                    opacity=0.5 if ended else 0.95,
                    dash_array="6,8" if ended else None,
                    tooltip=(
                        f"<b>{route_name}</b><br>{section}{count}<br>"
                        f"{periods}"
                        f"直轄国道（出典: 熊本河川国道事務所）"
                    ),
                ).add_to(mlit_layer_for(ended))
                # 県フィードの地震後の規制と同じく、線の中点にも⊗を置く。
                # 直轄国道の規制はいずれも今回の地震以降のものなので、
                # 「⊗＝地震後に始まった規制」という凡例の規則に合わせる。
                folium.Marker(
                    location=items[0]["path"][len(items[0]["path"]) // 2],
                    icon=_x_circle_icon("#e60000" if full else "#e67e22", ended),
                ).add_to(mlit_layer_for(ended))
                mlit_drawn += 1

            # 区間の線を引けない規制のうち、場所が1地点として特定できたものは
            # その位置に印を置く（キロポストや地名でしか示されていないもの）。
            # 位置は地名から復元しているので誤差があり、その旨をツールチップに出す。
            for item in mlit_items:
                pt = item.get("point")
                if not pt or item.get("path"):
                    continue
                ended = bool(item.get("end_timestamp"))
                full = item.get("content") in FULL_CLOSURE_CONTENTS
                color = "#e60000" if full else "#e67e22"
                # 観測点マーカー（塗りつぶしの丸）と見分けがつくよう、
                # PDFの別添図と同じ ⊗ で描く。
                folium.Marker(
                    location=[pt["lat"], pt["lon"]],
                    icon=_x_circle_icon(color, ended),
                    tooltip=(
                        f"<b>{item['route_name']}</b><br>{item['section']}<br>"
                        f"<b>{item['content']}／{'解除済み' if ended else '規制中'}</b><br>"
                        f"{item['start_timestamp']} 〜 "
                        f"{item['end_timestamp'] or '(継続中)'}<br>"
                        "位置は地名から復元した概略値<br>"
                        "直轄国道（出典: 熊本河川国道事務所）"
                    ),
                ).add_to(mlit_layer_for(ended))
                mlit_drawn += 1

            # 区間の端点（IC）を点で落とす。線だけだと、どのICからどのICまで
            # なのかが地図から読めないため。同じICが複数の区間の端点になる
            # （車帰ICは大津IC側と阿蘇西IC側の両方）ので、ICごとに1点だけ置き、
            # 属する区間をツールチップに並べる。
            # 観測点マーカーより小さく、白抜きにして混同しないようにする。
            # ICは区間とセットで意味を持つので、その区間と同じレイヤに入れる。
            # 同じICが規制中の区間と解除済みの区間の両方の端点になることが
            # あるため、レイヤごとに1つずつ置く（片方を消してももう片方に残る）。
            ic_points = {}
            for (route_name, section), items in by_section.items():
                layer = mlit_layer_for(
                    all(i.get("end_timestamp") for i in items)
                )
                for ep in items[0].get("endpoints") or []:
                    key = (round(ep["lat"], 6), round(ep["lon"], 6), id(layer))
                    entry = ic_points.setdefault(
                        key,
                        {"name": ep["name"], "node": ep.get("osm_node"),
                         "sections": [], "layer": layer},
                    )
                    label = f"{route_name} {section}"
                    if label not in entry["sections"]:
                        entry["sections"].append(label)
            for (lat, lon, _), info in ic_points.items():
                folium.CircleMarker(
                    location=[lat, lon],
                    radius=5,
                    color="#333333",
                    weight=2,
                    fill=True,
                    fill_color="#ffffff",
                    fill_opacity=0.95,
                    tooltip=(
                        f"<b>{info['name']}</b><br>"
                        + "".join(f"{sec} の端点<br>" for sec in info["sections"])
                        + "位置はOpenStreetMapのICノード"
                        + (f"（node/{info['node']}）" if info["node"] else "")
                    ),
                ).add_to(info["layer"])

            # 線形が無い規制のフォールバック（掛かっていた観測点の上に▲）
            if not point_summary.empty:
                max_z_for_radius = point_summary["max_abs_z"].max()
                if not (pd.notna(max_z_for_radius) and max_z_for_radius > 0):
                    max_z_for_radius = 1.0
                for _, row in point_summary.iterrows():
                    hits = [
                        h for h in mlit_regulations_for_point(mlit, row.get("point_code"))
                        if not h.get("path")
                    ]
                    if not hits:
                        continue
                    label = (point_labels or {}).get(row["point_id"], row["point_id"])
                    lines = "<br>".join(
                        f"{h['route_name']}（{h['section']}）{h['content']}<br>"
                        f"{h['start_timestamp']} 〜 {h['end_timestamp'] or '(継続中)'}"
                        for h in hits
                    )
                    folium.Marker(
                        location=[row["point_lat"], row["point_lon"]],
                        icon=folium.DivIcon(
                            html=(
                                '<div style="font-size:18px;font-weight:900;color:#b00000;'
                                'line-height:1;text-shadow:0 0 3px white,0 0 3px white;">▲</div>'
                            ),
                            # 観測点の丸に重ねると観測点の属性のように見えるので外側（上）に置く。
                            # 選択時は半径が+3されるためその分も見込む。
                            icon_size=(18, 18),
                            icon_anchor=(9, 18 + _point_radius(row["max_abs_z"], max_z_for_radius) + 6),
                        ),
                        tooltip=(
                            f"<b>直轄国道の規制</b>（区間の線形が作れないため位置のみ）<br>"
                            f"{lines}<br>{label} に掛かっていたもの<br>"
                            "出典: 熊本河川国道事務所（県ポータルのデータには含まれません）"
                        ),
                    ).add_to(mlit_active_layer)
                    mlit_drawn += 1

            if mlit_drawn:
                overlays += [
                    ((0, 0), mlit_active_layer), ((1, 0), mlit_ended_layer),
                ]

        if regulations or mlit_drawn:
            # 左上・右上だと観測点マーカーに被るので右下に置く
            for _, layer in sorted(overlays, key=lambda x: x[0]):
                layer.add_to(fmap)
            folium.LayerControl(position="bottomright", collapsed=False).add_to(fmap)

        # 観測点の形（＝道路の種別）は地図を見ながら参照するものなので、
        # 地図の外の凡例ではなく地図の中に置く。位置は右上（レイヤ一覧は右下、
        # ズームボタンは左上なので、どちらとも重ならない）。
        # マーカーに重なっても中身が読めるよう半透明の白地にする。
        n_general, n_express, n_unknown = _road_type_counts(point_summary)
        if n_express or n_unknown:
            # 何の凡例なのかが分かるよう見出しを付ける。区分名（一般国道／
            # 高速自動車国道）はJARTICが返す道路種別をそのまま使っており、
            # 道路の法的な位置づけを表すものではない（例: 九州中央自動車道は
            # 種別1だが、規制を公表しているのは直轄国道の管理者である
            # 熊本河川国道事務所）。この但し書きはREADMEとdocsに書いてある。
            rows = [
                '<span class="pt-legend-head">観測点の道路種別</span>',
                f'<span><i class="pt-mark pt-circle"></i>一般国道 {n_general}点</span>',
                f'<span><i class="pt-mark pt-square"></i>高速自動車国道 {n_express}点</span>',
            ]
            if n_unknown:
                # 形は●のままだが、一般国道と言い切れないので別行にする
                rows.append(
                    f'<span><i class="pt-mark pt-circle"></i>種別不明 {n_unknown}点</span>'
                )
            fmap.get_root().html.add_child(folium.Element(
                '<div class="pt-shape-legend">' + "".join(rows) + '</div>'
            ))

        folium.Marker(
            location=[mainshock["epicenter_lat"], mainshock["epicenter_lon"]],
            icon=folium.Icon(color="blue", icon="star"),
            popup=folium.Popup(
                f"震源（本震）: {mainshock['epicenter_name']}<br>"
                f"M{mainshock['magnitude']} 最大震度{mainshock['max_intensity']}",
                max_width=250,
            ),
        ).add_to(fmap)

        return fmap


def render_mlit_notice(mlit: dict, point_summary: pd.DataFrame, point_labels: dict) -> None:
    """
    直轄国道の規制を、出典と地図での描き方つきで一覧にする。

    通行規制は管理者ごとに公表の仕方が違う。県・市町村の道路は
    「防災情報くまもと」がJSONで配信しているが、国が管理する直轄国道は
    熊本河川国道事務所がPDFで公表するだけなので、別に転記して持っている。
    どの規制がどちらの経路で入り、地図にどう出ているのかを示すのがここの役割。
    """
    items = (mlit or {}).get("items", [])
    if not items:
        return
    src_name = mlit.get("source_name", "国土交通省")
    src_url = mlit.get("source_url", "")
    n_line = sum(1 for i in items if i.get("path"))
    n_spot = sum(1 for i in items if i.get("point") and not i.get("path"))
    with st.expander(
        f"直轄国道の規制（{len(items)}件・PDFから転記）", expanded=False
    ):
        st.markdown(
            "通行規制は道路の管理者ごとに公表の仕方が違うため、"
            "このダッシュボードでは2つの経路で集めています。\n\n"
            "- **県・市町村が管理する道路**: 「防災情報くまもと」が公開JSONで"
            "配信しているので、6時間ごとに自動取得しています。"
            "始点・終点の座標があるので、OSRMで道路網に沿った経路に"
            "スナップして地図に描いています\n"
            f"- **国が管理する直轄国道**: [{src_name}]({src_url}) がPDFで"
            "公表するだけで配信の仕組みがないため、**手作業で転記**しています。"
            "以下がその一覧です\n\n"
            "PDFには座標がないので、場所の示し方に応じて地図への出し方を"
            "変えています。\n\n"
            f"- **区間が「○○IC〜○○IC」で示されているもの（{n_line}件）**: "
            "端点のICの座標（OpenStreetMap）から道路網に沿った線形を復元して"
            "線で描いています。高速道路のIC名で並行する国道が示されている"
            "場合は、その国道上に落として線にしています\n"
            + (
                f"- **キロポストや地名でしか示されていないもの（{n_spot}件）**: "
                "地名から復元した概略の位置に **⊗** を置いています"
                "（キロポストから直接求めた位置ではありません）\n"
                if n_spot else ""
            )
            + "- 場所を特定できないものは、掛かっていた観測点が分かる場合だけ"
            "その**すぐ上**に **▲** を置いています"
        )
        code_to_label = {
            str(r["point_code"]): point_labels.get(r["point_id"], r["point_id"])
            for _, r in point_summary.iterrows()
            if pd.notna(r.get("point_code"))
        }
        for item in items:
            affected = [
                code_to_label.get(c, f"観測点 {c}")
                for c in (item.get("affected_point_codes") or [])
            ]
            st.markdown(
                f"**{item['route_name']}（{item['section']}"
                f"{'・約' + str(item['length_km']) + 'km' if item.get('length_km') else ''}）**  \n"
                f"{item['content']}｜{item['start_timestamp']} 〜 "
                f"{item['end_timestamp'] or '(継続中)'}｜{item.get('reason', '')}  \n"
                + (
                    f"この規制が掛かる観測点: {', '.join(affected)}  \n" if affected
                    else "観測点との対応づけなし（下記の根拠を参照）  \n"
                )
                + "出典: "
                + " / ".join(f"[{r['label']}]({r['url']})" for r in item.get("reports", []))
            )
            # 観測点と規制の対応づけは推測で行わない。根拠（または裏付けが取れな
            # かったこと）をそのまま出して、誤った因果の読み取りを防ぐ。
            if item.get("path_source"):
                st.caption(f"区間の線形: {item['path_source']}")
            if item.get("match_basis"):
                st.caption(f"観測点との対応づけ: {item['match_basis']}")
            st.markdown("---")
        if mlit.get("coverage_note"):
            st.caption(mlit["coverage_note"])
        if mlit.get("note"):
            st.caption(mlit["note"])


def build_points_feature_group(
    point_summary: pd.DataFrame, point_labels: dict, selected_points=()
) -> folium.FeatureGroup:
    """
    観測点マーカーだけを含むFeatureGroupを作る。選択状態で見た目が変わるのは
    このレイヤーだけなので、st_folium の feature_group_to_add に渡すことで
    地図全体の再マウントなしに差し替えられる。
    """
    max_z = point_summary["max_abs_z"].max()
    max_z = max_z if pd.notna(max_z) and max_z > 0 else 1.0

    fg = folium.FeatureGroup(name="観測点")
    for _, row in point_summary.iterrows():
        # 地震後が全て欠測だと zスコアが定義できず max_abs_z が NaN になる。
        # そのまま半径の計算に入れると NaN になってマーカーが描画されず、
        # 観測点が地図から消えてしまう（「異常なし」と区別できない）。
        # 欠測は欠測として、灰色・破線の枠で別に描く。
        no_data = pd.isna(row["max_abs_z"])
        frac = 0.0 if no_data else row["max_abs_z"] / max_z
        is_selected = row["point_id"] in selected_points
        sel_idx = list(selected_points).index(row["point_id"]) if is_selected else None
        border_color = SELECTION_COLORS[sel_idx] if is_selected else ("#777777" if no_data else "#333333")
        label = point_labels.get(row["point_id"], row["point_id"])
        if no_data:
            last_at = row.get("last_observed_at")
            # 地図・異常判定は1時間値ベースなので、時刻も1時間値のものだと明示する
            detail = (
                f"地震後のデータがありません（欠測）<br>"
                f"最後に値があった時刻（1時間値）: {last_at:%Y-%m-%d %H:%M}"
                if pd.notna(last_at) else "地震後のデータがありません（欠測）"
            )
        else:
            detail = (
                f"最大|zスコア|: {row['max_abs_z']:.2f} / 異常件数: {int(row['n_anomaly'])}"
            )
        radius = _point_radius(row["max_abs_z"], max_z, is_selected)
        weight = 4 if is_selected else (2 if no_data else 1)
        fill_color = "#f0f0f0" if no_data else _severity_color(frac)
        fill_opacity = 0.6 if no_data else 0.85
        tooltip = f"{label}{POINT_TOOLTIP_HINT}<br>{detail}"

        if _is_square_point(row.get("road_type")):
            # CircleMarkerに角を出す方法はないので、DivIconの四角で描く。
            # 円と同じ大きさに見えるよう1辺を直径に合わせ、中心をアンカーにする。
            # ツールチップは丸と同じ文字列にしてあるので、クリックの判定
            # （point_id_from_tooltip）は形が変わっても同じように効く。
            size = int(round(radius * 2))
            style = (
                f"width:{size}px;height:{size}px;box-sizing:border-box;"
                f"background:{_rgba(fill_color, fill_opacity)};"
                f"border:{weight}px {'dashed' if no_data else 'solid'} {border_color};"
            )
            folium.Marker(
                location=[row["point_lat"], row["point_lon"]],
                icon=folium.DivIcon(
                    icon_size=(size, size),
                    icon_anchor=(size // 2, size // 2),
                    html=f'<div style="{style}"></div>',
                ),
                tooltip=tooltip,
            ).add_to(fg)
        else:
            folium.CircleMarker(
                location=[row["point_lat"], row["point_lon"]],
                radius=radius,
                color=border_color,
                weight=weight,
                dash_array="4,3" if no_data else None,
                fill=True,
                fill_color=fill_color,
                fill_opacity=fill_opacity,
                tooltip=tooltip,
            ).add_to(fg)
    return fg


def build_point_labels(point_summary: pd.DataFrame) -> dict:
    """
    観測点の表示名を作る（セレクタ・地図・グラフ凡例で共用）。
    JARTICの「常時観測点コード」が観測点の公式なIDなので、それをそのまま使う。
    マスタから引けなかった観測点だけ、緯度経度の連番にフォールバックする。
    """
    labels = {}
    for i, (_, row) in enumerate(point_summary.iterrows()):
        code = row.get("point_code")
        labels[row["point_id"]] = (
            f"観測点 {code}" if code and pd.notna(code) else f"地点{i + 1}（コード不明）"
        )
    return labels


def point_id_from_tooltip(tooltip, point_labels: dict):
    """
    クリックされた図形のツールチップ本文から観測点を特定する。

    streamlit_folium の last_object_clicked は「クリックした位置」であって
    マーカーの中心ではない（onLayerClick が event.latlng をそのまま入れている）。
    そのため座標の近さで観測点を当てる方式にすると、
      ・大きなマーカーの端を押すと中心から離れて判定に失敗する
      ・通行規制の線など別の図形を押しただけで近くの観測点が選ばれてしまう
    という取り違えが起きる。ツールチップ本文で判定すれば、
    観測点マーカーを押したときだけ反応し、それ以外は何も起きない。

    ツールチップはHTMLタグを除いたテキストになる（コンポーネント側の
    extractContent が textContent を取る）ので、先頭がラベル＋定型句かで見る。
    """
    if not tooltip:
        return None
    text = str(tooltip).strip()
    for pid, label in (point_labels or {}).items():
        if text.startswith(f"{label}{POINT_TOOLTIP_HINT}"):
            return pid
    return None


def describe_baseline(baseline_windows, daytypes: dict = None) -> str:
    """
    「平常時」が具体的にどの期間を指すのかを説明する文を作る。
    使った日は fetch_and_prepare.py が quake_info.json に書き出した値をそのまま
    受け取るため、説明文と計算内容がずれない。
    """
    if daytypes:
        counts = {dt: len(days) for dt, days in daytypes.items() if days}
        allday = sorted(d for days in daytypes.values() for d in days)
        n = ", ".join(f"{dt} {c}日" for dt, c in counts.items())
        return (
            "**平常時**＝地震発生前の**同じ日区分・同じ時刻**の交通量。"
            "曜日で交通量の形が違うため、月・火・水・木・金はそれぞれ別に平均をとり、"
            "土曜は土曜、日曜と祝日は「日祝」としてまとめています"
            f"（{n}／{allday[0]}〜{allday[-1]}）。"
            "実測の各行は自分と同じ日区分の平常時と比べます。"
            "観測点ごと・日区分ごと・時刻（時）ごとに平均と標準偏差を求めています。"
        )
    # 日区分の情報が無い古いデータ向けのフォールバック
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    days = []
    for w in baseline_windows or []:
        try:
            d = datetime.fromisoformat(w["start"])
        except (ValueError, KeyError, TypeError):
            continue
        days.append((d, f"{d.month}/{d.day}"))
    if not days:
        return "**平常時**＝地震発生前の同じ日区分・同時刻の交通量（時刻帯ごとの平均）。"
    days.sort()
    wd = weekdays[days[0][0].weekday()]
    span = f"{days[0][1]}〜{days[-1][1]}の{wd}曜 {len(days)}日分"
    return (
        f"**平常時**＝地震発生前の同じ曜日（{span}）の、同じ時刻の交通量。"
        "観測点ごと・時刻（時）ごとに平均と標準偏差を求め、今回の実績と比べています。"
    )


def render_timeseries(
    observations: pd.DataFrame, selected_points, quake_at,
    other_event_times=(), point_labels: dict = None, baseline_windows=None,
    unit_label: str = "5分間値", extra_note: str = "",
    mlit_bands=(), baseline_daytypes: dict = None, series_mode: str = "total",
    x_range=None,
) -> None:
    if not selected_points:
        st.info("上のプルダウンから選ぶか、地図上の丸いマーカーをクリックして観測点を選ぶと、ここに時系列が表示されます（最大2地点まで比較可）。")
        return
    if observations.empty:
        st.warning("このビューのデータがまだ生成されていません。`python fetch_and_prepare.py` を実行してください。")
        return

    point_labels = point_labels or {}
    # 地震前日の深夜〜早朝は情報が薄いので、既定の表示は7/27 12:00から始める
    # （データ自体はTARGET_START=7/27 03:00から保持している）。
    # 呼び出し側が期間を指定していればそちらを使う。
    if x_range is None:
        x_range = [TIMESERIES_DISPLAY_START, observations["datetime"].max()]
    else:
        x_range = list(x_range)

    tick_vals, tick_text = _day_ticks(x_range)

    series = SERIES_VEHICLE if series_mode == "vehicle" else SERIES_TOTAL
    chart_height = 290
    for label, sub_series in series:
        missing = [
            suf for suf, _, _ in sub_series
            if f"traffic_{suf}" not in observations.columns
            or f"baseline_mean_{suf}" not in observations.columns
        ]
        if missing:
            st.info(
                f"「{label}」に必要な列がデータにありません（{', '.join(missing)}）。"
                "`python fetch_and_prepare.py` を実行してデータを作り直してください。"
            )
            continue
        fig = go.Figure()
        for i, pid in enumerate(selected_points):
            mark = point_labels.get(pid, pid)
            pdf = observations[observations["point_id"] == pid].sort_values("datetime")
            for suffix, veh_label, color_kind in sub_series:
                palette = SELECTION_ALT_COLORS if color_kind == "alt" else SELECTION_COLORS
                color = palette[i % len(palette)]
                mean_col = f"baseline_mean_{suffix}"
                std_col = f"baseline_std_{suffix}"
                # ±σの帯は1地点のときだけ。車種別では車種ごとに1本ずつ出す
                if len(selected_points) == 1:
                    fig.add_trace(go.Scatter(
                        x=pdf["datetime"], y=pdf[mean_col] + pdf[std_col],
                        mode="lines", line=dict(width=0), showlegend=False,
                    ))
                    fig.add_trace(go.Scatter(
                        x=pdf["datetime"], y=pdf[mean_col] - pdf[std_col],
                        mode="lines", line=dict(width=0), fill="tonexty",
                        fillcolor="rgba(100,100,100,0.2)",
                        name=f"平常時±σ{veh_label}",
                    ))
                fig.add_trace(go.Scatter(
                    x=pdf["datetime"], y=pdf[mean_col],
                    mode="lines", line=dict(color=color, dash="dot", width=1),
                    opacity=0.6, name=f"{mark}{veh_label} 平常時",
                ))
                fig.add_trace(go.Scatter(
                    x=pdf["datetime"], y=pdf[f"traffic_{suffix}"],
                    mode="lines+markers",
                    line=dict(color=color, width=1.2),
                    marker=dict(size=3),
                    name=f"{mark}{veh_label} 実績",
                ))
        # 直轄国道の通行止め期間。この規制は県ポータルのデータに含まれず地図に
        # 線として出ないため、交通量が0になっている理由が図から読めなくなる。
        # 該当観測点を選んだときだけ、期間を帯で示す。
        for band in mlit_bands:
            # 帯は灰色にする。選択中の観測点は赤/緑で描いているので、帯を赤系にすると
            # 緑で選ばれた地点の通行止めが赤帯の上に乗って読みづらくなる。
            fig.add_vrect(
                x0=band["start"], x1=band["end"],
                fillcolor="rgba(110,115,120,0.16)", line_width=0, layer="below",
            )
            fig.add_annotation(
                x=band["start"], y=0.97, yref="paper", xanchor="left", yanchor="top",
                text=band["label"], showarrow=False, align="left",
                font=dict(size=9, color="#4a4f55"),
                bgcolor="rgba(255,255,255,0.75)",
            )
        for t in other_event_times:
            fig.add_vline(x=t, line_dash="dot", line_color="lightgray", line_width=1, opacity=0.7)
        fig.add_vline(x=quake_at, line_dash="dot", line_color="black", line_width=2)
        fig.add_annotation(
            x=quake_at, y=1, yref="paper", yanchor="bottom",
            text="地震発生 16:27", showarrow=False,
            font=dict(size=11, color="black"),
        )
        # 上下2図を並べると縦に長くなり、観測点を比べるたびにスクロールが
        # 必要になるため1図の高さを抑える（400 -> 290）。凡例は2図で同じ内容
        # なので上の図だけに出し、下の図はその分の余白も詰める。
        show_legend = label == series[0][0]
        fig.update_layout(
            height=chart_height,
            # 凡例はグラフ下に置く。上部だとplotlyのモードバー（カメラ・ズーム等の
            # アイコン）と重なり、幅の狭いモバイルでは折り返して読めなくなるため。
            margin=dict(l=10, r=10, t=26, b=62 if show_legend else 24),
            showlegend=show_legend,
            legend=dict(
                orientation="h", yanchor="top", y=-0.28,
                xanchor="left", x=0, font=dict(size=10),
            ),
            xaxis=dict(
                range=x_range,
                tickvals=tick_vals, ticktext=tick_text,
                # 日付の目盛りだけだと日中の位置が読めないので、
                # 6時間ごとの補助線をラベルなしで入れる。
                minor=dict(
                    dtick=6 * 3600 * 1000, showgrid=True,
                    gridcolor="rgba(128,128,128,0.18)",
                ),
            ),
            # 交通量は負にならないので、0より下に軸も罫線も出さない。
            # 平常時±σの帯は下側が負になることがあり（平常時の平均が
            # 小さい深夜帯など）、既定のautorangeだとその分だけ軸が
            # 0より下まで伸びて意味のない目盛りが引かれていた。
            # nonnegative は表示範囲を切るだけで、帯の値自体は変えない。
            yaxis=dict(rangemode="nonnegative"),
        )
        title = f"{label}交通量（{unit_label}）"
        if series_mode == "vehicle":
            title += "　小型＝濃い色／大型＝明るい色"
        st.markdown(f"**{title}**")
        st.plotly_chart(fig, use_container_width=True)

    st.markdown(describe_baseline(baseline_windows, baseline_daytypes))
    st.caption(
        "実線「実績」＝今回の観測実績（地震前日からの推移を含む）。"
        "点線「平常時」＝上記の平常時平均。"
        "灰色の帯「平常時±σ」（1地点選択時のみ）＝平常時の平均±標準偏差で、"
        "この帯から外れているほど平常時と違う動きをしていることを示します。"
        "黒い点線が本震の発生時刻（16:27）、薄いグレーの細い点線がその他の主要な地震"
        f"（震度5弱以上、{len(other_event_times)}件）の発生時刻。"
        + (f" {extra_note}" if extra_note else "")
    )


def main():
    st.markdown(
        "<style>"
        'iframe[title="streamlit_folium.st_folium"] { width: 100% !important; }'
        # 本題（地図・時系列・異常検知）が早く視界に入るよう、上部の余白を詰める。
        # ただしStreamlit標準のヘッダー（Deploy/共有/メニュー）は position:fixed の
        # 高さ60pxで、本文の余白がそれより小さいとタイトルがヘッダーに潜って欠ける。
        # ヘッダー自体を低くするとツールバーのボタン（下端が固定で52px）がはみ出すため、
        # ヘッダーには触れず、その高さを必ず上回る余白にする（既定の6remよりは詰める）。
        ".stMainBlockContainer, .block-container { padding-top: 4.2rem !important; }"
        # 見出しは既定だと大きすぎて、それだけで1画面を使ってしまう
        ".dash-title { font-size:1.55rem; font-weight:700; line-height:1.3; margin:0 0 2px 0; }"
        ".dash-lead  { font-size:0.92rem; line-height:1.5; margin:0 0 6px 0; }"
        ".dash-src   { font-size:0.78rem; color:#5b6570; line-height:1.5; margin:0; }"
        # 粒度ラジオは4択。横並びのままだと 3+1 で折り返して座りが悪いので、
        # 2列のグリッドに固定して 2x2 にする。
        #   行 = 合計 / 車種別、列 = 5分間値 / 1時間値 の対応で読める。
        # 画面が狭いときは列が縦に積まれるので、グリッドは広い画面だけに掛ける。
        "@media (min-width: 901px){"
        '[data-testid="stRadio"] [role="radiogroup"]'
        "{display:grid;grid-template-columns:1fr 1fr;gap:0 8px;}}"
        # 観測点の選択と粒度の操作行を画面上部に貼り付ける。地図で観測点を
        # 選び直すたびにスクロールで戻る必要があったため。
        # sticky はウィジェット本体ではなく、それを包む element-container ではなく
        # さらに外側の「行」に掛ける必要がある。ウィジェット直下の
        # element-container は中身と同じ高さで、sticky が動く余地が無いため
        # （実測: どちらも 737px で一致し、貼り付いても位置が変わらない）。
        # スクロールしているのは section.main（overflow:auto）なので、
        # その中で sticky が効く。標準ヘッダー（position:fixed・高さ60px）に
        # 隠れないよう top を下げる。
        '[data-testid="stHorizontalBlock"]:has([data-testid="stMultiSelect"])'
        "{position:sticky;top:3.9rem;z-index:20;"
        "background-color:var(--background-color,#ffffff);"
        "padding:6px 2px 4px 2px;border-bottom:1px solid rgba(130,130,130,0.25);}"
        "@media (prefers-color-scheme: dark){"
        '[data-testid="stHorizontalBlock"]:has([data-testid="stMultiSelect"])'
        "{background-color:var(--background-color,#0e1117);}}"
        # 画面が狭いと列が縦に積まれて操作行が高くなり、貼り付けると
        # 画面の大半を占めてしまうので、その場合は貼り付けない。
        "@media (max-width: 900px){"
        '[data-testid="stHorizontalBlock"]:has([data-testid="stMultiSelect"])'
        "{position:static;border-bottom:none;}}"
        "</style>",
        unsafe_allow_html=True,
    )
    data_missing = not os.path.exists(os.path.join(DATA_DIR, "observations.parquet"))
    if data_missing:
        st.error(
            "data/observations.parquet が見つかりません。先に `python fetch_and_prepare.py` を実行してください。"
        )
        st.stop()

    # 異常検知（zスコア・地図の色分け・異常検知一覧）は1時間値ベースで定義する。
    # 時系列図の実績は既定で5分間値を使う（load_observationsで別途読み込む）。
    observations = load_observations("observations_hourly.parquet")
    quake_info = load_quake_info()
    regulations = load_regulations()
    mlit = load_mlit_regulations()
    mainshock = quake_info["mainshock"]
    quake_at = pd.Timestamp(mainshock["occurred_at"]).tz_localize(None)
    other_event_times = [
        pd.Timestamp(e["occurred_at"]).tz_localize(None)
        for e in quake_info.get("events", [])
        if e.get("eid") != mainshock.get("eid")
    ]

    post = observations[observations["is_post_quake"]]
    point_summary = build_point_summary(post, observations)

    if "selected_points" not in st.session_state:
        st.session_state["selected_points"] = _default_selection(point_summary)

    INTENSITY_LABELS = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5弱", 6: "5強", 7: "6弱", 8: "6強", 9: "7"}
    n_events = len(quake_info.get("events", []))
    min_intensity_label = INTENSITY_LABELS.get(quake_info.get("events_min_intensity"), "?")
    period_start = quake_info.get("events_period_start", "?")
    period_end = quake_info.get("events_period_end", "?")

    # 以前はマグニチュード・最大震度・観測点数・取得件数・異常件数を
    # st.metric 5枚で並べていたが、縦を取るわりに読む頻度が低い。
    # 地震の諸元はタイトルに、データの規模はリード文に畳み込み、
    # 観測点数は地図の見出しへ移した（異常件数は一覧タブにあるので出さない）。
    raw_5min = load_traffic_archive("traffic_raw.parquet")
    n_points = len(point_summary)
    scale_note = ""
    if not raw_5min.empty:
        scale_note = (
            f"（対象範囲の{n_points}点で全{len(raw_5min):,}件、"
            f"{raw_5min['datetime'].max():%Y-%m-%d %H:%M}時点）"
        )
    st.markdown(
        '<div class="dash-title">熊本地震（2026-07-28 M{m}・最大震度{i}）'
        '交通量変化ダッシュボード</div>'
        '<div class="dash-lead">'
        '<b><a href="https://www.jartic-open-traffic.org/" target="_blank">'
        'JARTIC 交通量オープンデータ</a></b> の常設トラカン交通量（5分間値・1時間値）'
        '{note}を主データに、'
        '平常時と比べて交通量がどれだけ外れたかを観測点ごとに可視化しています。'
        '地震情報と通行規制を重ね合わせ、変化の背景を追えるようにしています。'
        "</div>".format(
            m=mainshock["magnitude"],
            i=mainshock["max_intensity"] or "?",
            note=scale_note,
        ),
        unsafe_allow_html=True,
    )

    archive_period = ""
    if not raw_5min.empty and "datetime" in raw_5min.columns:
        archive_period = (
            f"交通量アーカイブ: {raw_5min['datetime'].min():%Y-%m-%d %H:%M}"
            f" 〜 {raw_5min['datetime'].max():%Y-%m-%d %H:%M}　｜　"
        )
    st.markdown(
        '<div class="dash-src">'
        f"{archive_period}"
        f"異常検知の対象期間: 本震（{period_start}）〜 {period_end}"
        f"（本震+2週間または現在時刻の早い方）　｜　"
        f"データ生成: {quake_info.get('generated_at', '不明')}<br>"
        "データ源: "
        '<a href="https://www.jartic-open-traffic.org/" target="_blank">JARTIC 交通量オープンデータ</a>'
        ' ／ <a href="https://www.jma.go.jp/jma/menu/20260728_kumamoto_jishin.html" target="_blank">気象庁</a>'
        ' ／ <a href="https://portal.bousai.pref.kumamoto.jp/?p=traffic" target="_blank">防災情報くまもと（通行規制情報）</a>'
        ' ／ <a href="https://www.qsr.mlit.go.jp/kumamoto/" target="_blank">熊本河川国道事務所（直轄国道の規制）</a>'
        # 交通量API利用規約 第5条1項が求める出典。「エンドユーザーが当該
        # サービスを利用する度に確認できる位置」に出す必要があるので、
        # タブより前・タイトル直下の常時表示にしている。
        # 第5条2項（加工して利用する場合）と第8条2項（利用者が一切の責任を
        # 負う旨の明記）もここでまとめて満たす。
        f'<br>{JARTIC_TERMS_NOTICE}'
        "</div>",
        unsafe_allow_html=True,
    )

    MAXI_DISPLAY = {
        "1": "1", "2": "2", "3": "3", "4": "4",
        "5-": "5弱", "5+": "5強", "6-": "6弱", "6+": "6強", "7": "7",
    }
    # 地震一覧は参照用なので折りたたむ（開いたままだと本題が画面の下に押し出される）
    # 見出しに本震だけの諸元（震源の深さ・震源地）を併記すると、
    # 複数地震の一覧であることと食い違うので入れない。
    with st.expander(
        f"最大震度{min_intensity_label}以上を観測した地震の一覧（{n_events}件）"
    ):
        events_rows = []
        for e in quake_info.get("events", []):
            dt = datetime.fromisoformat(e["occurred_at"])
            events_rows.append({
                "発生時刻": dt.strftime("%Y年%m月%d日%H時%M分"),
                "震央地名": e["epicenter_name"],
                "マグニチュード": e["magnitude"],
                "最大震度": MAXI_DISPLAY.get(e["max_intensity"], e["max_intensity"] or "-"),
                "震度": f"https://www.jma.go.jp/bosai/map.html#&contents=estimated_intensity_map&id={dt.strftime('%Y%m%d%H%M')}",
            })
        st.dataframe(
            pd.DataFrame(events_rows),
            hide_index=True,
            use_container_width=True,
            column_config={
                "震度": st.column_config.LinkColumn("震度", display_text="推計震度分布図"),
            },
        )
        st.caption(
            "出典: [気象庁](https://www.jma.go.jp/jma/menu/20260728_kumamoto_jishin.html)。"
            "推計震度分布図は地震発生直後に発表されたもの（発表がない地震ではリンク先に情報がない場合があります）。"
        )

    tab_overview, tab_list, tab_dl = st.tabs(
        ["地図・時系列", "異常検知一覧", "データダウンロード"]
    )

    # ------------------------------------------------------------------
    # 地図・時系列タブ（統合ビュー）
    # ------------------------------------------------------------------
    with tab_overview:
        if post.empty:
            st.info("地震発生後のデータがまだありません。")
        else:
            point_labels = build_point_labels(point_summary)
            coords_by_id = point_summary.set_index("point_id")[
                ["point_lat", "point_lon"]
            ].to_dict("index")
            selected_points = st.session_state["selected_points"]
            sel_version = st.session_state.get("_sel_version", 0)

            def _format_point(pid: str) -> str:
                c = coords_by_id[pid]
                return f"{point_labels[pid]}（{c['point_lat']:.3f}N, {c['point_lon']:.3f}E）"

            # 観測点の選択と粒度の切り替えを1行にまとめる。観測点を比べるときに
            # 何度も触るのはこの2つなので、同じ行に置いてまとめて画面に貼り付ける
            # （下のCSSで sticky）。粒度ラジオが地図より前に作られる点も維持する。
            col_select, col_view, col_range, col_clear = st.columns([3, 2, 1.3, 1])
            with col_select:
                picked = st.multiselect(
                    f"観測点の選び方：このプルダウンから選ぶか、地図上の丸いマーカーをクリック"
                    f"（最大{MAX_SELECTED_POINTS}地点まで並べて比較できます）",
                    options=list(point_labels.keys()),
                    default=[p for p in selected_points if p in point_labels],
                    max_selections=MAX_SELECTED_POINTS,
                    format_func=_format_point,
                    key=f"point_select_{sel_version}",
                )
            with col_view:
                view_names = list(TIMESERIES_VIEWS.keys())
                saved_view = st.session_state.get("_timeseries_view_choice", view_names[0])
                view = st.radio(
                    "交通量の集計方法",
                    view_names,
                    index=view_names.index(saved_view) if saved_view in view_names else 0,
                    horizontal=True,
                    key="timeseries_view",
                )
                st.session_state["_timeseries_view_choice"] = view
                cfg = TIMESERIES_VIEWS[view]
            with col_range:
                # 粒度ラジオと同じ理由で地図より前に作る（地図クリックの
                # st.rerun() で中断されるとウィジェットの状態が落ちるため）。
                range_names = list(TIMESERIES_RANGES.keys())
                saved_range = st.session_state.get(
                    "_timeseries_range_choice", TIMESERIES_DEFAULT_RANGE
                )
                range_name = st.selectbox(
                    "時系列の表示期間",
                    range_names,
                    index=(
                        range_names.index(saved_range) if saved_range in range_names
                        else range_names.index(TIMESERIES_DEFAULT_RANGE)
                    ),
                    key="timeseries_range",
                )
                st.session_state["_timeseries_range_choice"] = range_name
            with col_clear:
                st.write("")
                if st.button("選択をクリア", disabled=not selected_points):
                    st.session_state["selected_points"] = []
                    st.session_state["_sel_version"] = sel_version + 1
                    st.rerun()

            # プルダウンの選択は、この実行のなかでそのまま採用する。
            # 以前は session_state に入れて st.rerun() していたが、選択1回あたり
            # スクリプトが2回走り（実測: 96ms差で連続実行）、体感で倍の待ち時間に
            # なっていた。地図・時系列はこの下で描かれるので、picked を
            # そのまま使えば1回の実行で反映できる。
            if picked != selected_points:
                selected_points = picked
            st.session_state["selected_points"] = picked

            # 地図と時系列が近すぎて見分けづらいので、列の間隔を広く取る
            col_map, col_ts = st.columns([2, 3], gap="large")

            # 粒度のラジオは地図より先に作る。地図クリックの処理は st.rerun() で
            # スクリプトを中断するため、そこより後ろでウィジェットを作ると
            # その実行では作られず、Streamlit側の状態が落ちて既定値（5分間値）に
            # 戻ることがある（画面のラジオは1時間値のままなのに図だけ5分値になる）。
            # あわせて、選択内容をウィジェット以外のキーにも控えておき、
            # 状態が落ちても index で復元できるようにする。
            with col_ts:
                st.subheader("選択観測点の時系列（平常時 vs 観測実績）")

            with col_map:
                st.subheader(f"常時観測点（{n_points}点）別の異常度 × 通行規制")
                _now = _now_jst()
                # 色（規制の区分）と線の形（規制中か解除済みか）は独立した軸で、
                # 破線は青灰・赤・橙のどれにも、県feedにも直轄国道にも同じように
                # 掛かる。以前は「破線は解除済み」を地震前の規制の行に置いていて、
                # その区分だけの決まりごとに見えていたので、行を分けた。
                n_post = sum(
                    1 for r in regulations if _regulation_is_post_quake(r, quake_at)
                )
                n_pre = len(regulations) - n_post
                n_ended = sum(1 for r in regulations if _regulation_is_ended(r, _now))
                # 凡例は3行に圧縮する。見出しを独立行にすると1項目ごとに
                # 行が増えて余白ばかりになるので、見出しも同じ行に入れて
                # 「分類: 項目 項目 …」の形にし、文字も小さくする。
                n_points = len(point_summary)
                n_no_data = int(point_summary["max_abs_z"].isna().sum())
                _mlit_items = (mlit or {}).get("items", [])
                n_mlit_line = sum(1 for i in _mlit_items if i.get("path"))
                n_mlit_point = sum(
                    1 for i in _mlit_items
                    if not i.get("path") and i.get("affected_point_codes")
                )
                # 線も▲も出せない規制。PDFが区間をIC名で示していない
                # （キロポストや地名だけの）ものがこれにあたる。黙って
                # 落とすと、地図を見た人は「直轄国道の規制はこれで全部」と
                # 受け取ってしまうので、件数を明示する。
                n_mlit_spot = sum(
                    1 for i in _mlit_items if i.get("point") and not i.get("path")
                )
                n_mlit_hidden = (
                    len(_mlit_items) - n_mlit_line - n_mlit_point - n_mlit_spot
                )
                n_ended += sum(
                    1 for i in _mlit_items if i.get("path") and i.get("end_timestamp")
                )

                def _sw(style: str) -> str:
                    """凡例の色見本。縦位置を文字に揃える。"""
                    return f'<span style="display:inline-block;vertical-align:middle;{style}"></span>'

                bar = "width:20px;height:5px;"
                color_items = [
                    f'{_sw(bar + "background:#e60000;")} 全面/車両通行止め',
                    f'{_sw("width:20px;height:4px;background:#e67e22;")} 片側交互など',
                ]
                if n_pre:
                    color_items.append(
                        f'{_sw("width:20px;height:3px;background:#5b7c99;opacity:0.55;")}'
                        f' 地震前からの規制 {n_pre}件（工事・過去の災害）'
                    )
                if n_mlit_line:
                    # 規制区間の端点となるICも点で置いている。
                    color_items.append(
                        f'{_sw("width:11px;height:11px;border-radius:50%;"
                              "background:#fff;border:2px solid #333;")}'
                        ' 規制区間の端点（IC）'
                    )
                # 記号ごとに意味を1つにする。色＝規制の区分、⊗＝地震後に
                # 始まったかどうか、実線/破線＝いまの状態、線か⊗単独か＝
                # 場所が区間で示されているか地点でしか分からないか。
                shape_items = [
                    '<span style="color:#555;font-weight:700;">⊗</span>'
                    f' 地震後に始まった規制（県 {n_post}件・直轄国道'
                    f' {len(_mlit_items)}件。色は上の区分）',
                    f'{_sw("width:20px;height:4px;background:#95a5a6;")} 実線・外円が実線は規制中',
                ]
                if n_ended:
                    shape_items.append(
                        f'{_sw("width:20px;height:0;border-top:4px dashed #95a5a6;")}'
                        f' 破線・外円が点線は解除済み {n_ended}件'
                    )
                if n_mlit_spot:
                    shape_items.append(
                        f'区間は線、地点だけ分かるものは⊗のみ {n_mlit_spot}件（位置は概略）'
                    )
                if n_mlit_point:
                    shape_items.append(
                        '<span style="color:#b00000;font-weight:900;">▲</span>'
                        f' 直轄国道で線形が作れないもの {n_mlit_point}件（該当観測点のすぐ上）'
                    )
                # 形（道路の種別）の凡例は地図の中に置いてあるのでここには出さない
                point_items = [
                    f'{_sw("width:30px;height:11px;border-radius:6px;border:1px solid #aaa;"
                          "background:linear-gradient(to right,#deebf7,#08306b);")}'
                    f' 濃く大きいほど異常度|z|が大きい {n_points - n_no_data}点',
                ]
                if n_no_data:
                    point_items.append(
                        f'{_sw("width:11px;height:11px;border-radius:50%;background:#f0f0f0;"
                              "border:2px dashed #777;")}'
                        f' 枠が灰色の破線なら地震後が欠測 {n_no_data}点'
                    )
                rows = [
                    ("規制の色", color_items),
                    ("規制の印と状態", shape_items),
                    (f"観測点 {n_points}点", point_items),
                ]
                legend_html = "".join(
                    '<div style="display:flex;flex-wrap:wrap;gap:1px 12px;align-items:center;">'
                    f'<b style="white-space:nowrap;">{head}:</b>'
                    + "".join(f'<span style="white-space:nowrap;">{it}</span>' for it in items)
                    + "</div>"
                    for head, items in rows
                )
                st.markdown(
                    '<div style="font-size:0.79rem;line-height:1.45;margin:0 0 4px 0;">'
                    f"{legend_html}</div>",
                    unsafe_allow_html=True,
                )
                base_map = build_base_map(
                    point_summary, mainshock, regulations, mlit, point_labels
                )
                points_fg = build_points_feature_group(
                    point_summary, point_labels, selected_points
                )
                map_state = st_folium(
                    # 縦を詰めて右側の2図と高さを近づける（750 -> 620）
                    base_map, height=620, width=550,
                    feature_group_to_add=points_fg,
                    # 座標ではなく「何をクリックしたか」で判定する。
                    #   last_object_clicked_tooltip … クリックされた図形のツールチップ本文。
                    #        これで観測点マーカーかどうかを確実に見分けられる
                    #   last_object_clicked_count   … 図形クリックのたびに増える通し番号。
                    #        同じマーカーを続けて押しても値が変わるので反応する
                    returned_objects=[
                        "last_object_clicked_tooltip", "last_object_clicked_count",
                    ],
                    key="quake_map_v8",
                )
                render_mlit_notice(mlit, point_summary, point_labels)
                st.caption(
                    "通行規制データ: 「防災情報くまもと」の"
                    "[通行規制情報](https://portal.bousai.pref.kumamoto.jp/?p=traffic)ページ"
                    "（[熊本市防災情報ポータル](https://city-kumamoto.my.salesforce-sites.com/)からもリンクあり）。"
                    "始点・終点座標をOSRMで道路網にスナップして表示しています。"
                    "元データには2020年7月豪雨など今回の地震と無関係な長期規制も含まれるため、"
                    "規制の開始日時が本震（16:27）以降かどうかで色分けし、"
                    "地図の右下のレイヤ一覧で、道路の管理者（直轄国道／県・市町村道）と""状態（規制中／解除済み／地震前からの規制）の組み合わせごとに""表示を切り替えられます。既定では規制中だけを出しています。"
                )
                st.caption(
                    "通行規制は管理者ごとに公表の仕方が違うため、2つの経路で集めています。"
                    "①上記の「防災情報くまもと」は県・市町村が管理する道路が対象です。"
                    "②国が管理する直轄国道は配信の仕組みがなく、"
                    f"[{mlit.get('source_name', '国土交通省')}]({mlit.get('source_url', '')})"
                    f"のPDFから転記した{len(_mlit_items)}件を別レイヤで描いています"
                    f"（区間の線 {n_mlit_line}件"
                    + (f"、地点の⊗ {n_mlit_spot}件" if n_mlit_spot else "")
                    + (f"、位置を特定できず地図に出せないもの {n_mlit_hidden}件" if n_mlit_hidden else "")
                    + "）。"
                    "PDFには座標がないため、区間はOSMのIC座標から線形を復元し、"
                    "キロポストだけのものは地名から復元した概略の位置に置いています。"
                    "描き分けは①と同じで、色が規制の区分、破線が解除済みです。"
                    "内訳は上の「直轄国道の規制」に出しています。"
                    "③規制のアーカイブに記録が残っている最も古い時点は "
                    f"**{regulation_archive_start() or '本震の翌日'}**（本震より後）です。"
                    "それ以前に解除された規制は、県管理道路であっても残っていません。"
                )
                st.caption(
                    "観測点は色が濃いほど地震後の交通量変化（|zスコア|）が大きいことを示す（青系のグラデーション）。"
                    "地点番号は異常度の大きい順。青いマーカーは震源。"
                    "選択中の観測点は赤/緑の枠で強調表示されます（最大2地点）。"
                    "丸いマーカーをクリックしても選択できます（反映に1〜2秒かかります）。"
                    "すぐに切り替えたいときは上のプルダウンが速いです。"
                )

            # クリックされたのが観測点マーカーのときだけ選択を切り替える。
            # 通行規制の線などを押した場合はツールチップが一致しないので何も起きない
            # （ツールチップは表示されるので、押した本人の意図とも合う）。
            click_count = map_state.get("last_object_clicked_count") if map_state else None
            if click_count is not None and click_count != st.session_state.get("_last_click_count"):
                st.session_state["_last_click_count"] = click_count
                pid = point_id_from_tooltip(
                    map_state.get("last_object_clicked_tooltip"), point_labels
                )
                if pid is not None:
                    selected = list(st.session_state["selected_points"])
                    if pid in selected:
                        selected.remove(pid)
                    else:
                        if len(selected) >= MAX_SELECTED_POINTS:
                            selected.pop(0)
                        selected.append(pid)
                    st.session_state["selected_points"] = selected
                    # セレクタ側のウィジェット状態は古くなるので、キーを変えて作り直す
                    st.session_state["_sel_version"] = sel_version + 1
                    st.rerun()

            with col_ts:
                # 選択中の観測点に掛かっている直轄国道の通行止めを帯で示す
                mlit_bands = []
                for pid in selected_points:
                    row = point_summary[point_summary["point_id"] == pid]
                    if row.empty:
                        continue
                    for item in mlit_regulations_for_point(mlit, row["point_code"].iloc[0]):
                        start = _parse_reg_time(item.get("start_timestamp"))
                        end = _parse_reg_time(item.get("end_timestamp")) or observations["datetime"].max()
                        if start is None:
                            continue
                        mlit_bands.append({
                            "start": start, "end": end,
                            "label": (
                                f"{item['route_name']}（{item['section']}）{item['content']}"
                            ),
                        })
                ts_obs = load_observations(cfg["file"])
                last_at = (
                    ts_obs["datetime"].max() if not ts_obs.empty
                    else quake_at + pd.Timedelta(days=7)
                )
                render_timeseries(
                    ts_obs,
                    selected_points, quake_at, other_event_times, point_labels,
                    quake_info.get("hourly_baseline_windows"),
                    unit_label=cfg["unit_label"],
                    extra_note=cfg["note"],
                    mlit_bands=mlit_bands,
                    baseline_daytypes=quake_info.get("hourly_baseline_daytypes"),
                    series_mode=cfg.get("series", "total"),
                    x_range=TIMESERIES_RANGES[range_name](quake_at, last_at),
                )

    # ------------------------------------------------------------------
    # 異常検知一覧タブ
    # ------------------------------------------------------------------
    with tab_list:
        st.subheader("異常検知結果一覧（地震発生後・1時間値ベース）")
        anomalies = observations[observations["is_anomaly"]].sort_values("datetime")
        st.write(f"検知件数: {len(anomalies)} 件")
        st.caption(
            "1時間値の実績と、同じ日区分の平常時（月/火/水/木/金/土/日祝の各8日分）の"
            "1時間値の平均・標準偏差を比べ、|zスコア| >= 2 を異常としています。"
            "地図の色分けもこの判定に基づきます。"
        )
        display_cols = [
            "point_code", "point_id", "datetime", "traffic_up", "traffic_down",
            "baseline_mean_up", "baseline_mean_down", "z_up", "z_down",
            "distance_km_from_epicenter",
        ]
        display_cols = [c for c in display_cols if c in anomalies.columns]
        st.dataframe(anomalies[display_cols], use_container_width=True, height=500)
        st.download_button(
            "CSVダウンロード",
            anomalies[display_cols].to_csv(index=False).encode("utf-8-sig"),
            file_name="kumamoto_traffic_anomalies.csv",
            mime="text/csv",
        )

    # ------------------------------------------------------------------
    # データダウンロードタブ
    # ------------------------------------------------------------------
    with tab_dl:
        st.subheader("アーカイブデータのダウンロード")
        st.caption(
            "JARTICの5分値は過去1ヶ月・1時間値は過去3ヶ月しか遡れず、通行規制は解除されると"
            "ポータルの一覧から消えてしまいます。このダッシュボードは取得した分を追記専用で"
            "蓄積しているため、ここから取得済みの全期間をCSVで取り出せます。"
        )
        # 配布するCSVは「本機能により取得した情報を用いた資料」にあたるので、
        # 交通量API利用規約 第5条2項の出典をダウンロード箇所にも出す。
        st.caption(f"<b>出典</b>: {JARTIC_TERMS_NOTICE}", unsafe_allow_html=True)

        downloads = [
            (
                "5分間交通量（生データ・アーカイブ全期間）",
                load_traffic_archive("traffic_raw.parquet"),
                "kumamoto_traffic_5min_archive.csv",
                "観測点（常時観測点コード）×日時ごとの上り・下り交通量。車種別の内訳列も含みます。",
            ),
            (
                "1時間交通量（生データ・アーカイブ全期間）",
                load_traffic_archive("traffic_hourly.parquet"),
                "kumamoto_traffic_hourly_archive.csv",
                "同じ観測点の1時間値。平常時（日区分ごとに8日分）の母集団もこのデータから作っています。",
            ),
        ]
        regs_df, hist_df = load_regulations_archive()
        downloads += [
            (
                "通行規制 一覧（アーカイブ全期間）",
                regs_df,
                "kumamoto_road_regulations_archive.csv",
                "1件1行。初回・最終確認日時と、まだポータルに載っているか（still_listed）を含みます。"
                "経路の座標列は列数が膨大になるためCSVには含めていません（`data/archive/regulations_archive.json` にあります）。",
            ),
            (
                "通行規制 状態変化の履歴",
                hist_df,
                "kumamoto_road_regulations_history.csv",
                "規制内容や終了日時が変わった時点ごとに1行。"
                "全面通行止め → 片側交互通行止め → 解除 といった推移を追えます。",
            ),
            (
                "異常検知の入力データ（1時間値＋平常時＋zスコア）",
                observations,
                "kumamoto_observations_hourly.csv",
                "地図の色分けと異常検知一覧の根拠になっている表そのものです。",
            ),
        ]

        # --- データセット一覧（表形式）---
        # 以前は列定義書を先に置いていたが、「上記すべてのCSV」と書いても
        # その時点でどんなCSVがあるか示されていなかった。先に一覧を出す。
        sheet_of = {fname: sheet for fname, sheet, _, _ in DATA_DICTIONARY}
        col_w = [2.4, 3.8, 1.9, 1.7]
        head = st.columns(col_w)
        for col, label in zip(head, ["データセット", "内容", "規模", "ダウンロード"]):
            col.markdown(f"<div style='font-weight:600;font-size:0.85rem;'>{label}</div>",
                         unsafe_allow_html=True)
        st.markdown(
            "<hr style='margin:2px 0 6px 0;border:none;border-top:1px solid #ccc;'>",
            unsafe_allow_html=True,
        )
        for title, df, fname, desc in downloads:
            c1, c2, c3, c4 = st.columns(col_w)
            with c1:
                st.markdown(f"**{title}**")
                st.caption(f"`{fname}`  \n列定義書のシート: **{sheet_of.get(fname, '—')}**")
            c2.caption(desc)
            if df is None or df.empty:
                c3.caption("まだデータがありません")
                c4.button("CSV", disabled=True, key=f"dl_{fname}_disabled")
            else:
                csv_bytes = to_csv_bytes(df)
                scale = f"{len(df):,} 行 / {len(csv_bytes) / 1024:,.0f} KB"
                if "datetime" in df.columns:
                    scale += (
                        f"  \n期間: {df['datetime'].min():%Y-%m-%d %H:%M}"
                        f" 〜 {df['datetime'].max():%Y-%m-%d %H:%M}"
                    )
                c3.caption(scale)
                c4.download_button(
                    "CSV", csv_bytes, file_name=fname, mime="text/csv",
                    key=f"dl_{fname}", use_container_width=True,
                )
            st.markdown(
                "<hr style='margin:4px 0;border:none;border-top:1px solid #eee;'>",
                unsafe_allow_html=True,
            )

        # --- 列定義書（データディクショナリ） ---
        actual_columns = {
            fname: list(df.columns)
            for _, df, fname, _ in downloads
            if df is not None and not df.empty
        }
        dict_df, dict_notes = build_data_dictionary(actual_columns)
        st.markdown("**CSVの列定義書（各列の意味・単位）**")
        st.caption(
            f"上の表の{len(DATA_DICTIONARY)}つのCSVについて、列の並び順・単位・意味をまとめたExcelブックです"
            f"（1シート＝1データセットの全{len(DATA_DICTIONARY)}シート＋目次「はじめに」／計{len(dict_df)}列の定義）。"
            "シート名は上の表の「列定義書のシート」と対応します。"
            "データを受け取った人が中身を解釈できるよう、CSVと一緒に配布してください。"
        )
        st.download_button(
            "列定義書をダウンロード（kumamoto_data_dictionary.xlsx）",
            to_dictionary_xlsx_bytes(dict_df, tuple(dict_notes)),
            file_name="kumamoto_data_dictionary.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dl_dictionary",
        )
        # 定義書と実データのずれは異常ではなく一時的に起こりうるものなので、
        # 警告色ではなく理由の分かる備考として出す。
        for note in dict_notes:
            st.caption(note)
        with st.expander("列定義書をこの画面で見る"):
            # Excelのシート分けと同じ区切りで見られるようにタブにする
            dict_tabs = st.tabs([sheet for _, sheet, _, _ in DATA_DICTIONARY])
            # 「配布中CSVに存在」はxlsxと同じ条件（食い違いがあるときだけ）で出す
            drop_cols = ["csv_file", "dataset"]
            if not dictionary_has_missing_column(dict_df):
                drop_cols.append("in_actual_csv")
            for tab, (fname, _sheet, dataset, _cols) in zip(dict_tabs, DATA_DICTIONARY):
                with tab:
                    st.caption(f"{dataset}｜`{fname}`")
                    sub = dict_df[dict_df["csv_file"] == fname]
                    st.dataframe(
                        sub.drop(columns=drop_cols).rename(
                            columns={"in_actual_csv": "配布中CSVに存在"}
                        ),
                        use_container_width=True, hide_index=True,
                    )
        st.divider()

        st.caption(
            "出典を明記してご利用ください: 交通量は"
            "[JARTIC交通量オープンデータ](https://www.jartic-open-traffic.org/)、"
            "通行規制は「防災情報くまもと」の"
            "[通行規制情報](https://portal.bousai.pref.kumamoto.jp/?p=traffic)。"
            "通行規制の経路は始点・終点座標を[OSRM](https://project-osrm.org/)で"
            "道路網にスナップした推定値であり、元データそのものではありません。"
        )


if __name__ == "__main__":
    main()
