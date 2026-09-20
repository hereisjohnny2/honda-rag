#!/usr/bin/env bash
# Backup do banco em ./backups (mantém 14 dias). O banco é somente leitura em produção e pode ser
# recriado com deploy/export_data.ps1; o backup só evita ter que reenviar 50 MB.
# Agendar (crontab -e):  0 3 * * *  /opt/honda-rag/deploy/backup.sh >> /opt/honda-rag/backups/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
f="backups/honda_rag_$(date +%Y%m%d_%H%M).dump"
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T db \
  sh -c 'pg_dump -Fc --no-owner -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$f"
find backups -name 'honda_rag_*.dump' -mtime +14 -delete
echo "$(date -Is) backup ok: $f ($(du -h "$f" | cut -f1))"
