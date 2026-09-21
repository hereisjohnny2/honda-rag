# Plano — Seleção de modelo de IA (Gemini, Grok, Hugging Face, Claude, Ollama)

> **Status: fases 1–5 implementadas** (registro de provedores, adaptador Grok/HF, escolha em runtime,
> seletor na UI, CLI/eval/`check_env`/docs). Não testado contra as APIs reais nem contra um Postgres com
> dados — só verificado localmente (sintaxe, wiring do registro, contextvars, e o adaptador HTTP contra
> um servidor fake). As **fases 6 (guardar conversas)** e o **nível 2 de chaves (cofre na UI)** da seção 4
> continuam só planejadas: dependem de decisões em aberto (seção 7) que ainda não foram tomadas.

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
- Campo `type="password"` para colar uma chave **só na sessão** (`st.session_state`, nunca gravada em
  disco, nunca logada) serve para testar um provedor sem reiniciar o servidor.
- Guardar a chave **na VPS** (em vez de só no `.env.prod`) tem três níveis de esforço e um limite honesto
  do que a cifra protege: **ver a seção 4**.

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

## 4. Onde entram as chaves de API

Resposta curta: **hoje, no `.env.prod` do servidor** — é o único lugar de onde o app lê chave
(`docker-compose.prod.yml:32`, `env_file: .env.prod`). Abaixo, o que já existe, o que dá para melhorar
barato, e o que custa mais caro (digitar a chave pela própria UI).

### Nível 0 — `.env.prod` (é assim hoje; continua sendo a fonte da verdade)

```bash
ssh usuario@IP && cd /opt/honda-rag
nano .env.prod            # GEMINI_API_KEY=...  XAI_API_KEY=...  HF_TOKEN=...
chmod 600 .env.prod
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d app   # recria só o app
```

Já está razoável: fora do git (`.gitignore`), `600`, entregue só ao contêiner `app`, nunca exibido na UI.

Limitações honestas:

- trocar uma chave exige SSH e recriar o contêiner;
- o valor aparece em `docker inspect app` e em `/proc/<pid>/environ` dentro do contêiner — quem já tem
  root na VPS ou está no grupo `docker` lê de qualquer jeito (esse grupo equivale a root);
- variável de ambiente é herdada por todo subprocesso e costuma aparecer em dump de crash.

### Nível 1 — Docker secrets (arquivo em vez de variável) — **recomendado**

```yaml
# docker-compose.prod.yml
secrets:
  xai_api_key: { file: ./secrets/xai_api_key }     # chmod 600, dono do usuário, fora do git
  hf_token:    { file: ./secrets/hf_token }
services:
  app:
    secrets: [xai_api_key, hf_token, gemini_api_key, anthropic_api_key]
    environment:
      XAI_API_KEY_FILE: /run/secrets/xai_api_key
      HF_TOKEN_FILE:    /run/secrets/hf_token
```

No código, uma função só (~10 linhas), usada no lugar de `os.getenv` para chave:

```python
# config.py
def secret(name: str) -> str:
    """Convenção *_FILE (Docker secret) tem prioridade; senão a variável de ambiente."""
```

Ganho: a chave sai de `docker inspect`, de `/proc/environ` e dos subprocessos; `/run/secrets` é `tmpfs`.
Custo: um arquivo por chave e quatro linhas no compose. Cabe na **fase 5** do plano, sem tocar na UI.

### Nível 2 — digitar a chave na própria UI, cifrada na VPS

Só vale a pena se o objetivo for **trocar de provedor sem SSH**. Quatro decisões:

**a) Quem pode.** O `basic_auth` do Caddy é um login único e compartilhado (`mecanico`,
`deploy/make_auth.sh`): quem usa o chat entraria também na tela de chaves. Então a página "Configurações"
precisa de **senha própria** — `ADMIN_PASSWORD_HASH` (bcrypt) no `.env.prod`, conferida no app. Sem isso,
qualquer pessoa com o login do site troca a chave e gasta o seu crédito.

**b) Onde grava: arquivo em volume dedicado, _não_ no Postgres.** O `deploy/backup.sh` faz `pg_dump`
diário em `./backups` e esses dumps são copiados para fora da VPS; chave em tabela viraria chave
espalhada em backup. Volume novo, e o `./data` continua `:ro`:

```yaml
    volumes:
      - ./data:/app/data:ro
      - vault:/app/secrets          # gravável só pelo uid 1000 (usuário `app` do Dockerfile)
```

**c) Cifra: `Fernet` (AES-128-CBC + HMAC) da lib `cryptography`** — dependência nova no `pyproject.toml`.
Chave-mestra `SECRETS_KEY` no `.env.prod`, gerada uma vez com `Fernet.generate_key()`.

**d) O que isso protege de verdade** — vale dizer claramente, porque "cifrado" soa mais forte do que é:

| Protege contra | Não protege contra |
|---|---|
| Cópia do volume, do backup ou de um `scp` errado | Quem tem root na VPS ou está no grupo `docker` |
| `cat` casual no arquivo, chave em log | Execução de código dentro do app |
| Commit acidental no git | — |

O motivo é estrutural: o app precisa decifrar sozinho ao subir (sem alguém digitar senha a cada reboot),
então a chave-mestra mora no mesmo host que o texto cifrado. Segredo que resiste a root exige cofre fora
da máquina (Vault, Infisical, Doppler, KMS do provedor) — fica para quando fizer sentido.

**Na tela:** a chave nunca é exibida de volta, só `AIza…4f2c` (4 últimos caracteres) com os botões
*Substituir*, *Remover* e *Testar* (ping de `max_tokens=5`). Entrada em `st.text_input(type="password")`.
Nunca em log, nunca em `out["debug"]`.

### Ordem de precedência (a implementar em `config.secret()`)

```
1. chave digitada na sessão      (só memória, some ao fechar a aba)   ← teste rápido
2. cofre cifrado do servidor     (nível 2, trocada pela UI)
3. *_FILE / Docker secret        (nível 1)
4. variável do .env.prod         (nível 0)
5. nada → o provedor aparece desabilitado na lista, dizendo o que falta
```

### Recomendação

| Quando | O quê |
|---|---|
| Agora | Nível 0. É onde as chaves entram hoje e resolve o "guardar na VPS de forma segura" para o uso atual |
| Fase 5 do plano | Nível 1. Barato, não mexe na UI, tira a chave do `docker inspect` |
| Só se quiser trocar provedor sem SSH | Nível 2. Traz dependência nova, senha de admin e uma superfície de ataque a mais |

Independente do nível, três hábitos valem mais que a cifra:

- **limite de gasto/alerta** na conta de cada API (xAI, Google, HF) — o login do site é compartilhado;
- **rotacionar** a chave do Gemini que já foi colada em chat (`deploy/DEPLOY.md:12` já avisa) e qualquer
  chave que passe por canal não confiável;
- `secrets/` e `vault` no `.gitignore` e fora do `backup.sh`.

---

## 5. Guardar as conversas no servidor

Hoje o histórico vive em `st.session_state["history"]` (`ui/app.py:45`): memória do processo. Fechou a
aba, perdeu; recriou o contêiner, perdeu tudo. O lugar natural para guardar é o **Postgres que já está no
compose** — sem serviço novo, com backup diário já configurado (`deploy/backup.sh`), e o `rag.answer()`
já devolve num dict tudo o que interessa.

### 5.1 A armadilha: `public` faz a conversa morrer na próxima ingestão

`deploy/export_data.ps1` faz `pg_dump` do banco **inteiro** na sua máquina e `deploy/restore.sh` roda
`pg_restore --clean --if-exists` no servidor. Uma tabela de conversas em `public` estaria nos dois lados:
a versão vazia que veio do seu PC **substitui** a do servidor. Ou seja, repetir os passos 5–6 do
`DEPLOY.md` (rotina de "novos dados") apagaria o histórico, em silêncio.

Correção: esquema separado e uma flag no export.

```powershell
# deploy/export_data.ps1
pg_dump -Fc --no-owner --no-privileges --exclude-schema=chat ...
```

Com isso o `--clean` nunca encosta no `chat`, e o histórico sobrevive a quantas reingestões você fizer.

### 5.2 Esquema

```sql
CREATE SCHEMA IF NOT EXISTS chat;

CREATE TABLE chat.conversations (
  id           UUID PRIMARY KEY,
  started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  auth_user    TEXT                        -- login do Caddy, se repassado (5.4)
);

CREATE TABLE chat.turns (
  id              BIGSERIAL PRIMARY KEY,
  conversation_id UUID NOT NULL REFERENCES chat.conversations(id) ON DELETE CASCADE,
  n               INT NOT NULL,            -- ordem dentro da conversa
  asked_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  question        TEXT NOT NULL,
  answer          TEXT NOT NULL,
  refused         BOOLEAN NOT NULL,
  intent          TEXT,
  engine          TEXT,                    -- perfil do veículo no momento da pergunta
  trans           TEXT,
  provider        TEXT,                    -- 'grok'         ┐ só a partir da fase 3;
  model           TEXT,                    -- 'grok-4-fast'  ┘ nulos até lá
  sources         TEXT[],                  -- {'6-3','6-4'}
  figures         TEXT[],                  -- caminhos das figuras mostradas
  violations      JSONB,                   -- o que o validador cortou
  best_cos        REAL,                    -- melhor cosseno (calibrar MIN_COSINE com dado real)
  seconds         REAL,
  rating          SMALLINT,                -- 👍 +1 / 👎 -1; nulo = sem voto
  UNIQUE (conversation_id, n)
);
CREATE INDEX turns_asked_idx ON chat.turns (asked_at DESC);
```

Gravar sai quase de graça: `rag.answer()` já devolve `question`, `answer`, `refused`, `intent`,
`sources`, `figures` e `violations`; `best_cos` e `seconds` hoje só existem dentro de `out["debug"]`
(`rag.py:99-100`) e passam a sair sempre.

### 5.3 Onde chamar

Módulo novo `honda_rag/chat_log.py` com `record(conv_id, n, out)`, chamado pelo **`ui/app.py`** logo
depois do `rag.answer` — **não** de dentro do `rag.answer()`:

- `eval/run_eval.py` chama `rag.answer` 17 vezes a cada rodada e poluiria o histórico;
- a CLI idem (se quiser gravar, um `--log` explícito).

Gravação sempre em `try/except` com aviso em log: banco cheio ou indisponível **não pode** impedir o
mecânico de ver a resposta que já está pronta na tela.

### 5.4 Identidade da conversa

`conv_id = uuid4()` guardado no `st.session_state` na primeira pergunta. O `basic_auth` do Caddy é um
login único e compartilhado, então `auth_user` só ganha sentido se um dia houver mais de um usuário — aí
dá para repassar com `header_up X-Auth-User {http.auth.user.id}` no `Caddyfile` e ler em
`st.context.headers` (confirmar na versão do Streamlit instalada; `pyproject.toml` pede só `>=1.35`).

### 5.5 Para que serve — isso decide o resto

| Objetivo | O que precisa |
|---|---|
| **a) Continuidade** — reabrir o navegador e achar a conversa de ontem | gravar + **ler de volta** na UI, com lista de conversas no sidebar |
| **b) Melhorar o sistema** — ver o que perguntam de verdade | só gravar + um export (`COPY ... TO CSV`) |

**(b) é o de maior valor aqui, e o mais barato.** O conjunto de avaliação tem 17 perguntas que, pelo
próprio README, foram usadas para ajustar o sistema — a taxa alta não prova generalização. Pergunta real
gravada vira pergunta nova no `eval/questions.yaml`; `refused = true` mostra onde a busca (ou o manual)
está falhando; e com `provider`/`model` na mesma tabela dá para comparar Gemini × Grok × HF **no uso
real**, não só no eval.

O **voto 👍/👎** (coluna `rating`) custa dois botões no `render()` e multiplica o valor do log: sem ele
você sabe o que perguntaram, com ele sabe o que deu errado.

### 5.6 Retenção e privacidade

Volume é irrelevante: ~100 perguntas/dia × ~4 KB ≈ 12 MB/ano. Ainda assim:

- definir retenção (ex.: 180 dias) no mesmo cron do backup:
  `DELETE FROM chat.turns WHERE asked_at < now() - interval '180 days';`
- as respostas contêm trechos do manual protegido por direitos autorais, e o backup sai da VPS — vale o
  mesmo cuidado de acesso que o resto;
- a pergunta do mecânico pode conter placa, nome de cliente ou dado do carro. Se isso acontecer, a
  retenção curta é a proteção mais barata.

### 5.7 Alternativa mais simples: JSONL em volume

`data/chat/2026-09.jsonl`, uma linha por turno, sem DDL e sem tocar no banco. Bom para (b), ruim para (a)
e para qualquer consulta agregada. Precisaria de um volume gravável — hoje `./data` é montado `:ro`
(`docker-compose.prod.yml:39`), mesma situação do cofre da seção 4. Só recomendo se a ideia for nunca ler
de volta pela UI.

### 5.8 Fase

Cabe como **fase 6**, depois do seletor: as colunas `provider`/`model` só fazem sentido com a fase 3
pronta. Mas é independente — dá para fazer antes, deixando as duas colunas nulas.

---

## 6. Riscos

| Risco | Mitigação |
|---|---|
| Modelo novo inventa número ou cita página errada | O validador (`generation/validate.py`) é agnóstico de provedor e já corta linha com valor fora do contexto — vale igual para Grok e HF; medir com `run_eval.py --provider` antes de adotar |
| HF sem modo JSON nativo quebra `intent.analyze()` | `response_format` só quando declarado; instrução de JSON puro no prompt + `extract_json()` tolerante (mesma solução do Claude) |
| Modelo pequeno do HF ignora "responda em português" ou o formato de citação | Comparar no eval; marcar no README quais modelos passaram |
| Pessoa troca o embedding sem querer | Não exposto na UI; só leitura. `hybrid.py:21` já recusa a busca com mensagem clara |
| Chave vazando em log ou na tela | UI mostra só estado; chave de sessão nunca persistida; nada de chave em `out["debug"]` |
| Latência/custo de API por engano | Rodapé com provedor e tempo em cada resposta; `--provider` no eval mede antes de trocar o padrão |
| Quem usa o chat troca/apaga chave pela UI | O `basic_auth` do Caddy é login único: a tela de chaves pede senha própria (`ADMIN_PASSWORD_HASH`) — seção 4 |
| Histórico de conversas apagado pela próxima ingestão | Esquema `chat` + `--exclude-schema=chat` no `export_data.ps1`; o `--clean` do `pg_restore` não o alcança (seção 5.1) |
| Falha ao gravar a conversa derruba a resposta | `record()` em `try/except`: a resposta na tela nunca depende do log |
| Eval poluindo o histórico | `record()` é chamado pela UI, não por `rag.answer()` |
| Chave de API em backup do Postgres | Cofre em volume próprio, fora do `pg_dump` do `deploy/backup.sh` |
| Regressão na refatoração do `llm.py` | Fase 1 é refatoração pura: rodar `run_eval.py` com os 3 provedores atuais antes de seguir |

## 7. Pendências a confirmar com você

1. **Chaves disponíveis** — já tem `XAI_API_KEY` e `HF_TOKEN`, ou o plano precisa prever o cadastro?
2. **Modelo do HF** — algum preferido (Llama 3.3 70B, Qwen, Mistral), ou deixo um padrão e a UI permite trocar?
3. **Campo de chave na UI** (fase 5, opcional) — útil para testar, mas é chave em tela num PC de oficina. Entra?
4. **Conversas** — o objetivo é (a) continuidade para quem usa, (b) material para melhorar o sistema,
   ou os dois? E qual retenção (sugestão: 180 dias)?
5. **Escopo do seletor** — só o chat, ou também o LLM que classifica a pergunta (`intent.analyze`)?
   O plano acima usa **o mesmo** provedor para os dois; separar é possível, mas dobra a configuração.
