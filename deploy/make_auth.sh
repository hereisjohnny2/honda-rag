#!/usr/bin/env bash
# Gera deploy/auth.caddy: usuário e senha (hash bcrypt) do login que o Caddy exige antes do app.
# Uso: ./deploy/make_auth.sh [usuario]      (padrão: mecanico)
set -euo pipefail
cd "$(dirname "$0")/.."

user="${1:-mecanico}"
read -r -s -p "Senha para '$user' (mín. 12 caracteres): " pw; echo
read -r -s -p "Repita a senha: " pw2; echo
[ "$pw" = "$pw2" ]     || { echo "As senhas são diferentes." >&2; exit 1; }
[ "${#pw}" -ge 12 ]    || { echo "Use pelo menos 12 caracteres." >&2; exit 1; }

hash="$(docker run --rm caddy:2 caddy hash-password --plaintext "$pw")"
umask 077
printf 'basic_auth {\n\t%s %s\n}\n' "$user" "$hash" > deploy/auth.caddy
echo "deploy/auth.caddy criado (o Caddy só guarda o hash, não a senha)."
