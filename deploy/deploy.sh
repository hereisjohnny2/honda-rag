#!/usr/bin/env bash
# Atualiza o servidor para um commit: código (git) + imagem do app construída pelo CI (GHCR).
#
# Chamado pelo GitHub Actions (.github/workflows/deploy.yml), que manda este arquivo por SSH no stdin:
#   ssh vps "bash -s -- <sha> <imagem>" < deploy/deploy.sh
# (assim roda sempre a versão do script do commit que está sendo publicado, não a que está no servidor).
# Também dá para rodar à mão no servidor (rollback, por exemplo):
#   ./deploy/deploy.sh <sha> [ghcr.io/hereisjohnny2/honda-rag]
#
# Variáveis opcionais: APP_DIR (padrão /opt/honda-rag); GHCR_USER/GHCR_TOKEN para baixar a imagem se o
# pacote no GHCR for privado (o CI passa o GITHUB_TOKEN do job, que expira quando o job termina).
set -euo pipefail

main() {
  local sha="${1:?uso: deploy.sh <sha> [imagem]}"
  local image="${2:-ghcr.io/hereisjohnny2/honda-rag}"
  local current="honda-rag-app:current"     # o que o docker-compose.prod.yml usa no serviço app

  cd "${APP_DIR:-/opt/honda-rag}"
  local dc="docker compose --env-file .env.prod -f docker-compose.prod.yml"

  [ -f .env.prod ]         || die "falta .env.prod (passo 4 do deploy/DEPLOY.md)"
  [ -f deploy/auth.caddy ] || die "falta deploy/auth.caddy (rode deploy/make_auth.sh)"
  # Só conteúdo conta: um `chmod +x` feito no servidor não é alteração (e não pode travar o checkout).
  git config core.fileMode false
  if ! git diff --quiet HEAD --; then
    git status --short --untracked-files=no >&2
    die "há alterações locais nos arquivos acima; o servidor deve espelhar o repositório (desfaça com: git checkout -- <arquivo>)"
  fi

  echo "==> imagem ${image}:${sha}"
  if [ -n "${GHCR_TOKEN:-}" ]; then
    docker login ghcr.io -u "${GHCR_USER:-github-actions}" --password-stdin <<<"$GHCR_TOKEN" >/dev/null
  fi
  local pulled=0
  docker pull -q "${image}:${sha}" && pulled=1
  [ -n "${GHCR_TOKEN:-}" ] && docker logout ghcr.io >/dev/null
  [ "$pulled" = 1 ] || die "não consegui baixar ${image}:${sha}"

  echo "==> código em ${sha}"
  local old
  old="$(git rev-parse HEAD)"
  git fetch -q origin "$sha"
  git checkout -q -B main "$sha"

  echo "==> subindo"
  local previous
  previous="$(docker image inspect -f '{{.Id}}' "$current" 2>/dev/null || true)"
  docker tag "${image}:${sha}" "$current"
  docker rmi -f "${image}:${sha}" >/dev/null    # a imagem continua como :current; só tira a etiqueta
  $dc up -d --remove-orphans

  # O Caddyfile é montado como arquivo: o compose não percebe mudança nele.
  if ! git diff --quiet "$old" HEAD -- deploy/Caddyfile; then
    echo "==> Caddyfile mudou: recarregando o Caddy"
    $dc exec -T caddy caddy reload --config /etc/caddy/Caddyfile
  fi

  wait_healthy "$dc" app 180 || {
    $dc logs --tail 80 app
    if [ -n "$previous" ]; then
      echo "==> app não ficou saudável: voltando para a imagem anterior (o código fica em ${sha})" >&2
      docker tag "$previous" "$current"
      $dc up -d app
    fi
    die "deploy de ${sha} falhou"
  }

  docker image prune -f >/dev/null     # remove a imagem :current anterior, que ficou sem etiqueta
  echo "==> ok: ${sha} no ar"
}

wait_healthy() {   # <comando compose> <serviço> <segundos>
  local dc="$1" svc="$2" deadline=$((SECONDS + $3)) id status
  id="$($dc ps -q "$svc")"
  while [ "$SECONDS" -lt "$deadline" ]; do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null || echo missing)"
    [ "$status" = healthy ] && return 0
    sleep 5
  done
  echo "app ainda '${status}' após $3 s" >&2
  return 1
}

die() { echo "ERRO: $*" >&2; exit 1; }

# Tudo dentro de main e sem stdin: quando o script chega por "bash -s", nenhum comando pode ler o resto dele.
main "$@" </dev/null
