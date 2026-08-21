# ダッシュボード（Streamlit）のコンテナ。AWS ECS Fargate で動かす前提。
#
#   docker build -t kumamoto-traffic .
#   docker run --rm -p 8080:8080 kumamoto-traffic
#
# データは2通りで読む（modules/datastore.py）。
#   ・イメージに入れた data/ を読む（S3を設定しない場合）
#   ・DATA_S3_BUCKET を渡すとS3を読む（取得パイプラインの更新が再デプロイ
#     なしで反映される。イメージにはデータを入れなくてよい）
FROM python:3.11-slim

# curl はヘルスチェック用。それ以外は入れない（イメージを小さく保つ）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_PORT=8080 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# 依存だけ先に入れて、コードの変更でここを作り直さないようにする
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# アプリ本体。data/ に何を入れるかは .dockerignore で絞っている
# （ZIP・PDFのアーカイブ150MBは実行時に読まないので入れない）
COPY app.py ./
COPY modules/ ./modules/
COPY data/ ./data/

# ルートFSは読み取り専用で動く。書き込みが要るのは取得パイプライン側だけ。
USER 1000:1000

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8080/_stcore/health || exit 1

CMD ["streamlit", "run", "app.py"]
