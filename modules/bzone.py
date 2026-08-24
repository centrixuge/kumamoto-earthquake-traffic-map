"""
Bゾーンコード（道路交通センサス）を市区町村に読み替える。

商用車プローブの集計ODデータの起終点は7桁のBゾーンコードで来る。手元のデータを
調べると **7桁は「市区町村コード5桁＋ゾーン番号2桁」**で、ゾーン番号が1桁のときは
右側が空白になっている。市区町村までの集計であれば、センサスのゾーン区分表が
無くても、総務省の全国地方公共団体コードだけで読み替えられる。

表は `scripts/build_bzone_table.py` が作る（公的な公開データなのでリポジトリに
置いてある）。ゾーン内訳（市区町村内のどこか）まで要る場合は、センサスの
ゾーン区分表が別に必要。
"""
from __future__ import annotations

import io
import json

import pandas as pd
import streamlit as st

from modules import datastore

TABLE_REL = "bzone/municipality_codes.csv"
META_REL = "bzone/municipality_codes_meta.json"

RULE = ("**Bゾーンコード7桁 = 市区町村コード5桁 ＋ ゾーン番号2桁**"
        "（ゾーン番号が1桁のときは右側が空白）。市区町村までならこの規則で読み替えられます。")


def available() -> bool:
    return datastore.exists(TABLE_REL)


@st.cache_data(ttl=3600, show_spinner=False)
def load_table() -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(datastore.read_bytes(TABLE_REL)), dtype=str,
                       encoding="utf-8-sig")


@st.cache_data(ttl=3600, show_spinner=False)
def load_meta() -> dict:
    try:
        return json.loads(datastore.read_bytes(META_REL).decode("utf-8"))
    except Exception:
        return {}


def _lookup() -> dict:
    table = load_table()
    return {row["市区町村コード"]: (row["都道府県"], row["市区町村"])
            for _, row in table.iterrows()}


def city_of(code7: str) -> tuple[str, str, str]:
    """Bゾーンコード → (市区町村コード, 都道府県, 市区町村)。分からなければ空。"""
    code5 = str(code7)[:5]
    pref, city = _lookup().get(code5, ("", ""))
    return code5, pref, city


def attach(frame: pd.DataFrame, column: str, prefix: str = "") -> pd.DataFrame:
    """Bゾーンの列に、市区町村コード・都道府県・市区町村の列を足して返す。"""
    look = _lookup()
    code5 = frame[column].astype(str).str[:5]
    out = frame.copy()
    out[f"{prefix}市区町村コード"] = code5
    out[f"{prefix}都道府県"] = code5.map(lambda c: look.get(c, ("", ""))[0])
    out[f"{prefix}市区町村"] = code5.map(lambda c: look.get(c, ("", ""))[1])
    return out
