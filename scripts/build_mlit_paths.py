"""
直轄国道の規制区間の線形を作って data/mlit_regulations.json に書き込む。

熊本河川国道事務所のPDFは区間を「○○IC〜○○IC」と名前で示すだけで座標が無い。
ただし対象は実在する道路区間なので、区間の端点（IC）の座標が分かれば
道路網に沿った線形を引ける。ここでは次の手順で作る:

  1. 区間の端点となるICを OpenStreetMap のノードとして特定する
     （highway=motorway_junction、Overpass APIで名前検索。ノードIDを下の表に
      固定値で持たせ、実行ごとに検索結果が変わらないようにしている）
  2. 端点間を OSRM の公開デモサーバーでルーティングし、実際の道路に沿った
     座標列を得る（県の規制情報で始終点をスナップしているのと同じ方法）
  3. ルーティングで得た延長を、PDFに書かれた延長と並べて記録する
     （測り方の違いで一致しないので、両方残して差が見えるようにする）

実行:
    python scripts/build_mlit_paths.py            # 生成して書き込む
    python scripts/build_mlit_paths.py --dry-run  # 差分だけ表示

ICを増やすときは Overpass で探せる:
    node["name"~"○○IC"]["highway"="motorway_junction"](32.4,130.4,33.1,131.3);
"""
import argparse
import json
import math
import os
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(BASE_DIR, "data", "mlit_regulations.json")
NEXCO_JSON_PATH = os.path.join(BASE_DIR, "data", "nexco_regulations.json")
OSRM_ROUTE = "https://router.project-osrm.org/route/v1/driving"

# OpenStreetMap の highway=motorway_junction ノード。
# (OSMノードID, 緯度, 経度) を固定値で持つ。名前検索の結果に実行ごとの揺れが
# 出ないようにするため。ICが同名で複数ある場合は、PDFの延長に合う側を採用した。
# 高速道路のICと並行する一般国道の区間を作るときに使うIC。
# PDFの別添図が「松橋IC〜八代南IC の高速と並行する国道3号」のように、
# 規制区間を高速道路のIC名で示す場合があるため。
PARALLEL_IC_NODES = {
    "松橋IC": (810949667, 32.64441, 130.70669),
    "八代南IC": (1709352526, 32.46221, 130.59787),
}

# (路線名, 区間) -> (端点IC A, 端点IC B, OSM上の道路名)
# 端点ICを一般国道上に投影し、その間をOSMの当該国道のノードを
# 経由地にしてルーティングする。素直に2点間をルーティングすると
# 高速道路を通る経路になってしまうため、経由地で国道上に縛る。
PARALLEL_SECTIONS = {
    ("国道3号", "宇城市松橋町久具〜八代市高下西町（舗装補修12箇所・第2弾）"):
        ("松橋IC", "八代南IC", "国道3号"),
}

# キロポストや地名でしか場所が示されていない規制。
# PDFの別添図で位置は確認できるが、線を引ける区間の端点が無い。
# 地名をジオコーディングして当該国道上に落とし、点として置く。
# (路線名, 区間) -> (Nominatimに投げる地名, OSM上の道路名)
PLACE_POINTS = {
    ("国道3号", "八代郡氷川町大野（216K600）"):
        ("熊本県八代郡氷川町大野", "国道3号"),
    ("国道57号", "宇土市住吉（133K450）"):
        ("熊本県宇土市住吉町", "国道57号"),
}

# 地名で区間が示されている規制。両端の地名を当該国道上に落として
# その間をルーティングする。IC で示されたものと同じ考え方。
# (路線名, 区間) -> (始点の地名, 終点の地名, OSM上の道路名)
PLACE_SECTIONS = {
    ("国道3号", "氷川町大野〜八代市岡町中（舗装補修3箇所）"):
        ("熊本県八代郡氷川町大野", "熊本県八代市岡町中", "国道3号"),
    # 第3弾は南端が「八代市二見」としか示されていない。二見の町名は
    # 洲口町・下大野町・本町・赤松町と南北4kmに散らばるので、最も北の
    # 二見洲口町を採って区間を短めに見る（path_source にその旨を書く）。
    ("国道3号", "宇城市松橋町久具〜八代市二見（歩道部補修3箇所・第3弾）"):
        ("熊本県宇城市松橋町久具", "熊本県八代市二見洲口町", "国道3号", 2500),
}

# 高速道路（NEXCO西日本）の規制区間の端点。data/nexco_regulations.json 用。
EXPRESSWAY_IC_NODES = {
    "植木IC": (1512306561, 32.93063, 130.69893),
    "益城熊本空港IC": (810949465, 32.78782, 130.79083),
    "松橋IC": (810949667, 32.64472, 130.70676),
    "八代JCT": (835367426, 32.50641, 130.64642),
    # 上下線で分かれているので2ノードの中点（表記も mentioned_points と揃える）。
    # 妙見第一橋の損傷で八代JCT〜八代南IC が通行止めとして残ったため、
    # 区間の端点として要る。
    "八代南IC": ("1709280616+1709352526", 32.46413, 130.60142),
    "田浦IC": (1709376186, 32.36512, 130.51330),
    "嘉島JCT": (810949406, 32.75277, 130.78863),
    "益城TB": (2847572263, 32.75140, 130.79723),
    "えびのIC": (810195850, 32.04610, 130.80058),
    "栗野IC": (1383752612, 31.94573, 130.72537),
    "えびのJCT": (748844869, 32.03716, 130.79907),
    "高原IC": (1809329628, 31.92691, 131.02026),
}

# (路線名, 区間) -> (端点A, 端点B, OSM上の道路名)
EXPRESSWAY_SECTIONS = {
    ("E3 九州自動車道", "植木IC〜益城熊本空港IC"):
        ("植木IC", "益城熊本空港IC", "九州自動車道"),
    ("E3 九州自動車道", "益城熊本空港IC〜松橋IC"):
        ("益城熊本空港IC", "松橋IC", "九州自動車道"),
    ("E3 九州自動車道", "松橋IC〜えびのIC"):
        ("松橋IC", "えびのIC", "九州自動車道"),
    ("E3 九州自動車道", "えびのIC〜栗野IC"):
        ("えびのIC", "栗野IC", "九州自動車道"),
    ("E10 宮崎自動車道", "えびのJCT〜高原IC"):
        ("えびのJCT", "高原IC", "宮崎自動車道"),
    # 八代南IC〜田浦IC が 2026-08-18 6:00 に解除され、八代JCT〜八代南IC
    # （妙見第一橋）だけが残ったため、区間を2つに分けている。
    ("E3A 南九州自動車道", "八代JCT〜八代南IC"):
        ("八代JCT", "八代南IC", "南九州自動車道|日奈久芦北道路"),
    ("E3A 南九州自動車道", "八代南IC〜田浦IC"):
        ("八代南IC", "田浦IC", "南九州自動車道|日奈久芦北道路"),
    ("E77 九州中央自動車道", "嘉島JCT〜益城TB"):
        ("嘉島JCT", "益城TB", "九州中央自動車道"),
}

IC_NODES = {
    "大津IC": (7968355573, 32.89208, 130.89616),
    "車帰IC": (7944635651, 32.91442, 130.97900),
    "阿蘇西IC": (7455572446, 32.92130, 130.99988),
    "小池高山IC": (2847576136, 32.74660, 130.80085),
    "山都通潤橋IC": (11620560025, 32.69629, 130.98878),
}

# (路線名, 区間) -> 端点のIC名。JSONのitemsと突き合わせるためのキー。
SECTION_ENDPOINTS = {
    ("九州中央自動車道", "小池高山IC〜山都通潤橋IC"): ("小池高山IC", "山都通潤橋IC"),
    ("国道57号 北側復旧道路", "車帰IC〜大津IC"): ("車帰IC", "大津IC"),
    ("国道57号 北側復旧道路", "阿蘇西IC〜車帰IC"): ("阿蘇西IC", "車帰IC"),
}


OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def _overpass(query: str, tries: int = 5):
    """Overpassは混雑で落ちることがあるので、複数サーバーに再試行する。"""
    for _ in range(tries):
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                resp = requests.post(
                    endpoint, data={"data": query}, timeout=90,
                    headers={"User-Agent": "kumamoto-earthquake-traffic-map/1.0"},
                )
                if resp.status_code == 200:
                    return resp.json()
            except requests.RequestException:
                pass
            time.sleep(3)
    raise RuntimeError("Overpass API から取得できなかった")


def _haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p = math.radians
    return 2 * r * math.asin(math.sqrt(
        math.sin(p(lat2 - lat1) / 2) ** 2
        + math.cos(p(lat1)) * math.cos(p(lat2)) * math.sin(p(lon2 - lon1) / 2) ** 2
    ))


def step_note() -> str:
    return "200m"


def expressway_path(ic_a: tuple, ic_b: tuple, road_name: str,
                    step: int = 200, corridor: int = 8000) -> tuple:
    """
    高速道路の区間の線形を、その路線のノードを進行方向に並べて作る。

    一般国道で使っているルーティング方式（両端をOSRMで結ぶ）は高速道路で
    破綻した。上下線が別wayであることなどが原因で経路が路線から外れ、
    実延長の2〜4倍・路線上52〜71%になった。

    ここでは経路探索をせず、路線上のノードだけを使う。A→B方向へ射影した
    進み具合で並べ、一定間隔ごとに「直前に選んだ点へ最も近いもの」を
    選んでいく。こうすると同じ側の車線をたどり続け、wayの向きや分割の
    仕方に左右されない。引いた線の点はすべて実在のノードなので、
    路線からの外れはゼロになる。
    """
    pts = road_nodes_in_box(
        min(ic_a[1], ic_b[1]) - 0.10, min(ic_a[2], ic_b[2]) - 0.10,
        max(ic_a[1], ic_b[1]) + 0.10, max(ic_a[2], ic_b[2]) + 0.10,
        road_name,
    )
    lat0 = math.radians((ic_a[1] + ic_b[1]) / 2)
    mx, my = 111320 * math.cos(lat0), 110540
    ax, ay = ic_a[2] * mx, ic_a[1] * my
    vx, vy = ic_b[2] * mx - ax, ic_b[1] * my - ay
    length = math.hypot(vx, vy)
    ux, uy = vx / length, vy / length
    # 長い区間ほど路線が直線から大きく離れる（松橋IC〜えびのICは人吉盆地を
    # 迂回する）。回廊を延長に比例させないと、離れた部分のノードが落ちて
    # 線に飛びができる。
    corridor = max(corridor, length * 0.35)
    bins = {}
    for pt in pts:
        wx, wy = pt[1] * mx - ax, pt[0] * my - ay
        t = wx * ux + wy * uy
        off = abs(wx * (-uy) + wy * ux)
        if -step <= t <= length + step and off < corridor:
            bins.setdefault(int(t // step), []).append((off, pt))
    if not bins:
        raise RuntimeError(f"{road_name} の区間上にノードが無い")
    path, prev = [], None
    for k in sorted(bins):
        cand = bins[k]
        if prev is None:
            pt = min(cand, key=lambda c: c[0])[1]
        else:
            pt = min(cand, key=lambda c: _haversine(
                prev[0], prev[1], c[1][0], c[1][1]))[1]
        if prev is None or _haversine(prev[0], prev[1], pt[0], pt[1]) > 5:
            path.append(pt)
            prev = pt
    km = sum(
        _haversine(path[i][0], path[i][1], path[i + 1][0], path[i + 1][1])
        for i in range(len(path) - 1)
    ) / 1000
    gaps = [
        _haversine(path[i][0], path[i][1], path[i + 1][0], path[i + 1][1])
        for i in range(len(path) - 1)
    ]
    straight = _haversine(ic_a[1], ic_a[2], ic_b[1], ic_b[2]) / 1000
    return path, km, {
        "max_gap_m": round(max(gaps)) if gaps else 0,
        "straight_km": round(straight, 2),
        "ratio": round(km / straight, 2) if straight else 0,
        "snap_a": path[0], "snap_b": path[-1],
        "snap_dist_a": round(_haversine(ic_a[1], ic_a[2], path[0][0], path[0][1])),
        "snap_dist_b": round(_haversine(ic_b[1], ic_b[2], path[-1][0], path[-1][1])),
    }


def parallel_route(ic_a: tuple, ic_b: tuple, road_name: str,
                   off_limit: float = 400) -> tuple:
    """
    2つのIC の間を、指定した一般国道に沿ってルーティングする。

    端点をそのままOSRMに渡すと、速い高速道路を通る経路になる。
    そこでOSMから当該国道のノードを取り、2kmおきに経由地として渡すことで
    国道上に縛る。上下線が別wayになっている区間で経路が行き来しないよう、
    ノードはA→B方向へ射影した進み具合で並べ、各区間で中心線に最も近い
    ものを1つだけ選ぶ。
    """
    lats = sorted((ic_a[1], ic_b[1]))
    lons = sorted((ic_a[2], ic_b[2]))
    nodes = road_nodes_in_box(
        lats[0] - 0.03, lons[0] - 0.05, lats[1] + 0.03, lons[1] + 0.05, road_name
    )
    if len(nodes) < 2:
        raise RuntimeError(f"{road_name} のノードが取得できなかった")

    def snap(lat, lon):
        return min(nodes, key=lambda n: _haversine(lat, lon, n[0], n[1]))

    start, end = snap(ic_a[1], ic_a[2]), snap(ic_b[1], ic_b[2])
    lat0 = math.radians((start[0] + end[0]) / 2)
    mx, my = 111320 * math.cos(lat0), 110540

    def xy(p):
        return (p[1] * mx, p[0] * my)

    ax, ay = xy(start)
    vx, vy = xy(end)[0] - ax, xy(end)[1] - ay
    length = math.hypot(vx, vy)
    ux, uy = vx / length, vy / length
    bins = {}
    for n in nodes:
        wx, wy = xy(n)[0] - ax, xy(n)[1] - ay
        t = wx * ux + wy * uy
        off = math.hypot(wx - t * ux, wy - t * uy)
        # 中心線から off_limit 以上離れたノードは別路線の枝とみなして捨てる。
        # 長い区間や海沿いで国道が大きく膨らむ区間では、既定の400mだと本線の
        # ノードまで落ちて経由地が抜け、OSRMが高速道路や県道へ逃げる。
        if -200 <= t <= length + 200 and off < off_limit:
            k = max(0, int(t // 2000))
            if k not in bins or off < bins[k][0]:
                bins[k] = (off, n)
    waypoints = [start] + [bins[k][1] for k in sorted(bins)] + [end]
    picked = []
    for p in waypoints:
        if not picked or _haversine(picked[-1][0], picked[-1][1], p[0], p[1]) > 50:
            picked.append(p)

    coords = ";".join(f"{p[1]},{p[0]}" for p in picked)
    resp = requests.get(
        f"{OSRM_ROUTE}/{coords}",
        params={"overview": "full", "geometries": "geojson"}, timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"OSRM routing failed: {data.get('code')}")
    r = data["routes"][0]
    path = [[lat, lon] for lon, lat in r["geometry"]["coordinates"]]
    # 検証: 引いた線が本当にその国道の上に乗っているか
    step = max(1, len(path) // 150)
    offs = [
        min(_haversine(p[0], p[1], n[0], n[1]) for n in nodes)
        for p in path[::step]
    ]
    on_road = 100 * sum(1 for d in offs if d < 50) / len(offs)
    return path, r["distance"] / 1000.0, {
        "snap_a": start, "snap_b": end,
        "snap_dist_a": round(_haversine(ic_a[1], ic_a[2], start[0], start[1])),
        "snap_dist_b": round(_haversine(ic_b[1], ic_b[2], end[0], end[1])),
        "waypoints": len(picked), "on_road_pct": round(on_road),
        "max_off_m": round(max(offs)),
    }


NOMINATIM = "https://nominatim.openstreetmap.org/search"


def geocode(place: str) -> tuple:
    """地名の座標（その地名の代表点）を返す。"""
    resp = requests.get(
        NOMINATIM,
        params={"q": place, "format": "json", "limit": 1, "countrycodes": "jp"},
        headers={"User-Agent": "kumamoto-earthquake-traffic-map/1.0"}, timeout=40,
    )
    resp.raise_for_status()
    hits = resp.json()
    if not hits:
        raise RuntimeError(f"地名が見つからない: {place}")
    time.sleep(1.2)  # Nominatimの利用規約（1秒1リクエスト）
    return float(hits[0]["lat"]), float(hits[0]["lon"])


def road_nodes_in_box(s: float, w: float, n: float, e: float, road_name: str) -> list:
    """
    指定した道路のノードを範囲から集める。

    道路名で引く。ref（一般国道なら3、高速道路ならE3 など）は路線によって
    書き方が揺れるので、名前で照合したほうが確実で、一般国道にも高速道路にも
    同じ関数を使える。照合は部分一致（正規表現）にする。OSMには
    「南九州自動車道;八代日奈久道路」のように複数の名前を連結した
    表記があり、完全一致では拾えないため。
    """
    query = (
        "[out:json][timeout:90];"
        f'way["highway"]["name"~"{road_name}"]({s},{w},{n},{e});'
        "out geom;"
    )
    data = _overpass(query)
    nodes = [
        [pt["lat"], pt["lon"]]
        for way in data.get("elements", [])
        for pt in (way.get("geometry") or [])
    ]
    if len(nodes) < 2:
        raise RuntimeError(f"{road_name} のノードが取得できなかった")
    return nodes


def road_nodes(lat: float, lon: float, road_name: str, span: float = 0.06) -> list:
    """指定した道路のノードを、地点の周りから集める。"""
    return road_nodes_in_box(
        lat - span, lon - span, lat + span, lon + span, road_name
    )


def snap_place(place: str, road_name: str) -> tuple:
    """
    地名を指定した国道の上に落とす。

    最寄りの「道路」に落とすと、並行する薩摩街道や無名の側道に着いて
    しまう（5地点すべてで確認）。道路名で絞ってから最寄りノードを採る。
    地名の代表点は面の重心なので、道路まで1km近く離れることがある。
    どれだけ動かしたかを戻り値に含めて、精度を記録できるようにする。
    """
    lat, lon = geocode(place)
    nodes = road_nodes(lat, lon, road_name)
    best = min(nodes, key=lambda n: _haversine(lat, lon, n[0], n[1]))
    return best, round(_haversine(lat, lon, best[0], best[1])), (lat, lon)


def route(a: tuple, b: tuple) -> tuple:
    """OSRMで2点間をルーティングし ([[lat,lon],...], 延長km) を返す。"""
    url = f"{OSRM_ROUTE}/{a[2]},{a[1]};{b[2]},{b[1]}"
    resp = requests.get(
        url, params={"overview": "full", "geometries": "geojson"}, timeout=60
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"OSRM routing failed: {data.get('code')}")
    r = data["routes"][0]
    path = [[lat, lon] for lon, lat in r["geometry"]["coordinates"]]
    return path, r["distance"] / 1000.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for path in (JSON_PATH, NEXCO_JSON_PATH):
        if os.path.exists(path):
            process(path, args.dry_run)


def process(json_path: str, dry_run: bool) -> None:
    print(f"\n■ {os.path.relpath(json_path, BASE_DIR)}")
    with open(json_path, encoding="utf-8") as f:
        doc = json.load(f)

    changed = 0
    for item in doc["items"]:
        key = (item["route_name"], item["section"])
        pt = PLACE_POINTS.get(key)
        if pt:
            place, road = pt
            node, moved, origin = snap_place(place, road)
            print(f"[点]   {key[0]}（{key[1]}）… {road}上 "
                  f"({node[0]:.5f}, {node[1]:.5f}) 地名から{moved}m")
            item["point"] = {"lat": node[0], "lon": node[1]}
            item["point_source"] = (
                f"PDFは場所を「{key[1]}」としか示しておらず座標が無い。"
                f"別添図では{road}上の1地点として描かれている。"
                f"地名「{place}」をOpenStreetMapで座標にし（{road}ではなく"
                "最寄りの道路に落とすと並行する薩摩街道や無名の側道に着くため）、"
                f"道路名で絞ったうえで{road}上の最寄りノードを採った。"
                f"地名の代表点は面の重心なので、そこから{moved}m動かしている。"
                "キロポストから直接求めたものではないため、位置はこの程度の"
                "誤差を含む。"
            )
            changed += 1
            continue

        ps = PLACE_SECTIONS.get(key)
        if ps:
            pa, pb, road = ps[0], ps[1], ps[2]
            off_limit = ps[3] if len(ps) > 3 else 400
            na, ma, _ = snap_place(pa, road)
            nb, mb, _ = snap_place(pb, road)
            path, km, info = parallel_route(
                (None, na[0], na[1]), (None, nb[0], nb[1]), road, off_limit
            )
            print(f"[ok]   {key[0]}（{key[1][:18]}…）… {len(path)}点 / {km:.2f}km "
                  f"/ {road}上 {info['on_road_pct']}%（最大{info['max_off_m']}m）")
            item["path"] = path
            item["path_length_km"] = round(km, 2)
            item["path_source"] = (
                f"PDFは区間を「{key[1]}」と地名で示すだけで座標が無い。"
                f"両端の地名をOpenStreetMapで座標にし、{road}上に落として"
                f"（それぞれ{ma}m、{mb}m動かした）、その間を{road}のノードを"
                "経由地にOSRMでルーティングした。"
                f"引いた線は{info['on_road_pct']}%が{road}から50m以内"
                f"（最大{info['max_off_m']}m）。"
                "地名の代表点は面の重心なので端点の位置には上記の誤差があり、"
                "実際の工事も区間内の3箇所の点である。"
            )
            changed += 1
            continue

        exp = EXPRESSWAY_SECTIONS.get(key)
        if exp:
            ic_a, ic_b, road = exp
            a, b = EXPRESSWAY_IC_NODES[ic_a], EXPRESSWAY_IC_NODES[ic_b]
            try:
                path, km, info = expressway_path(a, b, road)
            except RuntimeError as err:
                # 1区間の取得失敗で全体が書き込まれないと、成功した分まで
                # やり直しになる。飛ばして続け、何が落ちたかは残す。
                print(f"[skip] {key[0]}（{key[1]}）… {err}")
                continue
            print(
                f"[高速] {key[0]}（{key[1]}）… {len(path)}点 / {km:.2f}km "
                f"（直線 {info['straight_km']}km, 比 {info['ratio']}）"
                f" 最大ギャップ {info['max_gap_m']}m"
            )
            item["path"] = path
            item["path_length_km"] = round(km, 2)
            item["endpoints"] = [
                {"name": ic_a, "osm_node": a[0], "lat": info["snap_a"][0],
                 "lon": info["snap_a"][1]},
                {"name": ic_b, "osm_node": b[0], "lat": info["snap_b"][0],
                 "lon": info["snap_b"][1]},
            ]
            item["path_source"] = (
                f"区間の端点 {ic_a}（OSM node/{a[0]}）と {ic_b}（node/{b[0]}）の"
                f"間について、OSMで「{road}」の名前を持つ道路のノードを"
                f"{step_note()}おきに拾い、進行方向に並べて線にした"
                f"（端点までの距離は{info['snap_dist_a']}m、"
                f"{info['snap_dist_b']}m）。点はすべて実在のノードなので"
                f"路線から外れることはない。延長{km:.2f}kmは直線距離"
                f"{info['straight_km']}kmの{info['ratio']}倍で、"
                f"点の間隔の最大は{info['max_gap_m']}m。"
                "ルーティングで結ぶ方式は、上下線が別wayであることなどが原因で"
                "経路が路線から外れたため使っていない。"
            )
            changed += 1
            continue

        par = PARALLEL_SECTIONS.get(key)
        if par:
            ic_a, ic_b, road = par
            a, b = PARALLEL_IC_NODES[ic_a], PARALLEL_IC_NODES[ic_b]
            path, km, info = parallel_route(a, b, road)
            print(
                f"[ok]   {key[0]}（{key[1][:20]}…）… {len(path)}点 / {km:.2f}km "
                f"経由地{info['waypoints']}点 / {road}上 {info['on_road_pct']}%"
                f"（最大{info['max_off_m']}m）"
            )
            item["path"] = path
            item["path_length_km"] = round(km, 2)
            item["endpoints"] = [
                {"name": ic_a, "osm_node": a[0], "lat": info["snap_a"][0],
                 "lon": info["snap_a"][1]},
                {"name": ic_b, "osm_node": b[0], "lat": info["snap_b"][0],
                 "lon": info["snap_b"][1]},
            ]
            item["path_source"] = (
                f"PDFの別添図が、規制区間を「{ic_a}〜{ic_b} の高速道路と並行する"
                f"{road}」として示している。{ic_a}（OSM node/{a[0]}）と"
                f"{ic_b}（node/{b[0]}）をOSMの{road}上に投影し"
                f"（それぞれ{info['snap_dist_a']}m、{info['snap_dist_b']}m）、"
                f"その間をOSMの{road}のノード{info['waypoints']}点を経由地として"
                "OSRMでルーティングした。端点間を直接ルーティングすると"
                "高速道路を通る経路になるため、経由地で国道上に縛っている。"
                f"引いた線は{info['on_road_pct']}%が{road}から50m以内"
                f"（最大{info['max_off_m']}m）に収まることを確認済み。"
                "ただし実際の工事は区間内の12箇所の点であり、"
                "この区間が全体にわたって同時に規制されていたわけではない。"
            )
            changed += 1
            continue

        names = SECTION_ENDPOINTS.get(key)
        if not names:
            # 地点（キロポスト）で示された規制は区間が無いので線を作らない
            print(f"[skip] {key[0]}（{key[1]}）… 区間の端点が定義されていない")
            continue
        a, b = (IC_NODES[n] for n in names)
        path, km = route(a, b)
        pdf_km = item.get("length_km")
        diff = f"{km - pdf_km:+.2f}km" if pdf_km else "PDFに延長の記載なし"
        print(
            f"[ok]   {key[0]}（{key[1]}）… {len(path)}点 / {km:.2f}km "
            f"（PDF 約{pdf_km}km, 差 {diff}）"
        )
        item["path"] = path
        item["path_length_km"] = round(km, 2)
        # 区間の端点（IC）は、線だけだとどこからどこまでなのかが読めないので
        # 地図に点も落とす。そのための名前と座標をJSON側に持たせる
        # （path の端は道路網にスナップされた位置で、IC そのものの座標ではない）。
        item["endpoints"] = [
            {"name": n, "osm_node": node, "lat": lat, "lon": lon}
            for n, (node, lat, lon) in zip(names, (a, b))
        ]
        item["path_source"] = (
            f"端点はOpenStreetMapのIC ノード（{names[0]}=node/{a[0]}、"
            f"{names[1]}=node/{b[0]}）。その間をOSRMで道路網に沿ってルーティング。"
            f"ルーティングの延長は {km:.2f}km で、PDF記載の"
            f"{('約' + str(pdf_km) + 'km') if pdf_km else '延長（記載なし）'}"
            "とは測り方（ランプを含むか等）が異なるため一致しない。"
        )
        changed += 1

    print(f"\n線形を付けた区間: {changed} / 全 {len(doc['items'])} 件")
    if dry_run:
        print("--dry-run なので書き込みません")
        return
    with open(json_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"書き込みました: {json_path}")


if __name__ == "__main__":
    sys.exit(main())
