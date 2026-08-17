"""
高速道路の「緊急車両が通れる区間」について、画面に出す文言を作る。

地図のツールチップ（app.py）と、新しい報を取り込むときのPRの本文
（scripts/apply_nexco_report.py）で同じ文言を使うための置き場所。
別々に書くと、PRで見た内容と地図に出る内容がずれていく。

Streamlitやfoliumには依存しない（GitHub Actions側から呼ぶため）。
"""


def emergency_lines(item: dict) -> list:
    """1件ぶんの説明。地図のツールチップと同じ並び・同じ文言。"""
    ea = item.get("emergency_access")
    if not ea or not ea.get("sections"):
        return []
    lines = [
        f"緊急車両は {'・'.join(ea['sections'])} で通行可"
        f"（{ea['as_of']}時点）"
    ]
    for extra in (ea.get("planned"), ea.get("note")):
        if extra:
            lines.append(extra)
    if ea.get("source"):
        lines.append(f"出典: {ea['source']}")
    return lines


def emergency_note(nexco: dict) -> str:
    """地図の下に出す1行。該当が無ければ空文字。"""
    items = [
        i for i in (nexco or {}).get("items", [])
        if (i.get("emergency_access") or {}).get("sections")
        and not i.get("end_timestamp")
    ]
    if not items:
        return ""
    as_of = sorted({i["emergency_access"]["as_of"] for i in items})[-1]
    # 通れる範囲は、通行止め区間の全部のこともあれば一部のこともある。
    # 「一部で」と決め打ちにすると、全線が通れるようになったときに誤りになる。
    detail = "／".join(
        f"{i['route_name']}は{'・'.join(i['emergency_access']['sections'])}"
        for i in items
    )
    return (
        f"**規制中の高速道路{len(items)}区間では、緊急車両の通行ができます**"
        f"（{as_of}時点）。{detail}。"
        "一般車両は通行できません。通れる範囲が通行止めの区間と一致しないこと"
        "があるため線では描き分けず、線にマウスを載せると出るツールチップに"
        "区間名と出典の報を出しています。"
    )
