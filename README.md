# honda-rag

RAG local sobre o manual de serviço Honda Civic 1992–1995. Arquitetura e roadmap em [PLAN.md](PLAN.md).

## Setup (Fase 0) — Windows + Rancher Desktop

Comandos para PowerShell, na raiz do projeto.

### 1. Rancher Desktop
Em *Preferences → Container Engine*, veja qual motor está ativo:
- **dockerd (moby)** → use `docker compose ...` (recomendado)
- **containerd** → use `nerdctl compose ...` (mesmos argumentos)

### 2. Configuração
```powershell
Copy-Item .env.example .env
```
Confira no `.env` o caminho do `TESSERACT_CMD`. Se já existir um Postgres local na porta 5432, troque `PG_PORT` (ex.: 5433).

### 3. Postgres + pgvector
```powershell
docker compose up -d
docker compose ps          # aguarde o status "healthy"
```
O esquema (`src/honda_rag/db/schema.sql`) é aplicado automaticamente na primeira subida.
Para recriar o banco do zero: `docker compose down -v; docker compose up -d`.

### 4. Ambiente Python
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

### 5. Modelos do Ollama (~12 GB de download)
```powershell
ollama pull qwen3:8b
ollama pull qwen2.5vl:7b
ollama pull bge-m3
```

### 6. Ajustes do Ollama para 8 GB de VRAM
```powershell
setx OLLAMA_MAX_LOADED_MODELS 1
setx OLLAMA_FLASH_ATTENTION 1
setx OLLAMA_KV_CACHE_TYPE q8_0
```
Feche o Ollama pela bandeja do sistema, abra de novo e abra um terminal novo.

### 7. Verificação
```powershell
python scripts/check_env.py
```
Confere Tesseract, Ollama e modelos (inclui um embedding de teste), Postgres/pgvector e esquema, o PDF e um OCR de teste na página 42 (tabela 3-2).

## Uso (piloto: páginas 25–110)

Pipeline de ingestão (idempotente; cada etapa pode ser repetida):
```powershell
python -m honda_rag.ingest.run --stage ocr      # rasteriza (300 DPI) + layout + OCR Tesseract  (~70 s p/ 86 páginas)
python -m honda_rag.ingest.run --stage footer   # relê só o rodapé (dígitos) p/ os rótulos de página (~5 min p/ 1.434 páginas)
python -m honda_rag.ingest.run --stage load     # estrutura, specs, chunks, figuras no Postgres (recria o documento)
python -m honda_rag.ingest.run --stage embed    # embeddings bge-m3 dos chunks pendentes
```
Perguntar pela linha de comando ou pela UI:
```powershell
python -m honda_rag.cli "Qual o torque dos parafusos do cabeçote?" --engine D16Z6 --debug
streamlit run src/honda_rag/ui/app.py
```
Avaliação (recuperação, exatidão numérica, recusa, violações do validador):
```powershell
python eval/run_eval.py --load
```

### Estado (Fases do PLAN.md)
| Fase | Estado |
|---|---|
| 0 Benchmark | Ambiente verificado. Só Tesseract foi avaliado; PaddleOCR/Docling e a comparação de VLMs **não foram feitos** |
| 1 Ingestão do piloto | Feita: layout por réguas (2 colunas, tabelas), OCR com confiança, normalização, rótulos de página por sequência, procedimentos pelo sumário |
| 2 RAG de texto | Feito: intenção + tradução, SQL + híbrida (RRF), geração, validador numérico, CLI. Sem reranker |
| 3 Dados estruturados | Parcial: specs das p. 42–53 (fill-down, mm×pol), torques das figuras ligados ao passo, ferramentas, part numbers, DTC. **Falta** app de revisão e verificação manual (`verified` está sempre `FALSE`) |
| 4 Figuras | Parcial: recortes salvos e exibidos. **Falta** VLM (legendas, callouts, fluxogramas) e vínculo figura↔passo |
| 5 UI | Feita (Streamlit local) |
| 6 Manual completo | OCR das 1.434 páginas feito. Seções derivadas do sumário (`SECTION_STARTS`) e rótulos por âncoras locais do rodapé. Falta rodar `load`/`embed` e avaliar fora do piloto |

### Limitações conhecidas
- Callouts de figuras com texto sobreposto (ex.: p. 6-4) saem embaralhados; torques incompletos vão para `review_queue`.
- O parser de tabelas cobre as p. 42–53 quando as réguas são detectadas (p. 48, 50, 52 caem só no texto). Tabelas de *Design Specifications* (p. 54+) não têm parser.
- Numeração de passos em respostas geradas pelo LLM 8B pode ser confundida; por isso procedimentos trazem também os **passos originais em inglês**, extraídos sem LLM.
- O conjunto de avaliação (17 perguntas) foi usado para ajustar o sistema: a taxa alta não prova generalização.
- Rótulos: a etapa `footer` relê o canto do rodapé a 600 DPI só com dígitos (leu 1.194 rótulos vs 904 do OCR da página). Ficam sem rótulo ~240 páginas, quase todas na parte de esquemas da elétrica (~p. 1.215+), onde o rodapé traz números de esquema (`6-1`, `10-3`, `14-1`) que colidem com rótulos de outras seções. Ficam vazios até definir um prefixo próprio (ex.: `E6-3`).
- Elétrica (p. 936+): o sumário acaba na p. 935; o pipeline cria procedimentos a partir do título/subtítulo grande do topo de cada página (`structure.synthetic_entries`, ~280 procedimentos). Títulos com OCR ruim podem sair truncados ou herdar o título da página anterior.
- Para o manual completo: `--stage load --pages 25-1458` (o padrão continua sendo `PILOT_PAGES`).

### Provedor de LLM (chat)
`LLM_PROVIDER` no `.env` escolhe quem responde e classifica a pergunta; os embeddings continuam locais (bge-m3).

| Valor | Modelo (padrão) | Chave no `.env` |
|---|---|---|
| `ollama` (padrão) | `LLM_MODEL=qwen3:8b` | nenhuma |
| `claude` | `CLAUDE_MODEL=claude-haiku-4-5` | `ANTHROPIC_API_KEY` |
| `gemini` | `GEMINI_MODEL=gemini-2.5-flash` | `GEMINI_API_KEY` |

Com `claude`/`gemini`, trechos do manual são enviados à API e o contexto da resposta sobe de 9.000 para 24.000 caracteres (`CONTEXT_CHARS`, `CONTEXT_CHUNKS`). Para comparar provedores no mesmo conjunto de perguntas, sem editar o `.env`:
```powershell
$env:LLM_PROVIDER="claude"; python eval/run_eval.py
$env:LLM_PROVIDER="gemini"; python eval/run_eval.py
```
