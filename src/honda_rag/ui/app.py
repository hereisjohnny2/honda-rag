"""UI local (PC da oficina): perfil do veículo, chat, figuras e "ver página".

    streamlit run src/honda_rag/ui/app.py
"""
from __future__ import annotations

import streamlit as st
from psycopg.rows import dict_row

from honda_rag import config, rag
from honda_rag.db import repo

st.set_page_config(page_title="Honda Civic 92-95 · Manual", page_icon="🔧", layout="wide")


@st.cache_data(ttl=600)
def page_by_label(label: str) -> dict | None:
    with repo.connect() as conn:
        conn.row_factory = dict_row
        return conn.execute("SELECT pdf_page, page_label, image_path, view_path FROM pages WHERE page_label = %s "
                            "ORDER BY pdf_page LIMIT 1", (label,)).fetchone()


def show_page(label: str) -> None:
    p = page_by_label(label)
    if not p:
        st.warning(f"Página {label} não está no banco.")
        return
    st.image(str(config.ROOT / p["view_path"]), caption=f"Página {p['page_label']} (PDF p. {p['pdf_page']})",
             use_container_width=True)


with st.sidebar:
    st.header("Veículo")
    engine = st.selectbox("Motor", config.ENGINES, index=config.ENGINES.index(config.DEFAULT_ENGINE))
    trans = st.radio("Transmissão", ["(qualquer)", "M/T", "A/T"], horizontal=True)
    st.caption("O motor filtra as respostas. D16Y7/D16Y8 não estão neste manual (Civic 1996-2000).")
    st.divider()
    st.caption("Piloto: páginas 25–110 do PDF (seções 1, 3, 4, 5 e 6 até a p. 6-25).")
    if st.button("Limpar conversa"):
        st.session_state.pop("history", None)
        st.rerun()

st.title("Assistente do manual de serviço · Civic 1992–1995")
history: list[dict] = st.session_state.setdefault("history", [])


def render(entry: dict, idx: int) -> None:
    with st.chat_message("assistant"):
        st.markdown(entry["answer"])
        if entry.get("refused"):
            return
        if entry["sources"]:
            st.caption("Fontes (clique para ver a página original):")
            cols = st.columns(min(len(entry["sources"]), 6))
            for i, lab in enumerate(entry["sources"][:6]):
                if cols[i].button(f"p. {lab}", key=f"src-{idx}-{lab}"):
                    st.session_state["viewing"] = lab
        figs = entry.get("figures") or []
        if figs:
            with st.expander(f"Figuras do manual ({len(figs)})", expanded=False):
                cols = st.columns(2)
                for i, f in enumerate(figs[:6]):
                    cols[i % 2].image(str(config.ROOT / f["image_path"]),
                                      caption=f"p. {f['page_label']} · {f['figure_type']}")
        if entry.get("violations"):
            st.warning("O validador removeu linhas com valores que não estão no trecho recuperado.")


for i, turn in enumerate(history):
    with st.chat_message("user"):
        st.write(turn["question"])
    render(turn, i)

if q := st.chat_input("Pergunte em português (ex.: qual o torque dos parafusos do cabeçote?)"):
    with st.chat_message("user"):
        st.write(q)
    with st.spinner("Consultando o manual..."):
        res = rag.answer(q, engine, None if trans == "(qualquer)" else trans)
    history.append(res)
    st.rerun()

if lab := st.session_state.get("viewing"):
    st.divider()
    c1, c2 = st.columns([6, 1])
    c1.subheader(f"Página original {lab}")
    if c2.button("Fechar"):
        st.session_state.pop("viewing")
        st.rerun()
    show_page(lab)
