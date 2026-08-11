"""
「通れる道マップ」の各時点のGeoJSONを、1本の通行規制データにまとめる。

配布されているのは時点ごとのスナップショットで、1件1件に「いつ解除されたか」
は入っていない。そこで時点をまたいで同じ規制を追いかけ、

  ・最後の時点にも残っている        → 規制中
  ・途中で消えた                    → 解除済み（消えた最初の時点を解除の目安にする）

として状態を決める。開始日時は各レコードが持っているので、本震（16:27）より
前かどうかで「今回の災害前からの規制」も分ける。日時を持たないレコードが
あるため、その判定は「不明」を別に立てる（前だと決めつけない）。

    python scripts/build_mlit_map_regulations.py

出力: data/mlit_map_regulations.json
"""
import glob
import hashlib
import json
import os
import re
import zipfile
from collections import Counter
from datetime import datetime

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "mlit_r8kumamoto_map",
)
OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "mlit_map_regulations.json",
)
SOURCE_NAME = "国土交通省「通れる道マップ」"
SOURCE_URL = "https://www.mlit.go.jp/road/saigai/r8kumamoto/index.html"

# 本震。これより前に始まっていた規制は、今回の地震とは別の原因。
QUAKE_AT = datetime(2026, 7, 28, 16, 27)

# 時刻がファイル名に無い回（配布元の notice.txt の記載による）
DATE_ONLY_HOUR = {"260729": "0800"}

# 道路規制情報のGeoJSON（分割されていることがある）
REG_FILE = re.compile(r"^dourokisei\d*\.geojson$", re.I)

# データの「道路種別」を3つの段階にまとめる。段階は現在のダッシュボードの
# 観測点の描き分け（JARTICの道路種別 1＝高速自動車国道 / 3＝一般国道）に
# 合わせ、観測点の無い県道・市区町村道を3つ目に置く。
# 「一般国道」の中で直轄と補助が分かれるが、JARTIC側は区別しないので束ねる。
ROAD_LEVELS = [
    ("高速自動車国道", {"高速道路"}),
    ("一般国道", {"直轄国道", "補助国道", "一般国道"}),
    ("県道・市区町村道", {"都道府県道", "市区町村道"}),
]
# 道路種別の欄に路線名が入っている1件がある。値から拾えるものは拾う。
ROUTE_NAME_IN_TYPE = re.compile(r"^国道\d+号")

# 開始日時の書き方が2通りある
TIME_FORMATS = ("%Y/%m/%d %H:%M", "%Y/%m/%d/%H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d")

# 「規制内容」に解除と書かれている値
RELEASED_CONTENTS = {"通行止め解除", "通行止解除"}


def snapshot_time(zip_name: str) -> datetime:
    stem = zip_name[: -len("data.zip")]
    if len(stem) == 6:
        stem += DATE_ONLY_HOUR[stem]
    return datetime.strptime(stem, "%y%m%d%H%M")


def read_features(zip_path: str) -> list:
    features = []
    with zipfile.ZipFile(zip_path) as z:
        for entry in z.namelist():
            if REG_FILE.match(os.path.basename(entry)):
                data = json.loads(z.read(entry).decode("utf-8-sig"))
                features.extend(data.get("features", []))
    return features


def road_level(props: dict) -> str:
    value = (props.get("道路種別") or "").strip()
    for level, values in ROAD_LEVELS:
        if value in values:
            return level
    if ROUTE_NAME_IN_TYPE.match(value):
        # 「国道445号」のように路線名が入っている。国道なので一般国道に置く
        return "一般国道"
    return "不明"


def parse_time(text) -> datetime:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def identity(feature: dict) -> str:
    """
    時点をまたいで同じ規制だと見なすためのキー。

    整理ID（name）は3割ほどのレコードにしか入っていないので使えない。
    線形そのものは時点をまたいで一致する（実測で44/44）ので、
    形と、路線名・開始日時・始点を合わせたものを識別子にする。
    """
    props = feature.get("properties", {})
    coords = json.dumps(
        (feature.get("geometry") or {}).get("coordinates"), sort_keys=True
    )
    parts = [
        hashlib.md5(coords.encode()).hexdigest()[:10],
        props.get("路線名") or "",
        props.get("規制開始_日時") or "",
        props.get("始点住所") or props.get("始点") or "",
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


def build() -> dict:
    zips = sorted(
        (p for p in glob.glob(os.path.join(DATA_DIR, "*.zip"))),
        key=lambda p: snapshot_time(os.path.basename(p)),
    )
    snapshots = []
    for path in zips:
        features = read_features(path)
        if features:
            snapshots.append((snapshot_time(os.path.basename(path)), path, features))
    if not snapshots:
        raise RuntimeError("道路規制情報を含むZIPが見つからない")

    last_time = snapshots[-1][0]
    items = {}
    for stamp, path, features in snapshots:
        for feature in features:
            key = identity(feature)
            props = {
                k: v for k, v in feature.get("properties", {}).items()
                if not k.startswith("_")
            }
            item = items.get(key)
            if item is None:
                item = items[key] = {
                    "id": key,
                    "first_seen": stamp,
                    "properties": props,
                    "geometry": feature.get("geometry"),
                    "seen": [],
                }
            item["seen"].append(stamp)
            # 属性は最後に見た時点のものを採る（後の時点ほど項目が増えるため）
            item["properties"] = props
            item["geometry"] = feature.get("geometry")

    results = []
    for item in items.values():
        seen = item["seen"]
        props = item["properties"]
        started = parse_time(props.get("規制開始_日時"))
        content = (props.get("規制内容") or props.get("規制開始_内容") or "").strip()
        active = seen[-1] == last_time and content not in RELEASED_CONTENTS
        # 消えた最初の時点＝解除が確認できた時点。スナップショットの間隔
        # （半日〜3日）より細かくは分からない。
        released_by = None
        if not active:
            later = [s for s, _, _ in snapshots if s > seen[-1]]
            released_by = later[0] if later else None
        if started is None:
            before_quake = None
        else:
            before_quake = started < QUAKE_AT
        results.append({
            "id": item["id"],
            "道路の段階": road_level(props),
            "道路種別": props.get("道路種別"),
            "路線名": props.get("路線名"),
            "区間": " 〜 ".join(x for x in [
                props.get("始点住所") or props.get("始点"),
                props.get("終点住所") or props.get("終点"),
            ] if x) or None,
            "市町村": " ".join(x for x in [
                props.get("県名"), props.get("市町村名")
            ] if x) or None,
            "規制内容": content or None,
            "規制種別": props.get("規制種別"),
            "規制理由": props.get("規制理由"),
            "開始日時": started.strftime("%Y-%m-%d %H:%M") if started else None,
            "状態": "規制中" if active else "解除済み",
            "災害前から": before_quake,
            "初出時点": item["first_seen"].strftime("%Y-%m-%d %H:%M"),
            "最終確認時点": seen[-1].strftime("%Y-%m-%d %H:%M"),
            "解除確認時点": released_by.strftime("%Y-%m-%d %H:%M") if released_by else None,
            "出現時点数": len(seen),
            "geometry": item["geometry"],
        })

    results.sort(key=lambda r: (
        [n for n, _ in ROAD_LEVELS + [("不明", set())]].index(r["道路の段階"]),
        r["状態"] != "規制中",
        r["開始日時"] or "",
    ))
    return {
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "quake_at": QUAKE_AT.strftime("%Y-%m-%d %H:%M"),
        "snapshots": [s.strftime("%Y-%m-%d %H:%M") for s, _, _ in snapshots],
        "latest_snapshot": last_time.strftime("%Y-%m-%d %H:%M"),
        "note": (
            "各時点のスナップショットを突き合わせて状態を決めている。"
            "「規制中」は最新時点にも残っているもの、「解除済み」は途中で"
            "消えたもの（解除の時刻はスナップショットの間隔ぶん粗い）。"
            "「災害前から」は規制開始_日時が本震より前かどうかで、"
            "開始日時を持たないレコードは null（不明）にしている。"
        ),
        "items": results,
    }


def main() -> None:
    data = build()
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    items = data["items"]
    print(f'スナップショット {len(data["snapshots"])}時点'
          f'（最新 {data["latest_snapshot"]}）')
    print(f"通算の規制 {len(items)}件")
    for level, _ in ROAD_LEVELS + [("不明", set())]:
        sub = [i for i in items if i["道路の段階"] == level]
        if not sub:
            continue
        active = sum(1 for i in sub if i["状態"] == "規制中")
        pre = sum(1 for i in sub if i["災害前から"] is True)
        unknown = sum(1 for i in sub if i["災害前から"] is None)
        print(f"  {level:<9} {len(sub):3d}件  規制中 {active:3d} / 解除済み"
              f" {len(sub) - active:3d}  災害前 {pre:3d} / 判定不能 {unknown:3d}")
    print("規制内容:", Counter(i["規制内容"] for i in items).most_common(6))
    print("->", OUT_PATH)


if __name__ == "__main__":
    main()
