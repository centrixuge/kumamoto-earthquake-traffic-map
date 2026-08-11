"""
「通れる道マップ」版の規制だけを単体で見るための入口。

本体（app.py）のタブ「地図・時系列（通れる道マップ版・ベータ）」は、
これに観測点と時系列を重ねたもの。こちらは規制の地図と一覧だけを
手早く確認したいとき用。

    streamlit run app_mlit_map.py
"""
import streamlit as st

from modules import mlit_map_view


def main() -> None:
    st.set_page_config(page_title="通れる道マップ版 通行規制", layout="wide")
    st.title("通行規制（「通れる道マップ」版）")
    data = mlit_map_view.load_regulations()
    if data is None:
        st.info(
            "データがありません。"
            "`python scripts/build_mlit_map_regulations.py` で作成してください。"
        )
        return
    st.caption(mlit_map_view.source_note(data))

    col_map, col_side = st.columns([3, 2])
    with col_map:
        st.markdown(mlit_map_view.legend_html(data), unsafe_allow_html=True)
        from streamlit_folium import st_folium
        st_folium(
            mlit_map_view.build_map(data), height=560, width=700,
            returned_objects=[], key="mlit_map_standalone",
        )
    with col_side:
        st.markdown("**道路種別ごとの件数**")
        st.markdown(mlit_map_view.summary_html(data), unsafe_allow_html=True)

    st.dataframe(
        mlit_map_view.regulation_table(data),
        use_container_width=True, height=380,
    )


if __name__ == "__main__":
    main()
