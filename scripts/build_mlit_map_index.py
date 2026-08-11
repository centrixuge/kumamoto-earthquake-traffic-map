"""
「通れる道マップ」からダウンロードした各時点のZIPを、Excelの一覧にする。

どの時点にどのレイヤが入っているかは時点ごとにまちまちで
（初回は道路規制が無い、通行実績が5分割されている、など）、
ZIPを開かないと分からない。作業前に見渡せるよう表にしておく。

    python scripts/build_mlit_map_index.py

出力: data/mlit_r8kumamoto_map/通れる道マップ_データ一覧.xlsx
"""
import glob
import os
import re
import zipfile
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "mlit_r8kumamoto_map",
)
OUT_NAME = "通れる道マップ_データ一覧.xlsx"
FONT = "Meiryo"

# ZIP内のファイル名と、それが何の情報か。「通れる道マップ」の
# 表示レイヤに対応する（dourokisei=道路規制、tukoujisseki=通行実績）。
LAYERS = [
    ("道路規制情報", re.compile(r"^dourokisei\d*\.geojson$", re.I)),
    ("ETC2.0速度情報", re.compile(r"^ETC2\.0_speed_data\d*\.geojson$", re.I)),
    ("通行実績情報", re.compile(r"^tukoujisseki\d*\.geojson$", re.I)),
]

# 時刻がファイル名に入っていない回。配布元の notice.txt に
# 「260729data.zip is the data as of 2026/07/29 8:00」とある。
DATE_ONLY_HOUR = {"260729": "0800"}


def parse_timestamp(stem: str) -> tuple:
    """ファイル名（YYMMDDhhmm）から日時を作る。戻り値は (datetime, 根拠)。"""
    if len(stem) == 10:
        return datetime.strptime(stem, "%y%m%d%H%M"), "ファイル名"
    if len(stem) == 6 and stem in DATE_ONLY_HOUR:
        return (
            datetime.strptime(stem + DATE_ONLY_HOUR[stem], "%y%m%d%H%M"),
            "notice.txt",
        )
    raise ValueError(f"日時を読み取れないファイル名: {stem}")


def scan(data_dir: str) -> list:
    rows = []
    for path in sorted(glob.glob(os.path.join(data_dir, "*.zip"))):
        name = os.path.basename(path)
        stem = name[: -len("data.zip")]
        stamp, basis = parse_timestamp(stem)
        with zipfile.ZipFile(path) as z:
            files = [
                os.path.basename(n) for n in z.namelist()
                if n.lower().endswith(".geojson")
            ]
        found, notes = {}, []
        for label, pattern in LAYERS:
            hits = [f for f in files if pattern.match(f)]
            found[label] = "〇" if hits else ""
            if len(hits) > 1:
                notes.append(f"{label}が{len(hits)}分割")
        if basis != "ファイル名":
            notes.append(f"時刻の出典: {basis}")
        rows.append({
            "file": name,
            "stamp": stamp,
            "layers": found,
            "note": "、".join(notes),
        })
    rows.sort(key=lambda r: r["stamp"])
    return rows


def build(rows: list, out_path: str) -> None:
    headers = ["ファイル名", "情報の日時および時刻"] + [n for n, _ in LAYERS] + ["備考"]
    wb = Workbook()
    ws = wb.active
    ws.title = "データ一覧"

    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="DDEBF7")

    for col, head in enumerate(headers, start=1):
        c = ws.cell(row=1, column=col, value=head)
        c.font = Font(name=FONT, bold=True, size=10)
        c.fill = head_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border

    for i, row in enumerate(rows, start=2):
        values = [row["file"], row["stamp"]] + [
            row["layers"][n] for n, _ in LAYERS
        ] + [row["note"]]
        for col, value in enumerate(values, start=1):
            c = ws.cell(row=i, column=col, value=value)
            c.font = Font(name=FONT, size=10)
            c.border = border
            if col == 2:
                c.number_format = "yyyy/mm/dd hh:mm"
                c.alignment = Alignment(horizontal="center")
            elif 3 <= col <= 2 + len(LAYERS):
                c.alignment = Alignment(horizontal="center")

    # 集計行。値ではなく数式で置き、行を足しても追随するようにする。
    total_row = len(rows) + 2
    label = ws.cell(row=total_row, column=1, value="〇の数")
    label.font = Font(name=FONT, bold=True, size=10)
    label.border = border
    ws.cell(row=total_row, column=2, value=f"全{len(rows)}ファイル").font = Font(
        name=FONT, bold=True, size=10
    )
    ws.cell(row=total_row, column=2).alignment = Alignment(horizontal="center")
    ws.cell(row=total_row, column=2).border = border
    for col in range(3, 3 + len(LAYERS)):
        letter = get_column_letter(col)
        c = ws.cell(
            row=total_row, column=col,
            value=f'=COUNTIF({letter}2:{letter}{len(rows) + 1},"〇")',
        )
        c.font = Font(name=FONT, bold=True, size=10)
        c.alignment = Alignment(horizontal="center")
        c.border = border
    ws.cell(row=total_row, column=len(headers)).border = border

    # 出典と、日時をどう決めたかを表の下に残す（後から見て根拠が追えるように）
    notes = [
        "出典: 国土交通省「通れる道マップ」（https://www.mlit.go.jp/road/saigai/r8kumamoto/index.html）"
        "からダウンロードしたZIP。",
        "情報の日時および時刻: ファイル名の YYMMDDhhmm から読み取り（例 2608071600 → 2026/08/07 16:00）。"
        "260729data.zip だけ時刻が無く、配布元の notice.txt の記載（as of 2026/07/29 8:00）に従った。",
        "〇の判定: ZIP内のGeoJSONのファイル名で判定（dourokisei＝道路規制情報、"
        "ETC2.0_speed_data＝ETC2.0速度情報、tukoujisseki＝通行実績情報）。"
        "連番で分割されているものも1件として〇にし、分割数は備考に書いた。",
        f"作成: scripts/build_mlit_map_index.py",
    ]
    for j, text in enumerate(notes):
        c = ws.cell(row=total_row + 2 + j, column=1, value=text)
        c.font = Font(name=FONT, size=9, color="595959")
        c.alignment = Alignment(vertical="top")

    widths = [22, 20] + [15] * len(LAYERS) + [30]
    for col, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"

    wb.save(out_path)


def main() -> None:
    rows = scan(DATA_DIR)
    out_path = os.path.join(DATA_DIR, OUT_NAME)
    build(rows, out_path)
    for row in rows:
        flags = "".join(
            (row["layers"][n] or "-").ljust(2) for n, _ in LAYERS
        )
        print(f'{row["file"]:<22} {row["stamp"]:%Y-%m-%d %H:%M}  {flags} {row["note"]}')
    print(f"\n{len(rows)}件 -> {out_path}")


if __name__ == "__main__":
    main()
