#!/usr/bin/env python
# coding: utf-8
"""
熊本地震（2026-07-28）による交通行動変容分析ダッシュボード。

`fetch_and_prepare.py` が生成した data/*.parquet, data/quake_info.json を
読み込んで表示するだけのビュー層。GDAL依存のgeopandas/shapelyはここでは使わない
（Streamlit Community Cloud等の軽量環境でも動かせるようにするため）。
"""
import itertools
import json
import os
from contextlib import contextmanager

import branca.element as branca_element
import folium
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from streamlit_folium import st_folium

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MAX_SELECTED_POINTS = 2
SELECTION_COLORS = ["red", "green"]

st.set_page_config(
    page_title="熊本地震・交通行動変容ダッシュボード",
    layout="wide",
)


@st.cache_data(ttl=300)
def load_observations() -> pd.DataFrame:
    df = pd.read_parquet(os.path.join(DATA_DIR, "observations.parquet"))
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df


@st.cache_data(ttl=300)
def load_quake_info() -> dict:
    with open(os.path.join(DATA_DIR, "quake_info.json"), encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(ttl=300)
def build_point_summary(post: pd.DataFrame) -> pd.DataFrame:
    if post.empty:
        return pd.DataFrame(columns=[
            "point_id", "point_lon", "point_lat", "max_abs_z", "n_anomaly", "distance_km",
        ])
    summary = (
        post.groupby(["point_id", "point_lon", "point_lat"])
        .apply(
            lambda g: pd.Series({
                "max_abs_z": max(g["z_up"].abs().max(), g["z_down"].abs().max()),
                "n_anomaly": int(g["is_anomaly"].sum()),
                "distance_km": g["distance_km_from_epicenter"].iloc[0],
            }),
            include_groups=False,
        )
        .reset_index()
    )
    return summary.sort_values("max_abs_z", ascending=False).reset_index(drop=True)


def _severity_color(frac: float) -> str:
    """0(平常)〜1(最大異常)のfracを薄黄色〜濃い赤の16進カラーに変換する。"""
    frac = max(0.0, min(1.0, frac))
    low = (255, 237, 160)
    high = (165, 15, 21)
    rgb = [int(low[i] + (high[i] - low[i]) * frac) for i in range(3)]
    return "#{:02x}{:02x}{:02x}".format(*rgb)


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


def render_folium_map(
    point_summary: pd.DataFrame,
    mainshock: dict,
    selected_points=(),
) -> folium.Map:
    """
    決定的idにより、選択状態が変わらない限りHTMLが完全に同一になり
    streamlit_foliumの不要な再マウントを避けられる。
    folium.Map.render()は副作用を持ち複数回呼ぶと壊れるため、Mapオブジェクト
    自体はキャッシュしない（毎回作り直す）。

    避難所（748件）は、個別表示だとクリック応答が重く、クラスタ化すると
    個々の場所が分からず意味が薄いため、地図には表示しない。
    """
    with _deterministic_branca_ids():
        center = [point_summary["point_lat"].mean(), point_summary["point_lon"].mean()]
        fmap = folium.Map(location=center, zoom_start=9, tiles="OpenStreetMap")

        max_z = point_summary["max_abs_z"].max()
        max_z = max_z if max_z and max_z > 0 else 1.0

        for _, row in point_summary.iterrows():
            frac = row["max_abs_z"] / max_z
            is_selected = row["point_id"] in selected_points
            sel_idx = list(selected_points).index(row["point_id"]) if is_selected else None
            border_color = SELECTION_COLORS[sel_idx] if is_selected else "#333333"
            folium.CircleMarker(
                location=[row["point_lat"], row["point_lon"]],
                radius=(12 + 10 * frac) if is_selected else (9 + 10 * frac),
                color=border_color,
                weight=4 if is_selected else 1,
                fill=True,
                fill_color=_severity_color(frac),
                fill_opacity=0.85,
                tooltip=(
                    f"{row['point_id']}（クリックで時系列に表示/解除）<br>"
                    f"最大|zスコア|: {row['max_abs_z']:.2f} / 異常件数: {int(row['n_anomaly'])}"
                ),
            ).add_to(fmap)

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


def _nearest_point_id(lat, lon, point_summary: pd.DataFrame, tol_deg: float = 0.01):
    if lat is None or lon is None or point_summary.empty:
        return None
    d = ((point_summary["point_lat"] - lat) ** 2 + (point_summary["point_lon"] - lon) ** 2) ** 0.5
    idx = d.idxmin()
    if d.loc[idx] > tol_deg:
        return None
    return point_summary.loc[idx, "point_id"]


def render_timeseries(observations: pd.DataFrame, selected_points, quake_at) -> None:
    if not selected_points:
        st.info("地図上のマーカーをクリックすると、その観測点の時系列がここに表示されます（最大2地点まで比較可）。")
        return

    marks = ["①", "②"]

    for direction, mean_col, std_col, label in [
        ("traffic_up", "baseline_mean_up", "baseline_std_up", "上り"),
        ("traffic_down", "baseline_mean_down", "baseline_std_down", "下り"),
    ]:
        fig = go.Figure()
        for i, pid in enumerate(selected_points):
            color = SELECTION_COLORS[i % len(SELECTION_COLORS)]
            mark = marks[i % len(marks)]
            pdf = observations[observations["point_id"] == pid].sort_values("datetime")
            if len(selected_points) == 1:
                fig.add_trace(go.Scatter(
                    x=pdf["datetime"], y=pdf[mean_col] + pdf[std_col],
                    mode="lines", line=dict(width=0), showlegend=False,
                ))
                fig.add_trace(go.Scatter(
                    x=pdf["datetime"], y=pdf[mean_col] - pdf[std_col],
                    mode="lines", line=dict(width=0), fill="tonexty",
                    fillcolor="rgba(100,100,100,0.2)", name="平常時 平均±std",
                ))
            fig.add_trace(go.Scatter(
                x=pdf["datetime"], y=pdf[mean_col],
                mode="lines", line=dict(color=color, dash="dot", width=1),
                opacity=0.6, name=f"{mark}平常時平均",
            ))
            fig.add_trace(go.Scatter(
                x=pdf["datetime"], y=pdf[direction],
                mode="lines+markers",
                line=dict(color=color, width=1.2),
                marker=dict(size=3),
                name=f"{mark}実測",
            ))
        fig.add_vline(x=quake_at, line_dash="dot", line_color="black")
        fig.update_layout(
            height=380,
            margin=dict(l=10, r=10, t=30, b=10),
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02,
                xanchor="left", x=0, font=dict(size=10),
            ),
            xaxis=dict(tickformat="%m/%d\n%H:%M"),
        )
        st.markdown(f"**{label}交通量（5分間値）**")
        st.plotly_chart(fig, use_container_width=True)

    if len(selected_points) > 1:
        st.caption(
            f"①: {selected_points[0]} / ②: {selected_points[1]}"
        )
    st.caption(
        "黒い点線が地震発生時刻（16:27）。灰色の帯（1地点選択時のみ）が平常時の平均±標準偏差、"
        "点線が平常時平均、実線が実測値。"
    )


def main():
    st.markdown(
        '<style>iframe[title="streamlit_folium.st_folium"] { width: 100% !important; }</style>',
        unsafe_allow_html=True,
    )
    st.title("熊本地震（2026-07-28）交通行動変容ダッシュボード")
    st.caption(
        "[JARTIC交通量オープンデータ](https://www.jartic-open-traffic.org/)（常設トラカン5分値）"
        "と気象庁の地震情報を重ね合わせた簡易異常検知。"
        "震度分布そのものではなく震源からの距離を揺れの強さの代理指標として用いている点に注意。"
    )

    data_missing = not os.path.exists(os.path.join(DATA_DIR, "observations.parquet"))
    if data_missing:
        st.error(
            "data/observations.parquet が見つかりません。先に `python fetch_and_prepare.py` を実行してください。"
        )
        st.stop()

    observations = load_observations()
    quake_info = load_quake_info()
    mainshock = quake_info["mainshock"]
    quake_at = pd.Timestamp(mainshock["occurred_at"]).tz_localize(None)

    post = observations[observations["is_post_quake"]]
    point_summary = build_point_summary(post)

    if "selected_points" not in st.session_state:
        st.session_state["selected_points"] = (
            [point_summary["point_id"].iloc[0]] if not point_summary.empty else []
        )

    n_events = len(quake_info.get("events", []))
    info_cols = st.columns(6)
    info_cols[0].metric("マグニチュード", f"M{mainshock['magnitude']}")
    info_cols[1].metric("最大震度", mainshock["max_intensity"])
    info_cols[2].metric("震源の深さ", f"{mainshock['depth_km']:.0f} km")
    info_cols[3].metric("対象期間内の地震(M4.0+)", f"{n_events}件")
    info_cols[4].write(f"**発生時刻**\n\n{mainshock['occurred_at']}")
    info_cols[5].write(f"**震源地**\n\n{mainshock['epicenter_name']}")
    st.caption(f"データ生成時刻: {quake_info.get('generated_at', '不明')}")
    st.divider()

    tab_overview, tab_corr, tab_list = st.tabs(
        ["地図・時系列", "震源距離との相関", "異常検知一覧"]
    )

    # ------------------------------------------------------------------
    # 地図・時系列タブ（統合ビュー）
    # ------------------------------------------------------------------
    with tab_overview:
        if post.empty:
            st.info("地震発生後のデータがまだありません。")
        else:
            selected_points = st.session_state["selected_points"]
            labels = [
                f":{SELECTION_COLORS[i]}[{pid}]" for i, pid in enumerate(selected_points)
            ]
            col_status, col_clear = st.columns([5, 1])
            with col_status:
                st.markdown(
                    f"**選択中の観測点（最大{MAX_SELECTED_POINTS}地点、地図クリックで選択/入れ替え）:** "
                    + (" / ".join(labels) if labels else "なし（地図上のマーカーをクリックしてください）")
                )
            with col_clear:
                if st.button("選択をクリア", disabled=not selected_points):
                    st.session_state["selected_points"] = []
                    st.rerun()

            col_map, col_ts = st.columns([2, 3])

            with col_map:
                st.subheader("観測点別 異常度")
                fmap = render_folium_map(point_summary, mainshock, selected_points)
                map_state = st_folium(
                    fmap, height=750, width=550,
                    returned_objects=["last_object_clicked"], key="quake_map_v3",
                )
                st.caption(
                    "色・大きさが大きいほど地震後の交通量変化（|zスコア|）が大きい観測点。青いマーカーは震源。"
                    "クリックした観測点は赤/緑の枠で強調表示されます（最大2地点）。"
                )

            clicked = map_state.get("last_object_clicked") if map_state else None
            if clicked:
                coords = (round(clicked["lat"], 6), round(clicked["lng"], 6))
                if coords != st.session_state.get("_last_click_coords"):
                    st.session_state["_last_click_coords"] = coords
                    pid = _nearest_point_id(clicked["lat"], clicked["lng"], point_summary)
                    if pid is not None:
                        selected = list(st.session_state["selected_points"])
                        if pid in selected:
                            selected.remove(pid)
                        else:
                            if len(selected) >= MAX_SELECTED_POINTS:
                                selected.pop(0)
                            selected.append(pid)
                        st.session_state["selected_points"] = selected
                        st.rerun()

            with col_ts:
                st.subheader("選択観測点の時系列（平常時帯 vs 実測）")
                render_timeseries(observations, selected_points, quake_at)

    # ------------------------------------------------------------------
    # 相関タブ
    # ------------------------------------------------------------------
    with tab_corr:
        st.subheader("震源からの距離 × 異常度")
        if post.empty:
            st.info("地震発生後のデータがまだありません。")
        else:
            corr_df = post.copy()
            corr_df["abs_z"] = corr_df[["z_up", "z_down"]].abs().max(axis=1)
            fig = px.scatter(
                corr_df,
                x="distance_km_from_epicenter",
                y="abs_z",
                color="is_anomaly",
                hover_data=["point_id", "datetime"],
                labels={
                    "distance_km_from_epicenter": "震源からの距離 (km)",
                    "abs_z": "|zスコア|（上り・下りの最大値）",
                    "is_anomaly": "異常フラグ",
                },
                height=500,
            )
            st.plotly_chart(fig, use_container_width=True)
            st.caption(
                "震度分布データの取得が難しいため、震源からの距離を揺れの強さの簡易的な代理指標として使用。"
                "震源に近い観測点ほど|zスコア|が大きい傾向があれば、地震による行動変容の空間的な広がりを示唆する。"
            )

    # ------------------------------------------------------------------
    # 異常検知一覧タブ
    # ------------------------------------------------------------------
    with tab_list:
        st.subheader("異常検知結果一覧（地震発生後）")
        anomalies = observations[observations["is_anomaly"]].sort_values("datetime")
        st.write(f"検知件数: {len(anomalies)} 件")
        display_cols = [
            "point_id", "datetime", "traffic_up", "traffic_down",
            "baseline_mean_up", "baseline_mean_down", "z_up", "z_down",
            "distance_km_from_epicenter",
        ]
        st.dataframe(anomalies[display_cols], use_container_width=True, height=500)
        st.download_button(
            "CSVダウンロード",
            anomalies[display_cols].to_csv(index=False).encode("utf-8-sig"),
            file_name="kumamoto_traffic_anomalies.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
