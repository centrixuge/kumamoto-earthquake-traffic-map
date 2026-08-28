# ドコモODデータ

ダッシュボードの「ドコモODデータ」タブの仕組みをまとめています。**このタブは develop（非公開URLの確認用アプリ）だけに置いています。** 公開アプリ（main）には出していません。

## 提供の条件（3点）

提供にあたって次の3点が条件になっています。タブの先頭に、**データが読めるかどうかにかかわらず必ず出す**ようにしました（置き場の設定漏れで文言だけ消えることが無いよう、文言は [`modules/docomo_od.py`](../modules/docomo_od.py) に直接書いています）。

1. **クレジット** — 本分析データは、土木学会土木計画学研究委員会令和８年熊本地震対応特別プロジェクト実行委員会とNTTドコモ が共同で分析したもの
2. **データ内容の説明** — NTTドコモが基地局の運用データをもとに推計。位置情報の利用同意を得た運用データのみを対象とし、非識別処理・集計処理・秘匿処理の厳格な手順を行うことで、プライバシーを保護しながら安全な人流統計データを作成している
3. **利用できる範囲** — 上記委員会のメンバーおよび関係者（メンバーの管理監督権限が及ぶ人。例えば研究室の助教や学生）に限る

3点目があるため、データは公開リポジトリにも公開アプリにも置きません。置き場は `data/docomo_od/`（`.gitignore` 済み）と、非公開リポジトリ **[centrixuge/kumamoto-docomo-od-data](https://github.com/centrixuge/kumamoto-docomo-od-data)** です。

## 配布されているもの

「集計軸（居住地別・年代別・飛行機）× 期間（震災前・震災後）」の6ファイル（CP932）。

| 集計軸 | 震災前（2026/07/04〜07/10） | 震災後（2026/07/28〜08/21） |
| --- | --- | --- |
| 居住地別 | 自動車・鉄道／13地域 | 自動車（鉄道を置き換え）／**方向別** |
| 年代別 | 自動車・鉄道／10〜80代 | 自動車／**方向別** |
| 飛行機 | 日別のみ | 日別のみ |

**震災後だけ氷川町断面の方向別**（`DIRECTION`）です。値は北から／南からの2つ。

## 直したところ（1か所だけ）

配布データの `DIRECTION` の値が **`Fron South`（`From` の誤り）** になっていたので、`From South` に直しました。ほかの値・行は配布のままです。直した記録は `docomo_od_meta.json` の `direction_fix` に残しています。

```bash
python scripts/build_docomo_od_bundle.py
```

出力は `data/docomo_od/bundle/`。配布のままの形（文字コードだけ UTF-8 BOM付き・CRLF に）6ファイルと、震災前後を縦につないだ軸ごとの3ファイル（`期間区分` の列を追加）、それにメタ情報のJSONです。震災前には `DIRECTION` が無いので、つないだファイルではその列が空欄になります。

## アプリからの配り方

[`modules/private_store.py`](../modules/private_store.py) の共通処理で、次の順に探します（モバイル空間統計・商用車プローブと同じ）。

1. `data/docomo_od/bundle/`（手元での確認用）
2. 環境変数 `DOCOMO_OD_S3_BUCKET` / `DOCOMO_OD_S3_PREFIX`
3. `st.secrets["docomo_od"]`（`repo` ＝ 非公開GitHubリポジトリ、または `base_url`）

デプロイ済みアプリ（develop）の secrets は次のように書きます。

```toml
[docomo_od]
repo = "centrixuge/kumamoto-docomo-od-data"
ref  = "main"
token = "github_pat_..."   # Contents: Read-only の fine-grained PAT
```

**分析結果の可視化は作業中**である旨をタブに明記し、いまはデータの配布だけを行っています。
