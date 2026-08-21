# AWSへの移行（ECS Fargate + ALB + Cognito + S3）

Streamlit Community Cloud から AWS へ移し、サーバを増強してログインを付けるための手順です。
**この文書の時点で済んでいるのは「コンテナ化」と「S3から読めるようにする」ところまで**で、
AWS側の構築はドメインが決まってからです。

## なぜこの構成か

| 決めたこと | 理由 |
| --- | --- |
| ECS Fargate + ALB | **StreamlitはWebSocketが必須**。ALBは対応している。**App Runnerは非対応**なので使えない |
| 認証はALBのCognito | 認証をアプリの前段で終端でき、アプリのコードに認証を書かない。掛け忘れが起きない |
| データはS3 | GitHubのPATが不要になり、取得パイプラインの更新が再デプロイなしで反映される |
| 1vCPU / 2GB〜 | 実測でメモリはピーク578MB（モバイル空間統計を読んだとき）。1GBでは足りない |

**レプリカを2つ以上にするならALBのスティッキーセッションが要ります。** Streamlitはセッション状態を
プロセス内に持つので、操作のたびに別のタスクへ振られると状態が消えます。

## ドメインについて（未定）

いまの公開URL `kumamoto-earthquake-traffic-map.streamlit.app` は、Streamlit社のドメインを
借りている状態です。AWSに移すと、ALBには `xxx.ap-northeast-1.elb.amazonaws.com` という
AWSのドメイン名が付きますが、**このアドレスにはHTTPSの証明書を出せません**（amazonaws.com は
AWSのものなので、こちらの名前として証明書を発行できない）。パスワードを通す以上、HTTPのままは
避けたいので、**自分の名前のドメインが要ります**。

選択肢は2つです。

1. **新しく取る** — Route 53 で例えば `kumamoto-traffic.jp` のような名前を年$10〜15程度で取得。
   AWSの中で完結し、証明書（ACM）は無料
2. **所属機関の既存ドメインのサブドメインを借りる** — 例 `traffic.ibs.or.jp`。情報システム部門に
   「このAWSのアドレスを指すCNAMEを1本足してほしい」と依頼する形。費用はかからない

どちらでも構成は同じです。2の場合は、証明書の検証用レコードも1本足してもらう必要があります。

## 済んでいること

### コンテナ化

- [`Dockerfile`](../Dockerfile) … python:3.11-slim。ヘルスチェックは `/_stcore/health`
- [`.dockerignore`](../.dockerignore) … ZIP・PDFのアーカイブ（150MB）を除外し、**イメージに入る
  `data/` は14ファイル・5.0MB**。実行時に読むのはこれだけ

```bash
docker build -t kumamoto-traffic .
docker run --rm -p 8080:8080 kumamoto-traffic
```

### データの置き場を切り替えられるようにした

[`modules/datastore.py`](../modules/datastore.py) を足し、アプリの読み込みを全部ここに通しました。

- ローカルの `data/` にファイルがあればそれを読む（手元での開発とStreamlit Cloudは今までどおり）
- 無ければS3を読む。設定は環境変数 `DATA_S3_BUCKET` / `DATA_S3_PREFIX`
- 300秒のキャッシュ付き。**取得パイプラインがS3を更新すれば、コンテナを入れ替えなくても反映される**

モバイル空間統計の集計（`modules/mesh_population.py`）にもS3経路を足しました。
`MESH_S3_BUCKET` / `MESH_S3_PREFIX` を渡すとS3から読み、GitHubのPATは要りません。
どちらもIAMロールで読むので、**コンテナに鍵を置きません**。

## これからやること

### 1. S3バケットを作ってデータを置く

```
s3://<バケット>/data/          … observations.parquet, *.json, archive/*  （5MB）
s3://<バケット>/mss_build/     … mesh_population*.parquet, meta.json      （21MB）
```

`data/` 側は取得パイプライン（GitHub Actions）から `aws s3 sync` で更新します。
`mss_build/` はいまの非公開リポジトリの中身をそのまま置きます（**モバイル空間統計は
公開リポジトリに入れない**という条件はS3でも同じで、バケットは非公開のままにします）。

### 2. ECR にイメージを置き、Fargate で動かす

- GitHub Actions から OIDC でAWSに入る（長期のアクセスキーを作らない）
- タスク定義: 1vCPU / 2GB、環境変数に `DATA_S3_BUCKET` と `MESH_S3_BUCKET`
- タスクロールに対象バケットの `s3:GetObject` / `s3:ListBucket` だけ付ける
- ALB のターゲットグループはヘルスチェックを `/_stcore/health`、スティッキーセッション有効

### 3. ドメインと証明書

- Route 53 のホストゾーン、ACMで証明書を発行（DNS検証）
- ALB のリスナーは443のみ。80は443へリダイレクト

### 4. Cognito でログイン

- ユーザープールを作り、利用者を招待（パスワードは本人が設定）
- ALB のリスナールールに `authenticate-cognito` を置き、その後ろにアプリを転送
- サインアウトの導線と、セッションの有効期間（既定7日）を決める

### 5. 監視と費用

- CloudWatch Logs にアプリのログ、メモリ・CPUのアラーム
- AWS Budgets で月額の予算アラート（超過に気づけるように）

## 費用の概算（東京リージョン・月額）

| 項目 | 概算 |
| --- | --: |
| Fargate 1vCPU/2GB 常時 | $20前後 |
| ALB | $20前後 |
| S3・ECR・CloudWatch | $2〜5 |
| Cognito | 小規模なら無料枠内 |
| ドメイン | 年$10〜15（新規取得の場合） |
| **合計** | **月$45〜60程度** |

夜間や休日に止められるならFargateの分は減らせます（ECSのスケジュールスケーリング）。

## 切り替えの手順

1. AWS側を作り、`https://<新しいドメイン>` で動くことを確認する
2. Streamlit Cloud のアプリは残したまま、READMEと関係者への案内を新URLに変える
3. 数日運用して問題が無ければ Streamlit Cloud を停止する

DNSを切り替えるだけなので、戻すのも同じ操作です。
