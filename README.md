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
| 地図・時系列 | 観測点別の異常度（zスコア）と通行規制情報を地図上に重ね合わせ、選択した観測点（最大2地点）の時系列（平常時 vs 観測実績）を並べて表示 |
| 異常検知一覧 | 地震発生後に検知された異常のテーブルとCSVダウンロード |
| データダウンロード | アーカイブ済みの交通量（5分値・1時間値）と通行規制（一覧・状態変化履歴）をCSVで取得。各列の意味・単位をまとめた**列定義書**もCSVでダウンロードできます（実データの列と自動照合し、`in_actual_csv` 列で「その列が実際に配布中のCSVに入っているか」が分かります。食い違いはエラーではなく起こりうるものなので、理由の分かる備考として画面に表示します） |

## データソース

| データ | 提供元 | 補足 |
| --- | --- | --- |
| 常設トラカン5分間交通量／1時間交通量 | [JARTIC 交通量オープンデータ](https://www.jartic-open-traffic.org/) | [WFS APIの仕様書(PDF)](https://www.jartic-open-traffic.org/action_method.pdf)に基づき取得。認証・APIキー不要。5分値は過去1ヶ月・1時間値は過去3ヶ月まで遡れる。観測点は両者で同一（7点） |
| 地震情報（震源・マグニチュード・市町村別最大震度） | 気象庁が公開している防災情報JSON (`https://www.jma.go.jp/bosai/quake/data/list.json`) | 公式なAPI仕様として文書化されたものではなく、気象庁ウェブサイトの表示に使われている公開JSONを利用（多くの防災アプリ・サイトで実利用されている形式） |
| 道路通行規制情報（区間・時間帯・規制理由） | 熊本県「[防災情報くまもと](https://portal.bousai.pref.kumamoto.jp/)」の[通行規制情報](https://portal.bousai.pref.kumamoto.jp/?p=traffic)ページが使う公開JSON (`https://portal.bousai.pref.kumamoto.jp/data/traffic/traffic.json`) | 認証不要。始点・終点の座標のみのため、[OSRM](https://project-osrm.org/) の公開デモサーバーで実際の道路網に沿った経路にスナップして地図に表示 |

## 手法・注意点

- **平常時（ベースライン）**: 本震と同じ曜日（火）を8週分さかのぼった1時間交通量。観測点×時刻(hour)ごとに平均・標準偏差を求めます。5分間値は過去1ヶ月しか遡れないのに対し1時間値は3ヶ月遡れるため、母集団を広く取れる1時間値を採用しています。**祝日は交通量の傾向が平日と異なるため、[内閣府の祝日CSV](https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv)を参照して母集団から除外します**（該当日がなければ8日分そのまま。除外内容は実行ログに出ます）
- **異常検知**: 1時間値どうしの比較で定義します。1時間値の実績と上記の平常時の平均・標準偏差から `|zスコア| >= 2` を異常としてフラグ。**地図の色分けと異常検知一覧もこの1時間値ベースの判定に基づきます**
- **時系列の2ビュー**: 実績の既定は5分間値です
  - `5分間値（既定）`: 実績は5分間値。平常時は1時間値ベースなので単位を合わせるため1/12しています。**この帯は「平常時の1時間あたり交通量の日々のばらつき÷12」であり、5分間値そのもののばらつきではないため狭く出ます**（5分間値どうしで求めたσ 9.2 に対し 3.2 程度）
  - `1時間値（参考）`: 実績・平常時とも1時間値どうしの比較。異常検知の判定もこの粒度です
- **観測点のID**: JARTICの「常時観測点コード」（例 `9110030`）を観測点の識別子として使い、地図・セレクタ・グラフ凡例・CSVで共通にしています。アーカイブにはこの列を持たない時期のデータも含まれるため、`data/stations.json`（コードと緯度経度の対応表）を経由して後から付け直しています（座標はAPIの応答で末尾の桁がわずかに揺れるため、5桁≒1mに丸めて突き合わせ）
- **揺れの強さの代理指標**: 気象庁の推計震度分布データの取得が難しいため、**観測点と震源との距離[km]** を揺れの強さの簡易的な代理指標として使用しています。実際の震度分布とは異なる場合があります
- 対象エリア・期間・ベースライン期間は [`fetch_and_prepare.py`](fetch_and_prepare.py) の定数（`BBOX`, `TARGET_START`, `HOURLY_BASELINE_WEEKS`, `MAINSHOCK_EID`）で固定値になっています。別の地震・地域で使う場合はここを書き換えてください
- **動的取得期間**: 対象期間の終端は「本震発生時刻 + `RECOVERY_PERIOD`（既定2週間）」まで動的に伸びていきます。被災後72時間はもちろん、交通が平常に戻っていく復旧期の推移まで追えるようにするためです

## データのアーカイブについて

JARTIC交通量オープンデータの5分値は**過去1ヶ月分しか遡って取得できません**（[公式サイト](https://www.jartic-open-traffic.org/)）。この制約に対応するため、`fetch_and_prepare.py` は取得した生データを `data/archive/traffic_raw.parquet` に**追記専用（削除・上書きなし）**で蓄積します。

- `target.parquet`/`baseline.parquet`/`observations.parquet`/`quake_info.json` はこのアーカイブから毎回再生成される「現在時点のビュー」で、上書きされて構いません
- 実行のたびに、アーカイブ済みの範囲は再取得せず、まだ取得していない範囲だけをAPIに問い合わせます（APIへの負荷軽減、かつ既存データを失わない）
- GitHub Actions（後述）で定期実行することで、復旧期（本震+2週間）が終わるまで自動的にアーカイブが育っていきます
- 1時間値も同様に `data/archive/traffic_hourly.parquet` に蓄積します（1時間値は3ヶ月遡れる）

### 通行規制情報のアーカイブ

通行規制も**解除されるとポータルの一覧から消えて、後から取得できません**。そのため `data/archive/regulations_archive.json` に**追記専用**で蓄積します（削除は一切しません）。

- 各規制は「路線名・区間の始終点座標・規制開始日時」をキーに識別します（規制内容や終了日時は途中で変わるためキーに含めません）
- `first_seen` / `last_seen`（初めて／最後に確認した日時）と `still_listed`（まだポータルに載っているか）を保持するので、一覧から消えた後も「いつからいつまで、どういう規制だったか」を追えます
- 規制内容や終了日時が変わったときだけ `history` に1行追記します（全面通行止め → 片側交互通行止め → 解除 といった推移が残ります）
- スナップ済みの経路はアーカイブから再利用するため、定期実行のたびに全件をOSRMの公開デモサーバーへ投げ直しません
- ポータルからの取得に失敗した回は、**アーカイブを一切変更しません**（取得できなかったことを「一覧から消えた」と誤判定しないため）
- `data/regulations.json` は地図描画用の「現在のスナップショット」で、こちらは毎回上書きされます

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
  ├─ modules/holidays.py         … 内閣府の祝日CSV取得（平常時から祝日を除くため）
  ├─ modules/stations.py         … 常時観測点コードと緯度経度の対応（観測点マスタ）
  └─ modules/road_regulations.py … 「防災情報くまもと」の通行規制情報取得＋OSRM道路スナップ
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
data/archive/                  恒久アーカイブ（追記専用）: 5分値・1時間値・通行規制
data/                          前処理済みデータ（parquet/json）のスナップショット
.github/workflows/             定期自動取得（GitHub Actions）
requirements.txt               app.py 実行用の軽量な依存関係
requirements-fetch.txt         fetch_and_prepare.py 実行用の追加依存関係（geopandas等）
```

## ライセンス・免責

- 本リポジトリのコードはMITライセンスとします
- 各データの著作権・利用条件は提供元（JARTIC、気象庁、国土交通省）の利用規約に従います
- 本ダッシュボードは研究・情報共有目的の簡易分析であり、防災・避難行動の判断材料として公式に保証するものではありません
