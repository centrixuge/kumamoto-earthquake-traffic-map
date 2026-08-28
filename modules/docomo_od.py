"""
ドコモODデータのダウンロードタブ。

土木学会土木計画学研究委員会令和８年熊本地震対応特別プロジェクト実行委員会と
NTTドコモ が共同で分析したデータ。提供にあたって次の3点が条件になっている。

  1. クレジット（共同で分析した旨）を入れる
  2. データ内容の説明を入れる
  3. 利用できるのは委員会のメンバーと関係者に限る

このため、条件の文言はこのモジュールに直接書いて、データが読めるかどうかに
かかわらずタブの先頭に必ず出す（置き場の設定漏れで文言だけ消える、という
ことが起きないように）。データそのものは公開できないので、置き場は
modules/private_store.py 経由（モバイル空間統計・商用車プローブと同じ）。

  1. data/docomo_od/bundle/ にあればそれ（手元での確認用）
  2. 環境変数 DOCOMO_OD_S3_BUCKET / DOCOMO_OD_S3_PREFIX
  3. st.secrets["docomo_od"]（repo または base_url）
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from modules import private_store

LOCAL_DIR = Path(__file__).resolve().parents[1] / "data" / "docomo_od" / "bundle"
META_FILE = "docomo_od_meta.json"
SECTION = "docomo_od"
ENV_PREFIX = "DOCOMO_OD"

# ---- 提供の条件（そのまま出す） ----
CREDIT = (
    "本分析データは、**土木学会土木計画学研究委員会令和８年熊本地震対応特別"
    "プロジェクト実行委員会とNTTドコモ が共同で**分析したものです。"
)
DATA_NOTE = (
    "本分析データは、NTTドコモが基地局の運用データをもとに推計しています。"
    "位置情報の利用同意を得た運用データのみを対象とし、非識別処理・集計処理・"
    "秘匿処理の厳格な手順を行うことで、プライバシーを保護しながら安全な人流"
    "統計データを作成しています。"
)
USAGE_LIMIT = (
    "**利用できるのは、上記委員会のメンバーおよび関係者に限られます**"
    "（メンバーの管理監督権限が及ぶ人。例えば研究室の助教や学生）。"
    "この範囲を超えて配らないでください。"
)

WIP_NOTE = (
    "**分析結果の可視化は作業中です。** いまはデータの配布だけを行っています。"
)

# 中身の説明（期間・集計軸・直したところ）は、公開リポジトリに置かない。
# 置き場の docomo_od_meta.json の "display" から読む。読めないときは
# 提供の条件と「準備中」だけを出す。
FALLBACK_GROUPS = [
    {"key": "residence", "label": "居住地別"},
    {"key": "age", "label": "年代別"},
    {"key": "air", "label": "飛行機"},
]

PREPARING = (
    "ドコモODデータは準備中です。"
    "`python scripts/build_docomo_od_bundle.py` で整えたファイルを作り、"
    "`data/docomo_od/bundle/` に置くか、非公開の置き場を設定してください。"
)


class DocomoOdUnavailable(private_store.PrivateDataUnavailable):
    """整えたファイルが手元にも非公開の置き場にも無い。"""


def _fetch(name: str) -> bytes:
    try:
        return private_store.fetch(name, local_dir=LOCAL_DIR, section=SECTION,
                                   env_prefix=ENV_PREFIX)
    except private_store.PrivateDataUnavailable as e:
        raise DocomoOdUnavailable(str(e)) from e


def available() -> bool:
    return ((LOCAL_DIR / META_FILE).exists()
            or bool(private_store.config(SECTION, ENV_PREFIX)))


@st.cache_data(ttl=3600, show_spinner=False)
def load_meta() -> dict:
    return json.loads(_fetch(META_FILE).decode("utf-8"))


@st.cache_data(ttl=3600, show_spinner=False)
def load_csv(name: str) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(_fetch(name)), dtype=str, encoding="utf-8-sig")


@st.cache_data(ttl=3600, show_spinner=False)
def load_bytes(name: str) -> bytes:
    return _fetch(name)


def _terms() -> None:
    """提供の条件。データが読めなくても必ず出す。"""
    st.markdown(CREDIT)
    st.info(DATA_NOTE)
    st.warning(USAGE_LIMIT)


def _of(meta: dict, name: str) -> dict:
    for info in meta.get("files", []):
        if info.get("file") == name:
            return info
    return {}


def _period(info: dict) -> str:
    date = info.get("date") or {}
    if not date:
        return "-"
    return f"{date.get('from', '')}〜{date.get('to', '')}（{date.get('days', '')}日）"


def render_tab() -> None:
    st.subheader("ドコモODデータ")
    _terms()
    st.markdown(WIP_NOTE)

    if not available():
        st.info(PREPARING)
        return
    try:
        meta = load_meta()
    except private_store.PrivateDataUnavailable as e:
        st.warning(str(e))
        return

    display = meta.get("display", {})
    if display.get("intro"):
        st.markdown(display["intro"])

    rows = []
    for info in meta.get("files", []):
        rows.append({
            "ファイル": info["file"],
            "集計軸": info.get("axis", ""),
            "期間区分": info.get("period", ""),
            "行数": f"{info.get('rows', 0):,}",
            "収録期間": _period(info),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption(
        f"整えた日時: {meta.get('built_at', '-')} ／ 読み込み元: "
        f"{private_store.source_label(LOCAL_DIR, META_FILE, SECTION, ENV_PREFIX)}"
    )

    groups = display.get("groups") or FALLBACK_GROUPS
    tabs = st.tabs([g["label"] for g in groups])
    for tab, group in zip(tabs, groups):
        axis, label = group["key"], group["label"]
        with tab:
            st.markdown(f"**{label}**"
                        + (f" — {group['note']}" if group.get("note") else ""))
            names = [i["file"] for i in meta.get("files", [])
                     if i["file"].startswith(f"docomo_od_{axis}_")]
            # まとめたもの（_all）を先に出す
            names.sort(key=lambda n: (not n.endswith("_all.csv"), n))
            for name in names:
                info = _of(meta, name)
                try:
                    data = load_bytes(name)
                except private_store.PrivateDataUnavailable as e:
                    st.warning(str(e))
                    continue
                cols = st.columns([3, 2])
                with cols[0]:
                    st.caption(
                        f"{info.get('period', '')}　{info.get('rows', 0):,}行"
                        f"（{_period(info)}）"
                    )
                with cols[1]:
                    st.download_button(
                        f"{name} をダウンロード", data=data, file_name=name,
                        mime="text/csv", key=f"docomo_dl_{name}")
            head_name = next((n for n in names if n.endswith("_all.csv")), None)
            if head_name:
                with st.expander("先頭30行を見る"):
                    try:
                        st.dataframe(load_csv(head_name).head(30),
                                     use_container_width=True, hide_index=True)
                    except private_store.PrivateDataUnavailable as e:
                        st.warning(str(e))

    with st.expander("配布ファイルとの対応・直したところ"):
        if display.get("typo_note"):
            st.markdown(display["typo_note"])
        st.dataframe(pd.DataFrame([
            {"配布ファイル": src, "このタブのファイル": info["file"]}
            for src, info in ((i.get("source"), i) for i in meta.get("files", []))
            if src
        ]), use_container_width=True, hide_index=True)
        layout = meta.get("layout_file")
        if layout:
            try:
                st.download_button(
                    f"{layout} をダウンロード", data=load_bytes(layout),
                    file_name=layout,
                    mime=("application/vnd.openxmlformats-officedocument"
                          ".spreadsheetml.sheet"),
                    key="docomo_dl_layout")
                if display.get("layout_caption"):
                    st.caption(display["layout_caption"])
            except private_store.PrivateDataUnavailable as e:
                st.warning(str(e))
