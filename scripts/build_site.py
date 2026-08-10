"""
GitHub Pages 用の解説ページ（site/index.html）を data/ から生成する。

なぜ必要か:
  Streamlit Community Cloud のアプリは、GitHub からのリンクがすべて
  rel="nofollow" で、学会PDFのURLもリンク注釈を持たない文字列のため、
  検索エンジンがアプリへ辿り着く経路が1本も無い状態だった。
  GitHub Pages は自分のドメインなので、
    - アプリへのリンクに nofollow が付かない（クロール経路ができる）
    - robots.txt / sitemap.xml / メタ情報を自分で置ける
    - Google Search Console の所有権確認ができる
  という点で、この問題を解消できる唯一の置き場所になる。

掲載する数値はすべて data/ から計算する。手で書くと、収集が進むたびに
ページの数字だけが古くなるため。

    python scripts/build_site.py
"""
import html
import json
import os
from datetime import datetime, timedelta, timezone

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "site")

APP_URL = "https://kumamoto-earthquake-traffic-map.streamlit.app/"
REPO_URL = "https://github.com/centrixuge/kumamoto-earthquake-traffic-map"
SITE_URL = "https://centrixuge.github.io/kumamoto-earthquake-traffic-map/"
JST = timezone(timedelta(hours=9))


def _read(name):
    path = os.path.join(DATA, name)
    if name.endswith(".parquet"):
        df = pd.read_parquet(path)
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"])
        return df
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def collect() -> dict:
    obs = _read("observations_hourly.parquet")
    raw = _read(os.path.join("archive", "traffic_raw.parquet"))
    hourly = _read(os.path.join("archive", "traffic_hourly.parquet"))
    quake = _read("quake_info.json")
    mlit = _read("mlit_regulations.json")
    reg_archive = _read(os.path.join("archive", "regulations_archive.json"))

    main = quake["mainshock"]
    quake_at = pd.Timestamp(main["occurred_at"]).tz_localize(None)
    post = obs[obs["datetime"] >= quake_at]

    def ratio(kind: str):
        """地震後の実績合計 ÷ 同じ日区分・時刻の平常時合計。"""
        cols = [(f"traffic_{d}_{kind}", f"baseline_mean_{d}_{kind}") for d in ("up", "down")]
        act = base = 0.0
        for a, b in cols:
            sub = post[[a, b]].dropna()
            act += sub[a].sum()
            base += sub[b].sum()
        return 100 * act / base if base else float("nan")

    types = obs.groupby("point_code")["road_type"].first()
    return {
        "magnitude": main["magnitude"],
        "intensity": main["max_intensity"],
        "quake_at": quake_at,
        "n_events": len(quake.get("events", [])),
        "n_points": int(obs["point_code"].nunique()),
        "n_general": int((types == "3").sum()),
        "n_express": int((types == "1").sum()),
        "n_5min": len(raw),
        "n_hourly": len(hourly),
        "archive_from": raw["datetime"].min(),
        "archive_to": raw["datetime"].max(),
        "n_anomaly": int(obs["is_anomaly"].sum()),
        "n_reg": len(reg_archive["items"]),
        "n_mlit": len(mlit["items"]),
        "ratio_small": ratio("small"),
        "ratio_large": ratio("large"),
        "generated_at": datetime.now(JST),
    }


CSS = """
:root{--fg:#1b1f23;--muted:#5b6570;--bg:#ffffff;--line:#e3e6ea;--accent:#0b5cab;--card:#f6f8fa}
@media(prefers-color-scheme:dark){
  :root{--fg:#e6edf3;--muted:#9198a1;--bg:#0d1117;--line:#30363d;--accent:#6cb0ff;--card:#161b22}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,"Hiragino Kaku Gothic ProN","Noto Sans JP",Meiryo,sans-serif;
  line-height:1.75;font-size:16px}
main{max-width:820px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:1.7rem;line-height:1.35;margin:0 0 8px}
h2{font-size:1.2rem;margin:38px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:1.02rem;margin:22px 0 6px}
p,li{margin:0 0 10px}
a{color:var(--accent)}
.lead{font-size:1.02rem;color:var(--fg)}
.meta{color:var(--muted);font-size:.86rem;margin:0 0 22px}
.cta{display:inline-block;margin:6px 0 4px;padding:11px 20px;border-radius:7px;
  background:var(--accent);color:#fff;text-decoration:none;font-weight:700}
.cta:hover{opacity:.88}
.sub{color:var(--muted);font-size:.86rem;margin:6px 0 0}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:.93rem;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
th{background:var(--card);white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:7px;padding:11px 13px}
.stat b{display:block;font-size:1.28rem;line-height:1.3}
.stat span{color:var(--muted);font-size:.8rem}
.note{background:var(--card);border-left:3px solid var(--accent);padding:11px 14px;margin:14px 0;
  font-size:.93rem;border-radius:0 5px 5px 0}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--muted);font-size:.83rem}
code{background:var(--card);padding:1px 5px;border-radius:4px;font-size:.9em}
"""


def render(d: dict) -> str:
    e = html.escape
    desc = (
        f"2026年7月28日の熊本地震（M{d['magnitude']}・最大震度{d['intensity']}）の前後で、"
        f"交通量が平常時と比べてどう変わったかを常時観測点{d['n_points']}点ごとに可視化した"
        "ダッシュボードです。JARTIC交通量オープンデータ・気象庁地震情報・通行規制情報を"
        "重ね合わせ、平常時との差を曜日区分をそろえて比較します。データはCSVで取得できます。"
    )
    jsonld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebApplication",
                "name": "熊本地震（2026-07-28）交通量変化ダッシュボード",
                "url": APP_URL,
                "applicationCategory": "BrowserApplication",
                "operatingSystem": "Any",
                "inLanguage": "ja",
                "description": desc,
                "isAccessibleForFree": True,
                "license": "https://opensource.org/licenses/MIT",
                "codeRepository": REPO_URL,
            },
            {
                "@type": "Dataset",
                "name": "熊本地震前後の常時観測点交通量アーカイブ",
                "description": (
                    f"JARTIC交通量オープンデータから取得した熊本県内の常時観測点"
                    f"{d['n_points']}点の5分間交通量・1時間交通量（追記専用アーカイブ、"
                    f"{d['n_5min']:,}行）と、通行規制の状態変化履歴。"
                ),
                "url": SITE_URL,
                "inLanguage": "ja",
                "license": "https://opensource.org/licenses/MIT",
                "creator": {"@type": "Person", "name": "centrixuge"},
                "temporalCoverage": (
                    f"{d['archive_from']:%Y-%m-%d}/{d['archive_to']:%Y-%m-%d}"
                ),
                "spatialCoverage": {"@type": "Place", "name": "熊本県"},
                "distribution": {
                    "@type": "DataDownload",
                    "encodingFormat": "text/csv",
                    "contentUrl": APP_URL,
                },
            },
        ],
    }

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>熊本地震（2026-07-28）交通量変化ダッシュボード｜JARTIC交通量オープンデータによる可視化</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{SITE_URL}">
<meta property="og:type" content="website">
<meta property="og:locale" content="ja_JP">
<meta property="og:title" content="熊本地震（2026-07-28）交通量変化ダッシュボード">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{SITE_URL}">
<meta name="twitter:card" content="summary">
<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>
<style>{CSS}</style>
</head>
<body>
<main>

<h1>熊本地震（2026-07-28）交通量変化ダッシュボード</h1>
<p class="meta">最終更新 {d['generated_at']:%Y-%m-%d %H:%M} JST ／ 作成 <a href="https://github.com/centrixuge">centrixuge</a></p>

<p class="lead">2026年7月28日16:27頃に熊本地方で発生した地震（M{d['magnitude']}・最大震度{d['intensity']}）の前後で、
交通量が平常時と比べてどう変わったかを、常時観測点ごとに可視化した公開ダッシュボードです。
交通量・地震情報・通行規制をすべて公開データだけで組み合わせ、発災当日から記録を残しています。</p>

<p><a class="cta" href="{APP_URL}">ダッシュボードを開く</a></p>
<p class="sub">ブラウザだけで動きます。インストール・登録は不要です。</p>

<div class="grid">
  <div class="stat"><b>{d['n_points']}点</b><span>常時観測点（一般国道{d['n_general']}・高速{d['n_express']}）</span></div>
  <div class="stat"><b>{d['n_5min']:,}行</b><span>5分間交通量のアーカイブ</span></div>
  <div class="stat"><b>{d['n_hourly']:,}行</b><span>1時間交通量のアーカイブ</span></div>
  <div class="stat"><b>{d['n_reg']}件</b><span>記録した通行規制</span></div>
</div>
<p class="sub">収録期間 {d['archive_from']:%Y-%m-%d %H:%M} 〜 {d['archive_to']:%Y-%m-%d %H:%M}（JST）。
6時間ごとに自動更新しています。</p>

<h2>このダッシュボードで分かること</h2>
<ul>
<li><b>観測点ごとの交通量が平常時からどれだけ外れたか</b>。地図上でzスコアの大きさを色と大きさで示し、地震発生後に{d['n_anomaly']:,}件の異常を検知しています</li>
<li><b>選んだ観測点の時系列</b>。平常時の水準と実測を重ねて表示し、最大2地点を並べて比較できます。5分間値・1時間値・車種別（小型／大型）を切り替えられます</li>
<li><b>通行規制との重ね合わせ</b>。交通量が落ちた時間帯に、その場所でどんな規制がかかっていたかを地図上で確認できます</li>
<li><b>元データのダウンロード</b>。交通量・通行規制・異常検知の入力データをCSVで取得できます。各列の意味をまとめた列定義書もExcelで配布しています</li>
</ul>

<h2>データから見えたこと</h2>

<h3>車種によって動きが逆でした</h3>
<p>地震後の交通量を、同じ日区分・同じ時刻の平常時と比べると、
小型車は平常時の<b>{d['ratio_small']:.1f}%</b>にとどまる一方、
大型車は<b>{d['ratio_large']:.1f}%</b>と平常時を上回って推移しています。
合計交通量だけを見ると互いに打ち消し合って見えません。</p>

<h3>規制の記録と交通量が一致しました</h3>
<p>九州中央自動車道（小池高山IC〜山都通潤橋IC）は本震直後から翌03:00まで全面通行止めでしたが、
該当区間の観測点では19:00〜02:00の交通量が0〜3台（平常時は7〜137台）まで落ち、
規制解除の時刻とともに回復しています。</p>

<h3>曜日をそろえないと8割が「異常」になります</h3>
<p>平常時を火曜日だけで定義して評価すると、地震と無関係な平常時の土日でも8割の行が異常判定になりました。
曜日区分ごとに平常時を分けたうえで検証すると、誤検知は71%から12%に下がっています。</p>

<h2>手法</h2>
<p><b>平常時（ベースライン）</b>は日区分ごとに別々の1時間交通量から作ります。
月・火・水・木・金はそれぞれの曜日で、土は土曜、日曜と祝日は「日祝」としてまとめ、各8日分を母集団とします。
祝日は曜日によらず日祝に入れます。1日の区切りは深夜帯を分断しないよう03:00起点です。</p>
<p><b>異常検知</b>は、実測と同じ日区分・同じ時刻の平常時の平均・標準偏差から
zスコアを求め、絶対値が2以上を異常とします。判定は1時間値どうしの比較で行います。</p>

<div class="note"><b>異常検知は原因を特定するものではありません。</b>
「平常時からの統計的な外れ」を機械的に拾っているだけで、
通行規制・迂回・自主的な外出抑制・イベント・天候・観測機器の不調などが区別なく含まれます。
観測点は{d['n_points']}点しかなく、エリア全体の交通を代表するものでもありません。</div>

<h2>データと出典</h2>
<table>
<tr><th>データ</th><th>提供元</th></tr>
<tr><td>常時観測点の5分間交通量・1時間交通量</td>
    <td><a href="https://www.jartic-open-traffic.org/">JARTIC 交通量オープンデータ</a>（交通量API機能）</td></tr>
<tr><td>地震情報（震源・マグニチュード・市町村別最大震度）</td>
    <td>気象庁の防災情報JSON（最大震度5弱以上{d['n_events']}件を記録）</td></tr>
<tr><td>通行規制情報（県・市町村が管理する道路）</td>
    <td>熊本県「<a href="https://portal.bousai.pref.kumamoto.jp/">防災情報くまもと</a>」</td></tr>
<tr><td>通行規制情報（直轄国道・高規格道路）</td>
    <td>国土交通省 九州地方整備局 <a href="https://www.qsr.mlit.go.jp/kumamoto/">熊本河川国道事務所</a>の公表PDFから転記（{d['n_mlit']}件）</td></tr>
</table>

<p>交通量の5分値は過去1ヶ月、1時間値は過去3ヶ月しか遡れず、通行規制は解除されるとポータルの一覧から消えます。
そのため取得した生データを<b>追記専用でアーカイブ</b>し、後から遡って検証できるようにしています。</p>

<div class="note">このサービスは、交通量API 機能を使用していますが、サービスの内容は国土交通省によって保証されたものではありません。
掲載している交通量は「国土交通省API 機能による交通量(参考値)を加工して作成」したものです。
本サービスの作成・運営について、<a href="https://github.com/centrixuge">作成者</a>が一切の責任を負います。</div>

<h2>ソースコードと詳しい資料</h2>
<ul>
<li><a href="{REPO_URL}">GitHubリポジトリ</a>（MITライセンス）</li>
<li><a href="{REPO_URL}/blob/main/docs/methods.md">手法と注意点</a> — 平常時の定義、異常検知、時系列の見方、制約</li>
<li><a href="{REPO_URL}/blob/main/docs/regulations.md">通行規制データの欠落について</a> — 直轄国道が県のデータに載らない理由と転記の手順</li>
<li><a href="{REPO_URL}/blob/main/docs/data-pipeline.md">データの取得とアーカイブ</a> — APIの利用条件への対応、自動取得の仕組み</li>
</ul>

<footer>
<p>本ダッシュボードは研究・情報共有目的の簡易分析であり、防災・避難行動の判断材料として公式に保証するものではありません。
各データの著作権・利用条件は提供元の利用規約に従います。</p>
<p><a href="{APP_URL}">ダッシュボードを開く</a> ／ <a href="{REPO_URL}">GitHub</a></p>
</footer>

</main>
</body>
</html>
"""


def main() -> None:
    d = collect()
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(render(d))
    # Jekyll に処理させない（素のHTMLをそのまま配信する）
    open(os.path.join(OUT, ".nojekyll"), "w").close()
    with open(os.path.join(OUT, "robots.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}sitemap.xml\n")
    with open(os.path.join(OUT, "sitemap.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"  <url>\n    <loc>{SITE_URL}</loc>\n"
            f"    <lastmod>{d['generated_at']:%Y-%m-%d}</lastmod>\n"
            "    <changefreq>daily</changefreq>\n  </url>\n</urlset>\n"
        )
    print(f"site/index.html を生成しました（観測点{d['n_points']}点 / "
          f"5分値{d['n_5min']:,}行 / 更新 {d['generated_at']:%Y-%m-%d %H:%M}）")


if __name__ == "__main__":
    main()
