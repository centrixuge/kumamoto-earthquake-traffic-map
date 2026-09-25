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

# 1ファイルの上限。GitHubは100MBで受け取りを拒み、50MBを超えると警告を出すので、
# 分けて書く。経路データは配布ごと、集計経路データは年月ごとに分ける
# （どちらもその単位で完結しているので、分けても数え直さずに済む）。
PART_LIMIT_MB = 45

# 出力ファイル名 → 分け方
KEIRO_FILE = "transtron_keiro_link_all.csv.gz"
OD_FILE = "transtron_danmen_od_all.csv.gz"
ROUTE_FILE = "transtron_danmen_route_all.csv.gz"

# 同じ置き場から配る資料。配布元の仕様書（PDF）とレイアウト表（Excel）で、
# ファイル名も配布元のものなので、ここには書かずに data/transtron/ から拾う。
DOC_SUFFIXES = (".pdf", ".xlsx")


def _deliveries():
    """
    配布の一覧を data/transtron/ のファイル名から作る。

    配布は danmen<ラベル>.zip と keiro<ラベル>.zip の対で届く。次の配布が来ても
    このスクリプトを書き換えずに済むよう、対になっているものを拾って使う。
    ラベルは日付なので、文字列の順がそのまま時間の順になる。
    """
    found = {}
    for path in sorted(SRC.glob("danmen*.zip")):
        label = path.stem[len("danmen"):]
        keiro = SRC / f"keiro{label}.zip"
        if not keiro.exists():
            print(f"  ! {path.name} と対になる {keiro.name} がありません（とばします）")
            continue
        found[label] = {"danmen": path.name, "keiro": keiro.name}
    if not found:
        raise SystemExit(f"{SRC} に配布のzipがありません。")
    return found


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


def _load_one(label, zips, kind, prefix, cols):
    """配布1回分を読む。"""
    got = _read_members(SRC / zips[kind], prefix, cols)
    for df in got:
        df["配布"] = label
    print(f"  {zips[kind]} :: {prefix}* → {len(got)}ファイル "
          f"{sum(len(d) for d in got):,}行")
    if not got:
        return pd.DataFrame(columns=cols + ["元ファイル", "配布"])
    return pd.concat(got, ignore_index=True)


def _load(deliveries, kind, prefix, cols):
    return pd.concat(
        [_load_one(label, zips, kind, prefix, cols)
         for label, zips in deliveries.items()], ignore_index=True)


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


def _warn_if_big(info):
    mb = info["bytes_gz"] / 1e6
    if mb > PART_LIMIT_MB:
        print(f"  ! {info['file']} が {mb:.0f}MB あります"
              f"（1ファイル{PART_LIMIT_MB}MBまでの目安を超えています）")


def _dataset_summary(dataset, title, parts, note):
    """データの種類ごとのまとめ。アプリの一覧に出す。"""
    return {
        "dataset": dataset, "title": title, "note": note,
        "parts": [i["file"] for i in parts],
        "rows": sum(i["rows"] for i in parts),
        "bytes_gz": sum(i["bytes_gz"] for i in parts),
        "columns": parts[0]["columns"] if parts else [],
    }


def main():
    # 列名は置き場のJSONから読む（仕様書からの転記をリポジトリに置かないため）。
    # 位置で参照する箇所には、その位置が何かをコメントで書いておく。
    columns = _layout_columns()
    deliveries = _deliveries()
    print(f"配布 {len(deliveries)}回: {'、'.join(deliveries)}\n")
    meta = {"built_at": datetime.now().isoformat(timespec="seconds"),
            "deliveries": {k: list(v.values()) for k, v in deliveries.items()},
            "files": [], "datasets": []}

    # ── 経路データ（リンク単位の生データ）──
    # 縦につなぐだけでキーの重複が無いので、配布ごとに1ファイルにする。
    # 全期間を1本にすると100MBを超えてしまうのと、次の配布が来たときに
    # 前の分を作り直さずに済む（置き場のgitも新しい分だけ増える）。
    cols = columns[KEIRO_FILE]
    kind, prefix, _ = OUTPUTS[KEIRO_FILE]
    vehicle_col, trip_col, enter_col = cols[0], cols[1], cols[6]  # 車両/トリップ/入日時
    print("経路データ（リンク単位の生データ）")
    keiro_parts = []
    for label, zips in deliveries.items():
        df = _load_one(label, zips, kind, prefix, cols)
        df = df[cols + ["配布", "元ファイル"]]
        dt = pd.to_datetime(df[enter_col], errors="coerce")
        info = _write(df, f"transtron_keiro_link_{label}.csv.gz",
                      "配布ファイルを縦に連結しただけ（キーの重複なし）")
        info.update({
            "dataset": KEIRO_FILE, "part": label,
            "link_enter_from": str(dt.min()), "link_enter_to": str(dt.max()),
            "link_enter_blank_rows": int(dt.isna().sum()),
            "vehicles": int(df[vehicle_col].nunique()),
            "trips": int(df.groupby([vehicle_col, trip_col]).ngroups),
        })
        _warn_if_big(info)
        keiro_parts.append(info)
        del df
    meta["files"].extend(keiro_parts)
    summary = _dataset_summary(
        KEIRO_FILE, "経路データ", keiro_parts,
        "配布ごとに1ファイル。縦につなぐだけで重複はありません。")
    summary["link_enter_from"] = min(i["link_enter_from"] for i in keiro_parts)
    summary["link_enter_to"] = max(i["link_enter_to"] for i in keiro_parts)
    summary["vehicles"] = None   # 配布をまたぐ重複があるので合計はしない
    summary["trips"] = sum(i["trips"] for i in keiro_parts)
    meta["datasets"].append(summary)

    # ── 断面別の集計2種 ──
    # こちらは同じキーが配布をまたいで現れるので、全部読んでから足し合わせる。
    # 台数は最後の列、年月日は4番目、断面リンクは先頭2列。
    for file_name, label_ja, note in (
            (OD_FILE, "集計ODデータ",
             "同じキーが複数ファイルにある分は台数を合計した"),
            (ROUTE_FILE, "集計経路データ",
             "同じキーが複数ファイルにある分は台数を合計した")):
        kind, prefix, _ = OUTPUTS[file_name]
        cols = columns[file_name]
        count_col, date_col = cols[-1], cols[3]
        print(label_ja)
        df = _load(deliveries, kind, prefix, cols)
        df["県"] = _pref(df["元ファイル"])
        raw_rows = len(df)
        df = _sum_by_key(df, cols, count_col)
        df = df[["県"] + cols + ["元ファイル数", "配布", "元ファイル"]]

        # 足し合わせたあとの行は年月日ごとに完結しているので、大きいものは
        # 年月で分けて書く（分けても数え直しは要らない）。
        # 集計経路データは大きいので年月で分ける。集計ODデータは小さいので1本。
        parts = []
        if file_name == ROUTE_FILE:
            # 年月日が空の行もある（配布データにそのまま入っている）。
            # 落とさずに blank という1ファイルにまとめる。
            ym_col = df[date_col].astype(str).str[:6].where(
                df[date_col].notna(), "blank")
            groups = [(ym, df[ym_col == ym]) for ym in sorted(ym_col.unique())]
        else:
            groups = [(None, df)]
        for ym, part in groups:
            out_name = (file_name if ym is None
                        else file_name.replace("_all.csv.gz", f"_{ym}.csv.gz"))
            info = _write(part, out_name, note)
            info.update({"dataset": file_name, "part": ym or "all",
                         "rows_before_sum": (raw_rows if ym is None else None),
                         "count_total": int(part[count_col].sum()),
                         "date": _date_range(part, date_col),
                         "sections": int(part.groupby(list(cols[:2])).ngroups)})
            _warn_if_big(info)
            parts.append(info)
        meta["files"].extend(parts)
        summary = _dataset_summary(file_name, label_ja, parts, note)
        summary.update({
            "rows_before_sum": raw_rows,
            "count_total": sum(i["count_total"] for i in parts),
            "date": _date_range(df, date_col),
            "sections": int(df.groupby(list(cols[:2])).ngroups),
        })
        meta["datasets"].append(summary)
        del df

    # ── 仕様書とレイアウト表 ──
    # 中身の説明はこのリポジトリに書かず、ファイルそのものを置き場から配る。
    docs = []
    for src in sorted(SRC.iterdir()):
        if src.suffix.lower() not in DOC_SUFFIXES:
            continue
        (OUT / src.name).write_bytes(src.read_bytes())
        docs.append({"file": src.name, "bytes": src.stat().st_size})
        print(f"  → {src.name}  {src.stat().st_size / 1e3:,.0f}KB（コピー）")
    if not docs:
        print("  ! 仕様書・レイアウト表が見つかりません")
    meta["documents"] = docs
    meta["layout_file"] = next(
        (d["file"] for d in docs if d["file"].endswith(".xlsx")), None)

    (OUT / "transtron_bundle_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    total = sum(i["bytes_gz"] for i in meta["files"])
    print(f"\n合計 {len(meta['files'])}ファイル {total / 1e6:,.0f}MB")
    print(f"meta: {OUT / 'transtron_bundle_meta.json'}")


if __name__ == "__main__":
    main()
