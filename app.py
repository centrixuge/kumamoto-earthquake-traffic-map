#!/usr/bin/env python
# coding: utf-8
"""
熊本地震（2026-07-28）による交通行動変容分析ダッシュボード。

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

from modules.stations import attach_point_code, load_station_master

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MAX_SELECTED_POINTS = 2
SELECTION_COLORS = ["red", "green"]
# 観測点マーカーのツールチップに必ず入れる定型句。クリックされた図形が
# 観測点マーカーかどうかを、この文字列で判定する（point_id_from_tooltip）。
# 表示と判定でずれないよう1か所に置く。
POINT_TOOLTIP_HINT = "（クリックで時系列に表示/解除）"
# 時系列図の表示開始時刻（データ保持期間の先頭より後ろにしている）
TIMESERIES_DISPLAY_START = pd.Timestamp("2026-07-27 12:00")

# 時系列ビューの切り替え。実績の既定は5分間値、平常時はいずれも1時間値
# （同曜日8週分・祝日除く）から求めている。
TIMESERIES_VIEWS = {
    "5分間値（既定）": {
        "file": "observations.parquet",
        "unit_label": "5分間値",
        "note": (
            "平常時は1時間値から求め、単位を合わせるため1/12しています。"
            "そのため帯は「平常時の1時間あたり交通量の日々のばらつき÷12」であり、"
            "5分間値そのもののばらつきではありません（帯は狭めに出ます）。"
            "異常検知の判定は1時間値ベースで行っています。"
        ),
    },
    "1時間値（参考）": {
        "file": "observations_hourly.parquet",
        "unit_label": "1時間値",
        "note": "実績・平常時とも1時間値どうしの比較で、異常検知の判定もこの粒度で行っています。",
    },
}

# 通行規制の日時はJSTの壁時計時刻（naive）で保存されている。Streamlit Cloud等の
# UTCサーバーでdatetime.now()をそのまま使うと「終了済みか」の判定がずれるため、
# fetch_and_prepare.py と同様にJST固定の「今」を使う。
JST = timezone(timedelta(hours=9))


def _now_jst() -> datetime:
    return datetime.now(JST).replace(tzinfo=None)

st.set_page_config(
    page_title="熊本地震・交通行動変容ダッシュボード",
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
    ("point_id", "文字列", "内部の結合キー（経度_緯度を6桁に丸めた文字列）。観測点の識別にはpoint_codeを使ってください"),
    ("point_lon", "度（EPSG:4326）", "観測点の経度（6桁に丸め）"),
    ("point_lat", "度（EPSG:4326）", "観測点の緯度（6桁に丸め）"),
    ("datetime", "日時（JST）", "観測時刻（1時間値なので毎時0分）"),
    ("hour", "0〜23", "時刻の「時」。平常時の平均・標準偏差はこの単位で求めている"),
    ("traffic_up", "台/時", "上り方向の実績交通量"),
    ("traffic_down", "台/時", "下り方向の実績交通量"),
    ("baseline_mean_up", "台/時", "平常時（同曜日8週分・祝日除く）の上り交通量の平均"),
    ("baseline_std_up", "台/時", "同じ母集団での上り交通量の標準偏差"),
    ("baseline_mean_down", "台/時", "平常時の下り交通量の平均"),
    ("baseline_std_down", "台/時", "平常時の下り交通量の標準偏差"),
    ("z_up", "無次元", "上りのzスコア =（実績 − 平常時平均）÷ 標準偏差。標準偏差が0や極小のときは下限でクリップ"),
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

    intro["A1"] = "熊本地震・交通行動変容ダッシュボード CSV列定義書"
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
            "point_id", "point_code", "point_lon", "point_lat",
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


FULL_CLOSURE_CONTENTS = {"全面通行止め", "車両通行止め"}


def _point_radius(max_abs_z, max_z: float, selected: bool = False) -> float:
    """
    観測点マーカーの半径。異常度に比例させる。欠測（NaN）は最小サイズ。
    ▲の表示位置を丸の外側に置くためにも使うので、計算を1か所にまとめている。
    """
    frac = 0.0 if pd.isna(max_abs_z) else max_abs_z / max_z
    return (14 if selected else 11) + 12 * frac


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
            "color": "#e60000", "dash_array": dash, "show_x": not ended,
            "weight": 6, "opacity": 0.5 if ended else 0.95,
        }
    return {
        "color": "#e67e22", "dash_array": dash, "show_x": False,
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

        if regulations:
            now = _now_jst()
            quake_at = datetime.fromisoformat(mainshock["occurred_at"]).replace(tzinfo=None)
            # 地震前からの規制を先に描いて、地震起因の規制が上に重なるようにする。
            post_layer = folium.FeatureGroup(name="通行規制：今回の地震以降に開始")
            pre_layer = folium.FeatureGroup(name="通行規制：地震前からの規制（工事・過去の災害等）")
            for reg in regulations:
                is_post = _regulation_is_post_quake(reg, quake_at)
                style = _regulation_style(reg, now, quake_at)
                ended = _regulation_is_ended(reg, now)
                period = reg["start_timestamp"] or "?"
                period += f" 〜 {reg['end_timestamp']}" if ended and reg["end_timestamp"] else " 〜 (継続中)"
                target = post_layer if is_post else pre_layer
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
                        icon=folium.DivIcon(
                            html=(
                                '<div style="font-size:22px;font-weight:900;color:#e60000;'
                                'line-height:1;text-shadow:0 0 2px white,0 0 2px white;">×</div>'
                            ),
                            class_name="reg-x-mark",
                        ),
                    ).add_to(target)
            pre_layer.add_to(fmap)
            post_layer.add_to(fmap)

        # 直轄国道の規制。PDFは区間を「○○IC〜○○IC」と名前で示すだけで座標が無いが、
        # 実在する道路区間なので端点のIC座標から線形を復元してある
        # （scripts/build_mlit_paths.py が path として書き込む）。
        # 線形を作れなかった規制（キロポストで示された地点など）は、
        # 掛かっていた観測点が分かる場合だけ▲を置いてフォールバックする。
        mlit_items = (mlit or {}).get("items", [])
        mlit_drawn = 0
        if mlit_items:
            mlit_layer = folium.FeatureGroup(name="直轄国道の規制")
            for item in mlit_items:
                path = item.get("path")
                if not path:
                    continue
                ended = bool(item.get("end_timestamp"))
                full = item.get("content") in FULL_CLOSURE_CONTENTS
                folium.PolyLine(
                    locations=path,
                    color="#e60000" if full else "#e67e22",
                    weight=6 if full else 5,
                    opacity=0.5 if ended else 0.95,
                    dash_array="6,8" if ended else None,
                    tooltip=(
                        f"<b>直轄国道の規制</b>（区間の線はOSMのIC座標から復元）<br>"
                        f"{item['route_name']}（{item['section']}）<br>"
                        f"<b>{item['content']}／{'解除済み' if ended else '規制中'}</b><br>"
                        f"{item['start_timestamp']} 〜 {item['end_timestamp'] or '(継続中)'}<br>"
                        f"出典: 熊本河川国道事務所（県ポータルのデータには含まれません）"
                    ),
                ).add_to(mlit_layer)
                mlit_drawn += 1

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
                    ).add_to(mlit_layer)
                    mlit_drawn += 1

            if mlit_drawn:
                mlit_layer.add_to(fmap)

        if regulations or mlit_drawn:
            folium.LayerControl(collapsed=False).add_to(fmap)

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
    地図に線として描けない規制（直轄国道）があることを、理由と出典つきで示す。

    熊本県「防災情報くまもと」の通行規制情報は県・市町村が管理する道路が対象で、
    国が管理する直轄国道（国道57号など）は載らない。そのため観測点の交通量が
    0になっていても、その理由が地図から読み取れない状態になっていた。
    """
    items = (mlit or {}).get("items", [])
    if not items:
        return
    src_name = mlit.get("source_name", "国土交通省")
    src_url = mlit.get("source_url", "")
    n_line = sum(1 for i in items if i.get("path"))
    with st.expander(
        f"⚠ 県のデータに含まれない規制があります（直轄国道・{len(items)}件）", expanded=False
    ):
        st.markdown(
            "熊本県「防災情報くまもと」の通行規制情報は**県・市町村が管理する道路**が対象で、"
            "**国が管理する直轄国道（国道57号など）の規制は含まれません**。"
            "そのため観測点の交通量が0になっていても、地図上にその原因となる規制が出てきません。"
            f"下記は [{src_name}]({src_url}) が公表しているPDFから転記したものです。"
            f"PDFは区間を「○○IC〜○○IC」と名前で示すだけで座標がありませんが、"
            f"実在する道路区間なので、端点のICの座標（OpenStreetMap）から"
            f"道路網に沿った線形を復元して地図に描いています（{n_line}件）。"
            "キロポストで示された地点のように線形を作れないものは、"
            "掛かっていた観測点が分かる場合だけその**すぐ上**に **▲** を置いています。"
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
        folium.CircleMarker(
            location=[row["point_lat"], row["point_lon"]],
            radius=_point_radius(row["max_abs_z"], max_z, is_selected),
            color=border_color,
            weight=4 if is_selected else (2 if no_data else 1),
            dash_array="4,3" if no_data else None,
            fill=True,
            fill_color="#f0f0f0" if no_data else _severity_color(frac),
            fill_opacity=0.6 if no_data else 0.85,
            tooltip=f"{label}{POINT_TOOLTIP_HINT}<br>{detail}",
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


def describe_baseline(baseline_windows) -> str:
    """
    「平常時」が具体的にどの期間を指すのかを説明する文を作る。
    期間は fetch_and_prepare.py が実際に使った値を quake_info.json 経由で受け取るため、
    説明文と計算内容がずれない。
    """
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    days = []
    for w in baseline_windows or []:
        try:
            d = datetime.fromisoformat(w["start"])
        except (ValueError, KeyError, TypeError):
            continue
        days.append((d, f"{d.month}/{d.day}"))
    if not days:
        return "**平常時**＝地震発生前の平日同時刻の交通量（時刻帯ごとの平均）。"
    days.sort()
    wd = weekdays[days[0][0].weekday()]
    if len(days) <= 3:
        span = "・".join(f"{s}（{wd}）" for _, s in days)
    else:
        # 8週分などを列挙すると長いので、日数と範囲だけを示す
        span = f"{days[0][1]}〜{days[-1][1]}の{wd}曜 {len(days)}日分"
    return (
        f"**平常時**＝地震発生前の同じ曜日（{span}）の、同じ時刻の交通量。"
        "観測点ごと・時刻（時）ごとに平均と標準偏差を求め、今回の実績と比べています。"
    )


def render_timeseries(
    observations: pd.DataFrame, selected_points, quake_at,
    other_event_times=(), point_labels: dict = None, baseline_windows=None,
    unit_label: str = "5分間値", extra_note: str = "",
    mlit_bands=(),
) -> None:
    if not selected_points:
        st.info("上のプルダウンから選ぶか、地図上の丸いマーカーをクリックして観測点を選ぶと、ここに時系列が表示されます（最大2地点まで比較可）。")
        return
    if observations.empty:
        st.warning("このビューのデータがまだ生成されていません。`python fetch_and_prepare.py` を実行してください。")
        return

    point_labels = point_labels or {}
    # 地震前日の深夜〜早朝は情報が薄いので、表示は7/27 12:00から始める
    # （データ自体はTARGET_START=7/27 03:00から保持している）。
    x_range = [TIMESERIES_DISPLAY_START, observations["datetime"].max()]

    for direction, mean_col, std_col, label in [
        ("traffic_up", "baseline_mean_up", "baseline_std_up", "上り"),
        ("traffic_down", "baseline_mean_down", "baseline_std_down", "下り"),
    ]:
        fig = go.Figure()
        for i, pid in enumerate(selected_points):
            color = SELECTION_COLORS[i % len(SELECTION_COLORS)]
            mark = point_labels.get(pid, pid)
            pdf = observations[observations["point_id"] == pid].sort_values("datetime")
            if len(selected_points) == 1:
                fig.add_trace(go.Scatter(
                    x=pdf["datetime"], y=pdf[mean_col] + pdf[std_col],
                    mode="lines", line=dict(width=0), showlegend=False,
                ))
                fig.add_trace(go.Scatter(
                    x=pdf["datetime"], y=pdf[mean_col] - pdf[std_col],
                    mode="lines", line=dict(width=0), fill="tonexty",
                    fillcolor="rgba(100,100,100,0.2)", name="平常時±σ",
                ))
            fig.add_trace(go.Scatter(
                x=pdf["datetime"], y=pdf[mean_col],
                mode="lines", line=dict(color=color, dash="dot", width=1),
                opacity=0.6, name=f"{mark} 平常時",
            ))
            fig.add_trace(go.Scatter(
                x=pdf["datetime"], y=pdf[direction],
                mode="lines+markers",
                line=dict(color=color, width=1.2),
                marker=dict(size=3),
                name=f"{mark} 実績",
            ))
        # 直轄国道の通行止め期間。この規制は県ポータルのデータに含まれず地図に
        # 線として出ないため、交通量が0になっている理由が図から読めなくなる。
        # 該当観測点を選んだときだけ、期間を帯で示す。
        for band in mlit_bands:
            fig.add_vrect(
                x0=band["start"], x1=band["end"],
                fillcolor="rgba(230,0,0,0.10)", line_width=0, layer="below",
            )
            fig.add_annotation(
                x=band["start"], y=0.97, yref="paper", xanchor="left", yanchor="top",
                text=band["label"], showarrow=False, align="left",
                font=dict(size=9, color="#b00000"),
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
        fig.update_layout(
            height=400,
            # 凡例はグラフ下に置く。上部だとplotlyのモードバー（カメラ・ズーム等の
            # アイコン）と重なり、幅の狭いモバイルでは折り返して読めなくなるため。
            margin=dict(l=10, r=10, t=30, b=70),
            legend=dict(
                orientation="h", yanchor="top", y=-0.28,
                xanchor="left", x=0, font=dict(size=10),
            ),
            xaxis=dict(tickformat="%m/%d\n%H:%M", range=x_range),
        )
        st.markdown(f"**{label}交通量（{unit_label}）**")
        st.plotly_chart(fig, use_container_width=True)

    st.markdown(describe_baseline(baseline_windows))
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
        "</style>",
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="dash-title">熊本地震（2026-07-28）交通行動変容ダッシュボード</div>'
        '<div class="dash-lead">'
        '<b><a href="https://www.jartic-open-traffic.org/" target="_blank">'
        'JARTIC 交通量オープンデータ</a></b> の常設トラカン交通量（5分間値・1時間値）を主データに、'
        '平常時と比べて交通量がどれだけ外れたかを観測点ごとに可視化しています。'
        '地震情報と通行規制を重ね合わせ、変化の背景を追えるようにしています。'
        "</div>",
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
        st.session_state["selected_points"] = (
            [point_summary["point_id"].iloc[0]] if not point_summary.empty else []
        )

    INTENSITY_LABELS = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5弱", 6: "5強", 7: "6弱", 8: "6強", 9: "7"}
    n_events = len(quake_info.get("events", []))
    min_intensity_label = INTENSITY_LABELS.get(quake_info.get("events_min_intensity"), "?")
    period_start = quake_info.get("events_period_start", "?")
    period_end = quake_info.get("events_period_end", "?")

    # 交通量データが主題なので、地震の諸元だけでなくデータの規模も並べる。
    # 震源の深さ・発生時刻・震源地は下の地震一覧に入っているので出さない。
    raw_5min = load_traffic_archive("traffic_raw.parquet")
    info_cols = st.columns(5)
    info_cols[0].metric("マグニチュード", f"M{mainshock['magnitude']}")
    info_cols[1].metric("最大震度", mainshock["max_intensity"] or "?")
    info_cols[2].metric("常時観測点", f"{len(point_summary)} 点")
    info_cols[3].metric(
        "取得済み5分間値",
        f"{len(raw_5min):,} 件" if not raw_5min.empty else "—",
    )
    info_cols[4].metric("検知した異常", f"{int(observations['is_anomaly'].sum())} 件")

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

            col_select, col_clear = st.columns([5, 1])
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
                view_names = list(TIMESERIES_VIEWS.keys())
                saved_view = st.session_state.get("_timeseries_view_choice", view_names[0])
                view = st.radio(
                    "時系列の粒度と平常時の取り方",
                    view_names,
                    index=view_names.index(saved_view) if saved_view in view_names else 0,
                    horizontal=True,
                    key="timeseries_view",
                )
                st.session_state["_timeseries_view_choice"] = view
                cfg = TIMESERIES_VIEWS[view]

            with col_map:
                st.subheader("観測点別の異常度 × 通行規制")
                n_post = sum(
                    1 for r in regulations
                    if _regulation_is_post_quake(r, quake_at)
                    and not _regulation_is_ended(r, _now_jst())
                )
                n_pre = sum(
                    1 for r in regulations
                    if not _regulation_is_post_quake(r, quake_at)
                    and not _regulation_is_ended(r, _now_jst())
                )
                # 観測点の凡例。異常度が計算できたものと、地震後が欠測で
                # 計算できなかったものの両方を書く（片方だけだと、寒色の丸が
                # 何を表しているのか凡例から分からなくなる）。
                n_points = len(point_summary)
                n_no_data = int(point_summary["max_abs_z"].isna().sum())
                point_legend = (
                    '<div style="width:100%; margin-top:2px;">'
                    f'<b>観測点（{n_points}点）</b></div>'
                    '<div><span style="display:inline-block;width:34px;height:12px;border-radius:6px;'
                    'background:linear-gradient(to right,#deebf7,#08306b);'
                    'border:1px solid #aaa;vertical-align:middle;"></span>'
                    f' 濃く大きいほど異常度（|zスコア|）が大きい（{n_points - n_no_data}点）</div>'
                )
                if n_no_data:
                    point_legend += (
                        '<div><span style="display:inline-block;width:13px;height:13px;border-radius:50%;'
                        'background:#f0f0f0;border:2px dashed #777;vertical-align:middle;"></span>'
                        f' 灰色・破線は地震後が欠測で異常度を計算できない（{n_no_data}点）</div>'
                    )
                # 直轄国道の規制。線形を復元できたものと、できずに位置だけ示すものを分けて書く
                _mlit_items = (mlit or {}).get("items", [])
                n_mlit_line = sum(1 for i in _mlit_items if i.get("path"))
                n_mlit_point = sum(
                    1 for i in _mlit_items
                    if not i.get("path") and i.get("affected_point_codes")
                )
                mlit_legend = ""
                if n_mlit_line:
                    mlit_legend += (
                        '<div style="width:100%;"><span style="display:inline-block;width:22px;'
                        'height:0;border-top:5px dashed #e60000;vertical-align:middle;"></span>'
                        f' 直轄国道の規制（{n_mlit_line}件・別レイヤ）。'
                        '区間はOSMのIC座標から復元</div>'
                    )
                if n_mlit_point:
                    mlit_legend += (
                        '<div style="width:100%;"><span style="color:#b00000;font-weight:900;">▲</span>'
                        f' 直轄国道の規制のうち区間の線形が作れないもの（{n_mlit_point}件）。'
                        '掛かっていた観測点の<b>すぐ上</b>に表示</div>'
                    )
                st.markdown(
                    f"""
                    <div style="display:flex; flex-wrap:wrap; gap:6px 14px; align-items:center; font-size:0.85rem; margin:0 0 6px 0;">
                        <div style="width:100%;"><b>今回の地震以降に始まった規制（{n_post}件）</b></div>
                        <div><span style="display:inline-block;width:22px;height:5px;background:#e60000;vertical-align:middle;"></span>
                            <b>×</b> 全面/車両通行止め</div>
                        <div><span style="display:inline-block;width:22px;height:4px;background:#e67e22;vertical-align:middle;"></span>
                            片側交互通行止めなど</div>
                        {mlit_legend}
                        <div style="width:100%; margin-top:2px;"><b>地震前からの規制（{n_pre}件・工事や過去の災害など）</b></div>
                        <div><span style="display:inline-block;width:22px;height:3px;background:#5b7c99;opacity:0.55;vertical-align:middle;"></span>
                            今回の地震とは無関係</div>
                        <div><span style="display:inline-block;width:22px;height:0;border-top:3px dashed #95a5a6;vertical-align:middle;"></span>
                            破線はいずれも解除済み</div>
                        {point_legend}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                base_map = build_base_map(
                    point_summary, mainshock, regulations, mlit, point_labels
                )
                points_fg = build_points_feature_group(
                    point_summary, point_labels, selected_points
                )
                map_state = st_folium(
                    base_map, height=750, width=550,
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
                    "地図右上のチェックボックスで種別ごとに表示を切り替えられます。"
                )
                st.caption(
                    f"**この地図に出てこない規制があります。** ①上記データは県・市町村が管理する道路が対象で、"
                    f"国が管理する直轄国道（国道57号など）の規制は含まれません（"
                    f"[{mlit.get('source_name', '国土交通省')}]({mlit.get('source_url', '')})"
                    "から転記したものを別レイヤで描き、上の「県のデータに含まれない規制があります」に一覧を出しています）。"
                    "②規制のアーカイブに記録が残っている最も古い時点は "
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
                render_timeseries(
                    load_observations(cfg["file"]),
                    selected_points, quake_at, other_event_times, point_labels,
                    quake_info.get("hourly_baseline_windows"),
                    unit_label=cfg["unit_label"],
                    extra_note=cfg["note"],
                    mlit_bands=mlit_bands,
                )

    # ------------------------------------------------------------------
    # 異常検知一覧タブ
    # ------------------------------------------------------------------
    with tab_list:
        st.subheader("異常検知結果一覧（地震発生後・1時間値ベース）")
        anomalies = observations[observations["is_anomaly"]].sort_values("datetime")
        st.write(f"検知件数: {len(anomalies)} 件")
        st.caption(
            "1時間値の実績と、平常時（同曜日8週分・祝日除く）の1時間値の平均・標準偏差を比べ、"
            "|zスコア| >= 2 を異常としています。地図の色分けもこの判定に基づきます。"
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
                "同じ観測点の1時間値。平常時（同曜日8週分）の母集団もこのデータから作っています。",
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
