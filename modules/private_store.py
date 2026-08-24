"""
公開できないデータの置き場から読む共通部分。

配布条件で再配布できないデータ（モバイル空間統計、商用車プローブ）は
公開repoに置けない。手元にファイルがあればそれを使い、無ければ非公開の
置き場から取る、という手順はどのデータでも同じなので、ここに集めた。

探す順序は次の3つ。

  1. ローカルのディレクトリ（手元での確認用）
  2. 環境変数 <PREFIX>_S3_BUCKET / <PREFIX>_S3_PREFIX（AWSに移した後）
  3. st.secrets の指定セクション（Streamlit Community Cloud）

3は次のどちらかを書く。

  [<section>]
  repo = "owner/name"        # 非公開のGitHubリポジトリ
  ref  = "main"
  token = "github_pat_..."   # contents:read だけの fine-grained PAT

  [<section>]
  base_url = "https://.../"  # オブジェクトストレージ等。末尾は / 付き
  token = "..."              # 要るときだけ（Bearerで送る）

どれも無ければ `PrivateDataUnavailable` を投げる。呼び出し側はこれを捕まえて
「準備中」と出し、ページ全体は落とさない。トークンの中身は決して出さず、
切り分けに要る「形」（種類と文字数）だけを出す。
"""
from __future__ import annotations

import os
from pathlib import Path

import requests
import streamlit as st


class PrivateDataUnavailable(RuntimeError):
    """非公開の置き場から読めなかった。呼び出し側で捕まえて案内を出す。"""


def config(section: str, env_prefix: str) -> dict:
    """置き場の設定。環境変数（AWS）を優先し、無ければ st.secrets を見る。"""
    bucket = os.environ.get(f"{env_prefix}_S3_BUCKET", "").strip()
    if bucket:
        return {"bucket": bucket,
                "prefix": os.environ.get(f"{env_prefix}_S3_PREFIX", "").strip()}
    try:
        return dict(st.secrets[section])
    except Exception:
        return {}


def clean_token(value) -> str:
    """
    貼り付け事故に強くする。トークンに空白は入らないので全部落とし、
    引用符や `Bearer ` が一緒に入ってしまった場合も剥がす。

    secretsの入力欄で長い値が折り返されて改行が混ざると、見た目は正しくても
    401になる。
    """
    token = "".join(str(value).split())
    if token[:6].lower() == "bearer":
        token = token[6:]
    return token.strip("\"'")


def token_shape(token: str) -> str:
    """トークンの中身は出さず、形だけ出す（欠けや取り違えの切り分け用）。"""
    if not token:
        return "設定されたトークン: **空**"
    kinds = {
        "github_pat_": "fine-grained PAT",
        "ghp_": "classic PAT",
        "gho_": "OAuthトークン",
        "ghs_": "GitHub Appのトークン",
    }
    kind = next(
        (v for k, v in kinds.items() if token.startswith(k)), "**未知の形式**"
    )
    return (
        f"設定されたトークン: {kind}・{len(token)}文字"
        "（fine-grained PATは `github_pat_` で始まり90文字前後です。"
        "短ければ貼り付けが欠けています）"
    )


def http_hint(status: int) -> str:
    if status == 404:
        return (
            "**404** はトークンにそのリポジトリの権限が無いときにも出ます"
            "（存在しない場合と区別が付きません）。fine-grained PAT の "
            "Repository access で対象リポジトリを選んでいるか、"
            "Repository permissions の **Contents: Read-only** が付いているか、"
            "`repo` と `ref`（ブランチ名）の綴りをご確認ください。"
        )
    if status in (401, 403):
        return (
            f"**{status}** はトークンが無効・期限切れ・権限不足のときに出ます。"
            "fine-grained PAT の有効期限と Contents: Read-only をご確認ください。"
        )
    return "GitHubの応答をご確認ください。"


def _fetch_s3(cfg: dict, name: str) -> bytes:
    """S3から読む。鍵は持たず、実行ロール（ECSのタスクロール）で読む。"""
    import boto3  # AWSでだけ要る
    from botocore.exceptions import ClientError

    key = "/".join(p for p in (str(cfg.get("prefix", "")).strip("/"), name) if p)
    try:
        obj = boto3.client("s3").get_object(Bucket=cfg["bucket"], Key=key)
        return obj["Body"].read()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        raise PrivateDataUnavailable(
            f"{name} を取得できませんでした（S3: {code}）。"
            f"取得先: s3://{cfg['bucket']}/{key}" + "\n\n"
            "バケット名と、タスクロールに s3:GetObject が付いているかを"
            "確認してください。"
        ) from e


def fetch(name: str, *, local_dir: Path, section: str, env_prefix: str,
          timeout: int = 120) -> bytes:
    """1ファイル読む。ローカル → S3 → base_url/GitHub の順に探す。"""
    local = Path(local_dir) / name
    if local.exists():
        return local.read_bytes()

    cfg = config(section, env_prefix)
    if cfg.get("bucket"):
        return _fetch_s3(cfg, name)
    token = clean_token(cfg.get("token", ""))
    if cfg.get("base_url"):
        url = str(cfg["base_url"]).strip().rstrip("/") + "/" + name
        headers = {"Authorization": f"Bearer {token}"} if token else {}
    elif cfg.get("repo"):
        if not token:
            raise PrivateDataUnavailable(
                f"`token` が空です。secrets の `[{section}]` の中に "
                "`token = \"github_pat_...\"` があるかご確認ください"
                "（キー名の綴り違いや、別のセクションに入っている場合も"
                "空になります）。"
            )
        repo = str(cfg["repo"]).strip().strip("/")
        ref = str(cfg.get("ref", "main")).strip()
        path = str(cfg.get("path", "")).strip("/")
        target = f"{path}/{name}" if path else name
        url = f"https://api.github.com/repos/{repo}/contents/{target}?ref={ref}"
        headers = {
            "Accept": "application/vnd.github.raw",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    else:
        raise PrivateDataUnavailable("置き場が設定されていません。")

    res = requests.get(url, headers=headers, timeout=timeout)
    if res.status_code != 200:
        hint = http_hint(res.status_code) + "\n\n" + token_shape(token)
        # 例外をそのまま投げるとページ全体が落ちるうえ、公開環境では本文が
        # 伏せられて原因が分からない。状態コードと当たり先だけ残して返す
        # （トークンは出さない）。
        raise PrivateDataUnavailable(
            f"{name} を取得できませんでした（HTTP {res.status_code}）。"
            f"取得先: {url}\n\n" + hint
        )
    return res.content


def source_label(local_dir: Path, name: str, section: str,
                 env_prefix: str) -> str:
    """いまどこから読んでいるかを1行で返す（画面の脚注用）。"""
    if (Path(local_dir) / name).exists():
        return "手元のファイル"
    cfg = config(section, env_prefix)
    if cfg.get("bucket"):
        return f"S3（{cfg['bucket']}）"
    if cfg.get("base_url"):
        return "非公開の置き場（base_url）"
    if cfg.get("repo"):
        return f"非公開リポジトリ（{cfg['repo']}）"
    return "未設定"
