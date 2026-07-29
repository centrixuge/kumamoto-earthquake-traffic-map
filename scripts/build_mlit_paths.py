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
import os
import sys

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(BASE_DIR, "data", "mlit_regulations.json")
OSRM_ROUTE = "https://router.project-osrm.org/route/v1/driving"

# OpenStreetMap の highway=motorway_junction ノード。
# (OSMノードID, 緯度, 経度) を固定値で持つ。名前検索の結果に実行ごとの揺れが
# 出ないようにするため。ICが同名で複数ある場合は、PDFの延長に合う側を採用した。
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

    with open(JSON_PATH, encoding="utf-8") as f:
        doc = json.load(f)

    changed = 0
    for item in doc["items"]:
        key = (item["route_name"], item["section"])
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
        item["path_source"] = (
            f"端点はOpenStreetMapのIC ノード（{names[0]}=node/{a[0]}、"
            f"{names[1]}=node/{b[0]}）。その間をOSRMで道路網に沿ってルーティング。"
            f"ルーティングの延長は {km:.2f}km で、PDF記載の"
            f"{('約' + str(pdf_km) + 'km') if pdf_km else '延長（記載なし）'}"
            "とは測り方（ランプを含むか等）が異なるため一致しない。"
        )
        changed += 1

    print(f"\n線形を付けた区間: {changed} / 全 {len(doc['items'])} 件")
    if args.dry_run:
        print("--dry-run なので書き込みません")
        return
    with open(JSON_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"書き込みました: {JSON_PATH}")


if __name__ == "__main__":
    sys.exit(main())
