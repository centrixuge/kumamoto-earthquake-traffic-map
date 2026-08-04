# データの取得・アーカイブ・構成

[README](../README.md) から分割したページです。取れなくなる前に残すための追記専用アーカイブと、その自動取得、コードの構成を説明します。

## データのアーカイブについて

JARTIC交通量オープンデータの5分値は**過去1ヶ月分しか遡って取得できません**（[公式サイト](https://www.jartic-open-traffic.org/)）。この制約に対応するため、`fetch_and_prepare.py` は取得した生データを `data/archive/traffic_raw.parquet` に**追記専用（削除・上書きなし）**で蓄積します。

- `target.parquet`/`baseline.parquet`/`observations*.parquet`/`quake_info.json` はこのアーカイブから毎回再生成される「現在時点のビュー」で、上書きされて構いません
- 実行のたびに、アーカイブ済みの**コマ（5分間値なら1コマ=5分）と期待されるコマの並びを突き合わせ、まだ持っていないコマだけ**をAPIに問い合わせます。1コマ=1リクエストなので範囲でまとめて取り直すより無駄がなく、途中に空いた穴も次の実行で埋まります
- **JARTIC側がそもそも配信していないコマ**（リクエストしても0件が返る）が時々あります。この場合はデータが存在しないので埋まらず、時系列図ではその区間だけ線が途切れます
- GitHub Actions（後述）で定期実行することで、復旧期（本震+2週間）が終わるまで自動的にアーカイブが育っていきます
- 1時間値も同様に `data/archive/traffic_hourly.parquet` に蓄積します（1時間値は3ヶ月遡れる）

### 通行規制情報のアーカイブ

通行規制も**解除されるとポータルの一覧から消えて、後から取得できません**。そのため `data/archive/regulations_archive.json` に**追記専用**で蓄積します（削除は一切しません）。

- 各規制は「路線名・地域・区間の始終点座標・規制開始日時」をキーに識別します（規制内容や終了日時は途中で変わるためキーに含めません）
- `first_seen` / `last_seen`（初めて／最後に確認した日時）と `still_listed`（まだポータルに載っているか）を保持するので、一覧から消えた後も「いつからいつまで、どういう規制だったか」を追えます
- 規制内容や終了日時が変わったときだけ `history` に1行追記します（全面通行止め → 片側交互通行止め → 解除 といった推移が残ります）
- スナップ済みの経路はアーカイブから再利用するため、定期実行のたびに全件をOSRMの公開デモサーバーへ投げ直しません
- ポータルからの取得に失敗した回は、**アーカイブを一切変更しません**（取得できなかったことを「一覧から消えた」と誤判定しないため）
- `data/regulations.json` は地図描画用の「現在のスナップショット」で、こちらは毎回上書きされます

### 自動取得（GitHub Actions）

[`.github/workflows/fetch-and-archive.yml`](../.github/workflows/fetch-and-archive.yml) が6時間ごとに `fetch_and_prepare.py` を実行し、`data/` 配下に変更があれば自動でコミット・pushします。手動で今すぐ実行したい場合は GitHub の Actions タブから `workflow_dispatch` で起動できます。

復旧期（本震から2週間）を過ぎるとフェッチ対象がなくなり実質no-opになりますが、ワークフロー自体は動き続けます。不要になったら GitHub の Actions 設定からスケジュールを無効化してください。

> CIランナーはUTCで動くため、日時の「いま」はすべてJSTに変換して扱っています（`_now_jst()`）。素の `datetime.now()` にすると対象期間の終端が9時間手前になり、本震後のデータを取り逃します。

## アーキテクチャ

```
fetch_and_prepare.py   … オフラインのデータ取得・前処理（geopandas/shapely使用）
  ├─ modules/api_request_func.py … JARTIC WFS APIリクエスト（範囲取得／コマ指定取得）
  ├─ modules/aggregation.py      … GeoJSON→GeoDataFrame変換、観測時刻のパース
  ├─ modules/earthquake_data.py  … 気象庁地震情報の取得・複数報の統合
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

`app.py` は前処理済みデータを読み込むだけの薄いビュー層です。これにより、Streamlit Community Cloud等のGDALが使えない/使いにくい環境でも動かせるようにしています。重い依存（geopandas/shapely/pyproj）は `fetch_and_prepare.py` の実行時（ローカルまたはGitHub Actions）にのみ必要です。

## ディレクトリ構成

```
app.py                        Streamlitダッシュボード本体
fetch_and_prepare.py           データ取得・前処理スクリプト
modules/                       データ取得・変換・異常検知ロジック
scripts/                       補助スクリプト（規制PDFのテキスト抽出）
data/qsr_regulations/          熊本河川国道事務所の規制PDF（転記元。手動で追加）
data/archive/                  恒久アーカイブ（追記専用）: 5分値・1時間値・通行規制
data/                          前処理済みデータ（parquet/json）のスナップショット
.github/workflows/             定期自動取得（GitHub Actions）
requirements.txt               app.py 実行用の軽量な依存関係
requirements-fetch.txt         fetch_and_prepare.py 実行用の追加依存関係（geopandas等）
```
