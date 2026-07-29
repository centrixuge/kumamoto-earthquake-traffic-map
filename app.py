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
from datetime import datetime, timedelta, timezone

import branca.element as branca_element
import folium
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_folium import st_folium

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MAX_SELECTED_POINTS = 2
SELECTION_COLORS = ["red", "green"]

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
def load_observations() -> pd.DataFrame:
    df = pd.read_parquet(os.path.join(DATA_DIR, "observations.parquet"))
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


FULL_CLOSURE_CONTENTS = {"全面通行止め", "車両通行止め"}


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


def _regulation_is_ended(reg: dict, now: datetime) -> bool:
    """規制が発災後に開始・終了済み（既に解除済み）かどうかを判定する。"""
    if reg.get("content") == "解除":
        return True
    end_ts = reg.get("end_timestamp")
    if not end_ts:
        return False
    try:
        end_dt = datetime.strptime(end_ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return end_dt < now


def _regulation_style(reg: dict, now: datetime) -> dict:
    """規制の状態（継続中/終了済み）と内容に応じて線の色・破線・×マーカーの要否を決める。"""
    if _regulation_is_ended(reg, now):
        return {"color": "#95a5a6", "dash_array": "6,8", "show_x": False}
    if reg.get("content") in FULL_CLOSURE_CONTENTS:
        return {"color": "#e60000", "dash_array": None, "show_x": True}
    if reg.get("content") == "片側交互通行止め":
        return {"color": "#e67e22", "dash_array": None, "show_x": False}
    return {"color": "#95a5a6", "dash_array": "6,8", "show_x": False}


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
        folium.TileLayer("OpenStreetMap", name="OpenStreetMap", opacity=0.55).add_to(fmap)

        bounds = [
            [point_summary["point_lat"].min(), point_summary["point_lon"].min()],
            [point_summary["point_lat"].max(), point_summary["point_lon"].max()],
        ]
        fmap.fit_bounds(bounds, padding=(40, 40))

        if regulations:
            now = _now_jst()
            reg_layer = folium.FeatureGroup(name="通行規制情報（熊本県防災ポータル）")
            for reg in regulations:
                style = _regulation_style(reg, now)
                ended = _regulation_is_ended(reg, now)
                period = reg["start_timestamp"] or "?"
                period += f" 〜 {reg['end_timestamp']}" if ended and reg["end_timestamp"] else " 〜 (継続中)"
                status_label = "解除済み" if ended else "規制中"
                folium.PolyLine(
                    locations=reg["path"],
                    color=style["color"],
                    weight=5,
                    opacity=0.5 if ended else 0.85,
                    dash_array=style["dash_array"],
                    tooltip=(
                        f"{reg['route_name']}（{reg['region']}）｜{status_label}<br>"
                        f"{reg['content']}｜{reg['reason_type']}"
                        f"{('・' + reg['reason_detail']) if reg['reason_detail'] else ''}<br>"
                        f"{period}"
                    ),
                ).add_to(reg_layer)
                if style["show_x"]:
                    mid = reg["path"][len(reg["path"]) // 2]
                    folium.Marker(
                        location=mid,
                        icon=folium.DivIcon(html=(
                            '<div style="font-size:22px;font-weight:900;color:#e60000;'
                            'line-height:1;text-shadow:0 0 2px white,0 0 2px white;">×</div>'
                        )),
                    ).add_to(reg_layer)
            reg_layer.add_to(fmap)
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


def build_points_feature_group(
    point_summary: pd.DataFrame, point_labels: dict, selected_points=()
) -> folium.FeatureGroup:
    """
    観測点マーカーだけを含むFeatureGroupを作る。選択状態で見た目が変わるのは
    このレイヤーだけなので、st_folium の feature_group_to_add に渡すことで
    地図全体の再マウントなしに差し替えられる。
    """
    max_z = point_summary["max_abs_z"].max()
    max_z = max_z if max_z and max_z > 0 else 1.0

    fg = folium.FeatureGroup(name="観測点")
    for _, row in point_summary.iterrows():
        frac = row["max_abs_z"] / max_z
        is_selected = row["point_id"] in selected_points
        sel_idx = list(selected_points).index(row["point_id"]) if is_selected else None
        border_color = SELECTION_COLORS[sel_idx] if is_selected else "#333333"
        label = point_labels.get(row["point_id"], row["point_id"])
        folium.CircleMarker(
            location=[row["point_lat"], row["point_lon"]],
            radius=(14 + 12 * frac) if is_selected else (11 + 12 * frac),
            color=border_color,
            weight=4 if is_selected else 1,
            fill=True,
            fill_color=_severity_color(frac),
            fill_opacity=0.85,
            tooltip=(
                f"{label}（クリックで時系列に表示/解除）<br>"
                f"最大|zスコア|: {row['max_abs_z']:.2f} / 異常件数: {int(row['n_anomaly'])}"
            ),
        ).add_to(fg)
    return fg


def build_point_labels(point_summary: pd.DataFrame) -> dict:
    """
    観測点IDは "130.688167_32.56558" のような生の緯度経度文字列で読みにくいため、
    異常度の大きい順に番号を振った短い表示名を作る（セレクタ・地図・グラフ凡例で共用）。
    """
    return {
        row["point_id"]: f"地点{i + 1}"
        for i, (_, row) in enumerate(point_summary.iterrows())
    }


def _nearest_point_id(lat, lon, point_summary: pd.DataFrame, tol_deg: float = 0.01):
    if lat is None or lon is None or point_summary.empty:
        return None
    d = ((point_summary["point_lat"] - lat) ** 2 + (point_summary["point_lon"] - lon) ** 2) ** 0.5
    idx = d.idxmin()
    if d.loc[idx] > tol_deg:
        return None
    return point_summary.loc[idx, "point_id"]


def render_timeseries(
    observations: pd.DataFrame, selected_points, quake_at,
    other_event_times=(), point_labels: dict = None,
) -> None:
    if not selected_points:
        st.info("上のセレクタか、地図上のマーカーをクリックして観測点を選ぶと、ここに時系列が表示されます（最大2地点まで比較可）。")
        return

    point_labels = point_labels or {}

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
                    fillcolor="rgba(100,100,100,0.2)", name="平常時 平均±std",
                ))
            fig.add_trace(go.Scatter(
                x=pdf["datetime"], y=pdf[mean_col],
                mode="lines", line=dict(color=color, dash="dot", width=1),
                opacity=0.6, name=f"{mark} 平常時平均",
            ))
            fig.add_trace(go.Scatter(
                x=pdf["datetime"], y=pdf[direction],
                mode="lines+markers",
                line=dict(color=color, width=1.2),
                marker=dict(size=3),
                name=f"{mark} 実測",
            ))
        for t in other_event_times:
            fig.add_vline(x=t, line_dash="dot", line_color="lightgray", line_width=1, opacity=0.7)
        fig.add_vline(x=quake_at, line_dash="dot", line_color="black", line_width=2)
        fig.add_annotation(
            x=quake_at, y=1, yref="paper", yanchor="bottom",
            text="地震発生 16:27", showarrow=False,
            font=dict(size=11, color="black"),
        )
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

    st.caption(
        "黒い点線が本震の発生時刻（16:27）、薄いグレーの細い点線がその他の主要な地震"
        f"（震度5弱以上、{len(other_event_times)}件）の発生時刻。"
        "灰色の帯（1地点選択時のみ）が平常時の平均±標準偏差、点線が平常時平均、実線が実測値。"
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
        "熊本県の道路通行規制情報（熊本県防災ポータル）も合わせて表示しています。"
    )

    data_missing = not os.path.exists(os.path.join(DATA_DIR, "observations.parquet"))
    if data_missing:
        st.error(
            "data/observations.parquet が見つかりません。先に `python fetch_and_prepare.py` を実行してください。"
        )
        st.stop()

    observations = load_observations()
    quake_info = load_quake_info()
    regulations = load_regulations()
    mainshock = quake_info["mainshock"]
    quake_at = pd.Timestamp(mainshock["occurred_at"]).tz_localize(None)
    other_event_times = [
        pd.Timestamp(e["occurred_at"]).tz_localize(None)
        for e in quake_info.get("events", [])
        if e.get("eid") != mainshock.get("eid")
    ]

    post = observations[observations["is_post_quake"]]
    point_summary = build_point_summary(post)

    if "selected_points" not in st.session_state:
        st.session_state["selected_points"] = (
            [point_summary["point_id"].iloc[0]] if not point_summary.empty else []
        )

    INTENSITY_LABELS = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5弱", 6: "5強", 7: "6弱", 8: "6強", 9: "7"}
    n_events = len(quake_info.get("events", []))
    min_intensity_label = INTENSITY_LABELS.get(quake_info.get("events_min_intensity"), "?")
    period_start = quake_info.get("events_period_start", "?")
    period_end = quake_info.get("events_period_end", "?")

    info_cols = st.columns(6)
    info_cols[0].metric("マグニチュード", f"M{mainshock['magnitude']}")
    info_cols[1].metric("最大震度", mainshock["max_intensity"] or "?")
    info_cols[2].metric("震源の深さ", f"{mainshock['depth_km']:.0f} km")
    info_cols[3].metric(f"地震件数(震度{min_intensity_label}+)", f"{n_events}件")
    info_cols[4].write(f"**発生時刻**\n\n{mainshock['occurred_at']}")
    info_cols[5].write(f"**震源地**\n\n{mainshock['epicenter_name']}")
    st.caption(
        f"対象期間: 本震発生（{period_start}）〜 {period_end}"
        f"（本震から復旧期の終端、または現在時刻のいずれか早い方まで）。"
        f"データ生成時刻: {quake_info.get('generated_at', '不明')}"
    )

    MAXI_DISPLAY = {
        "1": "1", "2": "2", "3": "3", "4": "4",
        "5-": "5弱", "5+": "5強", "6-": "6弱", "6+": "6強", "7": "7",
    }
    st.markdown(f"##### 最大震度{min_intensity_label}以上を観測した地震の発生状況")
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
    st.divider()

    tab_overview, tab_list = st.tabs(
        ["地図・時系列", "異常検知一覧"]
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
                    f"観測点を選択（最大{MAX_SELECTED_POINTS}地点。こちらが確実・高速です）",
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

            if picked != selected_points:
                st.session_state["selected_points"] = picked
                st.rerun()

            col_map, col_ts = st.columns([2, 3])

            with col_map:
                st.subheader("観測点別 異常度")
                st.markdown(
                    """
                    <div style="display:flex; flex-wrap:wrap; gap:14px; align-items:center; font-size:0.85rem; margin:0 0 6px 0;">
                        <div><span style="display:inline-block;width:22px;height:4px;background:#e60000;vertical-align:middle;"></span>
                            <b>×</b> 全面/車両通行止め（現行）</div>
                        <div><span style="display:inline-block;width:22px;height:4px;background:#e67e22;vertical-align:middle;"></span>
                            片側交互通行止め（現行）</div>
                        <div><span style="display:inline-block;width:22px;height:0;border-top:3px dashed #95a5a6;vertical-align:middle;"></span>
                            発災後に開始・終了済みの規制</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                base_map = build_base_map(point_summary, mainshock, regulations)
                points_fg = build_points_feature_group(
                    point_summary, point_labels, selected_points
                )
                map_state = st_folium(
                    base_map, height=750, width=550,
                    feature_group_to_add=points_fg,
                    returned_objects=["last_object_clicked"], key="quake_map_v5",
                )
                st.caption(
                    "通行規制データ: [熊本県防災ポータル](https://portal.bousai.pref.kumamoto.jp/?p=traffic)"
                    "（[熊本市防災情報ポータル](https://city-kumamoto.my.salesforce-sites.com/)からもリンクあり）。"
                    "始点・終点座標をOSRMで道路網にスナップして表示しています。"
                )
                st.caption(
                    "観測点は色が濃いほど地震後の交通量変化（|zスコア|）が大きいことを示す（青系のグラデーション）。"
                    "地点番号は異常度の大きい順。青いマーカーは震源。"
                    "選択中の観測点は赤/緑の枠で強調表示されます（最大2地点）。"
                    "地図のマーカークリックでも選択できますが、反映に数秒かかり、"
                    "同じマーカーを続けてクリックしても反応しません（上のセレクタの利用を推奨）。"
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
                        # セレクタ側のウィジェット状態は古くなるので、キーを変えて作り直す
                        st.session_state["_sel_version"] = sel_version + 1
                        st.rerun()

            with col_ts:
                st.subheader("選択観測点の時系列（平常時帯 vs 実測）")
                render_timeseries(
                    observations, selected_points, quake_at,
                    other_event_times, point_labels,
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
