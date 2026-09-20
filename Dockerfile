# Imagem do app (UI Streamlit + consulta). Só dependências de runtime: OCR/ingestão ficam fora.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Instalação editável de propósito: config.ROOT é derivado do caminho do arquivo (src/honda_rag/config.py
# -> /app), e os dados (data/pages/*/view.webp, data/figures) são resolvidos a partir dele.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e .

# Não roda como root
RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8501
CMD ["streamlit", "run", "src/honda_rag/ui/app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
