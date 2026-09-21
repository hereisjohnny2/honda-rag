"""Cliente de LLM: Ollama (local), Claude, Gemini, Grok (xAI) ou Hugging Face (APIs).

O provedor padrão vem de LLM_PROVIDER no .env; `use()`/`Choice` sobrepõem por chamada (UI, CLI, eval)
sem mexer em variável global — importante porque o app da UI atende mais de uma sessão ao mesmo tempo,
cada uma numa thread, e um `global` misturaria a escolha de uma pessoa com a de outra. Metadados de cada
provedor (chave, modelo padrão, URL) ficam em honda_rag.providers.

Embeddings têm provedor próprio (EMBED_PROVIDER): bge-m3 via Ollama ou gemini-embedding-001. Não são
escolhidos por sessão — os vetores do banco e os da pergunta precisam vir do mesmo modelo (README).
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

import requests

from honda_rag import config
from honda_rag import providers as P

_claude_client = None
_gemini_client = None


@dataclass(frozen=True)
class Choice:
    """Provedor + modelo para as próximas chamadas de chat. `model=None` usa o padrão do provedor."""
    provider: str
    model: str | None = None


_active: ContextVar[Choice | None] = ContextVar("llm_active", default=None)


@contextmanager
def use(choice: Choice | None) -> Iterator[None]:
    """Sobrepõe o provedor ativo dentro do bloco `with` — só nesta tarefa/thread (ContextVar), então duas
    sessões do Streamlit escolhendo provedores diferentes ao mesmo tempo não se atrapalham. `choice=None`
    não muda nada, para poder chamar sempre (`with llm.use(escolha_opcional):`) sem checar antes."""
    if choice is None:
        yield
        return
    token = _active.set(choice)
    try:
        yield
    finally:
        _active.reset(token)


def active() -> Choice:
    """O provedor desta chamada: a sobreposição da sessão (`use`), senão o padrão do .env."""
    return _active.get() or Choice(config.LLM_PROVIDER)


def resolve(choice: Choice | None = None) -> Choice:
    """Como `active()`/`choice`, mas com o modelo sempre preenchido (o padrão do provedor quando
    `model` é None). Útil para mostrar/gravar o que de fato vai ser usado antes de chamar `chat()`."""
    ch = choice or active()
    return Choice(ch.provider, ch.model or P.get(ch.provider).default_model)


def _ssl_context():
    """Certificados do sistema operacional (inclui a CA de proxies corporativos que inspecionam TLS).

    O Python usa por padrão só o pacote `certifi`, e atrás de um proxy desses a conexão falha com
    CERTIFICATE_VERIFY_FAILED. A verificação continua ligada. Desative com USE_SYSTEM_CERTS=0.
    """
    if not config.USE_SYSTEM_CERTS:
        return True                      # padrão do cliente (certifi)
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return True


def _claude():
    """Cliente único da Anthropic. Lê ANTHROPIC_API_KEY do ambiente/.env; o SDK já refaz 429/5xx."""
    global _claude_client
    if _claude_client is None:
        import anthropic

        _claude_client = anthropic.Anthropic(http_client=anthropic.DefaultHttpxClient(verify=_ssl_context()))
    return _claude_client


def _gemini():
    """Cliente único do Gemini. Lê GEMINI_API_KEY (ou GOOGLE_API_KEY) do ambiente/.env."""
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        from google.genai import types

        _gemini_client = genai.Client(
            http_options=types.HttpOptions(client_args={"verify": _ssl_context()}))
    return _gemini_client


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """A API da Anthropic recebe o system à parte; o resto vira a lista de mensagens."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    return system, [m for m in messages if m["role"] != "system"]


def _chat_claude(messages: list[dict], model: str, temperature: float, max_tokens: int) -> str:
    import anthropic

    system, msgs = _split_system(messages)
    try:
        resp = _claude().messages.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            system=system or anthropic.NOT_GIVEN, messages=msgs)
    except anthropic.AuthenticationError as e:
        raise RuntimeError("ANTHROPIC_API_KEY ausente ou inválida (defina no .env)") from e
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(f"resposta do Claude cortada em max_tokens={max_tokens}")
    return "".join(b.text for b in resp.content if b.type == "text")


def _chat_gemini(messages: list[dict], model: str, temperature: float, max_tokens: int,
                 json_mode: bool) -> str:
    from google.genai import errors, types

    system, msgs = _split_system(messages)
    contents = [types.Content(role="model" if m["role"] == "assistant" else "user",
                              parts=[types.Part(text=m["content"])]) for m in msgs]
    cfg: dict = {"temperature": temperature, "max_output_tokens": max_tokens}
    if system:
        cfg["system_instruction"] = system
    if json_mode:
        cfg["response_mime_type"] = "application/json"
    if config.GEMINI_THINKING_BUDGET != "":
        cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=int(config.GEMINI_THINKING_BUDGET))
    try:
        resp = _gemini().models.generate_content(
            model=model, contents=contents, config=types.GenerateContentConfig(**cfg))
    except errors.ClientError as e:
        if getattr(e, "code", None) in (401, 403):
            raise RuntimeError("GEMINI_API_KEY ausente ou inválida (defina no .env)") from e
        raise
    cand = resp.candidates[0] if resp.candidates else None
    fr = getattr(cand, "finish_reason", None)
    reason = getattr(fr, "name", str(fr or ""))
    if cand is None or not resp.text:
        raise RuntimeError(f"Gemini não devolveu texto (finish_reason={reason or 'sem candidato'})")
    if reason == "MAX_TOKENS":
        raise RuntimeError(f"resposta do Gemini cortada em max_output_tokens={max_tokens}")
    return resp.text


def _chat_ollama(messages: list[dict], model: str, json_mode: bool, temperature: float,
                 num_ctx: int, keep_alive: str, timeout: int) -> str:
    payload = {
        "model": model, "messages": messages, "stream": False,
        "think": False, "keep_alive": keep_alive,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if json_mode:
        payload["format"] = "json"
    r = requests.post(f"{config.OLLAMA_HOST}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["message"]["content"]


def _chat_openai_compat(p: P.Provider, messages: list[dict], model: str, temperature: float,
                        max_tokens: int, json_mode: bool, timeout: int) -> str:
    """Adaptador único para provedores que falam o formato de chat da OpenAI (POST .../chat/completions,
    Bearer token) — hoje Grok (xAI) e Hugging Face, e qualquer outro que entrar no registro com
    kind="openai_compat". As mensagens do projeto (system/user/assistant) já vêm nesse formato."""
    key = os.getenv(p.key_env or "", "").strip()
    if not key:
        raise RuntimeError(f"{p.key_env} ausente ou inválida (defina no .env)")
    body = {"model": model, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "stream": False}
    if json_mode and p.native_json:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {key}"}

    r, last_err = None, None
    for attempt in range(3):                    # 429/5xx e falha de rede: recua e tenta de novo
        try:
            r = requests.post(f"{p.base_url}/chat/completions", json=body, timeout=timeout,
                              headers=headers, verify=_ssl_context())
        except requests.RequestException as e:
            last_err, r = e, None
        else:
            if r.status_code not in (429,) and r.status_code < 500:
                break
        if attempt < 2:
            time.sleep(2 ** attempt)
    if r is None:
        raise RuntimeError(f"{p.label}: falha de rede após 3 tentativas ({last_err})") from last_err
    if r.status_code in (401, 403):
        raise RuntimeError(f"{p.key_env} ausente ou inválida (defina no .env)")
    if r.status_code in (400, 404):
        raise RuntimeError(f"{p.label}: modelo '{model}' inválido ou indisponível "
                           f"(confira {p.model_env}) — HTTP {r.status_code}: {r.text[:200]}")
    r.raise_for_status()

    data = r.json()
    choice = (data.get("choices") or [None])[0]
    if not choice:
        raise RuntimeError(f"{p.label} não devolveu texto (resposta: {json.dumps(data)[:300]})")
    finish = choice.get("finish_reason")
    if finish == "length":
        raise RuntimeError(f"resposta do {p.label} cortada em max_tokens={max_tokens}")
    text = (choice.get("message") or {}).get("content") or ""
    if not text:
        raise RuntimeError(f"{p.label} não devolveu texto (finish_reason={finish or '?'})")
    return text


def chat(messages: list[dict], model: str | None = None, json_mode: bool = False,
         temperature: float = 0.1, num_ctx: int = 8192, keep_alive: str = "10m",
         timeout: int = 300, max_tokens: int = 4096, choice: Choice | None = None) -> str:
    ch = choice or active()
    p = P.get(ch.provider)
    mdl = model or ch.model or p.default_model
    if p.kind == "claude":
        return _chat_claude(messages, mdl, temperature, max_tokens)
    if p.kind == "gemini":
        return _chat_gemini(messages, mdl, temperature, max_tokens, json_mode)
    if p.kind == "openai_compat":
        return _chat_openai_compat(p, messages, mdl, temperature, max_tokens, json_mode, timeout)
    return _chat_ollama(messages, mdl, json_mode, temperature, num_ctx, keep_alive, timeout)


def extract_json(raw: str) -> dict:
    """JSON do texto do modelo: aceita cerca de código (```json) ou texto antes/depois do objeto."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {}
        try:
            out = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return out if isinstance(out, dict) else {}


def chat_json(messages: list[dict], choice: Choice | None = None, **kw) -> dict:
    ch = choice or active()
    p = P.get(ch.provider)
    if not p.native_json:
        # Claude e os openai_compat sem resposta JSON nativa (ex.: Hugging Face, que depende do backend
        # que atender): pede JSON puro no prompt e extrai com tolerância (extract_json aceita cerca de
        # código e texto ao redor).
        messages = [dict(m) for m in messages]
        messages[0]["content"] += "\n\nReturn ONLY the JSON object, no prose and no code fence."
        kw.setdefault("max_tokens", 600)
    if p.key == "gemini":
        kw.setdefault("max_tokens", 1024)   # JSON nativo, mas o thinking consome tokens da saída
    return extract_json(chat(messages, json_mode=True, choice=ch, **kw))


def _embed_gemini(texts: list[str], kind: str, batch: int = 50) -> list[list[float]]:
    """gemini-embedding-001 reduzido a EMBED_DIM (MRL) e normalizado. `kind` escolhe o task_type:
    documentos e perguntas usam tipos diferentes, o que melhora a busca assimétrica."""
    import math

    from google.genai import errors, types

    cfg = types.EmbedContentConfig(
        task_type="RETRIEVAL_DOCUMENT" if kind == "document" else "RETRIEVAL_QUERY",
        output_dimensionality=config.EMBED_DIM)
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        for attempt in range(5):
            try:
                resp = _gemini().models.embed_content(model=config.GEMINI_EMBED_MODEL, contents=part, config=cfg)
                break
            except (errors.ServerError, errors.ClientError) as e:
                code = getattr(e, "code", None)
                if code in (401, 403):
                    raise RuntimeError("GEMINI_API_KEY ausente ou inválida (defina no .env)") from e
                if attempt == 4 or (isinstance(e, errors.ClientError) and code != 429):
                    raise
                time.sleep(2 ** attempt)          # 429/5xx: recua e tenta de novo
        if len(resp.embeddings) != len(part):
            raise RuntimeError(f"Gemini devolveu {len(resp.embeddings)} vetores para {len(part)} textos")
        for e in resp.embeddings:
            v = list(e.values)
            n = math.sqrt(sum(x * x for x in v)) or 1.0    # só 3072 dims vem normalizado
            out.append([x / n for x in v])
    return out


def embed(texts: list[str], kind: str = "query", cpu: bool = True) -> list[list[float]]:
    """Vetores para busca. kind='query' (pergunta) ou 'document' (chunk, na ingestão)."""
    if config.EMBED_PROVIDER == "gemini":
        return _embed_gemini(texts, kind)
    return _embed_ollama(texts, cpu)


def _embed_ollama(texts: list[str], cpu: bool = True) -> list[list[float]]:
    """Ollama/bge-m3. Na CPU nas consultas (a GPU fica livre para o que precisar dela)."""
    payload = {"model": config.EMBED_MODEL, "input": texts, "keep_alive": config.EMBED_KEEP_ALIVE}
    if cpu:
        payload["options"] = {"num_gpu": 0}
    r = requests.post(f"{config.OLLAMA_HOST}/api/embed", json=payload, timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"]
