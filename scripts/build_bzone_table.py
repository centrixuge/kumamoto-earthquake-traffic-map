"""
Bゾーンコード（道路交通センサス）を市区町村に読み替える表を作る。

商用車プローブの集計ODデータの起終点は7桁のBゾーンコードで来る。手元のデータを
調べると、**7桁は「市区町村コード5桁＋ゾーン番号2桁」**になっている（ゾーン番号が
1桁のときは右側が空白）。そのため、市区町村までの集計であれば、センサスの
ゾーン区分表が無くても総務省の全国地方公共団体コードだけで読み替えられる。

  検証（2026-08-24、手元の全期間データ 1,523ゾーン）
    先頭5桁が現在の市区町村コードに一致: 1,493ゾーン（98.0%）
    出現回数でみた被覆率: 99.93%
    一致しない30ゾーンは合併・改称前のコード（例: 福岡県の 40305 = 旧那珂川町）。
    センサスの時点の市区町村コードを使っているため。ゾーン内訳（ゾーン番号が
    市区町村内のどこか）まで要る場合は、センサスのゾーン区分表が別に必要。

出力は data/bzone/municipality_codes.csv（公的な公開データなのでリポジトリに置く）。

  python scripts/build_bzone_table.py
"""
from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "bzone"

# 総務省「全国地方公共団体コード」。市区町村と、政令指定都市の区の2シート。
# ページ: https://www.soumu.go.jp/denshijiti/code.html
CODE_URL = "https://www.soumu.go.jp/main_content/000925835.xlsx"
CODE_LABEL = "総務省 全国地方公共団体コード（令和6年1月1日現在）"


def _column(df: pd.DataFrame, keyword: str) -> str:
    """見出しに改行が入っているので、含む文字で選ぶ（カナの列は除く）。"""
    for col in df.columns:
        flat = str(col).replace("\n", "")
        if keyword in flat and "カナ" not in flat and "ｶﾅ" not in flat:
            return col
    raise SystemExit(f"{keyword} の列が見つかりません: {list(df.columns)}")


def main() -> None:
    res = requests.get(CODE_URL, timeout=120)
    res.raise_for_status()
    sheets = pd.read_excel(io.BytesIO(res.content), sheet_name=None, dtype=str)

    frames = []
    for name, df in sheets.items():
        frames.append(df[[_column(df, "団体コード"), _column(df, "都道府県名"),
                          _column(df, "市区町村名")]]
                      .set_axis(["団体コード", "都道府県", "市区町村"], axis=1)
                      .assign(出所シート=name))
    codes = (pd.concat(frames, ignore_index=True)
               .dropna(subset=["団体コード"]))
    # 団体コードは6桁（5桁＋検査数字）。Bゾーンに入っているのは先頭5桁。
    codes["市区町村コード"] = codes["団体コード"].str.strip().str[:5]
    codes = (codes[codes["市区町村コード"].str.fullmatch(r"\d{5}")]
             .drop_duplicates("市区町村コード")
             .sort_values("市区町村コード"))
    # 都道府県だけの行（市区町村名が空）は、県コードの行として残す
    codes["市区町村"] = codes["市区町村"].fillna("")
    out = codes[["市区町村コード", "都道府県", "市区町村", "団体コード"]]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "municipality_codes.csv"
    out.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\r\n")

    meta = {
        "source": CODE_LABEL,
        "source_url": CODE_URL,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(out)),
        "prefecture_rows": int((out["市区町村"] == "").sum()),
        "bzone_rule": ("Bゾーンコード7桁 = 市区町村コード5桁 ＋ ゾーン番号2桁"
                       "（ゾーン番号が1桁のときは右側が空白）"),
        "note": ("Bゾーンはセンサスの時点の市区町村コードを使っているため、"
                 "合併・改称前のコードはこの表に無い。"
                 "ゾーン内訳（市区町村内のどこか）にはセンサスのゾーン区分表が別に必要。"),
    }
    (OUT_DIR / "municipality_codes_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(f"{path}: {len(out):,}行"
          f"（うち都道府県だけの行 {meta['prefecture_rows']}）")
    print(f"  出所: {CODE_LABEL}")


if __name__ == "__main__":
    main()
