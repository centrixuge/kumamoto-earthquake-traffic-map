"""
モバイル空間統計（500mメッシュ・1時間の人口推計値）の読み込みと地図表示。

配布データそのものは公開できないので、公開repoにはファイルを置かない。
`scripts/build_mesh_population.py` が作った集計済みファイル
（熊本県内・500mメッシュ・1時間・総数のみ）を、次の順で探して読む。

  1. data/mss_build/ にファイルがあればそれ（手元での確認用）
  2. st.secrets["mesh_population"] の指す非公開の置き場から取得

2は次のどちらかを書く。

  [mesh_population]
  repo = "owner/name"        # 非公開のGitHubリポジトリ
  ref  = "main"
  token = "github_pat_..."   # contents:read だけの fine-grained PAT

  [mesh_population]
  base_url = "https://.../"  # オブジェクトストレージ等。末尾は / 付き
  token = "..."              # 要るときだけ（Bearerで送る）

どちらも無ければ、アプリはこのタブを「準備中」と出して落ちない。
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import folium
import pandas as pd
import requests
import streamlit as st

LOCAL_DIR = Path(__file__).resolve().parents[1] / "data" / "mss_build"
SERIES_FILE = "mesh_population.parquet"
SUMMARY_FILE = "mesh_population_summary.parquet"
META_FILE = "mesh_population_meta.json"

# 500mメッシュの大きさ（緯度1/240度・経度1/160度）
LAT_STEP = 1 / 240
LON_STEP = 1 / 160

# ツールチップに必ず入れる語。クリックされた図形がメッシュかどうかの判定と、
# メッシュコードの取り出しに使う。
MESH_TOOLTIP_KEY = "メッシュ"
_MESH_RE = re.compile(r"(\d{9})")

# 発災前後の比の色分け。人が減った側を青、増えた側を赤にする
# （避難で抜けた地域と、受け入れ先になった地域を見分けるため）。
# 区切りは15%・30%。対象の日数が少なく（平日6日 vs 5日、休日は2日ずつ）、
# 比は元々よく振れるので、10%・25%で切ると「動いた」区分にほとんどが
# 入ってしまい、地図から差が読めなかった。
RATIO_CLASSES = [
    (0.70, "#2166ac", "30%以上 減"),
    (0.85, "#8bbcda", "15〜30% 減"),
    (1.15, "#f0f0f0", "±15%"),
    (1.30, "#ef8a62", "15〜30% 増"),
    (float("inf"), "#b2182b", "30%以上 増"),
]
# 人口そのものを出すときの色分け（多いほど濃い1色系）。区切りは実測の
# 分布から（地図に出す3,834メッシュで中央値132人、上位1割が826人以上）。
POPULATION_CLASSES = [
    (100, "#ffffcc", "100人 未満"),
    (250, "#a1dab4", "100〜250人"),
    (500, "#41b6c4", "250〜500人"),
    (1000, "#2c7fb8", "500〜1,000人"),
    (float("inf"), "#253494", "1,000人 以上"),
]
NO_DATA_COLOR = "#9aa0a6"

# 地図に出す値。倍率だけだと、元から人が少ない場所の1〜2割の増減が
# 都心部と同じ色になるので、人口そのものも選べるようにする。
VALUE_MODES = {
    "増減率": "ratio",
    "人口（発災後）": "post",
    "人口（発災前）": "pre",
}
DEFAULT_VALUE_MODE = "増減率"

# 地図の色分けの単位。時間帯と日区分の2つを掛け合わせる。
# 夜間・昼間の代表時刻は3時・14時（どちらも移動の途中が混じりにくい時刻）。
# 「夜間人口」「昼間人口」とは呼ばない。あちらは常住地・従業地で数える
# 別の定義で、ここで見ているのはその時刻にそこに居た人数（滞留人口）。
HOUR_METRICS = {"全時間帯": "all", "夜間（3時）": "h3", "昼間（14時）": "h14"}
# 平日と休日では人の居場所が元から違うので、発災前の平日平均には発災後の
# 平日を、休日平均には休日を当てて比べる。平日と休日をまとめた「全日」は
# 出さない（その比は日区分の違いが混ざるだけで、読んでも意味がない）。
DAY_METRICS = {"平日": "wd", "休日（土日祝）": "hd"}
# 居住地別。そのメッシュのある市区町村に住んでいる人か、それ以外か。
# 「すべて」は総数の配信（01_total）で、居住者＋来訪者とは一致しない
# （居住地ごとに10人未満だと配信されないため、内訳の合計のほうが小さい）。
RESIDENCE_METRICS = {
    "すべて": "all",
    "居住者（当該市区町村）": "rs",
    "来訪者（それ以外）": "vi",
}
# 既定は平日の夜間。夜間はほぼ滞在人口なので、そこに人が残っているかを
# いちばん素直に読める。
DEFAULT_HOUR = "夜間（3時）"
DEFAULT_DAY = "平日"
DEFAULT_RESIDENCE = "すべて"


def metric_columns(hour_label: str, day_label: str,
                   residence_label: str = DEFAULT_RESIDENCE
                   ) -> tuple[str, str, str]:
    """区分の組み合わせに対応する列名（発災前・発災後・比）。"""
    key = (f"{RESIDENCE_METRICS[residence_label]}"
           f"_{DAY_METRICS[day_label]}_{HOUR_METRICS[hour_label]}")
    return f"pre_{key}", f"post_{key}", f"ratio_{key}"


def metric_label(hour_label: str, day_label: str,
                 residence_label: str = DEFAULT_RESIDENCE) -> str:
    text = f"{hour_label}・{day_label}"
    if RESIDENCE_METRICS[residence_label] != "all":
        text += f"・{residence_label}"
    return text


def required_summary_columns() -> list[str]:
    """いまのアプリが使う列。集計を作り直したときの取り違えを見つけるのに使う。"""
    return [
        col
        for hour in HOUR_METRICS
        for day in DAY_METRICS
        for res in RESIDENCE_METRICS
        for col in metric_columns(hour, day, res)
    ]


class MeshDataUnavailable(RuntimeError):
    """集計済みファイルが手元にも非公開の置き場にも無い。"""


# ----------------------------------------------------------------------
# 読み込み
# ----------------------------------------------------------------------
def _secrets() -> dict:
    try:
        return dict(st.secrets["mesh_population"])
    except Exception:
        return {}


def _fetch(name: str) -> bytes:
    local = LOCAL_DIR / name
    if local.exists():
        return local.read_bytes()

    cfg = _secrets()
    token = _clean_token(cfg.get("token", ""))
    if cfg.get("base_url"):
        url = str(cfg["base_url"]).strip().rstrip("/") + "/" + name
        headers = {"Authorization": f"Bearer {token}"} if token else {}
    elif cfg.get("repo"):
        if not token:
            raise MeshDataUnavailable(
                "`token` が空です。secrets の `[mesh_population]` の中に "
                "`token = \"github_pat_...\"` があるかご確認ください"
                "（キー名の綴り違いや、別のセクションに入っている場合も"
                "空になります）。"
            )
        repo = str(cfg["repo"]).strip().strip("/")
        ref = str(cfg.get("ref", "main")).strip()
        url = f"https://api.github.com/repos/{repo}/contents/{name}?ref={ref}"
        headers = {
            "Accept": "application/vnd.github.raw",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    else:
        raise MeshDataUnavailable("置き場が設定されていません。")

    res = requests.get(url, headers=headers, timeout=120)
    if res.status_code != 200:
        hint = _http_hint(res.status_code) + "\n\n" + _token_shape(token)
        # 例外をそのまま投げるとページ全体が落ちるうえ、公開環境では本文が
        # 伏せられて原因が分からない。状態コードと当たり先だけ残して返す
        # （トークンは出さない）。
        raise MeshDataUnavailable(
            f"{name} を取得できませんでした（HTTP {res.status_code}）。"
            f"取得先: {url}\n\n" + hint
        )
    return res.content


def _clean_token(value) -> str:
    """
    貼り付け事故に強くする。トークンに空白は入らないので全部落とし、
    引用符や `Bearer ` が一緒に入ってしまった場合も剥がす。

    secretsの入力欄で長い値が折り返されて改行が混ざると、見た目は正しくても
    401になる。
    """
    token = "".join(str(value).split())
    if token[:6].lower() == "bearer":
        token = token[6:]
    return token.strip("\"'")


def _token_shape(token: str) -> str:
    """トークンの中身は出さず、形だけ出す（欠けや取り違えの切り分け用）。"""
    if not token:
        return "設定されたトークン: **空**"
    kinds = {
        "github_pat_": "fine-grained PAT",
        "ghp_": "classic PAT",
        "gho_": "OAuthトークン",
        "ghs_": "GitHub Appのトークン",
    }
    kind = next(
        (v for k, v in kinds.items() if token.startswith(k)), "**未知の形式**"
    )
    return (
        f"設定されたトークン: {kind}・{len(token)}文字"
        "（fine-grained PATは `github_pat_` で始まり90文字前後です。"
        "短ければ貼り付けが欠けています）"
    )


def _http_hint(status: int) -> str:
    if status == 404:
        return (
            "**404** はトークンにそのリポジトリの権限が無いときにも出ます"
            "（存在しない場合と区別が付きません）。fine-grained PAT の "
            "Repository access で対象リポジトリを選んでいるか、"
            "Repository permissions の **Contents: Read-only** が付いているか、"
            "`repo` と `ref`（ブランチ名）の綴りをご確認ください。"
        )
    if status in (401, 403):
        return (
            f"**{status}** はトークンが無効・期限切れ・権限不足のときに出ます。"
            "fine-grained PAT の有効期限と Contents: Read-only をご確認ください。"
        )
    return "GitHubの応答をご確認ください。"


@st.cache_data(ttl=3600, show_spinner="モバイル空間統計を読み込み中")
def load_meta() -> dict:
    return json.loads(_fetch(META_FILE).decode("utf-8"))


@st.cache_data(ttl=3600, show_spinner="メッシュの一覧を読み込み中")
def load_summary() -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(_fetch(SUMMARY_FILE)))


def summary() -> pd.DataFrame:
    """
    メッシュ一覧。列が足りなければ一度だけ取り直す。

    集計は1時間キャッシュしているので、集計を作り直して置き場に上げても、
    動いているアプリはしばらく古いものを掴んだままになる。新しい列を使う
    コードと古い集計が噛み合わないと AttributeError で落ちるので、
    足りないと分かった時点でキャッシュを捨てて取り直す。
    """
    frame = load_summary()
    missing = [c for c in required_summary_columns() if c not in frame.columns]
    if missing:
        load_summary.clear()
        frame = load_summary()
        still = [c for c in required_summary_columns() if c not in frame.columns]
        if still:
            raise MeshDataUnavailable(
                "置き場の集計ファイルが古い版です（"
                f"{', '.join(still[:4])} などの列がありません）。"
                "`python scripts/build_mesh_population.py` を回し直して、"
                "出力を置き場に上げ直してください。"
            )
    return frame


@st.cache_data(ttl=3600, show_spinner="メッシュ別の人口を読み込み中")
def load_series() -> pd.DataFrame:
    """mesh × t（開始時点からの時間数）の long テーブル。mesh で引けるよう並べる。"""
    df = pd.read_parquet(io.BytesIO(_fetch(SERIES_FILE)))
    return df.set_index("mesh").sort_index()


def available() -> bool:
    return (LOCAL_DIR / META_FILE).exists() or bool(_secrets())


# ----------------------------------------------------------------------
# メッシュの形と色
# ----------------------------------------------------------------------
def mesh_bounds(code: int | str) -> tuple[float, float, float, float]:
    """メッシュコードから南西・北東の緯度経度を出す（south, west, north, east）。"""
    s = str(code)
    lat = int(s[:2]) / 1.5 + int(s[4]) / 12 + int(s[6]) / 120
    lon = int(s[2:4]) + 100 + int(s[5]) / 8 + int(s[7]) / 80
    q = int(s[8])
    lat += ((q - 1) // 2) * LAT_STEP
    lon += ((q - 1) % 2) * LON_STEP
    return lat, lon, lat + LAT_STEP, lon + LON_STEP


def _class_color(value, classes) -> str:
    if value is None or pd.isna(value):
        return NO_DATA_COLOR
    for upper, color, _ in classes:
        if value < upper:
            return color
    return classes[-1][1]


def ratio_color(ratio: float | None) -> str:
    return _class_color(ratio, RATIO_CLASSES)


def value_classes(mode: str):
    """選んだ値の色分けの区切り（凡例と塗りで同じものを使う）。"""
    return RATIO_CLASSES if VALUE_MODES[mode] == "ratio" else POPULATION_CLASSES


def _feature(row, pre_col: str, post_col: str, ratio_col: str,
             mode: str = DEFAULT_VALUE_MODE, outline: str = "") -> dict:
    south, west, north, east = mesh_bounds(row.mesh)
    ratio = getattr(row, ratio_col)
    kind = VALUE_MODES[mode]
    value = (ratio if kind == "ratio"
             else getattr(row, post_col if kind == "post" else pre_col))
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [round(west, 6), round(south, 6)],
                [round(east, 6), round(south, 6)],
                [round(east, 6), round(north, 6)],
                [round(west, 6), round(north, 6)],
                [round(west, 6), round(south, 6)],
            ]],
        },
        "properties": {
            "mesh": f"{MESH_TOOLTIP_KEY} {row.mesh}",
            "city": row.city,
            "pre": _fmt(getattr(row, pre_col)),
            "post": _fmt(getattr(row, post_col)),
            "ratio": "—" if pd.isna(ratio) else f"{ratio:.2f} 倍",
            "color": _class_color(value, value_classes(mode)),
            "outline": outline,
        },
    }


@st.cache_data(ttl=3600, show_spinner=False)
def mesh_geojson(summary: pd.DataFrame, hour_label: str, day_label: str,
                 min_hours: int, mode: str = DEFAULT_VALUE_MODE,
                 residence_label: str = DEFAULT_RESIDENCE) -> dict:
    """地図に載せるメッシュのFeatureCollectionを作る。"""
    pre_col, post_col, ratio_col = metric_columns(
        hour_label, day_label, residence_label
    )
    sub = summary[summary["n_hours"] >= min_hours]
    return {
        "type": "FeatureCollection",
        "features": [
            _feature(row, pre_col, post_col, ratio_col, mode)
            for row in sub.itertuples(index=False)
        ],
    }


def selected_geojson(summary: pd.DataFrame, hour_label: str, day_label: str,
                     selected, colors, mode: str = DEFAULT_VALUE_MODE,
                     residence_label: str = DEFAULT_RESIDENCE) -> dict:
    """
    選択中のメッシュを太枠で描くためのFeatureCollection。

    枠だけを別のRectangleで重ねると、canvasの当たり判定は塗りの有無を見ない
    ため、その枠が下のメッシュのクリックを奪って選択を解除できなくなる。
    選択の表示も同じ形・同じツールチップの図形にして、押せば解除できるよう
    にしている。
    """
    pre_col, post_col, ratio_col = metric_columns(
        hour_label, day_label, residence_label
    )
    rows = summary.set_index("mesh")
    features = []
    for mesh, color in zip(selected, colors):
        if mesh not in rows.index:
            continue
        row = rows.loc[[mesh]].reset_index().itertuples(index=False).__next__()
        features.append(
            _feature(row, pre_col, post_col, ratio_col, mode, outline=color)
        )
    return {"type": "FeatureCollection", "features": features}


def _fmt(value) -> str:
    return "—" if pd.isna(value) else f"{value:,.0f} 人"


def add_mesh_layer(fmap: folium.Map, geojson: dict, label: str,
                   interactive: bool = True) -> None:
    folium.GeoJson(
        geojson,
        name=f"人口の変化（{label}）",
        # 観測点を選んでいる間は、メッシュがクリックもツールチップも拾わない
        interactive=interactive,
        style_function=lambda f: {
            "fillColor": f["properties"]["color"],
            "color": f["properties"]["color"],
            "weight": 0.2,
            "fillOpacity": 0.55,
        },
        highlight_function=lambda f: {"weight": 2, "color": "#111"},
        tooltip=folium.GeoJsonTooltip(
            fields=["mesh", "city", "pre", "post", "ratio"],
            aliases=["", "", "発災前の平均:", "発災後の平均:", "発災後/発災前:"],
            sticky=False,
        ) if interactive else None,
        smooth_factor=0,
    ).add_to(fmap)


def add_selection_layer(fmap: folium.Map, geojson: dict,
                        interactive: bool = True) -> None:
    """選択中のメッシュを太枠で重ねる。押せば解除できるよう、同じツールチップを持たせる。"""
    if not geojson["features"]:
        return
    folium.GeoJson(
        geojson,
        name="選択中のメッシュ",
        interactive=interactive,
        style_function=lambda f: {
            "fillColor": f["properties"]["color"],
            "color": f["properties"]["outline"],
            "weight": 3,
            "fillOpacity": 0.55,
        },
        tooltip=folium.GeoJsonTooltip(
            fields=["mesh", "city", "pre", "post", "ratio"],
            aliases=["", "", "発災前の平均:", "発災後の平均:", "発災後/発災前:"],
            sticky=False,
        ) if interactive else None,
        smooth_factor=0,
    ).add_to(fmap)


def legend_html(label: str, mode: str = DEFAULT_VALUE_MODE) -> str:
    marks = " ".join(
        f'<span style="white-space:nowrap;">'
        f'<span style="display:inline-block;width:18px;height:10px;'
        f'background:{color};border:1px solid #bbb;vertical-align:middle;">'
        f"</span> {name}</span>"
        for _, color, name in value_classes(mode)
    )
    head = ("発災後/発災前" if VALUE_MODES[mode] == "ratio"
            else f"人口（{'発災後' if VALUE_MODES[mode] == 'post' else '発災前'}）")
    return (
        '<div style="font-size:0.79rem;line-height:1.45;margin:0 0 4px 0;">'
        '<div style="display:flex;flex-wrap:wrap;gap:1px 12px;align-items:center;">'
        f'<b style="white-space:nowrap;">{head}・{label}:</b>{marks}</div>'
        "</div>"
    )


def mesh_from_tooltip(tooltip: str | None) -> int | None:
    """クリックされた図形のツールチップからメッシュコードを取り出す。"""
    if not tooltip or MESH_TOOLTIP_KEY not in tooltip:
        return None
    m = _MESH_RE.search(tooltip)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------
# 時系列
# ----------------------------------------------------------------------
def mesh_label(summary: pd.DataFrame, mesh: int) -> str:
    row = summary[summary["mesh"] == mesh]
    city = row["city"].iloc[0] if len(row) else ""
    return f"{mesh}（{city}）" if city else str(mesh)


def series_for(mesh: int, meta: dict) -> pd.DataFrame:
    """
    1メッシュの時系列。配信されない時間帯（10人未満）は欠測のまま残す。

    0で埋めると「人がいなくなった」ように見えるが、実際は10人未満なので
    そうしない。折れ線は切れて出る。
    """
    series = load_series()
    start = pd.Timestamp(meta["start"])
    hours = pd.date_range(start, periods=meta["hours"], freq="h")
    out = pd.DataFrame({"datetime": hours})
    if mesh not in series.index:
        out["population"] = pd.NA
        return out
    got = series.loc[[mesh]]
    got = pd.DataFrame({
        "datetime": start + pd.to_timedelta(got["t"].astype(int), unit="h"),
        "population": got["population"].astype(float).values,
    })
    return out.merge(got, on="datetime", how="left")


def with_baseline(frame: pd.DataFrame, quake_at: pd.Timestamp,
                  holidays: set[str]) -> pd.DataFrame:
    """
    平常時＝発災前の、同じ日区分・同じ時刻の平均。

    発災前は8日分しかないので、曜日ごとではなく平日／土／日祝の3区分で
    平均を取る（曜日ごとにすると1日しか無い区分が出るため）。
    """
    day = pd.Series("平日", index=frame.index)
    day[frame["datetime"].dt.dayofweek == 5] = "土"
    day[(frame["datetime"].dt.dayofweek == 6)
        | frame["datetime"].dt.strftime("%Y-%m-%d").isin(holidays)] = "日祝"
    frame = frame.assign(day_type=day, hour=frame["datetime"].dt.hour)

    pre = frame[frame["datetime"] < quake_at]
    base = pre.groupby(["day_type", "hour"])["population"].mean().rename("baseline")
    return frame.merge(base, on=["day_type", "hour"], how="left").sort_values(
        "datetime"
    )
