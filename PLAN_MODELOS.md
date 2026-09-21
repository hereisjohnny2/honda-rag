# Plano — Seleção de modelo de IA (Gemini, Grok, Hugging Face, Claude, Ollama)

Objetivo: trocar o provedor de LLM **na interface**, sem editar o `.env` nem reiniciar o Streamlit, e
acrescentar dois provedores novos (**Grok/xAI** e **Hugging Face**) aos três já existentes
(`ollama`, `claude`, `gemini`).

Fora de escopo (e por quê): o **provedor de embeddings** (`EMBED_PROVIDER`) continua só no `.env`.
Os vetores do banco e os da pergunta precisam vir do mesmo modelo; trocar exige
`--stage embed` (recalcula os 1.638 chunks) e recalibrar `MIN_COSINE`. Deixar isso num seletor da UI
quebraria a busca em silêncio. A UI mostra o embedding ativo apenas como texto (somente leitura).

---

## 1. Situação atual

| Onde | O que acontece hoje |
|---|---|
| `config.py:44` | `LLM_PROVIDER` lido do `.env` **no import**; vale para todo o processo |
| `config.py:51-54` | `CONTEXT_CHARS`/`CONTEXT_CHUNKS` derivam do provedor **no import** (9.000/5 local, 24.000/8 API) |
| `llm.py:124-131` | `chat()` decide por `if config.LLM_PROVIDER == ...`; cadeia de `if` por provedor |
| `llm.py:150-158` | `chat_json()` repete a cadeia para ajustar modo JSON e `max_tokens` |
| `llm.py:14-56` | Clientes SDK em singletons de módulo (`_claude_client`, `_gemini_client`) |
| `ui/app.py:33-42` | Sidebar tem só motor/transmissão; nada sobre modelo |
| `rag.py`, `intent.py` | Chamam `llm.chat`/`llm.chat_json` sem saber de provedor — **bom**, não precisam mudar muito |

Três problemas a resolver:

1. **Escolha é por processo, não por sessão.** Um `global` não serve: cada sessão do Streamlit roda numa
   thread própria e o app de produção atende mais de uma pessoa.
2. **Cadeia de `if` não escala.** Com 5 provedores vira 5 ramos em `chat()` e mais 5 em `chat_json()`.
3. **Orçamento de contexto é constante de import.** Se o provedor muda em runtime, 9.000 vs 24.000
   caracteres precisa ser decidido em runtime também.

---

## 2. Decisões de projeto

### 2.1 Grok e Hugging Face entram pelo mesmo adaptador

Os dois expõem API **compatível com OpenAI** (`POST {base}/chat/completions`, `Authorization: Bearer`).
Um único adaptador HTTP com `requests` (já é dependência) atende os dois — e, de brinde, OpenRouter,
Groq, vLLM local ou qualquer outro compatível, só somando uma linha no registro.

| Provedor | `base_url` | Chave | Modelo (exemplo) |
|---|---|---|---|
| `grok` | `https://api.x.ai/v1` | `XAI_API_KEY` | `grok-4-fast`, `grok-3-mini` |
| `hf` | `https://router.huggingface.co/v1` | `HF_TOKEN` | `meta-llama/Llama-3.3-70B-Instruct` |

> Os nomes de modelo mudam com frequência (a mesma ressalva que o `.env.example` já faz para o Gemini):
> confirme na documentação de cada um. No HF, o id pode levar sufixo de backend
> (`...Instruct:together`, `:novita`, `:hf-inference`); sem sufixo o router escolhe.

**Sem novas dependências.** Nada de `openai` no `pyproject.toml`: o adaptador é ~40 linhas de `requests`,
e o `verify=_ssl_context()` (proxy corporativo da Fugro) continua valendo do mesmo jeito.

**Modo JSON:** o xAI aceita `response_format={"type":"json_object"}`. No HF depende do backend que
atender — então o adaptador **pede JSON no prompt e passa `response_format` só quando o provedor declara
suporte**; `extract_json()` (já existe, `llm.py:134`) absorve o resto. É a mesma tática já usada no Claude.

### 2.2 Registro de provedores em vez de cadeia de `if`

```python
# honda_rag/providers.py  (novo)
@dataclass(frozen=True)
class Provider:
    key: str                  # "gemini"
    label: str                # "Gemini (Google)"
    kind: str                 # "local" | "api"
    default_model: str        # config.GEMINI_MODEL
    model_env: str            # "GEMINI_MODEL"
    key_env: str | None       # "GEMINI_API_KEY"
    base_url: str | None      # só para os OpenAI-compatíveis
    native_json: bool         # aceita response_format / format=json
    chat: Callable[..., str]  # _chat_gemini, _chat_openai_compat(...), ...

PROVIDERS: dict[str, Provider]   # ollama, claude, gemini, grok, hf
```

Com isso `chat()` vira três linhas (`p = resolve(...); return p.chat(...)`) e `chat_json()` deixa de ter
ramo por provedor: consulta `p.native_json` e `p.json_hint`.

### 2.3 Escolha em runtime: `contextvars`

```python
# honda_rag/llm.py
_active: ContextVar[Choice | None] = ContextVar("llm_active", default=None)

@contextmanager
def use(choice: Choice | None): ...        # define e restaura

def active() -> Choice:                    # override da sessão, senão o do .env
    return _active.get() or Choice(config.LLM_PROVIDER, None)
```

`ContextVar` é isolado por thread/tarefa: duas sessões do Streamlit escolhendo provedores diferentes ao
mesmo tempo não se atrapalham, e um `global` atrapalharia. O `.env` continua sendo o padrão de quem não
mexe em nada — nenhum comportamento existente muda.

`rag.answer()` ganha **um** parâmetro opcional (`choice`) e embrulha o corpo em `with llm.use(choice):`.
Assim `intent.analyze()` e a geração final pegam o provedor escolhido sem precisar receber parâmetro.

### 2.4 Orçamento de contexto em runtime

`CONTEXT_CHARS`/`CONTEXT_CHUNKS` deixam de ser constantes derivadas e viram função:

```python
# config.py
def context_budget(provider_key: str) -> tuple[int, int]:
    """Env explícito vence; senão 24.000/8 para API e 9.000/5 para o 8B local."""
```

Se a pessoa definiu `CONTEXT_CHARS` no `.env`, o valor dela continua mandando (compatível com
`.env.prod.example:24-25`). `expand.build_context()` (`expand.py:16-17`) passa a chamar a função.

### 2.5 Chaves de API

- Origem normal: `.env` / variáveis de ambiente (produção já usa `env_file: .env.prod`, então as novas
  chaves entram no contêiner sem mexer no compose).
- A UI **nunca mostra a chave** — só o estado: `✅ configurada` / `⚠️ ausente`.
- Provedor sem chave aparece na lista **desabilitado**, com a dica de qual variável falta (em vez de
  deixar escolher e estourar erro só na hora de responder).
- Opcional (fase 5): campo `type="password"` para colar uma chave **só na sessão** (`st.session_state`,
  nunca gravada em disco, nunca logada). Vale para testar um provedor sem reiniciar o servidor.

---

## 3. Fases

| # | Entrega | Arquivos | Critério de pronto |
|---|---|---|---|
| 1 | Registro de provedores; `chat()`/`chat_json()` por tabela | `providers.py` (novo), `llm.py` | Os 3 provedores atuais respondem igual a antes; `eval/run_eval.py` sem regressão |
| 2 | Adaptador OpenAI-compatível + Grok + HF | `llm.py`, `providers.py`, `config.py` | Pergunta de torque respondida com citação `[p. x-y]` nos 2 provedores novos |
| 3 | Escolha em runtime (`contextvars`) + orçamento por provedor | `llm.py`, `config.py`, `expand.py`, `rag.py` | Duas chamadas seguidas com provedores diferentes usam modelos diferentes; sem vazamento entre threads |
| 4 | Seletor na UI | `ui/app.py` | Trocar no sidebar muda a resposta seguinte; recarregar a página mantém a escolha da sessão |
| 5 | CLI, eval, `check_env`, docs, `.env` | `cli.py`, `eval/run_eval.py`, `scripts/check_env.py`, `README.md`, `.env*.example`, `deploy/DEPLOY.md` | `--provider` funciona em CLI e eval; tabela comparativa dos 5 provedores no README |

### Fase 1 — Registro (refatoração sem mudança de comportamento)

- Criar `src/honda_rag/providers.py` com o `dataclass` e o dicionário `PROVIDERS`.
- Mover `_chat_claude`, `_chat_gemini`, `_chat_ollama` para funções registradas (ficam onde estão em
  `llm.py`; o registro só aponta para elas).
- `chat()` e `chat_json()` passam a consultar o registro. `_ssl_context()`, `extract_json()` e os
  singletons de cliente não mudam.
- **Guarda de regressão:** `LLM_PROVIDER` desconhecido hoje cai em Ollama em silêncio (`llm.py:131`);
  passa a levantar `RuntimeError` listando os válidos.

### Fase 2 — Grok e Hugging Face

```python
def _chat_openai_compat(p: Provider, messages, model, temperature, max_tokens, json_mode, timeout):
    body = {"model": model, "messages": messages,      # system/user/assistant já no formato certo
            "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    if json_mode and p.native_json:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(f"{p.base_url}/chat/completions", json=body, timeout=timeout,
                      headers={"Authorization": f"Bearer {key}"}, verify=_ssl_context())
    ...
```

Tratamento de erro no padrão dos outros provedores (mensagens em português, como `llm.py:74` e `98`):

- `401`/`403` → `RuntimeError("XAI_API_KEY ausente ou inválida (defina no .env)")`
- `429`/`5xx` → 3 tentativas com recuo exponencial (o SDK da Anthropic já faz isso sozinho; aqui é manual)
- `finish_reason == "length"` → `RuntimeError("resposta do Grok cortada em max_tokens=...")`, igual ao
  que já é feito para Claude e Gemini — resposta truncada com número pela metade é o pior caso deste projeto
- Modelo inexistente (`404`/`400`) → erro citando o id pedido e a variável que o define

O formato de mensagens do projeto (`system`/`user`) já é o formato OpenAI — não precisa de `_split_system`.

### Fase 3 — Runtime

- `Choice = namedtuple("Choice", "provider model")`; `model=None` usa o padrão do `.env`.
- `llm.use()` / `llm.active()`; `rag.answer(..., choice=None)`.
- `config.context_budget()`; `expand.build_context()` consulta o provedor ativo.
- `rag.answer()` devolve `out["provider"]` e `out["model"]` (para a UI mostrar e o eval registrar).
- Cuidado: `_claude_client` / `_gemini_client` são caches globais **do cliente**, não do modelo — seguem
  válidos, porque o modelo vai por chamada.

### Fase 4 — UI

No sidebar, abaixo de "Veículo":

```
Modelo de IA
  [ Gemini · gemini-2.5-flash    ▾ ]     ← só provedores com chave; os sem chave, desabilitados
  ▸ Ajustes avançados
      Modelo: [gemini-2.5-flash    ]     ← texto livre, vazio = padrão do .env
      Embeddings: bge-m3 (local) — fixo, trocar exige reindexar o banco
  ⚠️ Grok: falta XAI_API_KEY no .env
```

- Estado em `st.session_state["llm_choice"]`, inicializado com o `.env` — recarregar mantém a escolha.
- A escolha vale da **próxima pergunta** em diante; o histórico já respondido não é reprocessado.
- Cada resposta ganha um rodapé discreto: `st.caption("Gemini · gemini-2.5-flash · 4,2 s")`. Sem isso,
  comparar provedores no uso real fica no chute.
- `rag.answer(q, engine, trans, choice=st.session_state["llm_choice"])`.

### Fase 5 — CLI, eval, verificação e docs

- `cli.py`: `--provider {ollama,claude,gemini,grok,hf}` e `--model`.
- `eval/run_eval.py`: `--provider` (e aceitar lista, ex. `--provider gemini,grok,hf`, imprimindo uma
  tabela por provedor — é o que responde "qual modelo usar" com dado em vez de opinião).
- `scripts/check_env.py`: bloco novo `== Provedores de LLM` — para cada provedor, chave presente e um
  ping curto (`max_tokens=5`) opcional atrás de `--ping-llm` (não gastar crédito em toda checagem).
- `.env.example` / `.env.prod.example`: `XAI_API_KEY`, `GROK_MODEL`, `HF_TOKEN`, `HF_MODEL`; comentar que
  `LLM_PROVIDER` é só o **padrão** — a UI pode sobrepor.
- `README.md`: a tabela de "Provedor de LLM (chat)" ganha as duas linhas novas e a nota da seleção na UI.
- `deploy/DEPLOY.md`: citar as chaves novas (o `env_file: .env.prod` já leva tudo para o contêiner).

---

## 4. Riscos

| Risco | Mitigação |
|---|---|
| Modelo novo inventa número ou cita página errada | O validador (`generation/validate.py`) é agnóstico de provedor e já corta linha com valor fora do contexto — vale igual para Grok e HF; medir com `run_eval.py --provider` antes de adotar |
| HF sem modo JSON nativo quebra `intent.analyze()` | `response_format` só quando declarado; instrução de JSON puro no prompt + `extract_json()` tolerante (mesma solução do Claude) |
| Modelo pequeno do HF ignora "responda em português" ou o formato de citação | Comparar no eval; marcar no README quais modelos passaram |
| Pessoa troca o embedding sem querer | Não exposto na UI; só leitura. `hybrid.py:21` já recusa a busca com mensagem clara |
| Chave vazando em log ou na tela | UI mostra só estado; chave de sessão nunca persistida; nada de chave em `out["debug"]` |
| Latência/custo de API por engano | Rodapé com provedor e tempo em cada resposta; `--provider` no eval mede antes de trocar o padrão |
| Regressão na refatoração do `llm.py` | Fase 1 é refatoração pura: rodar `run_eval.py` com os 3 provedores atuais antes de seguir |

## 5. Pendências a confirmar com você

1. **Chaves disponíveis** — já tem `XAI_API_KEY` e `HF_TOKEN`, ou o plano precisa prever o cadastro?
2. **Modelo do HF** — algum preferido (Llama 3.3 70B, Qwen, Mistral), ou deixo um padrão e a UI permite trocar?
3. **Campo de chave na UI** (fase 5, opcional) — útil para testar, mas é chave em tela num PC de oficina. Entra?
4. **Escopo do seletor** — só o chat, ou também o LLM que classifica a pergunta (`intent.analyze`)?
   O plano acima usa **o mesmo** provedor para os dois; separar é possível, mas dobra a configuração.
