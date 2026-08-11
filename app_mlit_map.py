"""
通行規制を「通れる道マップ」のGeoJSONで作った版（別ver）。

現在のダッシュボード（app.py）は、県のJSON＋PDFからの手作業の転記で
規制を集めている。こちらは国土交通省が各時点で配布しているGeoJSONを
そのまま使い、時点をまたいで突き合わせて状態を出す。

    streamlit run app_mlit_map.py

データは scripts/build_mlit_map_regulations.py が作る
data/mlit_map_regulations.json を読む。
"""
import json
import os

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

DATA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data", "mlit_map_regulations.json",
)

# 道路の段階。現在のダッシュボードの観測点の描き分け（JARTICの道路種別
# 1＝高速自動車国道 / 3＝一般国道）に合わせ、観測点の無い県道・市区町村道
# を3つ目に置く。色はこの3段階に割り当てる。
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
def load_data() -> dict:
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def state_style(item: dict) -> dict:
    """色＝道路の段階、実線／破線＝規制中／解除済み。現在の地図と同じ考え方。"""
    ended = item["状態"] == "解除済み"
    pre_quake = item["災害前から"] is True
    return {
        "color": "#5b7c99" if pre_quake else LEVEL_COLOR[item["道路の段階"]],
        "weight": LEVEL_WEIGHT[item["道路の段階"]],
        "opacity": 0.45 if ended else 0.95,
        "dashArray": "6,8" if ended else None,
    }


def tooltip_html(item: dict) -> str:
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
    rows.append((
        "災害前から",
        {True: "はい", False: "いいえ", None: "判定不能（開始日時が無い）"}[
            item["災害前から"]
        ],
    ))
    body = "".join(
        f"<tr><td style='color:#666;padding-right:6px;white-space:nowrap;'>{k}</td>"
        f"<td>{v or '—'}</td></tr>"
        for k, v in rows
    )
    return (
        f"<b>{item['道路の段階']}</b>"
        f"<table style='font-size:11px;border-collapse:collapse;'>{body}</table>"
    )


def build_map(data: dict, show_pre_quake_only: bool) -> folium.Map:
    fmap = folium.Map(location=MAP_CENTER, zoom_start=MAP_ZOOM, tiles=None)
    folium.TileLayer(
        "OpenStreetMap", name="OpenStreetMap", opacity=0.55, control=False,
    ).add_to(fmap)

    # レイヤは「道路の段階 × 状態」。規制中を上、解除済みをその下に置き、
    # 既定では規制中だけを出す（現在のダッシュボードと同じ並べ方）。
    layers = {}
    for state in ("規制中", "解除済み"):
        for level, _, _ in LEVELS:
            key = (level, state)
            group = folium.FeatureGroup(
                name=f"{level}：{state}", show=(state == "規制中")
            )
            layers[key] = group

    used = set()
    for item in data["items"]:
        if show_pre_quake_only and item["災害前から"] is not True:
            continue
        key = (item["道路の段階"], item["状態"])
        style = state_style(item)
        folium.GeoJson(
            {"type": "Feature", "geometry": item["geometry"], "properties": {}},
            style_function=lambda _f, s=style: s,
            tooltip=folium.Tooltip(tooltip_html(item), sticky=True),
        ).add_to(layers[key])
        used.add(key)

    for key, group in layers.items():
        if key in used:
            group.add_to(fmap)
    folium.LayerControl(position="bottomright", collapsed=False).add_to(fmap)
    return fmap


def main() -> None:
    st.set_page_config(page_title="通れる道マップ版 通行規制", layout="wide")
    data = load_data()
    items = data["items"]

    st.title("通行規制（「通れる道マップ」版）")
    st.caption(
        f"[{data['source_name']}]({data['source_url']}) が配布する各時点のGeoJSON "
        f"{len(data['snapshots'])}時点を突き合わせて作成。"
        f"最新の時点は {data['latest_snapshot']}。"
        "現在のダッシュボード（県のJSON＋PDFからの転記）とは別の系統のデータです。"
    )

    df = pd.DataFrame([
        {
            "道路の段階": i["道路の段階"],
            "状態": i["状態"],
            "災害前から": {True: "災害前", False: "災害後", None: "判定不能"}[
                i["災害前から"]
            ],
        }
        for i in items
    ])
    order = [n for n, _, _ in LEVELS]
    summary = (
        df.pivot_table(index="道路の段階", columns="状態", aggfunc="size", fill_value=0)
        .reindex(order).dropna(how="all").astype(int)
    )
    summary["合計"] = summary.sum(axis=1)

    col_map, col_side = st.columns([3, 2])
    with col_side:
        st.subheader("件数")
        st.dataframe(summary, use_container_width=True)
        st.caption(
            "「規制中」は最新時点にも残っているもの、「解除済み」は途中の時点で"
            "消えたもの。解除の時刻はスナップショットの間隔（半日〜3日）より"
            "細かくは分かりません。"
        )
        st.subheader("今回の災害より前からの規制")
        pre = sum(1 for i in items if i["災害前から"] is True)
        unknown = sum(1 for i in items if i["災害前から"] is None)
        st.metric("災害前から続く規制", f"{pre}件")
        st.caption(
            f"規制開始_日時が本震（{data['quake_at']}）より前のものを数えています。"
            f"開始日時を持たないレコードが{unknown}件あり、これは判定不能として"
            "別に数えています（前だと決めつけない）。"
        )
        only_pre = st.checkbox("災害前からの規制だけを表示", value=False)

    with col_map:
        st.subheader("地図")
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
            build_map(data, only_pre), height=620, width=700,
            returned_objects=[], key="mlit_map_v1",
        )

    st.subheader("一覧")
    table = pd.DataFrame([
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
            "災害前から": {True: "災害前", False: "災害後", None: "判定不能"}[
                i["災害前から"]
            ],
            "出現時点数": i["出現時点数"],
        }
        for i in items
    ])
    st.dataframe(table, use_container_width=True, height=380)
    st.caption(data["note"])


if __name__ == "__main__":
    main()
