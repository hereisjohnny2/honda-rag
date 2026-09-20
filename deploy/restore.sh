#!/usr/bin/env bash
# Restaura o banco e as imagens enviados por deploy/export_data.ps1.
# Rode na pasta do projeto no servidor, com honda_rag.dump e data.tar.gz na raiz (ao lado do compose).
set -euo pipefail
cd "$(dirname "$0")/.."

DC="docker compose --env-file .env.prod -f docker-compose.prod.yml"
[ -f .env.prod ]        || { echo "Falta .env.prod (copie de .env.prod.example)." >&2; exit 1; }
[ -f honda_rag.dump ]   || { echo "Falta honda_rag.dump na raiz do projeto." >&2; exit 1; }
[ -f data.tar.gz ]      || { echo "Falta data.tar.gz na raiz do projeto." >&2; exit 1; }

echo "1/4 subindo o Postgres..."
$DC up -d --wait db

echo "2/4 restaurando o dump (avisos sobre objetos inexistentes são normais)..."
$DC exec -T db sh -c 'pg_restore --no-owner --no-privileges --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < honda_rag.dump || echo "pg_restore terminou com avisos; confira as contagens abaixo."

echo "3/4 normalizando caminhos das imagens (bancos carregados no Windows usam barra invertida)..."
$DC exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
UPDATE pages   SET image_path = replace(image_path, E'\\', '/'), view_path = replace(view_path, E'\\', '/');
UPDATE figures SET image_path = replace(image_path, E'\\', '/');
SQL

echo "4/4 extraindo as imagens em ./data ..."
tar -xzf data.tar.gz

echo
echo "Conferência:"
$DC exec -T db sh -c 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
select 'chunks=' || count(*) || ' com_vetor=' || count(embedding) || ' modelo=' || string_agg(distinct embedding_model, ',') from chunks;
select 'paginas=' || count(*) || ' figuras=' || (select count(*) from figures) from pages;
SQL
echo "Imagens: $(find data/pages -name view.webp | wc -l) páginas, $(find data/figures -name '*.png' | wc -l) figuras"
echo
echo "Agora suba tudo:  $DC up -d --build"
