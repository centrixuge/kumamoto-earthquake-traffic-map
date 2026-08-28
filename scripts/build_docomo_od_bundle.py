"""
ドコモODデータを、配布のまま＋集計軸ごとに束ねた形で用意する。

  python scripts/build_docomo_od_bundle.py

配布は「集計軸（居住地別・年代別・飛行機）× 期間（震災前・震災後）」の6ファイル。
文字コードはCP932で、震災後のファイルには氷川町断面の方向（上下）が入る。

直しているのは1点だけ:
  DIRECTION の値が **"Fron South"（Fromのタイポ）** になっているので
  "From South" に直す。ほかの値・行は配布のまま触らない。

出力は data/docomo_od/bundle/（gitignore対象）。
  docomo_od_<軸>_<期間>.csv      … 配布ファイルと同じ内容（UTF-8 BOM付き）
  docomo_od_<軸>_all.csv         … 震災前後を縦につないだもの（期間区分の列を追加）
  docomo_od_meta.json            … 行数・期間・値の一覧
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "docomo_od"
OUT = SRC / "bundle"

# 配布ファイル → (集計軸, 期間区分)
FILES = {
    "居住地別/震災前_集計結果_航空機以外.csv": ("residence", "震災前"),
    "居住地別/震災後_集計結果_自動車.csv": ("residence", "震災後"),
    "年代別/震災前_集計結果_航空機以外.csv": ("age", "震災前"),
    "年代別/震災後_集計結果_自動車.csv": ("age", "震災後"),
    "飛行機/震災前_集計結果_航空機.csv": ("air", "震災前"),
    "飛行機/震災後_集計結果_航空機.csv": ("air", "震災後"),
}
AXIS_LABEL = {"residence": "居住地別", "age": "年代別", "air": "飛行機"}
SRC_ENCODING = "cp932"

# 配布データのタイポ。ほかは触らない。
DIRECTION_FIX = {"Fron South": "From South"}

LAYOUT_XLSX = "ファイル名説明_集計軸早見表.xlsx"


def _read(rel: str) -> pd.DataFrame:
    df = pd.read_csv(SRC / rel, encoding=SRC_ENCODING, dtype=str)
    if "DIRECTION" in df.columns:
        before = sorted(df["DIRECTION"].dropna().unique())
        df["DIRECTION"] = df["DIRECTION"].replace(DIRECTION_FIX)
        after = sorted(df["DIRECTION"].dropna().unique())
        if before != after:
            print(f"    DIRECTION を直した: {before} → {after}")
    return df


def _write(df: pd.DataFrame, name: str) -> dict:
    path = OUT / name
    # Excelでそのまま開けるようにUTF-8 BOM付き・CRLF
    df.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\r\n")
    print(f"  → {name}  {len(df):,}行 × {len(df.columns)}列  "
          f"{path.stat().st_size / 1e3:.0f}KB")
    info = {"file": name, "rows": int(len(df)), "columns": list(df.columns),
            "bytes": path.stat().st_size}
    if "DATE" in df.columns:
        info["date"] = {"from": df["DATE"].min(), "to": df["DATE"].max(),
                        "days": int(df["DATE"].nunique())}
    for col in ("TRANSPORTATION_TYPE", "ADDRESS", "AGE", "DIRECTION", "期間区分"):
        if col in df.columns:
            info.setdefault("values", {})[col] = sorted(df[col].dropna().unique())
    return info


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"built_at": datetime.now().isoformat(timespec="seconds"),
            "source_files": list(FILES), "direction_fix": DIRECTION_FIX,
            "files": []}

    by_axis: dict[str, list[pd.DataFrame]] = {}
    for rel, (axis, period) in FILES.items():
        print(f"{rel}")
        df = _read(rel)
        meta["files"].append(
            _write(df, f"docomo_od_{axis}_{'pre' if period == '震災前' else 'post'}.csv")
            | {"axis": AXIS_LABEL[axis], "period": period, "source": rel})
        by_axis.setdefault(axis, []).append(df.assign(期間区分=period))

    for axis, frames in by_axis.items():
        joined = pd.concat(frames, ignore_index=True)
        # 期間区分を先頭に置く（震災前には DIRECTION が無いので空欄になる）
        cols = ["期間区分"] + [c for c in joined.columns if c != "期間区分"]
        meta["files"].append(
            _write(joined[cols], f"docomo_od_{axis}_all.csv")
            | {"axis": AXIS_LABEL[axis], "period": "震災前＋震災後"})

    # 画面に出す説明文（期間・区分など、データの中身に触れるもの）は、
    # 公開リポジトリに置かない。gitignore下の display.json に書いておき、
    # ここでは読んでメタに載せるだけにする。無ければ何も足さない
    # （アプリ側は提供の条件だけを出す）。
    display = SRC / "display.json"
    if display.exists():
        meta["display"] = json.loads(display.read_text(encoding="utf-8"))
        print(f"  display.json を読み込んだ（{len(meta['display'])}項目）")
    else:
        print("  display.json がありません（画面の説明文は出ません）")

    layout = SRC / LAYOUT_XLSX
    if layout.exists():
        (OUT / layout.name).write_bytes(layout.read_bytes())
        meta["layout_file"] = layout.name
        print(f"  → {layout.name}（コピー）")

    (OUT / "docomo_od_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\nmeta: {OUT / 'docomo_od_meta.json'}")


if __name__ == "__main__":
    main()
