"""
「通れる道マップ」版の通行規制だけを単体で見るための入口。

中身は modules/mlit_map_view.py にあり、ダッシュボード本体（app.py）の
タブからも同じものを出している。こちらは開発中に単体で確認したいとき用。

    streamlit run app_mlit_map.py
"""
import streamlit as st

from modules.mlit_map_view import render


def main() -> None:
    st.set_page_config(page_title="通れる道マップ版 通行規制", layout="wide")
    render(standalone=True)


if __name__ == "__main__":
    main()
