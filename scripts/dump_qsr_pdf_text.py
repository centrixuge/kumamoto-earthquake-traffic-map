"""
熊本河川国道事務所の規制PDFからテキストを抽出して表示する補助スクリプト。

直轄国道の規制は熊本県のポータルに載らないため、`data/mlit_regulations.json`
には手作業で転記している。その転記作業のために、PDFの本文を読める形で
まとめて出すだけのスクリプト。ダッシュボードの動作には不要。

使い方:
    pip install pymupdf
    python scripts/dump_qsr_pdf_text.py                 # data/qsr_regulations/*.pdf 全部
    python scripts/dump_qsr_pdf_text.py 260729_road2-4  # ファイル名の一部で絞り込み

出力を見ながら data/mlit_regulations.json の items に追記する。1件の形式:

    {
      "route_name": "国道○号",
      "section": "○○IC〜○○IC",              // 地点なら「○○市○○（133K450）」
      "length_km": 10,                          // 不明なら null
      "content": "全面通行止め",                 // 片側交互通行規制 など
      "reason": "地震による路面亀裂",
      "start_timestamp": "2026-07-28 16:27:00", // 規制を始めた日時（報の発表時刻ではない）
      "end_timestamp": "2026-07-29 02:00:00",   // 継続中なら null
      "affected_point_codes": ["9310127"],      // 裏付けが取れた観測点だけ
      "match_basis": "どう対応づけたかの根拠。裏付けが無ければそう書く",
      "reports": [
        {"label": "道第○報（YYYY-MM-DD HH:MM時点）…", "pdf": "…​.pdf", "url": "https://…"}
      ]
    }

affected_point_codes は推測で埋めないこと。ダッシュボードはこの値をもとに
地図へ▲を置き、時系列図に規制期間の帯を描くため、間違えると
「規制のせいで交通量が落ちた」という誤った読み方を誘発する。
観測点がどの路線上にあるかは OSRM の nearest で確認できる:

    curl -s "https://router.project-osrm.org/nearest/v1/driving/130.97624,32.91327?number=3"
"""
import os
import re
import sys

try:
    import fitz  # pymupdf
except ImportError:
    sys.exit("pymupdf が必要です: pip install pymupdf")

PDF_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "qsr_regulations")
# 全報に共通の問い合わせ先ブロックは転記に使わないので落とす
CONTACT_RE = re.compile(r"＜問い合わせ先＞.*?（代表）\s*", re.S)


def main() -> None:
    needle = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(f for f in os.listdir(PDF_DIR) if f.lower().endswith(".pdf"))
    files = [f for f in files if needle in f]
    if not files:
        sys.exit(f"{PDF_DIR} に該当するPDFがありません（絞り込み: {needle!r}）")

    for name in files:
        doc = fitz.open(os.path.join(PDF_DIR, name))
        print("=" * 72)
        print(f"### {name}  ({doc.page_count}ページ)")
        for i in range(doc.page_count):
            text = CONTACT_RE.sub("", doc[i].get_text()).strip()
            if not text:
                continue
            print(f"--- p{i + 1} ---")
            print(text)
        print()


if __name__ == "__main__":
    main()
