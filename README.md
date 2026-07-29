# 熊本地震・交通行動変容ダッシュボード (kumamoto-earthquake-traffic-map)

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://kumamoto-earthquake-traffic-map.streamlit.app/)

**🔗 公開URL: https://kumamoto-earthquake-traffic-map.streamlit.app/**

2026年7月28日16:27頃に熊本地方で発生した地震（M7.1・最大震度7）の前後で、交通量オープンデータにどのような変化（行動変容）が見られたかを可視化する簡易ダッシュボードです。

- 交通量データと地震情報を重ね合わせ、平常時との比較による**簡易異常検知**を行います
- 観測点をクリックして時系列を比較できるインタラクティブな地図を備えます（最大2地点を選択して比較可能）

## デモ

上記の公開URLからブラウザで直接見られます。ローカルで動かす場合は以下の「セットアップ」を参照してください。

## できること

| タブ | 内容 |
| --- | --- |
| 地図・時系列 | 観測点別の異常度（zスコア）を地図上に表示し、クリックした観測点（最大2地点）の時系列（平常時帯 vs 実測）を並べて表示 |
| 震源距離との相関 | 震源からの距離と異常度（\|zスコア\|）の散布図 |
| 異常検知一覧 | 地震発生後に検知された異常のテーブルとCSVダウンロード |

## データソース

| データ | 提供元 | 補足 |
| --- | --- | --- |
| 常設トラカン5分間交通量 | [JARTIC 交通量オープンデータ](https://www.jartic-open-traffic.org/) | [WFS APIの仕様書(PDF)](https://www.jartic-open-traffic.org/action_method.pdf)に基づき取得。認証・APIキー不要 |
| 地震情報（震源・マグニチュード・市町村別最大震度） | 気象庁が公開している防災情報JSON (`https://www.jma.go.jp/bosai/quake/data/list.json`) | 公式なAPI仕様として文書化されたものではなく、気象庁ウェブサイトの表示に使われている公開JSONを利用（多くの防災アプリ・サイトで実利用されている形式） |
| 道路通行規制情報（区間・時間帯・規制理由） | [熊本県防災ポータル](https://portal.bousai.pref.kumamoto.jp/) の公開JSON (`https://portal.bousai.pref.kumamoto.jp/data/traffic/traffic.json`) | 認証不要。始点・終点の座標のみのため、[OSRM](https://project-osrm.org/) の公開デモサーバーで実際の道路網に沿った経路にスナップして地図に表示 |

## 手法・注意点

- **異常検知**: 観測点×時刻(hour)ごとに、平常時（地震前2週間の同曜日ペア、2026-07-14/15・07-21/22）の交通量の平均・標準偏差を算出し、地震発生後の実測値とのzスコアを計算。`|zスコア| >= 2` を異常候補としてフラグ
- **揺れの強さの代理指標**: 気象庁の推計震度分布データの取得が難しいため、**観測点と震源との距離[km]** を揺れの強さの簡易的な代理指標として使用しています。実際の震度分布とは異なる場合があります
- 対象エリア・期間・ベースライン期間は [`fetch_and_prepare.py`](fetch_and_prepare.py) の定数（`BBOX`, `TARGET_START`, `BASELINE_WINDOWS`, `MAINSHOCK_EID`）で固定値になっています。別の地震・地域で使う場合はここを書き換えてください
- **動的取得期間**: 対象期間の終端は「本震発生時刻 + `RECOVERY_PERIOD`（既定2週間）」まで動的に伸びていきます。被災後72時間はもちろん、交通が平常に戻っていく復旧期の推移まで追えるようにするためです

## データのアーカイブについて

JARTIC交通量オープンデータの5分値は**過去1ヶ月分しか遡って取得できません**（[公式サイト](https://www.jartic-open-traffic.org/)）。この制約に対応するため、`fetch_and_prepare.py` は取得した生データを `data/archive/traffic_raw.parquet` に**追記専用（削除・上書きなし）**で蓄積します。

- `target.parquet`/`baseline.parquet`/`observations.parquet`/`quake_info.json` はこのアーカイブから毎回再生成される「現在時点のビュー」で、上書きされて構いません
- 実行のたびに、アーカイブ済みの範囲は再取得せず、まだ取得していない範囲だけをAPIに問い合わせます（APIへの負荷軽減、かつ既存データを失わない）
- GitHub Actions（後述）で定期実行することで、復旧期（本震+2週間）が終わるまで自動的にアーカイブが育っていきます

### 自動取得（GitHub Actions）

[`.github/workflows/fetch-and-archive.yml`](.github/workflows/fetch-and-archive.yml) が6時間ごとに `fetch_and_prepare.py` を実行し、`data/` 配下に変更があれば自動でコミット・pushします。手動で今すぐ実行したい場合は GitHub の Actions タブから `workflow_dispatch` で起動できます。

復旧期（本震から2週間）を過ぎるとフェッチ対象がなくなり実質no-opになりますが、ワークフロー自体は動き続けます。不要になったら GitHub の Actions 設定からスケジュールを無効化してください。

## アーキテクチャ

```
fetch_and_prepare.py   … オフラインのデータ取得・前処理（geopandas/shapely使用）
  ├─ modules/api_request_func.py … JARTIC WFS APIリクエスト
  ├─ modules/aggregation.py      … GeoJSON→GeoDataFrame変換
  ├─ modules/earthquake_data.py  … 気象庁地震情報の取得
  ├─ modules/anomaly.py          … 平常時ベースラインとのzスコア計算
  └─ modules/road_regulations.py … 熊本県防災ポータルの通行規制情報取得＋OSRM道路スナップ
       ↓ 追記
data/archive/traffic_raw.parquet  … 恒久アーカイブ（削除・上書きなし）
       ↓ 切り出して生成
data/*.parquet, data/quake_info.json  … 前処理済みの軽量データ（現在時点のビュー）
       ↓ 読み込みのみ
app.py                  … Streamlitダッシュボード（GDAL依存なし、pandas/plotly/foliumのみ）
```

`app.py` は前処理済みデータを読み込むだけの薄いビュー層です。これにより、Streamlit Community Cloud等のGDALが使えない/使いにくい環境でも動かせるようにしています。重い依存（geopandas/shapely/pyproj）は `fetch_and_prepare.py` の実行時（ローカル）にのみ必要です。

## セットアップ

### 1. ダッシュボードを見るだけの場合

```bash
pip install -r requirements.txt
streamlit run app.py
```

`data/` 配下に本リポジトリ同梱の前処理済みデータ（2026-07-28 19時頃取得時点のスナップショット）が入っているため、これだけで起動できます。

### 2. データを最新化する場合

```bash
pip install -r requirements.txt -r requirements-fetch.txt
python fetch_and_prepare.py
```

## ディレクトリ構成

```
app.py                        Streamlitダッシュボード本体
fetch_and_prepare.py           データ取得・前処理スクリプト
modules/                       データ取得・変換・異常検知ロジック
data/archive/traffic_raw.parquet  恒久アーカイブ（追記専用）
data/                          前処理済みデータ（parquet/json）のスナップショット
.github/workflows/             定期自動取得（GitHub Actions）
requirements.txt               app.py 実行用の軽量な依存関係
requirements-fetch.txt         fetch_and_prepare.py 実行用の追加依存関係（geopandas等）
```

## ライセンス・免責

- 本リポジトリのコードはMITライセンスとします
- 各データの著作権・利用条件は提供元（JARTIC、気象庁、国土交通省）の利用規約に従います
- 本ダッシュボードは研究・情報共有目的の簡易分析であり、防災・避難行動の判断材料として公式に保証するものではありません
