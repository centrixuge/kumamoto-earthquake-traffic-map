# メッシュ関係のデータレイアウト

500mメッシュまわりで書き出しているファイルの列定義です。集計そのものの考え方は
[docs/mobile-spatial-statistics.md](mobile-spatial-statistics.md) にあります。

置き場は `data/mss_build/`（`.gitignore` 済み）。このうち **`mesh_city_*` の3つは
モバイル空間統計の値を含みません**。中身は国土数値情報 行政区域とメッシュコードだけから
作れるもので、公開の制限はかかりません。`mesh_population*` は集計結果なので非公開の
置き場（`centrixuge/kumamoto-mesh-population-data`）に置きます。

| ファイル | 行数 | 生成 | 公開 |
| --- | --: | --- | --- |
| `mesh_city_table.csv` | 27,830 | [`scripts/export_mesh_city_table.py`](../scripts/export_mesh_city_table.py) | 可 |
| `mesh_city_coverage.csv` | 31,209 | 同上 | 可 |
| `mesh_city_table_section9110040.csv` | 104 | 下の抜粋の作り方 | 可 |
| `mesh_population.parquet` | 4,098,436 | [`scripts/build_mesh_population.py`](../scripts/build_mesh_population.py) | 不可 |
| `mesh_population_summary.parquet` | 27,830 | 同上 | 不可 |
| `mesh_population_summary.gpkg` / `.geojson` | 27,830 | 同上 | 不可 |
| `mesh_population_meta.json` | — | 同上 | 不可 |

CSVはいずれも **UTF-8（BOM付き）・CRLF・ヘッダ1行**です。Excelでそのまま開けます。

---

## mesh_city_table.csv

メッシュ1件＝1行。**主キーは `mesh`**（一意）。

| 列 | 型 | 例 | 内容 |
| --- | --- | --- | --- |
| `mesh` | 整数9桁 | `483075254` | 500mメッシュコード |
| `lat` | 小数 | `32.606250` | メッシュ**中心**の緯度（WGS84）。32.0979〜33.1937 |
| `lon` | 小数 | `130.696875` | メッシュ**中心**の経度（WGS84）。129.941〜131.322 |
| `n_hours` | 整数 | `336` | 人口が配信された時点の数。1〜336。**336未満は10人未満の時間帯があった**ということ |
| `city_code_area` | 文字5桁 | `43213` | **割当先**の市区町村コード（行政区域コード）。熊本県内なので必ず `43` で始まる |
| `city_area` | 文字 | `宇城市` | 割当先の市区町村名。熊本市は行政区まで（例 `熊本市中央区`） |
| `share_area` | 小数 | `1.0000` | 割当先がメッシュ面積に占める割合。0.0001〜1 |
| `city_code_center` | 文字5桁 | `43213` | 参考：**中心点**がどの市区町村ポリゴンに入るか。県外もある（`40`〜`46`）。**空欄は中心が海上**（1,009件） |
| `city_center` | 文字 | `宇城市` | 同上の名称 |
| `n_cities` | 整数 | `1` | メッシュに掛かっている市区町村の数（隣接県を含む）。1〜4 |
| `kumamoto_share` | 小数 | `1.0000` | メッシュ面積のうち**熊本県内の陸域**が占める割合。残りは海または県外 |
| `straddles` | 真偽 | `False` | `n_cities > 1`（市区町村界をまたぐ） |
| `differs` | 真偽 | `False` | 面積被覆と中心点判定で割当先が違う |

### 使うときの注意

- **市区町村コードは文字列として読んでください。** 数値にすると先頭0の県（該当なしですが）や欠測の混在で `43213.0` のような表記になります

  ```python
  pd.read_csv("mesh_city_table.csv", encoding="utf-8-sig",
              dtype={"city_code_area": str, "city_code_center": str})
  ```
- `city_code_area` が実際の集計で使われている割当です。`*_center` は以前の方式で、比較用にだけ入れています（食い違いは1,167件。うち1,009件は中心が海上で、以前は集計対象から落ちていたもの）
- `share_area < 0.5` のメッシュが1,032件あります。海面や県外を含むメッシュで、陸の中では最大でも面積の半分に届かないという意味です。「どの市区町村か1つに決める」以上は避けられません
- `n_hours` は**そのメッシュに人がいた時間数ではありません**。10人未満だと配信されないため、`n_hours` が小さいメッシュは「人が少ない」メッシュです

## mesh_city_coverage.csv

メッシュ × 市区町村。**またぐメッシュは複数行**（最大4行）になります。主キーは
`mesh` + `city_code`。`mesh_city_table.csv` の `city_area` は、このうち `area_m2` が
最大の行です。

| 列 | 型 | 例 | 内容 |
| --- | --- | --- | --- |
| `mesh` | 整数9桁 | `483065633` | 500mメッシュコード |
| `city_code` | 文字5桁 | `43202` | 市区町村コード（熊本県＋隣接4県） |
| `city` | 文字 | `八代市` | 市区町村名 |
| `area_m2` | 小数 | `163836` | 重なり面積（m²）。0〜272,529 |
| `share` | 小数 | `0.6042` | `area_m2 ÷ メッシュ面積`。0〜1 |

並びは `mesh` 昇順、同じメッシュの中は `area_m2` の降順です。つまり**各メッシュの
先頭行が割当先**になります。

### 使うときの注意

- **`share` の合計は1になりません。** 海や県外がある1,816メッシュでは1未満です。丸めの分だけ1.0001になることもあります
- 面積は平面直角座標系 第II系（EPSG:6670）で測っています。1メッシュは0.269〜0.273km²です
- 按分に使う場合、人口は面積に比例しないことにご注意ください。面積按分でよいかは用途次第で、国勢調査のメッシュ人口を重みにするほうが妥当な場面が多いです

## mesh_city_table_section9110040.csv

観測点9110040（氷川町・国道3号）から3km以内の104メッシュの抜粋です。列は
`mesh_city_table.csv` と同じで、次の2列が3列目・4列目に入ります。

| 列 | 型 | 例 | 内容 |
| --- | --- | --- | --- |
| `dist_km` | 小数 | `0.26` | 観測点からメッシュ中心までの距離（km） |
| `side` | 文字 | `south` | 観測点を通る東西線から見て `north` / `south` |

作り方（`mesh_city_table.csv` から2手）:

```python
PT, LINE = (32.56558, 130.688167), 32.56558
d = np.hypot((t.lat - PT[0]) * 111.32,
             (t.lon - PT[1]) * 111.32 * np.cos(np.radians(PT[0])))
near = t[d <= 3.0].copy()
near.insert(3, "dist_km", d[d <= 3.0].round(2))
near.insert(4, "side", np.where(near.lat >= LINE, "north", "south"))
```

---

## mesh_population.parquet（非公開）

メッシュ × 時点。人口推計値そのものなので**公開できません**。

| 列 | 型 | 内容 |
| --- | --- | --- |
| `mesh` | int32 | 500mメッシュコード |
| `t` | int16 | `meta.start` からの経過時間数。0〜335 |
| `population` | int32 | 総数 |
| `pop_rs` | int32 | そのメッシュのある市区町村の居住者 |
| `pop_vi` | int32 | それ以外（来訪者） |

- 配信されなかった時点（10人未満）は**行がありません**。0ではないので0で埋めないでください
- `pop_rs + pop_vi < population` です（居住地ごとに10人未満だと配信されないため。全期間で総数の87.9%）

## mesh_population_summary.parquet / .gpkg / .geojson（非公開）

メッシュ1件＝1行の88列。`.gpkg` と `.geojson` は同じ内容に500mメッシュのポリゴン
（EPSG:4326）を付けたものです。

| 列 | 型 | 内容 |
| --- | --- | --- |
| `mesh` | int64 | 500mメッシュコード |
| `lat` / `lon` | float | メッシュ中心の緯度経度 |
| `city` / `city_code` | 文字 | 割当先の市区町村（面積被覆。`mesh_city_table.csv` の `city_area` と同じ） |
| `max_population` | float | 全期間での最大値 |
| `n_hours` | float | 値が配信された時点の数（最大336） |
| `{種類}_{母集団}_{日区分}_{時間帯}` | float | 81列。下の組み合わせ |

- 種類: `pre`＝発災前（本震 2026-07-28 16:27 より前）の平均 / `post`＝発災後の平均 / `ratio`＝`post ÷ pre`
- 母集団: `all`＝総数 / `rs`＝居住者 / `vi`＝来訪者
- 日区分: `all`＝全日 / `wd`＝平日 / `hd`＝休日（土日祝）
- 時間帯: `all`＝全時間帯 / `h3`＝3時 / `h14`＝14時

例: `pre_rs_wd_h3` は「発災前・居住者・平日・3時の平均人口」。アプリが地図で使うのは
`ratio_all_wd_h3`（既定）です。

## mesh_population_meta.json（非公開）

| キー | 内容 |
| --- | --- |
| `source` / `boundary` | 出典と、使った行政区域データ |
| `area` / `city_rule` | 対象範囲と、メッシュ⇔市区町村の決め方（面積被覆） |
| `unit` | 集計の粒度 |
| `start` / `end` / `hours` | 収録期間と時点数（336） |
| `quake_at` | 発災（本震）の時刻。前後の切り分けに使う |
| `meshes` / `rows` | `mesh_population.parquet` の件数 |
| `min_population` | 配信の下限（10人） |
| `residence_coverage` | 居住地別の合計が総数に占める割合 |
| `representative_hours` | 夜間・昼間の代表時刻（3時・14時） |
| `suppressed_note` | 秘匿処理の説明 |
| `day_type_days` / `phase_days` | 平日・休日の日数（発災前は平日6日・休日2日、発災後は平日5日・休日2日） |
