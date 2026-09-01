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
import math
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

# ファイル名の時刻と、中の道路規制情報の時点がずれている回。
# 8/7 16:00 の回は、規制情報だけ10:00時点のものが入っている。
# 「いつ時点の規制か」はこちらを使う（ファイル名の時刻ではない）。
REGULATION_AS_OF = {
    "2608071600data.zip": datetime(2026, 8, 7, 10, 0),
}

# 道路規制情報のGeoJSON（分割されていることがある）
REG_FILE = re.compile(r"^dourokisei\d*\.geojson$", re.I)

# 元データの「道路種別」を3つにまとめ直す。区分は現在のダッシュボードの
# 観測点の描き分け（JARTICの道路種別 1＝高速自動車国道 / 3＝一般国道）に
# 合わせ、観測点の無い県道・市区町村道を3つ目に置く。まとめる前の値は
# 「道路種別（元データ）」として各件に残す。
# 「一般国道」の中で直轄と補助が分かれるが、JARTIC側は区別しないので束ねる。
ROAD_LEVELS = [
    ("高速自動車国道", {"高速道路"}),
    ("一般国道", {"直轄国道", "補助国道", "一般国道"}),
    ("県道・市区町村道", {"都道府県道", "市区町村道"}),
]
# 道路種別の欄に路線名が入っている1件がある。値から拾えるものは拾う。
ROUTE_NAME_IN_TYPE = re.compile(r"^国道\d+号")

# 開始日時の書き方が2通りある
# 配布データは「2026/8/7 10:00」の形。県の公開JSONは「2026-08-07 10:00:00」の形
TIME_FORMATS = ("%Y/%m/%d %H:%M", "%Y/%m/%d/%H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")

# 「規制内容」に解除と書かれている値
RELEASED_CONTENTS = {"通行止め解除", "通行止解除"}


def snapshot_time(zip_name: str) -> datetime:
    stem = zip_name[: -len("data.zip")]
    if len(stem) == 6:
        stem += DATE_ONLY_HOUR[stem]
    return datetime.strptime(stem, "%y%m%d%H%M")


def read_features(zip_path: str) -> list:
    return _read_regulations(zip_path)[0]


def _read_regulations(zip_path: str):
    """
    道路規制情報の中身と、その中身のハッシュを返す。

    ハッシュは「この回の規制情報は前の回と同じものか」を見るために使う。
    配布の回が新しくても、中の規制情報が前の回の使い回しであることがある
    （実測: 2026-08-31 16:00 の回に入っているのは 08-25 09:00 の回と
    同一のファイル）。その場合、規制の時点は前の回のものとして扱わないと、
    実際より新しい情報のように見えてしまう。
    """
    features, digest = [], hashlib.md5()
    with zipfile.ZipFile(zip_path) as z:
        for entry in sorted(z.namelist()):
            if REG_FILE.match(os.path.basename(entry)):
                raw = z.read(entry)
                digest.update(raw)
                features.extend(
                    json.loads(raw.decode("utf-8-sig")).get("features", []))
    return features, (digest.hexdigest() if features else None)


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


# ---- 熊本県の公開JSONとの突き合わせ -------------------------------------
# 通れる道マップの配布は、県道・市町村道の解除が反映されないまま止まることが
# ある（実測: 2026-08-31 の配布に入っているのは 08-25 の内容で、その中には
# 県の公開JSONではとうに解除されている区間が残っている）。
# 同じ区間を県のフィードで見つけられたら、その状態も持たせておく。
PREF_FILE = os.path.join(os.path.dirname(DATA_DIR), "regulations.json")
# 路線名が一致し、端点がこの距離以内なら同じ区間とみなす
PREF_MATCH_KM = 3.0


def _norm_route(name) -> str:
    """路線名の書き方をそろえる（県道17号坂本人吉線 → 坂本人吉線）。"""
    text = re.sub(r"[\s　]", "", str(name or ""))
    text = re.sub(r"^(主要地方道|一般県道|市道|町道|村道)", "", text)
    text = re.sub(r"^県道\d+号", "", text)
    text = re.sub(r"^(国道\d+号).*", r"", text)
    return text


def _km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    return 6371 * 2 * math.asin(math.sqrt(
        math.sin((la2 - la1) / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))


def _ends(geometry) -> list:
    g = geometry or {}
    c = g.get("coordinates") or []
    if g.get("type") == "LineString" and c:
        return [(c[0][1], c[0][0]), (c[-1][1], c[-1][0])]
    if g.get("type") == "MultiLineString" and c and c[0]:
        return [(c[0][0][1], c[0][0][0]), (c[-1][-1][1], c[-1][-1][0])]
    if g.get("type") == "Point" and len(c) >= 2:
        return [(c[1], c[0])]
    return []


def _load_pref() -> list:
    """
    熊本県の公開JSON。6時間ごとの自動取得で更新されるが、その取得は main に
    しかコミットされないので、作業ブランチでは古いことがある。**古いまま
    突き合わせると、とうに解除された規制を規制中のままにしてしまう**ので、
    ファイルの更新時刻を出して、古ければ警告する。
    """
    if not os.path.exists(PREF_FILE):
        print("  熊本県の公開JSONが見つからないので突き合わせは行わない")
        return []
    mtime = datetime.fromtimestamp(os.path.getmtime(PREF_FILE))
    age_days = (datetime.now() - mtime).total_seconds() / 86400
    with open(PREF_FILE, encoding="utf-8") as f:
        data = json.load(f)
    items = data["items"] if isinstance(data, dict) and "items" in data else data
    items = items or []
    released = sum(1 for i in items if (i.get("content") or "").strip() == "解除")
    print(f'  熊本県の公開JSON: {len(items)}件（「解除」{released}件）'
          f' 更新 {mtime:%Y-%m-%d %H:%M}')
    if age_days > 2:
        print(f'  ※ この突き合わせ元は {age_days:.1f}日前のものです。'
              '自動取得は main に入るので、`git checkout origin/main -- '
              'data/regulations.json` で新しくしてから作り直してください')
    _load_pref.info = {
        "件数": len(items), "解除": released,
        "更新": mtime.strftime("%Y-%m-%d %H:%M"),
        "経過日数": round(age_days, 2),
    }
    return items


def _is_released(p: dict, now: datetime) -> bool:
    """県の公開JSONの1件が、いま解除されているか。"""
    ended = parse_time(p.get("end_timestamp"))
    return ((p.get("content") or "").strip() == "解除"
            or (ended is not None and ended <= now))


def cross_check(results: list, now: datetime) -> int:
    """
    県の公開JSONで同じ区間を探し、そちらの状態を各件に持たせる。
    見つからなければ何も足さない（憶測で埋めない）。
    """
    pref = _load_pref()
    if not pref:
        return 0
    by_route = {}
    for p in pref:
        by_route.setdefault(_norm_route(p.get("route_name")), []).append(p)

    released = 0
    for item in results:
        if item["状態"] != "規制中":
            continue
        ends = _ends(item.get("geometry"))
        best = None
        for p in by_route.get(_norm_route(item["路線名"]), []):
            try:
                pt = (float(p["start_lat"]), float(p["start_lon"]))
            except (TypeError, ValueError, KeyError):
                continue
            dist = min((_km(e, pt) for e in ends), default=None)
            if dist is not None and (best is None or dist < best[0]):
                best = (dist, p)
        same_route = by_route.get(_norm_route(item["路線名"]), [])
        basis = "区間単位"
        if best is None or best[0] > PREF_MATCH_KM:
            # 近い区間が見つからなくても、その路線について県が持っている
            # 規制がすべて解除済みなら、路線としては解除されたと見る
            # （県道・市区町村道は県のフィードが権威。高速・直轄国道は
            # そもそも県のフィードに載らないので、この判定はしない）。
            if item["道路種別"] != "県道・市区町村道" or not same_route:
                continue
            if not all(_is_released(p, now) for p in same_route):
                continue
            p = max(same_route, key=lambda x: str(x.get("end_timestamp") or ""))
            dist = best[0] if best else None
            basis = "路線単位"
        else:
            dist, p = best
        end_text = p.get("end_timestamp")
        is_released = _is_released(p, now)
        item["県フィード照合"] = {
            "路線名": p.get("route_name"),
            "内容": p.get("content"),
            "終了日時": end_text,
            "距離km": round(dist, 2) if dist is not None else None,
            "判定根拠": basis,
            "判定": "解除済み" if is_released else "規制中",
        }
        released += bool(is_released)
    return released


def build() -> dict:
    zips = sorted(
        (p for p in glob.glob(os.path.join(DATA_DIR, "*.zip"))),
        key=lambda p: snapshot_time(os.path.basename(p)),
    )
    snapshots, first_seen_digest = [], {}
    for path in zips:
        features, digest = _read_regulations(path)
        if not features:
            continue
        stamp = snapshot_time(os.path.basename(path))
        # 中身が前の回と同一なら、規制としての時点はその前の回のもの
        content_time = first_seen_digest.setdefault(digest, stamp)
        snapshots.append((stamp, path, features, content_time))
    if not snapshots:
        raise RuntimeError("道路規制情報を含むZIPが見つからない")

    last_time = snapshots[-1][0]
    # 「いつ時点の規制か」。ファイル名の時刻と中身の時点がずれる回がある。
    # 手で登録した分（REGULATION_AS_OF）を優先し、無ければ中身の使い回しを見る。
    last_reg_time = REGULATION_AS_OF.get(
        os.path.basename(snapshots[-1][1]), snapshots[-1][3]
    )
    stale_days = (last_time - last_reg_time).total_seconds() / 86400
    items = {}
    for stamp, path, features, _ in snapshots:
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
            later = [s for s, _, _, _ in snapshots if s > seen[-1]]
            released_by = later[0] if later else None
        if started is None:
            before_quake = None
        else:
            before_quake = started < QUAKE_AT
        results.append({
            "id": item["id"],
            "道路種別": road_level(props),
            "道路種別（元データ）": props.get("道路種別"),
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

    pref_released = cross_check(results, datetime.now())

    results.sort(key=lambda r: (
        [n for n, _ in ROAD_LEVELS + [("不明", set())]].index(r["道路種別"]),
        r["状態"] != "規制中",
        r["開始日時"] or "",
    ))
    return {
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "quake_at": QUAKE_AT.strftime("%Y-%m-%d %H:%M"),
        "snapshots": [s.strftime("%Y-%m-%d %H:%M") for s, _, _, _ in snapshots],
        # 規制情報が最後に変わってから、配布が何日ぶん進んだか
        "regulation_stale_days": round(stale_days, 2),
        # 県の公開JSONでは解除されているのに、配布にはまだ残っている件数
        "pref_released_count": pref_released,
        # 突き合わせに使った県の公開JSONの素性（古いまま使うと取りこぼす）
        "pref_source": getattr(_load_pref, "info", None),
        "latest_snapshot": last_time.strftime("%Y-%m-%d %H:%M"),
        # 規制情報としての最新時点。上のファイル名の時刻とずれることがある
        "latest_regulation_time": last_reg_time.strftime("%Y-%m-%d %H:%M"),
        "note": (
            "各時点のスナップショットを突き合わせて状態を決めている。"
            "「規制中」は最新時点にも残っているもの、「解除済み」は途中で"
            "消えたもの（解除の時刻はスナップショットの間隔ぶん粗い）。"
            "「災害前から」は規制開始_日時が本震より前かどうかで、"
            "開始日時を持たないレコードは null（不明）にしている。"
            "最新の配布は latest_snapshot の回だが、その回の道路規制情報は"
            "latest_regulation_time 時点のものが入っている。"
        ),
        "items": results,
    }


def main() -> None:
    data = build()
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    items = data["items"]
    print(f'スナップショット {len(data["snapshots"])}時点'
          f'（最新の配布 {data["latest_snapshot"]}／'
          f'規制情報は {data["latest_regulation_time"]} 時点）')
    print(f"通算の規制 {len(items)}件")
    for level, _ in ROAD_LEVELS + [("不明", set())]:
        sub = [i for i in items if i["道路種別"] == level]
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
