"""
読み取った「第○報」の内容を data/nexco_regulations.json に反映し、
判断材料をまとめたMarkdownを書き出す。

自動で書き換えるのは、既にある通行止めの項目に対する
「緊急車両が通れる区間」（emergency_access）だけ。通行止めそのものの
追加・解除は、開始/終了時刻や線形の判断が要るので手作業に残し、
食い違いは「要確認」として書き出す。

    python scripts/apply_nexco_report.py <PDFのパス> [--summary out.md] [--dry-run]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_nexco_report import parse   # noqa: E402

sys.path.insert(0, ROOT_FOR_MODULES := os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
from modules.nexco_text import emergency_lines, emergency_note  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(ROOT, "data", "nexco_regulations.json")
REPO_URL = "https://github.com/centrixuge/kumamoto-earthquake-traffic-map"
APP_URL = "https://kumamoto-earthquake-traffic-map.streamlit.app/"
# developブランチを映すStreamlitアプリを作ったら、その URL を
# リポジトリ変数 PREVIEW_APP_URL に入れる。入っていればPRに出す。
PREVIEW_URL = os.environ.get("PREVIEW_APP_URL", "").strip()
SOURCE_URL = "https://www.w-nexco.co.jp/"


def _active(items):
    return [i for i in items if not i.get("end_timestamp")]


def apply_report(report: dict, data: dict) -> tuple:
    """戻り値は (変更点の一覧, 要確認の一覧)。"""
    changes, checks = [], []
    label = f"第{report['report_no']}報（{report['published_at']}）"

    # 1. 緊急車両の通行可能区間を、路線ごとにまとめて当てはめる
    by_road = {}
    for row in report["emergency"]["rows"]:
        by_road.setdefault(row["road"], []).append(row["span"])

    for item in _active(data["items"]):
        spans = by_road.pop(item["route_name"], None)
        before = item.get("emergency_access") or {}
        if spans is None:
            if before.get("sections"):
                checks.append(
                    f"{item['route_name']}（{item['section']}）は、これまで緊急車両の"
                    f"通行可能区間があったが、この報の一覧に出てこない。"
                    f"無くなったのか、書き方が変わったのかを確認してください"
                )
            continue
        # 手元のほうが新しいときは触らない。古い報を取り直したときや、
        # 人が後から直した内容を、機械が巻き戻さないようにする。
        if (
            before.get("as_of") and report["emergency"]["as_of"]
            and before["as_of"] > report["emergency"]["as_of"]
        ):
            checks.append(
                f"{item['route_name']}（{item['section']}）は手元の内容"
                f"（{before['as_of']}時点）のほうが新しいので触っていません"
            )
            continue
        after = {
            "as_of": report["emergency"]["as_of"],
            "sections": spans,
            "planned": None,
            "note": None,
            "source": label,
        }
        if before.get("sections") != spans or before.get("as_of") != after["as_of"]:
            changes.append({
                "item": f"{item['route_name']}（{item['section']}）",
                "before": "・".join(before.get("sections") or []) or "（なし）",
                "before_as_of": before.get("as_of"),
                "after": "・".join(spans),
                "after_as_of": after["as_of"],
            })
        item["emergency_access"] = after
        reports = item.setdefault("reports", [])
        if not any(r["pdf"] == report["pdf"] for r in reports):
            reports.append({
                "label": f"{label}（自動転記）",
                "pdf": report["pdf"],
                "url": SOURCE_URL,
            })

    for road, spans in by_road.items():
        checks.append(
            f"{road} に緊急車両の通行可能区間（{'・'.join(spans)}）があるが、"
            f"対応する規制中の項目が手元に無い"
        )

    # 2. 通行止めの一覧と、手元の項目の食い違い
    listed = {(r["road"], r["span"]) for r in report["closure"]["rows"]}
    held = {(i["route_name"], i["section"]) for i in _active(data["items"])}
    for road, span in sorted(listed - held):
        checks.append(f"報にある通行止め {road} {span} が手元に無い（新規の可能性）")
    for road, span in sorted(held - listed):
        checks.append(f"手元で規制中の {road} {span} が報の一覧に無い（解除の可能性）")

    # 3. これから通行可能になる区間の告知
    for row in report.get("becoming", []):
        checks.append(
            f"告知: {row['road']} {row['span']} が "
            f"{row.get('when') or '（日時の記載を読み取れず）'} 頃に通行可能になる"
            f"（この報の一覧にはまだ入っていない）"
        )
    return changes, checks


def screen_preview(data: dict) -> list:
    """
    反映後に画面へ出る文言をそのまま並べる。地図を開けない場所からでも、
    何がどう出るのかをPRの本文だけで判断できるようにするため。
    地図と同じ関数（modules/nexco_text.py）で作っている。
    """
    lines = ["**地図の下の注記**", "", "> " + (emergency_note(data) or "（表示なし）")]
    for item in data["items"]:
        text = emergency_lines(item)
        if not text:
            continue
        lines += [
            "",
            f"**{item['route_name']}（{item['section']}）の線のツールチップ**",
            "",
        ]
        lines += ["> " + t + "  " for t in text]
    return lines


def summary_md(report: dict, changes: list, checks: list, data: dict = None) -> str:
    lines = [
        f"NEXCO西日本の**第{report['report_no']}報**（発表 {report['published_at']}）を"
        "読み取り、緊急車両の通行可能区間を反映しました。",
        "",
        "## このPRで変わること（地図に出る内容）",
        "",
    ]
    if changes:
        lines += ["| 区間 | これまで | この報 |", "| --- | --- | --- |"]
        for c in changes:
            lines.append(
                f"| {c['item']} | {c['before']}<br>（{c['before_as_of'] or '—'}時点）"
                f" | **{c['after']}**<br>（{c['after_as_of']}時点） |"
            )
    else:
        lines.append("緊急車両の通行可能区間に変更はありませんでした。")

    lines += ["", "## 報から読み取った内容", "",
              f"**緊急車両が通れる区間（{report['emergency']['as_of']}時点）**", ""]
    for r in report["emergency"]["rows"] or [{"road": "（なし）", "span": ""}]:
        lines.append(f"- {r['road']} {r['span']}")
    lines += ["", f"**通行止め（{report['closure']['as_of']}時点）**", ""]
    for r in report["closure"]["rows"] or [{"road": "（なし）", "span": ""}]:
        lines.append(f"- {r['road']} {r['span']}")

    if data is not None:
        lines += ["", "## 反映後に画面に出る文言", ""] + screen_preview(data)

    if checks:
        lines += ["", "## 判断が要るもの（自動では触っていません）", ""]
        lines += [f"- {c}" for c in checks]
    if report.get("warnings"):
        lines += ["", "## 読み取りの注意", ""]
        lines += [f"- {w}" for w in report["warnings"]]

    lines += [
        "",
        "## 確認用",
        "",
        f"- 元のPDF（NEXCO西日本のニュースリリース）: {SOURCE_URL}",
        f"- このPRに入っているPDF: {REPO_URL}/blob/auto/nexco-report/"
        f"data/nexco_west_regulations/{report['pdf'].replace(' ', '%20')}",
    ]
    if PREVIEW_URL:
        lines += [
            f"- **このPRの内容を映した確認用アプリ**: {PREVIEW_URL}",
            "  マージ前の状態が見られます（このブランチを映しています）",
        ]
    else:
        lines += [
            "- 確認用アプリ（マージ前の状態を映すもの）は未設定です。"
            "Streamlit Cloudでこのブランチのアプリを作り、そのURLを"
            "リポジトリ変数 `PREVIEW_APP_URL` に入れると、ここに出ます。"
            "上の「反映後に画面に出る文言」で判断することもできます。",
        ]
    lines += [
        f"- 公開中の地図（**マージ後**に数分で更新されます）: {APP_URL}",
        "",
        "自動で書き換えているのは緊急車両の通行可能区間だけです。"
        "通行止めそのものの追加・解除は、開始/終了時刻や線形の判断が要るため"
        "手作業に残しています。",
        "",
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--summary")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    report = parse(args.pdf)
    data = json.load(open(JSON_PATH, encoding="utf-8"))
    changes, checks = apply_report(report, data)

    if not args.dry_run:
        data["transcribed_at"] = (report["published_at"] or "")[:10]
        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    md = summary_md(report, changes, checks, data)
    if args.summary:
        with open(args.summary, "w", encoding="utf-8") as f:
            f.write(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
