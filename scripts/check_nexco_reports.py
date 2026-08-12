"""
NEXCO西日本のトップページを見て、熊本地震の新しい報が出ていれば取ってくる。

規制の中身（区間・日時・緊急車両の通行可能区間）は、PDFを読んで人が
`data/nexco_regulations.json` に転記している。ここで自動化するのは
「新しい報が出たことに気づく」ところまでで、転記はしない。
取り違えたまま自動で公開されるほうが、気づくのが遅れるより害が大きい。

    python scripts/check_nexco_reports.py            # 確認してPDFを保存
    python scripts/check_nexco_reports.py --dry-run  # 確認だけ

新しい報があれば、保存したファイル名を標準出力に出し、GitHub Actions
から使えるよう $GITHUB_OUTPUT にも書く（new_count / new_list）。
"""
import argparse
import os
import re
import sys
import time
import urllib.parse
import urllib.request

TOP_URL = "https://www.w-nexco.co.jp/"
SAVE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "nexco_west_regulations",
)
# 熊本地震の報だけを拾う（台風や他路線の工事のお知らせは対象外）
TITLE_PATTERN = re.compile(r"熊本地震")
LINK_PATTERN = re.compile(
    r'<a[^>]+href="([^"]+\.pdf)"[^>]*>(.*?)</a>', re.S | re.I
)
# 「（第15報）」「（第13 報）」のような表記ゆれがある
REPORT_NO = re.compile(r"第\s*(\d+)\s*報")
USER_AGENT = (
    "kumamoto-earthquake-traffic-map/1.0 "
    "(+https://github.com/centrixuge/kumamoto-earthquake-traffic-map)"
)


def fetch(url: str, binary: bool = False):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as res:
        body = res.read()
    return body if binary else body.decode("utf-8", "replace")


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # 末尾の「（1MB）」のようなファイルサイズを落とす
    return re.sub(r"（[\d.]+[KM]B）\s*$", "", text).strip()


def listed_reports(html: str) -> list:
    """トップページから、熊本地震のPDFの（URL, 表題, 報番号）を拾う。"""
    found, seen = [], set()
    for m in LINK_PATTERN.finditer(html):
        href, title = m.group(1), clean(m.group(2))
        if not TITLE_PATTERN.search(title):
            continue
        url = urllib.parse.urljoin(TOP_URL, href)
        if url in seen:
            continue
        seen.add(url)
        no = REPORT_NO.search(title)
        found.append({
            "url": url,
            "title": title,
            "no": int(no.group(1)) if no else None,
        })
    return found


def local_report_numbers(save_dir: str) -> set:
    """手元にあるPDFの報番号。ファイル名の表記ゆれを吸収して数字だけ見る。"""
    numbers = set()
    for name in os.listdir(save_dir):
        if not name.lower().endswith(".pdf"):
            continue
        m = REPORT_NO.search(name)
        if m:
            numbers.add(int(m.group(1)))
    return numbers


def safe_name(title: str) -> str:
    """表題をそのままファイル名にする（既存のPDFと同じ付け方）。"""
    name = re.sub(r'[\\/:*?"<>|]', "_", title).strip()
    return f"{name}.pdf"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    html = fetch(TOP_URL)
    listed = listed_reports(html)
    have = local_report_numbers(SAVE_DIR)
    print(f"ページ上の熊本地震のPDF {len(listed)}件 / 手元の報 {sorted(have)}")

    new, saved = [], []
    for item in listed:
        if item["no"] is None:
            # 報番号の無いお知らせ（お盆の交通混雑など）は対象外
            print(f"  [skip] 報番号なし: {item['title'][:48]}")
            continue
        if item["no"] in have:
            continue
        new.append(item)

    if not new:
        print("新しい報はありません。")
    for item in sorted(new, key=lambda x: x["no"]):
        path = os.path.join(SAVE_DIR, safe_name(item["title"]))
        print(f"  [新] 第{item['no']}報: {item['title'][:60]}")
        print(f"       {item['url']}")
        if args.dry_run:
            continue
        data = fetch(item["url"], binary=True)
        with open(path, "wb") as f:
            f.write(data)
        saved.append(path)
        print(f"       -> {os.path.relpath(path)} ({len(data) // 1024}KB)")
        time.sleep(1)   # 続けて取りに行かない

    # 保存したPDFのパスを残す。取り直したものが既存と同一バイトだと
    # git status には出ないので、後の処理はこのファイルを見る。
    with open(os.path.join(SAVE_DIR, os.pardir, os.pardir, "new_reports.txt"),
              "w", encoding="utf-8") as f:
        # 改行はLF固定。Windowsで作ってもLinux側のシェルがそのまま読める
        f.write("\n".join(saved))

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"new_count={len(new)}\n")
            f.write("new_list=" + " / ".join(
                f"第{i['no']}報" for i in sorted(new, key=lambda x: x["no"])
            ) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
