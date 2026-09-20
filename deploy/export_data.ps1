# Empacota o que o servidor precisa, a partir da sua máquina (Windows):
#   dist\honda_rag.dump  - dump do Postgres local (esquema + dados + vetores bge-m3)
#   dist\data.tar.gz     - só as imagens que a UI usa (view.webp das páginas + recortes de figuras)
# NÃO inclui page.png (580 MB), ocr.json nem o PDF: são só da ingestão (e o PDF é protegido por direitos autorais).
#
# Uso (com o Docker/Rancher aberto e o container do banco no ar):
#   .\deploy\export_data.ps1
param(
    [string]$Container = "honda-rag-db",
    [string]$Out = "dist"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if ((docker inspect $Container --format '{{.State.Running}}' 2>$null) -ne "true") {
    throw "O container '$Container' não está rodando. Abra o Rancher Desktop e rode 'docker compose up -d'."
}
New-Item -ItemType Directory -Force $Out | Out-Null

Write-Host "1/3 dump do Postgres..."
docker exec $Container sh -c 'pg_dump -Fc --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB" -f /tmp/honda_rag.dump'
if ($LASTEXITCODE -ne 0) { throw "pg_dump falhou" }
docker cp "${Container}:/tmp/honda_rag.dump" "$Out\honda_rag.dump"
if ($LASTEXITCODE -ne 0) { throw "docker cp falhou" }

Write-Host "2/3 copiando imagens da UI..."
$stage = Join-Path $Out "data"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
robocopy data\pages "$stage\pages" view.webp /S /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy (páginas) falhou: $LASTEXITCODE" }
robocopy data\figures "$stage\figures" *.png /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy (figuras) falhou: $LASTEXITCODE" }

Write-Host "3/3 compactando..."
if (Test-Path "$Out\data.tar.gz") { Remove-Item "$Out\data.tar.gz" }
tar -czf "$Out\data.tar.gz" -C $Out data
if ($LASTEXITCODE -ne 0) { throw "tar falhou" }
Remove-Item $stage -Recurse -Force

Get-ChildItem $Out -File | ForEach-Object { "{0,8:N0} MB  {1}" -f ($_.Length / 1MB), $_.Name }
Write-Host "`nPronto. Envie para o servidor: scp $Out\honda_rag.dump $Out\data.tar.gz usuario@IP:/opt/honda-rag/"
