# Deploy em VPS (Hostinger KVM 1, Ubuntu + Docker)

Stack: **Caddy** (HTTPS + login) → **app** (Streamlit) → **Postgres/pgvector** + **Ollama** (só bge-m3, na CPU).
O chat usa uma API (Gemini ou Claude), porque a VPS não tem GPU. A ingestão (OCR) continua na sua máquina.

> **Status:** os arquivos foram escritos e validados por análise estática (sintaxe, `docker compose config`,
> imports com só as dependências de runtime). **A imagem nunca foi construída nem o compose subiu de fato**
> (o Docker local estava parado). Espere ajustes no primeiro deploy e leia o passo 9 (verificação).

## 0. Antes de começar

- **Troque a chave do Gemini** que foi colada no chat e gere uma nova (Google AI Studio). Defina um limite
  de gasto/alerta de cobrança na conta da API: qualquer pessoa com o login usa o seu crédito.
- Tenha o domínio (ex.: `manual.seudominio.com`) e acesso ao painel de DNS.

## 1. Criar a VPS

1. Plano **KVM 1** (4 GB de RAM), datacenter **São Paulo**, template **Ubuntu 24.04 com Docker** (ou instale
   o Docker manualmente: <https://docs.docker.com/engine/install/ubuntu/>).
2. Entre por SSH (`ssh root@IP`), crie um usuário comum com `sudo`, cadastre sua chave SSH e desative o login
   por senha do root.
3. Firewall (o painel da Hostinger também tem um; libere só o necessário):
   ```bash
   sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable
   ```
4. Swap de 2 GB (a RAM é justa com o Ollama):
   ```bash
   sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```

## 2. DNS

Crie um registro **A** do seu subdomínio apontando para o IP da VPS. Confira antes de subir (o Caddy só
consegue o certificado quando o DNS já resolve):
```bash
dig +short manual.seudominio.com    # deve mostrar o IP da VPS
```

## 3. Código no servidor

```bash
sudo mkdir -p /opt/honda-rag && sudo chown $USER /opt/honda-rag
git clone https://github.com/hereisjohnny2/honda-rag.git /opt/honda-rag && cd /opt/honda-rag
```
Clone por **HTTPS** (o repositório é público): o deploy automático faz `git fetch` no servidor e assim não
precisa de chave do GitHub lá.

## 4. Configuração

```bash
cd /opt/honda-rag
cp .env.prod.example .env.prod && chmod 600 .env.prod
nano .env.prod          # DOMAIN, PG_PASSWORD (senha longa), LLM_PROVIDER e a chave da API
./deploy/make_auth.sh mecanico     # cria deploy/auth.caddy (login do site)
```

## 5. Enviar os dados (da sua máquina Windows)

Com o Rancher Desktop aberto e o banco local no ar:
```powershell
.\deploy\export_data.ps1
scp dist\honda_rag.dump dist\data.tar.gz usuario@IP:/opt/honda-rag/
```
Vão só o dump (~50 MB) e as imagens da UI (~420 MB). **O PDF do manual não vai** (direitos autorais).

## 6. Restaurar

```bash
cd /opt/honda-rag && ./deploy/restore.sh
```
A conferência final deve mostrar `chunks=1638 com_vetor=1638 modelo=bge-m3` e as contagens de imagens.

## 7. Subir

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```
Na primeira vez o `ollama-init` baixa o bge-m3 (~1,2 GB); leva alguns minutos.

## 8. Acompanhar

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml ps
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f caddy app
```

## 9. Verificar (o que ainda não foi testado)

- `curl -I https://manual.seudominio.com` deve dar **401** (sem login) e, com o login, a página.
- Faça uma pergunta pela UI e clique numa fonte para ver a página original.
- **Latência:** o KVM 1 tem 1 vCPU e cada pergunta embute até ~7 textos com o bge-m3 na CPU. Meça o tempo total
  e `docker stats`/`free -h` durante uma pergunta. Se passar de ~10 s, migre para o KVM 2 (2 vCPU) ou reduza o
  número de consultas por pergunta em `retrieval/intent.py`.
- Se a memória apertar (`docker stats` perto dos limites, ou swap em uso constante), o KVM 2 (8 GB) resolve.
- Se o primeiro `chat` falhar com erro de modelo, revise `GEMINI_MODEL`; modelos novos do Gemini podem rejeitar
  `thinking_budget=0`: nesse caso ponha `GEMINI_THINKING_BUDGET=` (vazio) no `.env.prod`.
- `MIN_COSINE` (limiar de recusa) segue 0.55, calibrado para o bge-m3 (o mesmo modelo do banco).

## 10. Rotina

| Tarefa | Comando |
|---|---|
| Atualizar o código | automático a cada push na `main` (seção 11); à mão: `./deploy/deploy.sh <sha>` |
| Voltar uma versão | *Actions > Deploy > Run workflow* com o SHA anterior, ou `./deploy/deploy.sh <sha>` |
| Trocar a senha do site | `./deploy/make_auth.sh` e `docker compose ... restart caddy` |
| Novos dados (nova ingestão) | repita os passos 5 e 6 |
| Backup do banco (cron 03:00) | `0 3 * * * /opt/honda-rag/deploy/backup.sh >> /opt/honda-rag/backups/backup.log 2>&1` |

## 11. Deploy automático (GitHub Actions)

A cada push na `main` (exceto mudanças só em `.md`, `eval/` e `scripts/`), o workflow
[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml):

1. constrói a imagem do app no runner do GitHub e publica em `ghcr.io/hereisjohnny2/honda-rag:<sha>`
   (a VPS não compila nada);
2. entra na VPS por SSH e roda [`deploy/deploy.sh`](deploy.sh) do próprio commit: baixa a imagem, faz
   `git checkout` do mesmo SHA (compose, Caddyfile, scripts), marca a imagem como `honda-rag-app:current`
   e faz `up -d`. Se o Caddyfile mudou, recarrega o Caddy;
3. espera o app ficar *healthy* (até 3 min). Se não ficar, volta para a imagem anterior e o job falha.

O que ele **não** faz: enviar dados (passos 5 e 6) nem mexer no `.env.prod`/`auth.caddy`, que continuam só
no servidor. Faça o **primeiro deploy à mão** (passos 1–9) e só depois ligue o automático.

### Configuração (uma vez)

1. **Usuário de deploy** na VPS com acesso ao Docker e à pasta (pode ser o seu usuário comum):
   ```bash
   sudo usermod -aG docker $USER      # saia e entre de novo no SSH
   ```
2. **Chave SSH só para o CI** (na sua máquina, sem senha):
   ```bash
   ssh-keygen -t ed25519 -N "" -C "github-actions-deploy" -f deploy_key
   ssh-copy-id -i deploy_key.pub usuario@IP      # ou cole o .pub em ~/.ssh/authorized_keys na VPS
   ssh-keyscan -H IP > known_hosts               # confira a impressão digital com a do painel da Hostinger
   ```
3. No GitHub, em *Settings > Environments*, crie o ambiente **`production`** e nele cadastre:

   | Tipo | Nome | Valor |
   |---|---|---|
   | Secret | `VPS_SSH_KEY` | conteúdo de `deploy_key` (a privada) |
   | Secret | `VPS_KNOWN_HOSTS` | conteúdo de `known_hosts` |
   | Variable | `VPS_HOST` | IP (ou nome) da VPS |
   | Variable | `VPS_USER` | usuário de deploy |
   | Variable | `VPS_PORT` | opcional, padrão `22` |
   | Variable | `VPS_APP_DIR` | opcional, padrão `/opt/honda-rag` |

   Opcional: em *Required reviewers*, adicione você mesmo para cada deploy esperar um clique; em
   *Deployment branches*, restrinja a `main`.
4. Apague `deploy_key` da sua máquina depois de cadastrar (se perder, gere outra).

A imagem no GHCR sai privada; o CI entrega à VPS um token que só vale durante o job, então o servidor não
guarda credencial do GitHub. Para rodar `./deploy/deploy.sh <sha>` à mão no servidor, torne o pacote público
(*Packages > honda-rag > Package settings*; ele só tem o código do app, que já é público) ou faça
`docker login ghcr.io` com um token `read:packages`.

**Não edite arquivos versionados no servidor:** o `deploy.sh` para com erro se houver alterações locais.
Configuração do servidor fica no `.env.prod` e no `auth.caddy`, que o Git ignora.

## Segurança (resumo)

- Só as portas 22/80/443 ficam abertas; Postgres e Ollama não são publicados.
- Login por senha do Caddy na frente de tudo + HTTPS. Use senha longa e única.
- A chave SSH do CI só serve para entrar na VPS; se vazar, remova a linha dela do `~/.ssh/authorized_keys`.
- `.env.prod` e `deploy/auth.caddy` ficam só no servidor (`chmod 600`) e estão no `.gitignore`.
- O conteúdo é um manual protegido por direitos autorais: mantenha o acesso restrito a quem precisa.
