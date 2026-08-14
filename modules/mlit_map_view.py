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

DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "mlit_map_regulations.json",
)

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
# 道路種別が取れなかったレコードに付ける名前。地図には出さない（drawn_items）
UNKNOWN_LEVEL = "不明"


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


def drawn_items(data: dict) -> list:
    """
    地図に出す規制。次の2つは除く（一覧・CSV・GeoJSONには残す）。

    ・解除の告知だけのレコード
    ・道路種別が「不明」のレコード。線形だけで属性が一切無く、配布元の
      地図でも凡例に無い紫色で描かれているため、データの不備と見られる。
      何の規制か分からないものを地図に出すと、規制があるという誤解だけが
      残るので出さない。
    """
    return [
        i for i in data["items"]
        if not is_release_record(i) and i["道路種別"] != UNKNOWN_LEVEL
    ]


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
    if not os.path.exists(DATA_PATH):
        return None
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def _style(item: dict) -> dict:
    """
    色＝規制の内容、太さ＝道路種別、実線／破線＝規制中／解除済み。
    色の意味を「道路規制×交通量」タブと揃え、道路種別は
    レイヤと線の太さで分かるようにする。
    """
    ended = item["状態"] == "解除済み"
    return {
        "color": CONTENT_COLOR.get(content_class(item), "#5b6470"),
        "weight": LEVEL_WEIGHT.get(item["道路種別"], 4),
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
        f"<b>{item['道路種別']}</b>"
        f"<table style='font-size:11px;border-collapse:collapse;'>{body}</table>"
    )


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

    # レイヤは「道路種別 × 状態」。規制中を上、解除済みをその下に置き、
    # 既定では規制中だけを出す（現在の地図と同じ並べ方）。
    # 名前は短くする。長いとレイヤ一覧の幅がそれに引きずられて、
    # 地図の右下で場所を取る（本家で県・市町村道の行を詰めたのと同じ理由）。
    layers = {}
    for state in ("規制中", "解除済み"):
        for level, _, _ in LEVELS:
            layers[(level, state)] = folium.FeatureGroup(
                name=f"{SHORT_LEVEL.get(level, level)}：{state}",
                show=(state == "規制中"),
            )

    used = set()
    for item in drawn_items(data):
        key = (item["道路種別"], item["状態"])
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
        {"道路種別": i["道路種別"], "状態": i["状態"]} for i in drawn_items(data)
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
    present_content = {content_class(i) for i in items}
    present_level = {i["道路種別"] for i in items}

    def _row(head: str, marks: str) -> str:
        return (
            '<div style="display:flex;flex-wrap:wrap;gap:1px 12px;'
            'align-items:center;">'
            f'<b style="white-space:nowrap;">{head}:</b>{marks}</div>'
        )

    color_marks = " ".join(
        f'<span style="white-space:nowrap;">'
        f'<span style="display:inline-block;width:20px;height:5px;'
        f'background:{color};vertical-align:middle;"></span> {name}</span>'
        for name, color, _ in CONTENT_CLASSES if name in present_content
    )
    width_marks = " ".join(
        f'<span style="white-space:nowrap;">'
        f'<span style="display:inline-block;width:20px;height:{weight}px;'
        f'background:#8a94a6;vertical-align:middle;"></span> {name}</span>'
        for name, _, weight in LEVELS if name in present_level
    )
    return (
        '<div style="font-size:0.79rem;line-height:1.45;margin:0 0 4px 0;">'
        + _row("規制の色", color_marks)
        + _row(
            "線の太さ（道路種別）",
            width_marks
            + '<span style="white-space:nowrap;">'
            '<span style="display:inline-block;width:20px;height:0;'
            'border-top:4px dashed #888;vertical-align:middle;"></span>'
            " 破線は解除済み</span>",
        )
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
    counts = Counter(content_class(i) for i in items)
    detail = "、".join(
        f"{name} {counts[name]}件"
        for name, _, _ in CONTENT_CLASSES if counts.get(name)
    )
    # 道路種別が「不明」のものは下の断り書きで別に数えるので、ここでは
    # それに当たらない解除の告知だけを数える（同じ件を二重に数えないため）。
    released = [
        i for i in data["items"]
        if is_release_record(i) and i["道路種別"] != UNKNOWN_LEVEL
    ]
    text = (
        f"色は規制の内容で分けています（地図に出している{len(items)}件の内訳は"
        f"{detail}）。"
    )
    if released:
        text += (
            f"このほかに「通行止め解除」とだけ書かれたレコードが{len(released)}件"
            "ありますが、規制ではなく解除の告知で、解除後に残る規制も"
            "書かれていないため地図には出していません（下の一覧には入っています）。"
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
    bare = sum(
        1 for i in items
        if not any(i.get(k) for k in ("路線名", "区間", "規制内容", "開始日時"))
    )
    return (
        f"**道路種別が取れない{len(items)}件は地図に出していません。** "
        f"うち{bare}件は配布されているGeoJSONに道路の線形（LineString）だけが"
        "入っていて、路線名・区間・規制内容・開始日時のどれも持ちません"
        f"（{stamps[0]} 以降の配布分に現れます）。"
        "配布元の地図でも凡例に無い紫色で描かれており、データの不備と"
        "見られます。何の規制か分からないものを地図に出すと、規制があると"
        "いう誤解だけが残るため、地図・凡例・件数からは外しました"
        "（このページ下の「規制の一覧」とCSV・GeoJSONには残しています）。"
    )


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
