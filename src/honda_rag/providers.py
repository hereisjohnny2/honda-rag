"""Registro dos provedores de LLM de chat: metadados usados por `llm.py` (como chamar cada API),
`config.context_budget()` (orçamento de contexto por tipo de provedor) e a UI (o que listar no seletor).

Somar um provedor OpenAI-compatível novo (OpenRouter, Groq, um vLLM próprio etc.) é só uma entrada
nova aqui com `kind="openai_compat"`: `llm._chat_openai_compat` atende qualquer um deles.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from honda_rag import config


@dataclass(frozen=True)
class Provider:
    key: str                     # "gemini" — valor de LLM_PROVIDER / --provider / Choice.provider
    label: str                   # "Gemini (Google)" — mostrado na UI
    kind: str                    # "ollama" | "claude" | "gemini" | "openai_compat" (como montar a chamada)
    default_model: str
    model_env: str               # nome da variável que define o modelo padrão (só para mensagens/UI)
    key_env: str | None          # nome da variável com a chave de API; None para o Ollama (sem chave)
    base_url: str | None = None  # só openai_compat
    native_json: bool = False    # aceita pedir JSON estruturado (response_format / format=json)
    budget: str = "api"          # "local" | "api" — ver config.context_budget()

    def has_key(self) -> bool:
        """Provedor local não precisa de chave; provedor de API precisa da variável definida."""
        return self.key_env is None or bool(os.getenv(self.key_env, "").strip())


PROVIDERS: dict[str, Provider] = {
    p.key: p for p in [
        Provider("ollama", "Ollama (local)", "ollama", config.LLM_MODEL, "LLM_MODEL",
                 key_env=None, native_json=True, budget="local"),
        Provider("claude", "Claude (Anthropic)", "claude", config.CLAUDE_MODEL, "CLAUDE_MODEL",
                 key_env="ANTHROPIC_API_KEY"),
        Provider("gemini", "Gemini (Google)", "gemini", config.GEMINI_MODEL, "GEMINI_MODEL",
                 key_env="GEMINI_API_KEY", native_json=True),
        Provider("grok", "Grok (xAI)", "openai_compat", config.GROK_MODEL, "GROK_MODEL",
                 key_env="XAI_API_KEY", base_url=config.XAI_BASE_URL, native_json=True),
        Provider("hf", "Hugging Face", "openai_compat", config.HF_MODEL, "HF_MODEL",
                 key_env="HF_TOKEN", base_url=config.HF_BASE_URL, native_json=False),
    ]
}


def get(key: str) -> Provider:
    try:
        return PROVIDERS[key]
    except KeyError:
        raise RuntimeError(
            f"Provedor de LLM desconhecido: '{key}' (válidos: {', '.join(PROVIDERS)})") from None


def available() -> list[Provider]:
    """Provedores utilizáveis agora, na ordem de exibição: locais sempre; de API só com a chave definida."""
    return [p for p in PROVIDERS.values() if p.has_key()]
