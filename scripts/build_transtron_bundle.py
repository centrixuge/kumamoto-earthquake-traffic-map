"""
トランストロン商用車プローブの配布ファイルを、利用しやすい形に束ねる。

配布は期間ごと・県ごと・日ごとにファイルが分かれていて、しかも
**同じキーが隣の期間のファイルにも現れる**（トリップ単位で切り出しているため、
期間の境をまたぐトリップが後の期間のファイルに入る）。そのままつなぐと
同じ断面・同じ時間帯の行が複数できるので、集計値はキーで足し合わせる。

  足し合わせでよいことは生の経路データと突き合わせて確認している
  （scratch/transtron_crosscheck.py）。断面リンク自身の行の走行台数を
  経路データから数え直したトリップ数と比べると、
  合計ルールで 3,401/3,468（98.1%）一致、最大ルールでは 3,286（94.8%）。
  複数ファイルに分かれていた120組に限れば、合計 116件一致 / 最大 1件一致。

  python scripts/build_transtron_bundle.py

出力は data/transtron/bundle/（gitignore対象）。
提供条件の確認が済むまで、リポジトリにも外部にも置かない。

アプリのタブが表示する項目の定義（仕様書からの転記）は、このリポジトリには
置かず、同じ置き場の transtron_layout.json から読む。JSONは scratch/ の
スクリプトで作る（scratch/transtron_layout_json.py）。
"""
import gzip
import hashlib
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "transtron"
OUT = SRC / "bundle"

# 見出しに使う項目名は、仕様書からの転記なのでこのリポジトリには置かない。
# 非公開の置き場にある transtron_layout.json（scratch/transtron_layout_json.py が
# 作る）から読む。列の順序もそのJSONのとおり。
LAYOUT_JSON = OUT / "transtron_layout.json"
# 出力ファイル名 → (zipの種別, zip内のファイル名の先頭, 台数の列)
OUTPUTS = {
    "transtron_keiro_link_all.csv.gz": ("keiro", "keiro", None),
    "transtron_danmen_od_all.csv.gz": ("danmen", "oddb", -1),
    "transtron_danmen_route_all.csv.gz": ("danmen", "keiro", -1),
}

# 配布ラベル → zipファイル名
DELIVERIES = {
    "202607": {"danmen": "danmen202607.zip", "keiro": "keiro202607.zip"},
    "20260801to04": {"danmen": "danmen20260801to04.zip",
                     "keiro": "keiro20260801to04.zip"},
}


def _layout_columns():
    """置き場のJSONから、出力ファイルごとの列名（配布ファイルの順）を読む。"""
    if not LAYOUT_JSON.exists():
        raise SystemExit(
            f"{LAYOUT_JSON} がありません。項目名は仕様書からの転記なので"
            "リポジトリには置いていません。"
            "scratch/transtron_layout_json.py を先に実行してください。")
    doc = json.loads(LAYOUT_JSON.read_text(encoding="utf-8"))
    return {name: [c[0] for c in spec["columns"]]
            for name, spec in doc["datasets"].items()}


def _read_members(zip_path, prefix, cols):
    """zipの中の該当CSVを読み、配布ラベルと元ファイル名を付けて返す。"""
    frames = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in sorted(zf.namelist()):
            base = name.split("/")[-1]
            if not (name.endswith(".csv") and base.startswith(prefix)):
                continue
            with zf.open(name) as f:
                df = pd.read_csv(f, header=None, names=cols, dtype=str,
                                 encoding="cp932")
            df["元ファイル"] = base
            frames.append(df)
    return frames


def _load(kind, prefix, cols):
    frames = []
    for label, zips in DELIVERIES.items():
        got = _read_members(SRC / zips[kind], prefix, cols)
        for df in got:
            df["配布"] = label
        frames.extend(got)
        print(f"  {zips[kind]} :: {prefix}* → {len(got)}ファイル "
              f"{sum(len(d) for d in got):,}行")
    return pd.concat(frames, ignore_index=True)


def _pref(series):
    """元ファイル名から県を拾う（配布が県別に分かれているのは断面データだけ）。"""
    return series.str.contains("kumamoto").map({True: "熊本", False: "宮崎"})


def _sum_by_key(df, cols, count_col):
    """キーで足し合わせる。年月日が空の行も落とさない（dropna=False）。"""
    keys = [c for c in cols if c != count_col] + ["県"]
    df = df.copy()
    df[count_col] = df[count_col].astype(int)
    grouped = df.groupby(keys, dropna=False, sort=True).agg(
        **{count_col: (count_col, "sum"),
           "元ファイル数": ("元ファイル", "nunique"),
           "元ファイル": ("元ファイル", lambda s: ";".join(sorted(set(s)))),
           "配布": ("配布", lambda s: ";".join(sorted(set(s))))})
    return grouped.reset_index()


def _write(df, name, note):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    # 改行はCRLF（Excelでそのまま開けるように）、文字コードはUTF-8 BOM付き。
    # gzipのヘッダには既定で書いた時刻が入り、中身が同じでもハッシュが変わって
    # しまうので mtime=0 で作る（作り直したときに中身が変わったのかを見分けるため）。
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with io.TextIOWrapper(gz, encoding="utf-8-sig", newline="\r\n") as f:
                df.to_csv(f, index=False, lineterminator="\r\n")
    size = path.stat().st_size
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    print(f"  → {name}  {len(df):,}行 × {len(df.columns)}列  "
          f"{size / 1e6:.1f}MB(gz)  sha256:{digest}")
    return {"file": name, "rows": int(len(df)), "columns": list(df.columns),
            "bytes_gz": size, "sha256_16": digest, "note": note}


def _date_range(df, col):
    d = df[col].dropna()
    return {"from": d.min(), "to": d.max(), "days": int(d.nunique()),
            "blank_rows": int(df[col].isna().sum())}


def main():
    # 列名は置き場のJSONから読む（仕様書からの転記をリポジトリに置かないため）。
    # 位置で参照する箇所には、その位置が何かをコメントで書いておく。
    columns = _layout_columns()
    meta = {"built_at": datetime.now().isoformat(timespec="seconds"),
            "deliveries": {k: list(v.values()) for k, v in DELIVERIES.items()},
            "files": []}

    name = "transtron_keiro_link_all.csv.gz"
    kind, prefix, _ = OUTPUTS[name]
    cols = columns[name]
    print("経路データ（リンク単位の生データ）")
    keiro = _load(kind, prefix, cols)
    keiro = keiro[cols + ["配布", "元ファイル"]]
    vehicle_col, trip_col, enter_col = cols[0], cols[1], cols[6]  # 車両/トリップ/入日時
    dt = pd.to_datetime(keiro[enter_col], errors="coerce")
    info = _write(keiro, name, "配布ファイルを縦に連結しただけ（キーの重複なし）")
    info["link_enter_from"] = str(dt.min())
    info["link_enter_to"] = str(dt.max())
    info["link_enter_blank_rows"] = int(dt.isna().sum())
    info["vehicles"] = int(keiro[vehicle_col].nunique())
    info["trips"] = int(keiro.groupby([vehicle_col, trip_col]).ngroups)
    meta["files"].append(info)
    del keiro

    # 断面別の集計2種は作りが同じなので同じ手順で回す。
    # 台数は最後の列、年月日は4番目、断面リンクは先頭2列。
    for name, label, note in (
            ("transtron_danmen_od_all.csv.gz", "集計ODデータ",
             "同じキーが複数ファイルにある分は台数を合計した"),
            ("transtron_danmen_route_all.csv.gz", "集計経路データ",
             "同じキーが複数ファイルにある分は台数を合計した")):
        kind, prefix, _ = OUTPUTS[name]
        cols = columns[name]
        count_col, date_col = cols[-1], cols[3]
        print(label)
        df = _load(kind, prefix, cols)
        df["県"] = _pref(df["元ファイル"])
        raw_rows = len(df)
        df = _sum_by_key(df, cols, count_col)
        df = df[["県"] + cols + ["元ファイル数", "配布", "元ファイル"]]
        info = _write(df, name, note)
        info["rows_before_sum"] = raw_rows
        info["count_total"] = int(df[count_col].sum())
        info["date"] = _date_range(df, date_col)
        info["sections"] = int(df.groupby(list(cols[:2])).ngroups)
        meta["files"].append(info)
        del df

    # レイアウト表も同じ置き場に置く（アプリの「商用車プローブ」タブから配る）
    layout = SRC / "商用車プローブ_データレイアウト.xlsx"
    if layout.exists():
        (OUT / layout.name).write_bytes(layout.read_bytes())
        meta["layout_file"] = layout.name
        print(f"  → {layout.name}  {layout.stat().st_size / 1e3:.0f}KB（コピー）")
    else:
        print("  レイアウト表が見つかりません（scratch/transtron_layout_xlsx.py で作る）")

    (OUT / "transtron_bundle_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\nmeta: {OUT / 'transtron_bundle_meta.json'}")


if __name__ == "__main__":
    main()
