"""
NEXCO西日本の「第○報」PDFから、通行止めと緊急車両の通行可能区間を読み取る。

第12報以降は書式が揃っていて、

    2. 緊急車両通行可能区間（8/11 8：00現在）
       道路名 / 区間 / 方向
    3. 通行止め状況（8/11 8：00現在）
       道路名 / 区間 / 方向 / 通行止め日時

の表がある。ここを拾う。読めなかったときは黙って空を返さず、
理由を warnings に入れて呼び出し側から見えるようにする
（勝手に「規制なし」と解釈されるほうが危ない）。

    python scripts/parse_nexco_report.py <PDFのパス>
"""
import argparse
import json
import os
import re
import sys

# 見出し。「8/11 8：00現在」の全角コロンや空白の揺れを吸収する。
# 「（8/13（木曜） 20:00時点）」のように括弧が入れ子になる回があるので、
# 1段だけ入れ子を許す。
NESTED_PAREN = r"（((?:[^（）]|（[^）]*）)*)）"
# 節番号は報によって変わる（2.だったり4.だったり）ので数字は問わない
SECTION_EMERGENCY = re.compile(
    r"\d\.\s*緊急車両通行可能区間\s*" + NESTED_PAREN
)
SECTION_CLOSURE = re.compile(r"\d\.\s*通行止め状況\s*" + NESTED_PAREN)
# 「1. 緊急車両通行可能となる区間／見込み区間」。これから開く区間の告知で、
# 2.の一覧（○○時点）にはまだ入っていない。反映するかは人が決める。
SECTION_BECOMING = re.compile(
    r"\d\.\s*(?:緊急車両通行可能となる(?:見込み)?区間"
    r"|通行止め解除(?:と)?なる(?:見込み)?区間)"
)
BECOMING_TIME = re.compile(
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*日.{0,12}?(\d{1,2})\s*時\s*(\d{2})\s*分"
)
NEXT_SECTION = re.compile(r"\n\s*\d\.\s*[^\n]")
# 「8/11 8：00現在」「8/13（木曜） 20:00時点」など、日付と時刻の間に
# 曜日が入る書き方があるので、間は何でも通す
AS_OF = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2}).{0,12}?(\d{1,2})\s*[:：]\s*(\d{2})")
ROAD = re.compile(r"(E\d+A?)([一-龥ぁ-んァ-ヴー]+?(?:自動車道|道路))")
# 「松橋IC ～ 八代IC」「坂本PA(工事用出入口を活用) ～ えびのIC」
SPAN = re.compile(r"([一-龥ぁ-んァ-ヴー]{1,8}(?:IC|JCT|PA|SA|TB)(?:\([^)]*\))?)～([一-龥ぁ-んァ-ヴー]{1,8}(?:IC|JCT|PA|SA|TB)(?:\([^)]*\))?)")
# ふりがなだけの行（ひらがな・長音・空白のみ）
FURIGANA = re.compile(r"^[\sぁ-んー]+$")
REPORT_NO = re.compile(r"第\s*(\d+)\s*報")
PUBLISHED = re.compile(
    r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日\s*(\d+)\s*時\s*(\d+)\s*分"
)


def _zen2han(text: str) -> str:
    return text.translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))


def read_text(pdf_path: str) -> str:
    import fitz  # 読み取りのときだけ要る（ダッシュボードの動作には不要）

    doc = fitz.open(pdf_path)
    lines = []
    for page in doc:
        for line in page.get_text().splitlines():
            if line.strip() and not FURIGANA.match(line):
                lines.append(line.strip())
    return "\n".join(lines)


def _section(text: str, head: re.Pattern) -> tuple:
    m = head.search(text)
    if not m:
        return None, None
    rest = text[m.end():]
    nxt = NEXT_SECTION.search(rest)
    return m.group(1), rest[: nxt.start()] if nxt else rest


def _spans(block: str) -> list:
    """
    道路名と区間の組を拾う。

    PDFの表はセルごとに改行が入り、区間名も「松橋 / IC ～ 八代 / IC」の
    ように途中で切れる。行単位では追えないので、空白と改行を落として
    1本の文字列にしてから、道路名と区間を出てくる順に拾う。
    """
    flat = re.sub(r"[\s　]+", "", block)
    # 「（南九州自動車道(日奈久IC～田浦IC)は国土交通省管理）」のような但し書きは
    # 表の行ではないので落とす。残すと国交省管理の区間まで拾ってしまう。
    flat = re.sub(r"（[^）]*国土交通省[^）]*）", "", flat)
    rows, road = [], None
    pattern = re.compile(f"(?:{ROAD.pattern})|(?:{SPAN.pattern})")
    for m in pattern.finditer(flat):
        if m.group(1):                       # 道路名
            road = f"{m.group(1)} {m.group(2)}"
        elif m.group(3) and road:            # 区間
            rows.append({"road": road, "span": f"{m.group(3)}〜{m.group(4)}"})
    return rows


def parse(pdf_path: str) -> dict:
    text = _zen2han(read_text(pdf_path))
    warnings = []

    no = REPORT_NO.search(text)
    pub = PUBLISHED.search(text)
    published = None
    if pub:
        y, mo, d, h, mi = (int(x) for x in pub.groups())
        published = f"{2018 + y}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}"

    def _as_of(label):
        m = AS_OF.search(label or "")
        if not m or not published:
            return None
        mo, d, h, mi = (int(x) for x in m.groups())
        return f"{published[:4]}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}"

    bc = SECTION_BECOMING.search(text)
    becoming = []
    if bc:
        rest = text[bc.end():]
        nxt = NEXT_SECTION.search(rest)
        block = rest[: nxt.start()] if nxt else rest
        when = BECOMING_TIME.search(re.sub(r"[\s　]+", "", block))
        for row in _spans(block):
            row["when"] = (
                f"{int(when.group(1)):02d}-{int(when.group(2)):02d} "
                f"{int(when.group(3)):02d}:{when.group(4)}" if when else None
            )
            becoming.append(row)

    em_label, em_block = _section(text, SECTION_EMERGENCY)
    cl_label, cl_block = _section(text, SECTION_CLOSURE)
    if em_block is None:
        warnings.append("「緊急車両通行可能区間」の表が見つからない")
    if cl_block is None:
        warnings.append("「通行止め状況」の表が見つからない")

    result = {
        "pdf": os.path.basename(pdf_path),
        "report_no": int(no.group(1)) if no else None,
        "published_at": published,
        "emergency": {
            "as_of": _as_of(em_label),
            "rows": _spans(em_block or ""),
        },
        "closure": {
            "as_of": _as_of(cl_label),
            "rows": _spans(cl_block or ""),
        },
        # これから通行可能になると告知された区間（2.の一覧にはまだ無い）
        "becoming": becoming,
        "warnings": warnings,
    }
    if em_block is not None and not result["emergency"]["rows"]:
        warnings.append("緊急車両の表から区間を読み取れなかった")
    if cl_block is not None and not result["closure"]["rows"]:
        warnings.append("通行止めの表から区間を読み取れなかった")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    data = parse(args.pdf)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    print(f"第{data['report_no']}報（発表 {data['published_at']}）")
    print(f"  緊急車両通行可能（{data['emergency']['as_of']}時点）")
    for r in data["emergency"]["rows"]:
        print(f"    {r['road']} {r['span']}")
    for r in data["becoming"]:
        print(f"  [告知] {r['road']} {r['span']} が {r.get('when')} 頃に通行可能に")
    print(f"  通行止め（{data['closure']['as_of']}時点）")
    for r in data["closure"]["rows"]:
        print(f"    {r['road']} {r['span']}")
    for w in data["warnings"]:
        print(f"  [注意] {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
