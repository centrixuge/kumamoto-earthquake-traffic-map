"""
商用車プローブデータ（トランストロン）のダウンロードタブ。

配布は期間ごと・県ごと・日ごとに分かれている（いまは3配布・46ファイル）。分析のたびに
つなぎ直すのは手間で、しかも**同じキーが隣の期間のファイルにも現れる**ため
素朴につなぐと二重に数える。そこで `scripts/build_transtron_bundle.py` が
全期間を1本にまとめたものを作り、このタブではそれを配る。

**このモジュールには、データの中身の説明を一切書かない。**
develop は公開リポジトリのブランチなので、アプリのURLが非公開でも、
リポジトリに入れたものは公開される。配布元の仕様書には秘密情報の表示があり、
第三者提供の可否も未確認なので、項目の定義・値域・断面の一覧といった
「データの中身」は非公開の置き場に置いた `transtron_layout.json` から読んで
表示する。ここにあるのは、それを並べる仕組みだけ。

ファイルを探す順序は modules/private_store.py と共通。

  1. data/transtron/bundle/ にあればそれ（手元での確認用）
  2. 環境変数 TRANSTRON_S3_BUCKET / TRANSTRON_S3_PREFIX
  3. st.secrets["transtron"]（repo または base_url）

どれも無ければタブは「準備中」と出して落ちない。
"""
from __future__ import annotations

import gzip
import io
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from modules import bzone, private_store

LOCAL_DIR = Path(__file__).resolve().parents[1] / "data" / "transtron" / "bundle"
META_FILE = "transtron_bundle_meta.json"
LAYOUT_JSON = "transtron_layout.json"
LAYOUT_XLSX = "商用車プローブ_データレイアウト.xlsx"
SECTION = "transtron"
ENV_PREFIX = "TRANSTRON"

# 断面の一覧はこのファイル（0.3MB）から作る。大きい2つは読まない。
OD_FILE = "transtron_danmen_od_all.csv.gz"

PREPARING = (
    "商用車プローブデータは準備中です。"
    "`python scripts/build_transtron_bundle.py` で束ねたファイルを作り、"
    "`data/transtron/bundle/` に置くか、非公開の置き場を設定してください。"
)


class TranstronUnavailable(private_store.PrivateDataUnavailable):
    """束ねたファイルが手元にも非公開の置き場にも無い。"""


def _fetch(name: str) -> bytes:
    try:
        return private_store.fetch(name, local_dir=LOCAL_DIR, section=SECTION,
                                   env_prefix=ENV_PREFIX)
    except private_store.PrivateDataUnavailable as e:
        raise TranstronUnavailable(str(e)) from e


def available() -> bool:
    return ((LOCAL_DIR / META_FILE).exists()
            or bool(private_store.config(SECTION, ENV_PREFIX)))


@st.cache_data(ttl=3600, show_spinner=False)
def load_meta() -> dict:
    return json.loads(_fetch(META_FILE).decode("utf-8"))


@st.cache_data(ttl=3600, show_spinner=False)
def load_doc() -> dict:
    """項目の定義や注意書き（データの中身の説明）を置き場から読む。"""
    return json.loads(_fetch(LAYOUT_JSON).decode("utf-8"))


@st.cache_data(ttl=3600, show_spinner="ファイルを用意しています")
def load_file(name: str) -> bytes:
    return _fetch(name)


@st.cache_data(ttl=3600, show_spinner="先頭だけ読み込んでいます")
def load_head(name: str, rows: int = 200) -> pd.DataFrame:
    """先頭だけ読む。47MBを落とさなくても中身が見えるように。"""
    with gzip.open(io.BytesIO(_fetch(name)), "rt", encoding="utf-8-sig") as f:
        return pd.read_csv(f, nrows=rows, dtype=str)


@st.cache_data(ttl=3600, show_spinner="断面リンクの一覧を作っています")
def load_sections() -> pd.DataFrame:
    """
    断面リンクの一覧を集計ODデータ（0.3MB）から作る。
    仕様書に断面の一覧表は付いていないので、実データから拾うしかない。

    列名は置き場のJSONから取る（このリポジトリに項目名を書かないため）。
    並びは配布ファイルどおりで、先頭3列が断面リンク（メッシュ・リンク番号・方向）、
    4列目が通過年月日、最後が台数。
    """
    doc = load_doc()
    cols = [c[0] for c in doc["datasets"][OD_FILE]["columns"]]
    mesh, link, direction, date, count = cols[0], cols[1], cols[2], cols[3], cols[-1]
    with gzip.open(io.BytesIO(_fetch(OD_FILE)), "rt", encoding="utf-8-sig") as f:
        od = pd.read_csv(f, dtype=str)
    od[count] = od[count].astype(int)
    return (od.groupby(["県", mesh, link])
              .agg(方向=(direction, lambda s: "・".join(sorted(set(s)))),
                   収録日数=(date, "nunique"),
                   行数=(count, "size"),
                   台数=(count, "sum"))
              .reset_index()
              .sort_values(["県", "台数"], ascending=[True, False]))


def _meta_of(meta: dict, file_name: str) -> dict:
    for info in meta.get("files", []):
        if info.get("file") == file_name:
            return info
    return {}


def _fmt_size(n) -> str:
    try:
        return f"{float(n) / 1e6:,.1f}MB"
    except (TypeError, ValueError):
        return "-"


def _period(info: dict) -> str:
    """収録期間。断面データは年月日の列から、経路データは日時から。"""
    def ymd(v):
        v = str(v or "")
        return f"{v[:4]}/{v[4:6]}/{v[6:8]}" if len(v) == 8 else v

    date = info.get("date") or {}
    if date:
        return f"{ymd(date.get('from'))}〜{ymd(date.get('to'))}"
    return (f"{str(info.get('link_enter_from', ''))[:10]}〜"
            f"{str(info.get('link_enter_to', ''))[:10]}").replace("-", "/")


def _layout_frame(spec: dict, extras: dict) -> pd.DataFrame:
    """置き場から読んだ定義を表にする（項目名・桁数・単位・内容・実データ）。"""
    rows = []
    for i, col in enumerate(spec.get("columns", []), start=1):
        name, digits, unit, desc, check = (list(col) + [""] * 5)[:5]
        rows.append({"項目番号": i, "項目名": name, "桁数": digits, "単位": unit,
                     "内容（仕様書）": desc, "実データの確認": check})
    for extra in spec.get("extras", []):
        rows.append({"項目番号": "＋", "項目名": extra, "桁数": "-", "単位": "-",
                     "内容（仕様書）": "（このダッシュボードで追加した列）",
                     "実データの確認": extras.get(extra, "")})
    return pd.DataFrame(rows)


def _download_block(file_name: str, key: str, info: dict) -> None:
    """47MBを毎回読まないよう、押されてから用意する2段構えにする。"""
    state_key = f"transtron_ready_{key}"
    cols = st.columns([1, 1])
    with cols[0]:
        if st.button("ファイルを用意する", key=f"transtron_prep_{key}"):
            st.session_state[state_key] = True
    if st.session_state.get(state_key):
        try:
            data = load_file(file_name)
        except private_store.PrivateDataUnavailable as e:
            st.warning(str(e))
            return
        with cols[1]:
            st.download_button(
                f"{file_name} をダウンロード（{_fmt_size(len(data))}）",
                data=data, file_name=file_name, mime="application/gzip",
                key=f"transtron_dl_{key}")
    else:
        st.caption(
            f"gzip圧縮で{_fmt_size(info.get('bytes_gz'))}あります。"
            "押すと読み込んでからダウンロードのボタンが出ます。")


# 提供元。日野データシステムのデータは受け取り待ち。
PROVIDERS = [
    ("トランストロン", "受領済み"),
    ("日野データシステム", "受け取り待ち"),
]
WIP_NOTE = (
    "**分析結果の可視化は作業中です。** いまはデータの配布だけを行っています。"
)


def _provider_note() -> None:
    """提供元の状況。データが読めるかどうかにかかわらず出す。"""
    st.markdown(WIP_NOTE)
    st.caption(
        "提供元: "
        + " ／ ".join(f"{name}（{state}）" for name, state in PROVIDERS)
        + "。受け取ったものから順にこのタブへ足していきます。"
    )


def render_tab() -> None:
    st.subheader("商用車プローブデータ")
    _provider_note()
    if not available():
        st.info(PREPARING)
        return
    try:
        meta = load_meta()
        doc = load_doc()
    except private_store.PrivateDataUnavailable as e:
        st.warning(str(e))
        return

    st.markdown("#### トランストロン")
    st.caption(doc.get("source_note", ""), unsafe_allow_html=True)
    if doc.get("terms_note"):
        st.warning(doc["terms_note"])
    if doc.get("intro"):
        st.markdown(doc["intro"])

    datasets = doc.get("datasets", {})
    extras = doc.get("extra_columns", {})
    # 置き場にあるファイルの順序（metaの並び）に合わせる
    files = [i["file"] for i in meta.get("files", []) if i["file"] in datasets]

    st.dataframe(pd.DataFrame([
        {"データ": datasets[f].get("title", f),
         "ファイル": f,
         "行数": f"{_meta_of(meta, f).get('rows', 0):,}",
         "列数": len(_meta_of(meta, f).get("columns", [])),
         "期間": _period(_meta_of(meta, f)),
         "大きさ(gz)": _fmt_size(_meta_of(meta, f).get("bytes_gz"))}
        for f in files
    ]), use_container_width=True, hide_index=True)
    st.caption(
        f"束ねた日時: {meta.get('built_at', '-')} ／ 読み込み元: "
        f"{private_store.source_label(LOCAL_DIR, META_FILE, SECTION, ENV_PREFIX)}"
    )

    tabs = st.tabs([datasets[f].get("short", f) for f in files])
    for tab, file_name in zip(tabs, files):
        spec = datasets[file_name]
        info = _meta_of(meta, file_name)
        with tab:
            st.markdown(f"**{spec.get('title', file_name)}**")
            if spec.get("note"):
                st.markdown(spec["note"])
            if spec.get("sources"):
                st.caption(f"元の配布ファイル: {spec['sources']}")
            if spec.get("combine"):
                st.caption(f"束ね方: {spec['combine']}")
            if info.get("rows_before_sum"):
                merged = info["rows_before_sum"] - info["rows"]
                st.caption(
                    f"連結すると{info['rows_before_sum']:,}行で、キーで足し合わせると"
                    f"{info['rows']:,}行（{merged:,}行が合算）になります。"
                )
            _download_block(file_name, spec.get("short", file_name), info)

            st.markdown("**データレイアウト**")
            st.dataframe(_layout_frame(spec, extras),
                         use_container_width=True, hide_index=True)
            with st.expander("先頭200行を見る"):
                try:
                    st.dataframe(load_head(file_name),
                                 use_container_width=True, hide_index=True)
                except private_store.PrivateDataUnavailable as e:
                    st.warning(str(e))

    if doc.get("combine_evidence"):
        with st.expander("同じキーが複数ファイルに現れる理由と、足し合わせの裏付け"):
            st.markdown(doc["combine_evidence"])

    with st.expander("断面リンクの一覧（実データから拾ったもの）"):
        if doc.get("sections_note"):
            st.markdown(doc["sections_note"])
        try:
            st.dataframe(load_sections(), use_container_width=True,
                         hide_index=True)
        except private_store.PrivateDataUnavailable as e:
            st.warning(str(e))
        if doc.get("sections_caption"):
            st.caption(doc["sections_caption"])

    with st.expander("Bゾーンコードを市区町村に読み替える"):
        st.markdown(bzone.RULE)
        if doc.get("bzone_note"):
            st.markdown(doc["bzone_note"])
        if bzone.available():
            table = bzone.load_table()
            meta_b = bzone.load_meta()
            st.dataframe(table.head(30), use_container_width=True,
                         hide_index=True)
            st.caption(
                f"{len(table):,}行。出所: {meta_b.get('source', '-')}"
                f"（取得 {str(meta_b.get('fetched_at', ''))[:10]}）。"
                "上は先頭30行です。"
            )
            st.download_button(
                "市区町村コード表をダウンロード（municipality_codes.csv）",
                data=table.to_csv(index=False).encode("utf-8-sig"),
                file_name="municipality_codes.csv", mime="text/csv",
                key="bzone_dl")
        else:
            st.info(
                "市区町村コード表がありません。"
                "`python scripts/build_bzone_table.py` を実行してください。"
            )
        st.caption(
            "ゾーン内訳（市区町村内のどのゾーンか）まで要る場合は、"
            "道路交通センサスのゾーン区分表が別に必要です。"
        )

    with st.expander("元の配布ファイルとの対応"):
        if doc.get("delivery_note"):
            st.markdown(doc["delivery_note"])
        st.dataframe(pd.DataFrame([
            {"配布": label, "ZIP": z}
            for label, zips in meta.get("deliveries", {}).items() for z in zips
        ]), use_container_width=True, hide_index=True)
        if doc.get("delivery_caption"):
            st.caption(doc["delivery_caption"])

    with st.expander("レイアウト表（Excel）"):
        if st.button("レイアウト表を用意する", key="transtron_prep_layout"):
            st.session_state["transtron_ready_layout"] = True
        if st.session_state.get("transtron_ready_layout"):
            try:
                st.download_button(
                    f"{LAYOUT_XLSX} をダウンロード",
                    data=load_file(LAYOUT_XLSX), file_name=LAYOUT_XLSX,
                    mime=("application/vnd.openxmlformats-officedocument"
                          ".spreadsheetml.sheet"),
                    key="transtron_dl_layout")
            except private_store.PrivateDataUnavailable as e:
                st.warning(str(e))
        if doc.get("layout_caption"):
            st.caption(doc["layout_caption"])

    notes = doc.get("analysis_notes") or []
    if notes:
        with st.expander("分析にあたっての注意と、別途必要になるデータ"):
            st.markdown("\n".join(f"- {n}" for n in notes))

    st.markdown("#### 日野データシステム")
    st.info(
        "データを受け取り次第、トランストロンと同じようにここへ置きます"
        "（提供条件の確認が済んだものから）。"
    )
