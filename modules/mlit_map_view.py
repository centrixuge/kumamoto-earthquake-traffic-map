"""
「通れる道マップ」のGeoJSONで作った通行規制の表示。

現在のダッシュボードは、県の公開JSON＋PDFからの手作業の転記で規制を
集めている。こちらは国土交通省が各時点で配布しているGeoJSONだけを使う
別系統で、同じ画面のタブから見比べられるようにしている。

データは scripts/build_mlit_map_regulations.py が作る
data/mlit_map_regulations.json。
"""
import json
import os

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "mlit_map_regulations.json",
)

# 道路の段階。現在の地図の観測点の描き分け（JARTICの道路種別
# 1＝高速自動車国道 / 3＝一般国道）に合わせ、観測点の無い
# 県道・市区町村道を3つ目に置く。色はこの3段階に割り当てる。
LEVELS = [
    ("高速自動車国道", "#2b6cb0", 7),
    ("一般国道", "#c05621", 6),
    ("県道・市区町村道", "#2f855a", 4),
    ("不明", "#718096", 4),
]
LEVEL_COLOR = {name: color for name, color, _ in LEVELS}
LEVEL_WEIGHT = {name: weight for name, _, weight in LEVELS}

MAP_CENTER = [32.72, 130.85]
MAP_ZOOM = 9


@st.cache_data(ttl=300)
def load_regulations() -> dict:
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def _style(item: dict) -> dict:
    """色＝道路の段階、実線／破線＝規制中／解除済み。現在の地図と同じ考え方。"""
    ended = item["状態"] == "解除済み"
    return {
        "color": LEVEL_COLOR.get(item["道路の段階"], "#718096"),
        "weight": LEVEL_WEIGHT.get(item["道路の段階"], 4),
        "opacity": 0.45 if ended else 0.95,
        "dashArray": "6,8" if ended else None,
    }


def _tooltip(item: dict) -> str:
    rows = [
        ("路線", item["路線名"]),
        ("区間", item["区間"]),
        ("市町村", item["市町村"]),
        ("規制", item["規制内容"]),
        ("理由", item["規制理由"]),
        ("開始", item["開始日時"] or "（データに無し）"),
        ("状態", item["状態"]),
    ]
    if item["状態"] == "解除済み":
        rows.append(("解除の確認", item["解除確認時点"] or "（最新時点で消失）"))
    body = "".join(
        f"<tr><td style='color:#666;padding-right:6px;white-space:nowrap;'>{k}</td>"
        f"<td>{v or '—'}</td></tr>"
        for k, v in rows
    )
    return (
        f"<b>{item['道路の段階']}</b>"
        f"<table style='font-size:11px;border-collapse:collapse;'>{body}</table>"
    )


def build_map(data: dict) -> folium.Map:
    fmap = folium.Map(location=MAP_CENTER, zoom_start=MAP_ZOOM, tiles=None)
    folium.TileLayer(
        "OpenStreetMap", name="OpenStreetMap", opacity=0.55, control=False,
    ).add_to(fmap)

    # レイヤは「道路の段階 × 状態」。規制中を上、解除済みをその下に置き、
    # 既定では規制中だけを出す（現在の地図と同じ並べ方）。
    layers = {}
    for state in ("規制中", "解除済み"):
        for level, _, _ in LEVELS:
            layers[(level, state)] = folium.FeatureGroup(
                name=f"{level}：{state}", show=(state == "規制中")
            )

    used = set()
    for item in data["items"]:
        key = (item["道路の段階"], item["状態"])
        style = _style(item)
        folium.GeoJson(
            {"type": "Feature", "geometry": item["geometry"], "properties": {}},
            style_function=lambda _f, s=style: s,
            tooltip=folium.Tooltip(_tooltip(item), sticky=True),
        ).add_to(layers[key])
        used.add(key)

    for key, group in layers.items():
        if key in used:
            group.add_to(fmap)
    folium.LayerControl(position="bottomright", collapsed=False).add_to(fmap)
    return fmap


def render(standalone: bool = False) -> None:
    """タブの中身を描く。standalone=True なら見出しも自分で出す。"""
    if not os.path.exists(DATA_PATH):
        st.info(
            "「通れる道マップ」版のデータがありません。"
            "`python scripts/build_mlit_map_regulations.py` で作成してください。"
        )
        return
    data = load_regulations()
    items = data["items"]

    if standalone:
        st.title("通行規制（「通れる道マップ」版）")
    st.caption(
        f"通行規制を[{data['source_name']}]({data['source_url']})の"
        f"配布データだけで作った版です。各時点のGeoJSON {len(data['snapshots'])}時点"
        f"（最新 {data['latest_snapshot']}）を突き合わせ、最新時点にも残っていれば"
        "「規制中」、途中で消えていれば「解除済み」としています。"
        "「地図・時系列」タブの規制（県の公開JSON＋PDFからの転記）とは"
        "別系統のデータで、収録の範囲も違います。"
    )

    df = pd.DataFrame([
        {"道路の段階": i["道路の段階"], "状態": i["状態"]} for i in items
    ])
    order = [n for n, _, _ in LEVELS]
    summary = (
        df.pivot_table(index="道路の段階", columns="状態", aggfunc="size", fill_value=0)
        .reindex(order).dropna(how="all").astype(int)
    )
    summary["合計"] = summary.sum(axis=1)

    col_map, col_side = st.columns([3, 2])
    with col_map:
        st.markdown(
            " ".join(
                f'<span style="display:inline-block;width:22px;height:4px;'
                f'background:{color};vertical-align:middle;"></span> {name}'
                for name, color, _ in LEVELS
                if name in set(df["道路の段階"])
            )
            + '　<span style="display:inline-block;width:22px;height:0;'
            'border-top:4px dashed #888;vertical-align:middle;"></span> 破線は解除済み',
            unsafe_allow_html=True,
        )
        st_folium(
            build_map(data), height=560, width=700,
            returned_objects=[], key="mlit_map_v1",
        )
    with col_side:
        st.markdown("**道路の段階ごとの件数**")
        st.dataframe(summary, use_container_width=True)
        st.caption(
            "道路の段階は元データの「道路種別」をまとめたもの。"
            "高速自動車国道＝高速道路、一般国道＝直轄国道・補助国道・一般国道、"
            "県道・市区町村道＝都道府県道・市区町村道。"
            "「不明」は道路種別の値が無いレコード。"
        )
        st.caption(
            "解除の時刻は、配布の間隔（半日〜3日）より細かくは分かりません。"
            "解除済みの行にはその規制が消えていた最初の時点を出しています。"
        )

    with st.expander(f"規制の一覧（{len(items)}件）", expanded=False):
        st.dataframe(
            pd.DataFrame([
                {
                    "道路の段階": i["道路の段階"],
                    "道路種別（元データ）": i["道路種別"],
                    "路線名": i["路線名"],
                    "区間": i["区間"],
                    "規制内容": i["規制内容"],
                    "理由": i["規制理由"],
                    "開始日時": i["開始日時"],
                    "状態": i["状態"],
                    "解除の確認時点": i["解除確認時点"],
                    "出現時点数": i["出現時点数"],
                }
                for i in items
            ]),
            use_container_width=True, height=360,
        )
        st.caption(
            "「出現時点数」は、その規制が何時点のデータに現れたか。"
            f"全{len(data['snapshots'])}時点のうちの数です。"
        )
