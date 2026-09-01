"""
「通れる道マップ」のGeoJSONで作った通行規制の表示部品。

現在のダッシュボードは、県の公開JSON＋PDFからの手作業の転記で規制を
集めている。こちらは国土交通省が各時点で配布しているGeoJSONだけを使う
別系統で、同じ画面の隣のタブから見比べられるようにしている。

地図・凡例・件数・一覧をここに置き、タブの組み立て（観測点の選択や
時系列との並べ方）は app.py 側で行う。

データは scripts/build_mlit_map_regulations.py が作る
data/mlit_map_regulations.json。
"""
import json
import os
from collections import Counter

import folium
import pandas as pd
import streamlit as st

from modules import datastore

# data/ からの相対パス。置き場（ローカル or S3）は datastore が決める。
DATA_FILE = "mlit_map_regulations.json"

# 道路種別（3区分にまとめたもの）。現在の地図の観測点の描き分け（JARTICの道路種別
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

# 色は規制の内容で分ける（道路種別はレイヤと線の太さで分かるので、
# 色は内容に使う）。区分は「いまその区間がどういう状態か」で並べる。
# 対面通行・片側交互は、通行止めが解除されたあとに残る規制として出てくる
# （いまの配布データには記載が無いが、来たら拾えるようにしてある）。
CONTENT_CLASSES = [
    ("全面通行止め", "#e60000",
     lambda t: "通行止" in t and "緊急車両" not in t and "対面" not in t),
    ("緊急車両のみ通行可", "#e67e22", lambda t: "緊急車両" in t),
    ("対面通行・片側交互など", "#d4a017",
     lambda t: any(k in t for k in ("対面", "片側", "車線"))),
    ("規制内容不明", "#5b6470", lambda t: not t),
]
CONTENT_COLOR = {name: color for name, color, _ in CONTENT_CLASSES}
# 「通行止め解除」とだけ書かれたレコードは、規制ではなく解除の告知で、
# 解除後に残る規制も書かれていない。色を割り当てても意味が無いので
# 地図・凡例・件数からは外す（一覧には残す）。
RELEASED = ("通行止め解除", "通行止解除")
# 解除済みは内容によらず灰色にする。「いま何が規制されているか」を先に読める
# ようにするためで、解除済みの規制内容は色ではなくツールチップと一覧で見る。
ENDED_COLOR = "#9aa3af"
# 道路種別が取れなかったレコードに付ける名前。地図では規制中として出さず、
# 解除済みの扱いにする（display_state）
UNKNOWN_LEVEL = "不明"

# 重なりの順。レイヤ一覧の並び（規制中が上、解除済みが下）とは別に、
# 描く順は「解除済みの上に規制中」で固定する。レイヤは切り替えるたびに
# 追加し直されるので、追加した順に任せるとオン・オフの操作で上下が入れ替わる。
# 数字はLeafletの既定の重なり（タイル200・overlayPane400）の間に入れて、
# 規制の線が観測点のマーカーを覆わないようにする。
PANES = {"解除済み": ("kisei-ended", 396), "規制中": ("kisei-active", 398)}


def is_release_record(item: dict) -> bool:
    """解除だけを告知しているレコードか（規制ではないので地図に出さない）。"""
    return (item.get("規制内容") or "").strip() in RELEASED


def content_class(item: dict) -> str:
    """
    規制内容の文字列を、色分けの区分に振り分ける。
    「全面通行止」「全面通行止め」「前面通行止め」のような表記ゆれがある。
    """
    text = (item.get("規制内容") or "").strip()
    for name, _, matches in CONTENT_CLASSES:
        if matches(text):
            return name
    return "規制内容不明"


def display_state(item: dict) -> str:
    """
    地図での扱い（規制中／解除済み）。

    道路種別が取れないレコードは、最新の配布に入っていても「規制中」として
    は出さない。この形のレコードは配布元の地図でも凡例に無い紫色で描かれて
    おり、いまの配布分については不備と見られるためで、何の規制か分からない
    ものを「いま規制中」として出すと誤解だけが残る。

    ただし、いちど出ていたという事実はその時点の記録なので、地図から消さずに
    「解除済み」として残す（既定では非表示のレイヤに入り、レイヤ一覧の
    「不明：解除済み」から出せる）。データの「状態」そのものは書き換えない
    （一覧・CSV・GeoJSONには最新の配布にあるかどうかをそのまま残す）。
    """
    if item["道路種別"] == UNKNOWN_LEVEL:
        return "解除済み"
    return item["状態"]


def _parse_stamp(value):
    """"YYYY-MM-DD HH:MM" を Timestamp に。空なら None。

    （このモジュールにはファイル名用の _stamp が別にあるので名前を分ける）
    """
    if not value:
        return None
    return pd.Timestamp(value)


def active_in(item: dict, start, end) -> bool:
    """
    その規制が、指定した期間に効いていたか。

    配布データには規制の開始・解除の時刻そのものが入っていないので、
    時点をまたいだ突き合わせの結果を使う。

      効いていた期間 = [初出時点, 解除確認時点)
        初出時点     … その規制が最初に配布に現れた回
        解除確認時点 … 配布から消えたのを最初に確認した回（規制中ならNone）

    したがって**配布の間隔（半日〜3日）より細かい判定はできません**。
    最初の配布（2026-07-29 12:00）より前に始まって解除された規制は、
    そもそもこのデータに入っていません。
    """
    first = _parse_stamp(item.get("初出時点"))
    released = _parse_stamp(item.get("解除確認時点"))
    if first is not None and first >= end:
        return False
    if released is not None and released <= start:
        return False
    return True


def filter_window(data: dict, start, end) -> dict:
    """指定した期間に効いていた規制だけにした data を返す。"""
    if start is None or end is None:
        return data
    items = [i for i in data["items"] if active_in(i, start, end)]
    return {**data, "items": items, "window": (start, end)}


def drawn_items(data: dict) -> list:
    """規制として色分けして出すもの。解除の告知だけのレコードは含めない。"""
    return [i for i in data["items"] if not is_release_record(i)]


# レイヤ一覧に出す短い名前。正式な呼び方は凡例に出している。
SHORT_LEVEL = {
    "高速自動車国道": "高速",
    "一般国道": "国道",
    "県道・市区町村道": "県・市町村道",
}

MAP_CENTER = [32.72, 130.85]
MAP_ZOOM = 9


@st.cache_data(ttl=300)
def load_regulations() -> dict:
    return datastore.read_json(DATA_FILE, default=None)


def _style(item: dict) -> dict:
    """
    色は「いまの状態」を先に表す。

      規制中   … 規制の内容ごとの色（赤〜黄）
      解除済み … 内容によらず灰色

    線種は色と重ねて読めるようにそろえる（実線＝規制中、破線＝解除済み）。
    太さは道路種別で、レイヤの区切りと同じ。

    解除済みまで内容で色分けすると、地図の赤の大半が「もう解除された規制」に
    なり、いま通れない場所が読み取れない。解除済みの内容はツールチップと
    下の一覧で見る。
    """
    ended = display_state(item) == "解除済み"
    return {
        "color": ENDED_COLOR if ended
                 else CONTENT_COLOR.get(content_class(item), "#5b6470"),
        "weight": LEVEL_WEIGHT.get(item["道路種別"], 4),
        "opacity": 0.55 if ended else 0.95,
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
    if is_release_record(item):
        rows.append((
            "地図での扱い",
            "解除の告知（規制ではありません）。解除後に残る規制は"
            "このデータには書かれていません",
        ))
    if item["状態"] == "解除済み":
        rows.append(("解除の確認", item["解除確認時点"] or "（最新時点で消失）"))
    elif display_state(item) == "解除済み":
        # 最新の配布には入っているが、道路種別が取れないので規制中としては
        # 出していない。地図の見た目（破線）とデータの食い違いを説明する。
        rows.append((
            "地図での扱い",
            "解除済みとして表示（道路種別が取れないため、"
            "最新の配布にあっても規制中としては出していません）",
        ))
    body = "".join(
        f"<tr><td style='color:#666;padding-right:6px;white-space:nowrap;'>{k}</td>"
        f"<td>{v or '—'}</td></tr>"
        for k, v in rows
    )
    return (
        f"<b>{item['道路種別']}</b>"
        f"<table style='font-size:11px;border-collapse:collapse;'>{body}</table>"
    )


def _draw(item: dict, style: dict, group, pane: str) -> None:
    """
    1件を地図に置く。線が入っていないレコード（1地点だけのもの）は、
    folium に任せると既定の青いピンになり、色の規則から外れる。
    同じ色の小さい丸で描いて、規則の中に収める。

    pane は重なりの順を決める（PANES 参照）。レイヤ一覧の並びとは別に、
    解除済みが規制中の上に乗らないようにするために要る。
    """
    geometry = item["geometry"]
    tooltip = folium.Tooltip(_tooltip(item), sticky=True)
    if geometry.get("type") == "Point":
        lon, lat = geometry["coordinates"][:2]
        marker = folium.CircleMarker(
            location=[lat, lon], radius=5, color=style["color"],
            weight=2, opacity=style["opacity"], fill=True,
            fill_color=style["color"], fill_opacity=style["opacity"] * 0.5,
            tooltip=tooltip,
        )
        # folium は円の描画オプションを絞り込んでいて pane を落とすので、
        # 出来上がったオプションに直接入れる（これが無いとこの1件だけ
        # 既定の重なりに乗り、解除済みでも規制中の上に出る）。
        marker.options["pane"] = pane
        marker.add_to(group)
        return
    folium.GeoJson(
        {"type": "Feature", "geometry": geometry, "properties": {}},
        style_function=lambda _f, s=style: s,
        tooltip=tooltip, pane=pane,
    ).add_to(group)


def build_map(data: dict, center=None, zoom: int = None,
              epicenter_icon=None, epicenter=None,
              point_legend: str = "", point_legend_css: str = "") -> folium.Map:
    """
    規制の地図を作る。中心・縮尺・震源の印・観測点の凡例は、並べて見比べ
    られるように「道路規制×交通量」タブと同じものを呼び出し側から
    渡せるようにしている（観測点の描き方は両方の地図で共通なので、
    その凡例もこちらに出さないと何の印か分からなくなる）。
    """
    fmap = folium.Map(
        location=center or MAP_CENTER, zoom_start=zoom or MAP_ZOOM, tiles=None,
    )
    folium.TileLayer(
        "OpenStreetMap", name="OpenStreetMap", opacity=0.55, control=False,
    ).add_to(fmap)
    # 「地図・時系列」タブと同じCSSを当てる。これが無いと、st_folium が
    # 地図divに焼き込む固定幅と実際のiframe幅がずれ、右下のレイヤ一覧が
    # 地図の外へはみ出す（レイヤ名が長いほど顕著に出る）。
    fmap.get_root().header.add_child(folium.Element(
        "<style>"
        "#map_div{width:100% !important;}"
        # 規制の線のツールチップが、下にある観測点マーカーのクリックを
        # 奪わないようにする
        ".leaflet-tooltip{pointer-events:none;width:max-content;max-width:260px;"
        "white-space:normal;font-size:11.5px;line-height:1.5;padding:5px 8px;}"
        ".leaflet-control-layers{font-size:12px;max-width:calc(100vw - 28px);}"
        ".leaflet-control-layers-overlays label,"
        ".leaflet-control-layers-overlays label>span{white-space:nowrap;}"
        ".leaflet-control-layers-overlays label>span>span"
        "{white-space:normal;overflow-wrap:anywhere;}"
        + point_legend_css +
        "</style>"
    ))

    # レイヤは「道路種別 × 状態」。規制中を上、解除済みをその下に置く。
    # 名前は短くする。長いとレイヤ一覧の幅がそれに引きずられて、
    # 地図の右下で場所を取る（本家で県・市町村道の行を詰めたのと同じ理由）。
    layers = {}
    for state in ("規制中", "解除済み"):
        for level, _, _ in LEVELS:
            # 解除済みも既定で出す。既定の交通量は発災後1週間で、その頃の
            # 規制はいまはほとんど解除済み。隠すと、交通量が落ちた場所に
            # 規制があったことが地図から読めない。
            layers[(level, state)] = folium.FeatureGroup(
                name=f"{SHORT_LEVEL.get(level, level)}：{state}", show=True,
            )

    # 重なりの順を決めるpane。ツールチップを出したいので pointer_events を
    # 有効にする（既定のFalseだと線に当たらなくなる）。
    for pane_name, z_index in PANES.values():
        folium.map.CustomPane(pane_name, z_index=z_index,
                              pointer_events=True).add_to(fmap)

    used = set()
    for item in drawn_items(data):
        state = display_state(item)
        key = (item["道路種別"], state)
        _draw(item, _style(item), layers[key], PANES[state][0])
        used.add(key)

    for key, group in layers.items():
        if key in used:
            group.add_to(fmap)
    if epicenter and epicenter_icon is not None:
        folium.Marker(
            location=epicenter, icon=epicenter_icon, tooltip="震源（本震）",
        ).add_to(fmap)
    if point_legend:
        fmap.get_root().html.add_child(folium.Element(point_legend))
    # 一覧が規制の線にかぶるので、たたんだ状態で置く（本家と同じ）
    folium.LayerControl(position="bottomright", collapsed=True).add_to(fmap)
    return fmap


def _counts(data: dict) -> pd.DataFrame:
    df = pd.DataFrame([
        {"道路種別": i["道路種別"], "状態": display_state(i)}
        for i in drawn_items(data)
    ])
    order = [n for n, _, _ in LEVELS]
    table = (
        df.pivot_table(index="道路種別", columns="状態", aggfunc="size", fill_value=0)
        .reindex(order).dropna(how="all").astype(int)
    )
    for state in ("規制中", "解除済み"):
        if state not in table.columns:
            table[state] = 0
    return table[["規制中", "解除済み"]]


def legend_html(data: dict) -> str:
    """
    地図の上に置く規制の凡例。件数は下の表に出すのでここには入れない。

    行は2つ。色＝規制の内容、太さ＝道路種別（レイヤの区切りと同じ）。
    実際に出てくる区分だけを並べる（配布データに片側交互が無いなど、
    区分は時期で変わる）。

    観測点（色の濃さ＝異常度）の行はここでは組まない。app.py 側で続きの
    行として出す（引数で受け渡すと、公開側が app.py だけ読み直して
    このモジュールを古いまま使った時に、引数の不一致でページ全体が落ちる）。
    """
    items = drawn_items(data)
    # 規制中に出てくる内容だけを色の凡例に並べる。解除済みは内容によらず
    # 灰色なので、解除済みにしか無い内容の色を出しても地図には存在しない。
    active_content = {content_class(i) for i in items
                      if display_state(i) == "規制中"}
    has_ended = any(display_state(i) == "解除済み" for i in items)
    present_level = {i["道路種別"] for i in items}

    def _row(head: str, marks: str) -> str:
        return (
            '<div style="display:flex;flex-wrap:wrap;gap:1px 12px;'
            'align-items:center;">'
            f'<b style="white-space:nowrap;">{head}:</b>{marks}</div>'
        )

    def _line(color: str, label: str, dashed: bool = False) -> str:
        # 破線は border の dashed だと、20px の見本では破線に見えない
        # （5px 幅の border は破線の1つ分が10px近くあり、1本しか入らない）。
        # 地図と同じ 6px の線・8px のすき間を、背景の繰り返しで描く。
        if dashed:
            bar = (
                f'<span style="display:inline-block;width:34px;height:5px;'
                f'background:repeating-linear-gradient(to right,'
                f'{color} 0 6px,transparent 6px 14px);'
                f'vertical-align:middle;"></span>'
            )
        else:
            bar = (
                f'<span style="display:inline-block;width:20px;height:5px;'
                f'background:{color};vertical-align:middle;"></span>'
            )
        return f'<span style="white-space:nowrap;">{bar} {label}</span>'

    active_marks = " ".join(
        _line(color, name)
        for name, color, _ in CONTENT_CLASSES if name in active_content
    )
    ended_marks = []
    if has_ended:
        ended_marks.append(_line(ENDED_COLOR, "内容によらず灰色の破線",
                                 dashed=True))
    width_marks = " ".join(
        f'<span style="white-space:nowrap;">'
        f'<span style="display:inline-block;width:20px;height:{weight}px;'
        f'background:#8a94a6;vertical-align:middle;"></span> {name}</span>'
        for name, _, weight in LEVELS if name in present_level
    )
    rows = [_row("規制中の色（規制の内容）", active_marks)]
    if ended_marks:
        rows.append(_row("解除済み", " ".join(ended_marks)))
    rows.append(_row("線の太さ（道路種別）", width_marks))
    return (
        '<div style="font-size:0.79rem;line-height:1.45;margin:0 0 4px 0;">'
        + "".join(rows)
        + "</div>"
    )


def summary_html(data: dict) -> str:
    """段階ごとの件数を、地図の上に収まる小さな表で出す。"""
    table = _counts(data)
    td = "padding:1px 8px;border-bottom:1px solid #eee;"
    body = "".join(
        f'<tr><td style="{td}">{level}</td>'
        f'<td style="{td}text-align:right;">{row["規制中"]}</td>'
        f'<td style="{td}text-align:right;color:#888;">{row["解除済み"]}</td></tr>'
        for level, row in table.iterrows()
    )
    return (
        '<table style="font-size:0.75rem;border-collapse:collapse;margin:0 0 6px 0;">'
        f'<tr><th style="{td}text-align:left;">道路種別</th>'
        f'<th style="{td}">規制中</th>'
        f'<th style="{td}color:#888;">解除済み</th></tr>{body}</table>'
    )


def content_note(data: dict) -> str:
    """
    色分けの区分ごとの件数を、データから数えて書く。

    以前はここに件数を直書きしていて、データが増えたあとも古い数字
    （104件・赤84件…）が残っていた。数え直して出す。
    """
    items = drawn_items(data)
    active = [i for i in items if display_state(i) == "規制中"]
    ended = [i for i in items if display_state(i) == "解除済み"]
    counts = Counter(content_class(i) for i in active)
    detail = "、".join(
        f"{name} {counts[name]}件"
        for name, _, _ in CONTENT_CLASSES if counts.get(name)
    ) or "なし"
    ended_detail = Counter(content_class(i) for i in ended)
    # 地図から外しているのは解除の告知だけ（道路種別が取れないものは
    # 解除済みの扱いで地図に残すので、ここでは外さない）。
    released = [i for i in data["items"] if is_release_record(i)]
    text = (
        f"色は「いまの状態」を先に読めるようにしています。**規制中の{len(active)}件**は"
        f"規制の内容ごとの色（{detail}）、**解除済みの{len(ended)}件は内容によらず灰色**の"
        "破線です（解除済みの内容は線をなぞるとツールチップに出ます。内訳は "
        + "、".join(f"{name} {ended_detail[name]}件"
                    for name, _, _ in CONTENT_CLASSES if ended_detail.get(name))
        + "）。"
    )
    if released:
        text += (
            f"このほかに「通行止め解除」とだけ書かれたレコードが{len(released)}件"
            "ありますが、地図には出していません。規制ではなく解除の告知で、"
            "同じ区間の規制じたいは解除済み（灰色）として出ているためです"
            "（下の一覧には入っています）。"
        )
    if not counts.get("対面通行・片側交互など"):
        text += (
            "なお、この配布データには対面通行や片側交互の記載がまだ無く、"
            "その色は出てきません。"
        )
    return text


def unknown_level_note(data: dict) -> str:
    """
    段階が「不明」の規制についての断り書き。

    ダウンロードできるGeoJSONに道路の線形（LineString）だけが入っていて
    属性が無いレコードで、何の規制かは元データから分からない。件数を
    黙って混ぜると「道路種別が取れなかった」ように見えるので、
    地図の下でそのことを明記する。
    """
    items = [i for i in data["items"] if i["道路種別"] == UNKNOWN_LEVEL]
    if not items:
        return ""
    stamps = sorted({i["初出時点"] for i in items})
    latest = [i for i in items if i["状態"] == "規制中"]
    bare = sum(
        1 for i in items
        if not any(i.get(k) for k in ("路線名", "区間", "規制内容", "開始日時"))
    )
    text = (
        f"**道路種別が取れないレコードが{len(items)}件あります。** "
        f"うち{bare}件は配布されているGeoJSONに道路の線形（LineString）だけが"
        "入っていて、路線名・区間・規制内容・開始日時のどれも持ちません"
        f"（{stamps[0]} 以降の配布分に現れます）。"
        "**このレコードは地図では「規制中」として出さず、解除済みの扱いで"
        "残しています**（レイヤ一覧の「不明：解除済み」で表示を切り替え、"
        "既定では非表示）。配布元の地図でも凡例に無い紫色で描かれており、"
        "いまの配布分については不備と見られるためで、そこに規制があったという"
        "記録は消さずに残す、という扱いにしています。"
    )
    if latest:
        text += (
            f"うち{len(latest)}件は最新の配布にも入っていますが、"
            "同じ扱いです。"
        )
    return text + "各件の中身は、このページ下の「規制の一覧」で確認できます。"


def regulation_table(data: dict) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "道路種別": i["道路種別"],
            "道路種別（元データ）": i["道路種別（元データ）"],
            "路線名": i["路線名"],
            "区間": i["区間"],
            "規制内容": i["規制内容"],
            "理由": i["規制理由"],
            "開始日時": i["開始日時"],
            "状態": i["状態"],
            "解除の確認時点": i["解除確認時点"],
            "出現時点数": i["出現時点数"],
        }
        for i in data["items"]
    ])


def regulation_csv(data: dict) -> bytes:
    """
    一覧をCSVにする。画面の表は幅の都合で列を削っているので、CSVには
    識別子や初出時点も入れる（線形＝geometryだけは、CSVに入れても
    扱えないので外す）。Excelでそのまま開けるよう BOM 付きにする。
    """
    df = pd.DataFrame([
        {
            "id": i["id"],
            "道路種別": i["道路種別"],
            "道路種別（元データ）": i["道路種別（元データ）"],
            "路線名": i["路線名"],
            "区間": i["区間"],
            "市町村": i["市町村"],
            "規制内容": i["規制内容"],
            "規制種別": i["規制種別"],
            "規制理由": i["規制理由"],
            "開始日時": i["開始日時"],
            "状態": i["状態"],
            "初出時点": i["初出時点"],
            "最終確認時点": i["最終確認時点"],
            "解除の確認時点": i["解除確認時点"],
            "出現時点数": i["出現時点数"],
        }
        for i in data["items"]
    ])
    return df.to_csv(index=False).encode("utf-8-sig")


def regulation_geojson(data: dict) -> bytes:
    """
    一覧をGeoJSON（FeatureCollection）にする。

    配布元のGeoJSONは時点ごとに分かれていて、属性の並びも時点で違う。
    こちらは時点をまたいで突き合わせた結果なので、1件に線形と、
    状態（規制中／解除済み）・初出／最終確認の時点まで入る。
    GISでそのまま開ける形にしておく。
    """
    # 「災害前から」は画面に出していない（この配布データは今回の災害に
    # 限ったもので該当が0件、開始日時を持たないレコードもあるため）。
    # 出す側と出さない側で中身が食い違わないよう、書き出しからも外す。
    drop = {"geometry", "災害前から"}
    # 項目名はCSVと揃える（並べて見たときに同じものだと分かるように）
    rename = {"解除確認時点": "解除の確認時点"}
    features = []
    for item in data["items"]:
        props = {
            rename.get(k, k): v for k, v in item.items() if k not in drop
        }
        features.append({
            "type": "Feature",
            "geometry": item["geometry"],
            "properties": props,
        })
    body = {
        "type": "FeatureCollection",
        # 出典と作り方をファイル自体に残す（GeoJSONの仕様外の項目だが、
        # 読み込み側は無視するだけなので害がない）
        "source_name": data["source_name"],
        "source_url": data["source_url"],
        "snapshots": data["snapshots"],
        "latest_snapshot": data["latest_snapshot"],
        "note": data["note"],
        "features": features,
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _stamp(data: dict) -> str:
    return data["latest_snapshot"].replace("-", "").replace(" ", "_").replace(":", "")


def csv_file_name(data: dict) -> str:
    return f"kumamoto_mlit_map_regulations_{_stamp(data)}.csv"


def geojson_file_name(data: dict) -> str:
    return f"kumamoto_mlit_map_regulations_{_stamp(data)}.geojson"


def source_note(data: dict) -> str:
    return (
        f"[{data['source_name']}]({data['source_url']})が配布する各時点のGeoJSON "
        f"{len(data['snapshots'])}時点を突き合わせています"
        f"（最新の配布は {data['latest_snapshot']} の回で、"
        f"そこに入っている規制情報は {data['latest_regulation_time']} 時点）。"
        "最新時点にも残っていれば「規制中」、途中で消えていれば"
        "「解除済み」としています。解除の時刻は配布の間隔（半日〜3日）より"
        "細かくは分かりません。道路種別は元データの「道路種別」をまとめたもので、"
        "高速自動車国道＝高速道路、一般国道＝直轄国道・補助国道・一般国道、"
        "県道・市区町村道＝都道府県道・市区町村道です。"
    )
