"""
アプリが読むデータの取り出し口。

置き場を2つ持つ。

  1. ローカルの data/ （リポジトリに入っているもの。開発と Streamlit Cloud）
  2. S3 （AWSに置いたとき。IAMロールで読むので鍵を持たない）

ローカルにファイルがあればそれを使い、無ければS3を見る。両方無ければ None を
返し、呼び出し側が「データが無い」ときの表示に落とす。この順にしているのは、
手元での開発とテストが今までどおり動くようにするため。

S3の設定は環境変数（AWSではタスク定義から渡す）か st.secrets["data_store"]。

  DATA_S3_BUCKET  = kumamoto-traffic-data
  DATA_S3_PREFIX  = data/            （省略可）

読み込みは st.cache_data で ttl 秒だけ持つ。取得パイプラインがS3を更新すれば、
コンテナを入れ替えなくても ttl 経過後に新しいデータに変わる。
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
TTL = 300


def _config() -> dict:
    """S3の設定。環境変数を優先し、無ければ st.secrets を見る。"""
    bucket = os.environ.get("DATA_S3_BUCKET", "").strip()
    prefix = os.environ.get("DATA_S3_PREFIX", "").strip()
    if bucket:
        return {"bucket": bucket, "prefix": prefix}
    try:
        cfg = dict(st.secrets["data_store"])
    except Exception:  # noqa: BLE001 - secrets が無いのは通常運転
        return {}
    return {"bucket": str(cfg.get("bucket", "")).strip(),
            "prefix": str(cfg.get("prefix", "")).strip()}


def s3_enabled() -> bool:
    return bool(_config().get("bucket"))


def source_label() -> str:
    """どこから読んでいるかを画面や診断に出すための文字列。"""
    cfg = _config()
    if cfg.get("bucket"):
        return f"s3://{cfg['bucket']}/{cfg.get('prefix', '')}".rstrip("/") + "（ローカル優先）"
    return str(DATA_DIR)


@st.cache_data(ttl=TTL, show_spinner=False)
def _fetch_s3(rel: str) -> bytes | None:
    cfg = _config()
    if not cfg.get("bucket"):
        return None
    import boto3  # AWSでだけ要る。手元には入れなくてよい
    from botocore.exceptions import ClientError

    key = "/".join(p for p in (cfg.get("prefix", "").strip("/"), rel) if p)
    try:
        obj = boto3.client("s3").get_object(Bucket=cfg["bucket"], Key=key)
        return obj["Body"].read()
    except ClientError as e:  # 無いものは無いと返す（落とさない）
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "AccessDenied"):
            print(f"[datastore] s3://{cfg['bucket']}/{key} を読めません（{code}）",
                  flush=True)
            return None
        raise


def read_bytes(rel: str) -> bytes | None:
    """data/ からの相対パスで中身を返す。無ければ None。"""
    local = DATA_DIR / rel
    if local.exists():
        return local.read_bytes()
    return _fetch_s3(rel)


def exists(rel: str) -> bool:
    return read_bytes(rel) is not None


def read_json(rel: str, default=None):
    raw = read_bytes(rel)
    if raw is None:
        return default
    return json.loads(raw.decode("utf-8"))


def read_parquet(rel: str) -> pd.DataFrame:
    raw = read_bytes(rel)
    if raw is None:
        return pd.DataFrame()
    return pd.read_parquet(io.BytesIO(raw))
