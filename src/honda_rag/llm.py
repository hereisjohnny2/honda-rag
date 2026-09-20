"""Cliente de LLM: Ollama (local), Claude ou Gemini (APIs), escolhido por LLM_PROVIDER no .env.

Embeddings sempre locais (bge-m3 via Ollama), qualquer que seja o provedor de chat.
"""
from __future__ import annotations

import json
import re

import requests

from honda_rag import config

_claude_client = None
_gemini_client = None


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


def _chat_claude(messages: list[dict], temperature: float, max_tokens: int) -> str:
    import anthropic

    system, msgs = _split_system(messages)
    try:
        resp = _claude().messages.create(
            model=config.CLAUDE_MODEL, max_tokens=max_tokens, temperature=temperature,
            system=system or anthropic.NOT_GIVEN, messages=msgs)
    except anthropic.AuthenticationError as e:
        raise RuntimeError("ANTHROPIC_API_KEY ausente ou inválida (defina no .env)") from e
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(f"resposta do Claude cortada em max_tokens={max_tokens}")
    return "".join(b.text for b in resp.content if b.type == "text")


def _chat_gemini(messages: list[dict], temperature: float, max_tokens: int, json_mode: bool) -> str:
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
            model=config.GEMINI_MODEL, contents=contents, config=types.GenerateContentConfig(**cfg))
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


def _chat_ollama(messages: list[dict], model: str | None, json_mode: bool, temperature: float,
                 num_ctx: int, keep_alive: str, timeout: int) -> str:
    payload = {
        "model": model or config.LLM_MODEL, "messages": messages, "stream": False,
        "think": False, "keep_alive": keep_alive,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if json_mode:
        payload["format"] = "json"
    r = requests.post(f"{config.OLLAMA_HOST}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["message"]["content"]


def chat(messages: list[dict], model: str | None = None, json_mode: bool = False,
         temperature: float = 0.1, num_ctx: int = 8192, keep_alive: str = "10m",
         timeout: int = 300, max_tokens: int = 4096) -> str:
    if config.LLM_PROVIDER == "claude":
        return _chat_claude(messages, temperature, max_tokens)
    if config.LLM_PROVIDER == "gemini":
        return _chat_gemini(messages, temperature, max_tokens, json_mode)
    return _chat_ollama(messages, model, json_mode, temperature, num_ctx, keep_alive, timeout)


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


def chat_json(messages: list[dict], **kw) -> dict:
    if config.LLM_PROVIDER == "claude":
        # a API do Claude não tem modo JSON aqui: pede JSON puro e extrai com tolerância
        messages = [dict(m) for m in messages]
        messages[0]["content"] += "\n\nReturn ONLY the JSON object, no prose and no code fence."
        kw.setdefault("max_tokens", 600)
    elif config.LLM_PROVIDER == "gemini":
        kw.setdefault("max_tokens", 1024)   # JSON nativo (response_mime_type); extract_json por segurança
    return extract_json(chat(messages, json_mode=True, **kw))


def embed(texts: list[str], cpu: bool = True) -> list[list[float]]:
    """Embeddings na CPU nas consultas (a GPU fica livre para o que precisar dela)."""
    payload = {"model": config.EMBED_MODEL, "input": texts, "keep_alive": "10m"}
    if cpu:
        payload["options"] = {"num_gpu": 0}
    r = requests.post(f"{config.OLLAMA_HOST}/api/embed", json=payload, timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"]
