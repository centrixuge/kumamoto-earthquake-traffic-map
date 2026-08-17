"""
モバイル空間統計のリアルタイム配信分（1時間ごとの小さなzipが日付フォルダに並ぶ）を、
過去分と同じ「1オプション＝1本のcsv.zip」の形に束ねる。

  入力  data/mss/realtime/YYYYMMDD/clipped_mesh_pop_YYYYMMDDHHMM_{option}.csv.zip
  出力  data/mss/realtime/{01_total|02_age_gender|03_residence_pref|04_residence_city}.csv.zip

列は入力のまま（date,time,area,residence,age,gender,population）で、改行もCRLFのまま
バイト列として連結する。ヘッダ行は先頭の1本だけ残す。並びは日時の昇順で、同一時点内の
並びは配信ファイルの順序をそのまま保つ（過去分と同じ並び方）。

  python scripts/pack_mss_realtime.py            # 04_residence_city（既定）
  python scripts/pack_mss_realtime.py 01         # 01_total
  python scripts/pack_mss_realtime.py 02 03      # まとめて

出力は配布データそのものなので、公開リポジトリには置かないこと（data/mss/ は .gitignore 済み）。
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "mss" / "realtime"

OPTIONS = {
    "00000": "01_total",
    "00001": "02_age_gender",
    "00002": "03_residence_pref",
    "00003": "04_residence_city",
}
HEADER = b"date,time,area,residence,age,gender,population\r\n"
STAMP = re.compile(r"_(\d{12})_(\d{5})\.csv\.zip$")


def parts_for(option: str) -> list[tuple[str, Path]]:
    """（時点, ファイル）を時点の昇順で返す。"""
    found: list[tuple[str, Path]] = []
    for day in sorted(p for p in SRC.iterdir() if p.is_dir()):
        for path in day.iterdir():
            m = STAMP.search(path.name)
            if m and m.group(2) == option:
                found.append((m.group(1), path))
    found.sort(key=lambda x: x[0])
    return found


def pack(option: str) -> None:
    name = OPTIONS[option]
    parts = parts_for(option)
    if not parts:
        raise SystemExit(f"{option} のファイルが {SRC} に見つかりません")

    days: dict[str, int] = {}
    for stamp, _ in parts:
        days[stamp[:8]] = days.get(stamp[:8], 0) + 1
    print(f"[{name}] {len(parts)}時点 / {len(days)}日")
    for day, n in sorted(days.items()):
        mark = "" if n == 24 else f"  ← 24時点そろっていません"
        print(f"  {day}: {n}時点{mark}")

    out = SRC / f"{name}.csv.zip"
    rows = 0
    raw = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
        with zout.open(f"{name}.csv", "w") as dst:
            dst.write(HEADER)
            for i, (stamp, path) in enumerate(parts, 1):
                with zipfile.ZipFile(path) as zin:
                    inner = zin.namelist()
                    if len(inner) != 1:
                        raise SystemExit(f"{path.name}: 中身が1ファイルではありません")
                    data = zin.read(inner[0])
                if not data.startswith(HEADER):
                    raise SystemExit(f"{path.name}: 列が違います {data[:60]!r}")
                body = data[len(HEADER):]
                if body and not body.endswith(b"\r\n"):
                    body += b"\r\n"
                dst.write(body)
                rows += body.count(b"\r\n")
                raw += len(body)
                if i % 24 == 0:
                    print(f"  ... {stamp} まで {rows:,}行")

    size = out.stat().st_size
    first, last = parts[0][0], parts[-1][0]
    print(f"[{name}] {rows:,}行 / {first}〜{last} / "
          f"{size/1e6:.1f} MB（展開後 {(raw+len(HEADER))/1e6:.1f} MB）")
    print(f"  → {out}")


def main() -> None:
    args = sys.argv[1:] or ["04"]
    for arg in args:
        key = arg if arg in OPTIONS else f"{int(arg) - 1:05d}"
        if key not in OPTIONS:
            raise SystemExit(f"不明なオプション: {arg}（01〜04 か 00000〜00003）")
        pack(key)


if __name__ == "__main__":
    main()
