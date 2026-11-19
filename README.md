# Verificador de preços e cupons do Mercado Livre

API (FastAPI + [Camoufox](https://camoufox.com)) que recebe o link de um produto do Mercado Livre, o preço
esperado e, opcionalmente, um cupom. Ela abre o produto num navegador logado, lê o preço da loja, confere o
cupom e responde se o preço final é **menor**, **maior** ou **igual** ao informado.

O Mercado Livre exige login para abrir produtos e pode pedir captcha. Por isso o navegador usa um **perfil
persistente** (`profile/`) com a sessão logada, e o captcha pode ser resolvido por um **link gerado pela API**
(noVNC), sem acesso ao servidor.

## Sumário

1. [Como funciona](#como-funciona)
2. [Variáveis de ambiente](#variáveis-de-ambiente)
3. [Rodar com Docker (recomendado)](#rodar-com-docker-recomendado)
4. [Login no Mercado Livre](#login-no-mercado-livre)
5. [Captcha pelo dashboard](#captcha-pelo-dashboard)
   - [Login pelo dashboard](#login-pelo-dashboard)
6. [Endpoints](#endpoints)
7. [Regras de negócio](#regras-de-negócio)
8. [Produção: checklist](#produção-checklist)
9. [CI/CD (GitHub Actions)](#cicd-github-actions)
10. [Rodar sem Docker (desenvolvimento)](#rodar-sem-docker-desenvolvimento)
11. [Solução de problemas](#solução-de-problemas)
12. [Testes e estrutura](#testes-e-estrutura)
13. [Limitações e próximos passos](#limitações-e-próximos-passos)

---

## Como funciona

```
seu sistema/dashboard ──HTTP + token──▶ API (FastAPI, porta 8000)
                                          │
                                          ▼
                              Camoufox (Firefox) com perfil logado  ──▶ mercadolivre.com.br
                              └─ numa tela virtual (Xvfb :99), espelhada por VNC
```

- **Uma conta, um navegador, um perfil.** A sessão do Mercado Livre fica em `profile/`.
- **Ritmo controlado:** 1 aba por vez e intervalo mínimo entre leituras (`MIN_GAP_S`), para reduzir captcha.
- **Cache de 10 min por URL:** repetir o mesmo produto não reabre a loja.
- **Cupons** vêm da conta logada (`/cupons/active`); se o código não estiver lá, a API o insere na conta.
- Se o navegador do servidor fechar ou morrer, a API **encerra o processo** e o Docker o reinicia sozinho.

---

## Variáveis de ambiente

Copie `.env.example` para `.env` e ajuste. O compose lê o `.env` automaticamente (`env_file`).

| Variável | Padrão | Para que serve |
|---|---|---|
| `API_TOKEN` | *(vazio)* | Senha da API. Aceita `Authorization: Bearer <token>` ou `X-Api-Token: <token>`. **Vazio = API aberta** (só para dev). Gere com `openssl rand -hex 32`. |
| `PUBLIC_BASE_URL` | *(vazio)* | URL pública da API (ex.: `https://ml-check.suaempresa.com`). Usada para montar o link do noVNC. Sem ela o link vem relativo (`/vnc/...`). |
| `HEADLESS` | `0` | `0` = com janela (Mac/dev) · `1` = headless · `virtual` = display virtual do Camoufox (Linux) · `display` = navegador visível numa tela virtual espelhada por VNC (**usado no Docker**, permite o captcha pelo dashboard). |
| `SLOTS` | `1` | Quantas leituras de produto em paralelo. |
| `MIN_GAP_S` | `10` | Segundos mínimos entre o início de duas leituras. Aumente se o captcha aparecer muito. |
| `REQUEST_TIMEOUT_S` | `150` | Prazo total de cada requisição (`status: timeout`). |
| `CAPTCHA_SESSION_TTL_S` | `900` | Validade do link de captcha (15 min). |
| `SESSION_CACHE_TTL_S` | `30` | Por quanto tempo o `/session` reaproveita a última resposta (evita abrir uma janela por chamada). |
| `SESSION_TIMEOUT_S` | `25` | Limite da checagem do `/session`. |

> O `docker-compose.yml` já fixa `HEADLESS=display` para o serviço `verify`.

---

## Rodar com Docker (recomendado)

No Mac use `docker-compose` (com hífen, se o plugin `docker compose` não estiver instalado); no servidor,
`docker compose`. Abaixo uso `docker compose`.

### Primeira vez

```bash
cp .env.example .env            # edite API_TOKEN e PUBLIC_BASE_URL
docker compose build verify     # DEMORA na 1ª vez (baixa ~1,3 GB do navegador); depois fica em cache
```

Depois faça o [login no Mercado Livre](#login-no-mercado-livre) e suba a API:

```bash
docker compose up -d verify
docker compose ps               # espere ficar (healthy)
curl -H 'X-Api-Token: <token>' localhost:8000/session    # {"logged_in": true}
```

### Comandos do dia a dia

```bash
docker compose logs -f --tail 50 verify      # acompanhar o log
docker compose logs --tail 200 verify | grep -v "GET /vnc"   # log sem o ruído do noVNC
docker compose restart verify                # reiniciar
docker compose stop verify                   # parar (necessário antes do serviço de login)
docker compose up -d --build verify          # atualizar após mudar o código
docker compose down                          # remove os containers (o profile/ é mantido)
```

- **Um container por perfil.** Nunca suba dois containers com o mesmo `profile/` (o Firefox bloqueia o perfil).
- **Disco:** a build precisa de ~5 GB livres. No Colima o padrão é 10 GiB; se der `No space left on device`,
  rode `docker builder prune` (ou aumente o disco com `colima start --disk 30`).
- **Plataforma:** a build usa a arquitetura da máquina (arm64 no Mac, amd64 no servidor).

---

## Login no Mercado Livre

A sessão fica em `profile/` e **não** deve ir para o Git. O login é feito pelo navegador do container, via
noVNC (a tela do container aparece numa aba do seu navegador).

### Fazer o login (primeira vez, sessão caída ou troca de conta)

```bash
docker compose stop verify
docker compose --profile login up login       # abre o navegador do container e o noVNC (porta 6080)
```

1. Em outro terminal, abra o túnel até o servidor (se não estiver no mesmo computador):
   `ssh -L 6080:localhost:6080 usuario@servidor`
2. Abra `http://localhost:6080/vnc.html?autoconnect=1` no seu navegador.
3. Na tela do container, clique em **Entre**, faça o login (com 2FA, se pedir) e resolva o captcha, se aparecer.
4. Quando o login for detectado, o log mostra `LOGIN OK` / `SESSÃO SALVA` e o container **encerra sozinho**.
5. Suba a API de novo e confira:

```bash
docker compose up -d verify
curl -H 'X-Api-Token: <token>' localhost:8000/session     # logged_in: true
```

### Resolver o captcha de um produto específico durante o login

```bash
VERIFY_URL='https://www.mercadolivre.com.br/<link-do-produto>' docker compose --profile login up login
```

Depois do login o container abre esse produto e só termina quando ele abre sem captcha.

### Observações

- A porta 6080 fica publicada **só em `127.0.0.1`** (o noVNC não tem senha). Nunca a exponha na internet.
- Copiar a pasta `profile/` de outro computador **não é confiável**: o Mercado Livre costuma invalidar a sessão
  quando muda o sistema/IP/dispositivo. Faça o login pelo próprio container.
- Só existe **uma conta** por vez. Trocar de conta substitui a sessão; reinicie o `verify` depois para limpar
  os caches de cupons e produtos.

---

## Captcha pelo dashboard

Quando o Mercado Livre pede captcha, o `/check` devolve `status: "captcha"`. O dashboard pode, sem terminal,
gerar um link para quem estiver de plantão resolver. Requer `HEADLESS=display` (já é o padrão no compose).

**Fluxo**

1. `GET /captcha/status` → `needed: true` e `reason`:
   - `"captcha"`: o Mercado Livre pediu verificação (a sessão abre o produto que bloqueou);
   - `"login"`: a conta está deslogada. Use o [login pelo dashboard](#login-pelo-dashboard) (`POST /login/session`).
2. Botão → `POST /captcha/session` (sem corpo) → devolve `{id, url, expires_in}`.
3. A pessoa abre o `url` no navegador, vê o navegador do servidor, resolve o captcha (ou faz o login) e **fecha só a aba dela**.
4. O dashboard consulta `GET /captcha/session/{id}` → `pending` → `solved` (ou `expired`). A sessão só vira `solved`
   quando a página saiu do bloqueio **e a conta está logada**.
5. O próximo `/check` volta a `ok`, e `/captcha/status` volta a `needed: false`.

> O `/session` (e o `/check`) deslogado já acendem o alerta: o dashboard não precisa esperar um erro para saber.

```bash
curl -H 'X-Api-Token: <token>' localhost:8000/captcha/status
curl -X POST localhost:8000/captcha/session -H 'X-Api-Token: <token>'            # usa o produto que bloqueou
curl -X POST localhost:8000/captcha/session -H 'X-Api-Token: <token>' \
     -H 'content-type: application/json' -d '{"url":"https://www.mercadolivre.com.br/<produto>"}'
curl -H 'X-Api-Token: <token>' localhost:8000/captcha/session/<id>
```

**Detalhes importantes**

- O link só funciona enquanto a sessão está `pending` (até 15 min). Se o produto abrir sem captcha, a sessão
  vira `solved` em ~8 s e o link morre (404). Reiniciar o container também apaga as sessões.
- Enquanto há sessão pendente, o `/check` devolve `captcha` com `detail: "verificação em andamento"`.
- O link dá **controle total do navegador logado** na conta do Mercado Livre. É de uso único, expira e só é gerado
  por quem tem o `API_TOKEN`; mostre-o apenas a operadores e use **HTTPS**.
- Quem usa o link **não deve fechar a janela/última aba do navegador do servidor** (o Firefox está configurado para
  não encerrar ao fechar a última aba, e a API reinicia o processo se ele morrer mesmo assim).
- Para testar a infraestrutura sem captcha real, abra uma sessão com
  `{"url":"https://www.mercadolivre.com.br/captcha/wall/teste"}` (a sessão fica pendente). Para encerrar, abra
  um produto normal na tela do noVNC.
- O navegador do servidor mostra **uma única janela** ao operador. A API não abre outras janelas enquanto há
  uma sessão pendente (o `/session` responde por requisição, sem janela), para a tela não ficar piscando/em branco.

### Login pelo dashboard

Para entrar (ou **trocar**) a conta do Mercado Livre sem terminal, no mesmo molde do captcha:

1. `POST /login/session` → `{id, kind: "login", url, expires_in}`. Corpo opcional `{"force": true}`.
   - Já logado e sem `force`: devolve `{"already_logged_in": true}` e não abre nada.
   - `force: true` abre mesmo logado: serve para **trocar de conta** (a pessoa sai e entra com outra).
2. A pessoa abre o `url`: o navegador do servidor já abre na **página de login** do Mercado Livre. Ela digita o
   e-mail/telefone, a senha, resolve o captcha e o 2FA, se houver.
3. O dashboard consulta `GET /login/session/{id}` → `{status, logged_in}`: `pending` → `solved` (`logged_in: true`)
   ou `expired` (15 min). A API detecta o login sem navegar nem abrir janelas: o sinal é o **menu do usuário na janela
   do operador** (cookies sozinhos não bastam, podem estar velhos). Com `force`, ela só marca `solved` depois de ver
   a conta **sair** e **entrar** de novo; por isso, na troca de conta, a pessoa precisa clicar em **Sair** antes.
4. Ao terminar, a API **limpa os caches** de cupons e produtos (a conta pode ter mudado) e o `/captcha/status`
   volta a `needed: false`.

```bash
curl -X POST localhost:8000/login/session -H 'X-Api-Token: <token>'
curl -X POST localhost:8000/login/session -H 'X-Api-Token: <token>' -H 'content-type: application/json' -d '{"force": true}'
curl -H 'X-Api-Token: <token>' localhost:8000/login/session/<id>
```

Só há **uma sessão do operador por vez** (captcha ou login). Se já existe uma pendente, a rota devolve a existente.

---

## Endpoints

Todas as rotas abaixo exigem o token (`X-Api-Token` ou `Authorization: Bearer`). A documentação interativa fica em
`/docs` (sem token).

**Testar pelo `/docs`:** clique em **Authorize** (canto superior direito), cole o `API_TOKEN` do servidor e
confirme. O Swagger passa a enviar o header `X-Api-Token` em **todas** as rotas, então o "Try it out" de
qualquer endpoint já funciona — sem montar `curl` na mão. O token fica só na aba do navegador; recarregar a
página pede o Authorize de novo.

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/session` | `{"logged_in": true/false, "cached": bool}`: o perfil está logado? A resposta é reaproveitada por `SESSION_CACHE_TTL_S` (30 s); use `?refresh=true` para forçar. Chamadas simultâneas dividem uma só checagem; se passar de `SESSION_TIMEOUT_S` (25 s) devolve o último valor com `stale: true` (ou 504 se nunca checou). Se estiver deslogado, também marca `needed: true` com `reason: "login"` em `/captcha/status`. |
| `POST` | `/check` | Confere um produto. |
| `POST` | `/check/batch` | Confere vários (lista de itens). |
| `GET` | `/captcha/status` | Há bloqueio? `{needed, reason, blocked_url, since, session}`. `reason` é `"captcha"` (verificação) ou `"login"` (conta deslogada). |
| `POST` | `/captcha/session` | Cria a sessão/link de captcha. Corpo opcional `{"url": "..."}`. |
| `GET` | `/captcha/session/{id}` | `pending` · `solved` · `expired`. |
| `POST` | `/login/session` | Abre o link do noVNC já na página de login. Corpo opcional `{"force": true}` (trocar de conta). |
| `GET` | `/login/session/{id}` | `{status, logged_in}`: `pending` · `solved` · `expired`. |

### `POST /check`

```bash
curl -X POST localhost:8000/check \
  -H 'X-Api-Token: <token>' -H 'content-type: application/json' \
  -d '{"url":"https://www.mercadolivre.com.br/<produto>","price":89.9,"coupon":"COMPRASML"}'
```

Corpo: `url` (obrigatória, domínios `mercadolivre.com.br`, `mercadolivre.com`, `meli.la`), `price` (obrigatório,
preço esperado) e `coupon` (opcional).

Resposta de exemplo:

```json
{
  "status": "ok",
  "store_price": 56.9,
  "price_matches": true,
  "coupon_applied": false,
  "price_with_coupon": null,
  "cached": false,
  "coupon_eligible": true,
  "matched_showcase": true,
  "comparison": "igual"
}
```

| Campo | Significado |
|---|---|
| `store_price` | Preço lido na loja. |
| `price_matches` | O preço da loja bate com o informado (**sem centavos**). |
| `coupon_applied` / `price_with_coupon` | O cupom foi aplicado e o preço resultante. |
| `coupon_eligible` | O cupom vale para este produto (`true`/`false`) ou não deu para saber (`null`). |
| `matched_showcase` | O informado já bate com a vitrine (o site já exibe o preço com cupom). |
| `comparison` | `menor`, `maior` ou `igual`: preço final (com cupom, se aplicado) contra o informado. |
| `cached` | A leitura da loja veio do cache (10 min). |
| `detail` | Motivo, quando houver (ex.: mensagem do site, URL do bloqueio). |

### Valores de `status`

| `status` | Significado |
|---|---|
| `ok` | Conferência concluída. |
| `coupon_min_not_met` | Preço abaixo da compra mínima do cupom. |
| `coupon_not_applicable` | O cupom não vale para este produto. |
| `coupon_invalid` | O site rejeitou o código ("Confira se o cupom está correto"). |
| `coupon_expired` / `coupon_sold_out` | Cupom vencido / esgotado. |
| `coupon_unverified` | Resposta inconclusiva (ex.: "já foi adicionado", lista de cupons não carregou). |
| `captcha` | Mercado Livre pediu verificação (veja [Captcha](#captcha-pelo-dashboard)). |
| `login_required` | A conta está deslogada (o Mercado Livre manda para `/gz/account-verification` ou login). Refaça o [login](#login-no-mercado-livre) ou use a sessão do dashboard. `detail` traz a URL. |
| `not_found` | Não achou o preço na página. |
| `invalid_link` | A URL saiu dos domínios permitidos. |
| `timeout` | Passou do `REQUEST_TIMEOUT_S`. |
| `error` | Erro inesperado (veja `detail`). |

---

## Regras de negócio

- **Centavos são ignorados na comparação:** R$ 73,92 e R$ 73,00 são `igual` (compara só a parte inteira).
  Os valores devolvidos (`store_price`, `price_with_coupon`) mantêm os centavos.
- **A vitrine vence o cupom:** se o preço informado já bate com o da loja, o cupom não é aplicado de novo
  (o Mercado Livre costuma exibir o preço já com o cupom). O cupom continua calculado, só informativo.
- **Cupom:** calculado pela regra do cupom (`% × teto × compra mínima × validade`) lida em
  `/cupons/active`. A elegibilidade por produto vem de `/cupons/pdp`. Se o código não estiver na conta, a API o
  **insere na conta** (pelo modal "Inserir código") e confere de novo. Isso altera a conta de verdade.
- **Cache:** leitura da loja por URL por 10 min (só sucessos); lista de cupons da conta por 5 min.
- **Ritmo:** `MIN_GAP_S` segundos entre leituras. Um lote de várias URLs novas leva ~`MIN_GAP_S × quantidade`.

---

## Produção: checklist

- [ ] `.env` no servidor com `API_TOKEN` **forte** (`openssl rand -hex 32`) e `PUBLIC_BASE_URL` com o domínio.
- [ ] `.env` **fora do Git** (já está no `.gitignore`). Se um token foi commitado, troque-o.
- [ ] API atrás de **HTTPS** e acessível só por quem precisa (firewall/rede interna).
- [ ] O proxy repassa **WebSocket** (senão a tela do noVNC fica preta). Exemplo nginx:

  ```nginx
  location / {
      proxy_pass http://127.0.0.1:8000;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_set_header Host $host;
      proxy_read_timeout 900s;
  }
  ```
- [ ] Login feito pelo container (`profile/` do servidor) e `GET /session` com `logged_in: true`.
- [ ] Porta 6080 **não** exposta (só localhost); use túnel SSH para o serviço `login`.
- [ ] Monitorar `GET /session` de vez em quando: a sessão pode cair depois de um tempo parada.
- [ ] Um único container `verify` por `profile/`.

---

## CI/CD (GitHub Actions)

Duas instâncias desta API rodam atrás de um load balancer. O deploy é **sequencial**: o servidor 1 é
atualizado e só quando volta a `healthy` o servidor 2 é tocado — enquanto um reinicia, o LB mantém o tráfego
no outro. Se o servidor 1 falhar, o servidor 2 **não** é atualizado (continua com a versão boa no ar).

```
push na main ──> CI (testes) ──> servidor 1 (build, up -d --wait) ──> servidor 2 (idem)
                                          └─ não ficou healthy? o 2 nem começa
```

Tudo num arquivo só, `.github/workflows/ci-cd.yml`, com três jobs:

| Job | O que faz |
|---|---|
| `test` | `test_parse.py`, sintaxe do Python e dos shells, `docker compose config`. Em PR é o único que roda. |
| `plan` | Decide quais instâncias atualizar (`both`, `server1` ou `server2`). |
| `deploy` | Uma instância por vez: SSH, `git reset --hard <sha>` e `docker compose up -d --build --wait`. |

>  **Antes do primeiro push na `main`** com este arquivo, configure os segredos abaixo: o workflow dispara
>  sozinho nesse push e, sem os segredos, o job `deploy` falha no SSH (nada é alterado nos servidores).

Não há script de deploy: o job roda um comando SSH só, com três partes encadeadas por `&&`.

### Topologia

São **três** máquinas, e só duas rodam a aplicação:

| Máquina | Nome no pipeline | Acesso |
|---|---|---|
| VPS na nuvem | `server1` — instância 1 | SSH direto |
| Máquina de IP público na empresa | `bastion` — **não roda a aplicação** | SSH direto |
| Máquina na rede privada da empresa | `server2` — instância 2 | só pela LAN do gateway |

A instância 2 é alcançada com `ProxyJump bastion`: o runner conecta no gateway e, por dentro dessa conexão,
abre o SSH até a máquina da rede privada — o mesmo que você faz à mão (`ssh gateway`, depois `ssh maquina`),
em um comando. A máquina privada não precisa aceitar conexão de fora nem abrir porta no firewall; para ela, a
conexão chega da LAN. Exige só `AllowTcpForwarding yes` no gateway (padrão do OpenSSH).

### Segredos do repositório

Em *Settings → Secrets and variables → Actions* — **10 secrets**. O SSH usa a porta 22 nas três máquinas; se
algum dia uma delas mudar de porta, acrescente `Port <n>` na entrada dela no `~/.ssh/config` do workflow.

| Secret | Valor |
|---|---|
| `SSH_PRIVATE_KEY` | Chave privada de deploy, **autorizada nas três** máquinas (a mesma chave faz o salto e o destino). |
| `SSH_KNOWN_HOSTS` | Host keys das três máquinas (ver abaixo). |
| `BASTION_HOST` / `BASTION_USER` | Gateway: IP público da empresa e usuário SSH. Não precisa de Docker nem do repo. |
| `SERVER1_HOST` / `SERVER2_HOST` | IP público da VPS; IP de LAN da máquina privada. |
| `SERVER1_USER` / `SERVER2_USER` | Usuário SSH de cada instância (precisa estar no grupo `docker`). |
| `SERVER1_DIR` / `SERVER2_DIR` | Caminho do clone em cada instância, ex.: `/opt/bot-ml-check-price`. |

#### `SSH_PRIVATE_KEY`

Gere uma chave **dedicada ao deploy** (não reaproveite a sua pessoal) e **sem passphrase** — o runner não tem
como digitar uma:

```bash
ssh-keygen -t ed25519 -C "deploy-github-actions" -f ~/.ssh/ml-check-deploy -N ""
```

No **PowerShell** o `""` vazio é descartado antes de chegar ao `ssh-keygen` (erro
`option requires an argument -- N`); use aspas simples em volta, ou omita o `-N` e aperte Enter duas vezes:

```powershell
ssh-keygen -t ed25519 -C "deploy-github-actions" -f $HOME\.ssh\ml-check-deploy -N '""'
```

Isso cria dois arquivos. A **pública** (`.pub`) vai para as três máquinas; a **privada** é o conteúdo do
secret:

```bash
VPS=<user>@<ip-da-vps>
GW=<user>@<ip-publico-da-empresa>
LAN=<user>@<ip-lan-da-maquina-2>
K=~/.ssh/ml-check-deploy

# autoriza nas três (a privada nunca sai do seu computador/runner).
# ssh-copy-id é idempotente: rodar de novo não duplica a linha no authorized_keys
ssh-copy-id -i $K.pub "$VPS"   && ssh-copy-id -i $K.pub "$GW"   && ssh-copy-id -i $K.pub -o ProxyJump="$GW" "$LAN"   && echo "OK: autorizada nas 3"

# copia a chave privada para colar no secret
pbcopy < $K                                 # Mac
# Get-Content ~/.ssh/ml-check-deploy -Raw | Set-Clipboard    # Windows (PowerShell)
```

No Windows, use o **Git Bash** para estes comandos: ele traz `ssh-copy-id` e `ssh-keyscan`, que não existem no
PowerShell.

No secret vai o arquivo **inteiro**, incluindo as linhas `-----BEGIN OPENSSH PRIVATE KEY-----` e
`-----END OPENSSH PRIVATE KEY-----`.

#### `SSH_KNOWN_HOSTS`

É a identidade das máquinas (evita MITM; por isso o workflow não usa `StrictHostKeyChecking=no`). O runner não
alcança a rede privada para escanear, então a terceira linha sai de dentro do gateway:

```bash
ssh-keyscan <ip-da-vps>               # do seu computador
ssh-keyscan <ip-publico-da-empresa>   # do seu computador
ssh-keyscan <ip-lan-da-maquina-2>     # logado no gateway
```

Dá para fazer os três de uma vez sem entrar no gateway, rodando o terceiro `ssh-keyscan` remotamente por lá:

```bash
{ ssh-keyscan <ip-da-vps>
  ssh-keyscan <ip-publico-da-empresa>
  ssh <user>@<ip-publico-da-empresa> "ssh-keyscan <ip-lan-da-maquina-2>"
} > known_hosts.txt
```

O conteúdo de `known_hosts.txt` é o secret (o `ssh-keyscan` manda as chaves no stdout e o progresso no stderr,
então o redirecionamento já separa). Cada máquina costuma devolver 2–3 linhas, uma por tipo de chave — cole
**todas**.

> **Importante:** escaneie exatamente o mesmo valor que você vai pôr em `BASTION_HOST`, `SERVER1_HOST` e
> `SERVER2_HOST`. O `known_hosts` casa por *nome*: se o secret tem o IP e você escaneou o hostname (ou
> vice-versa), a verificação falha com `Host key verification failed`.

#### Validar antes de confiar no pipeline

Do seu Mac, que está na mesma situação do runner:

```bash
K=~/.ssh/ml-check-deploy
ssh -i $K -o IdentitiesOnly=yes <user>@<ip-da-vps> 'uname -n'

# a chave precisa ser informada TAMBÉM para o salto: opções de linha de comando (-i, -J)
# valem para o destino, não para o host do ProxyJump
ssh -i $K -o IdentitiesOnly=yes   -o ProxyCommand="ssh -i $K -o IdentitiesOnly=yes -W %h:%p <user>@<ip-publico-da-empresa>"   <user>@<ip-lan-da-maquina-2> 'uname -n'
```

Se os dois imprimirem o nome da máquina sem pedir senha, o deploy funciona. (No workflow isso não é um
problema: ele define `IdentityFile` na entrada `bastion` do `~/.ssh/config`, que o `ProxyJump` respeita.)

Pré-requisitos (uma vez, fora do pipeline):

- **Ajustes por máquina** (limites diferentes de CPU/memória, portas etc.) vão num
  `docker-compose.override.yml` ao lado do compose — ele é mesclado automaticamente pelo `docker compose` e
  está no `.gitignore`, então sobrevive ao `git reset --hard` do deploy. **Não** edite o
  `docker-compose.yml` versionado no servidor: a alteração seria perdida no próximo deploy. Exemplo:

  ```yaml
  # docker-compose.override.yml — só nesta máquina
  services:
    verify:
      mem_limit: 12g
      cpus: "8"
  ```
- **Nas duas instâncias**: repo clonado em `SERVER{N}_DIR`, com `.env` preenchido e `profile/` logado (ambos
  são **locais** da máquina e nunca são sobrescritos pelo deploy — estão no `.gitignore`); `git fetch`
  funcionando sem senha interativa (deploy key ou token no credential helper); e o `origin` apontando para o
  repo da organização — confira com `git remote -v` e, se estiver num fork, corrija:

  ```bash
  git remote set-url origin https://github.com/Pechinchou/bot-ml-check-price.git
  git fetch origin   # tem que funcionar sem pedir senha
  ```

  A chave de deploy do pipeline **não** serve para isso: ela é do runner para as máquinas; esta é da máquina
  para o GitHub.
- **Nas três máquinas**: chave pública do deploy no `authorized_keys`. A chave privada fica **só no runner** —
  não copie para o gateway, e não é preciso agent forwarding: a autenticação na instância 2 é de ponta a
  ponta, por dentro do túnel.
- **No gateway**: `AllowTcpForwarding yes` no sshd (padrão do OpenSSH). Nada mais — sem Docker, sem o repo.

### O que o deploy faz em cada servidor

```bash
cd <SERVER{N}_DIR>   && git fetch --prune origin   && git reset --hard <sha>   && docker compose up -d --build --wait --wait-timeout 300 verify
```

- **`reset --hard` em vez de `pull`**: implanta o commit exato que passou no CI, sem travar por causa de
  alteração local no servidor. Não toca em `.env` nem em `profile/` (ignorados pelo Git).
- **`--wait`**: o compose só retorna quando o healthcheck do serviço passa, e sai com erro se não passar.
  É o que mantém o deploy sequencial seguro — se o servidor 1 não voltar, o job falha e o `fail-fast` da
  matriz impede que o servidor 2 seja tocado.
- **`--build`**: a imagem é construída no próprio servidor. Mudança só em `app.py` reaproveita o cache das
  camadas do Camoufox e leva segundos.

Se algo falhar, um step de diagnóstico imprime `git log -1`, `docker compose ps` e as últimas linhas do log
daquele servidor no próprio Actions.

### Deploy manual e rollback

Na aba **Actions → CI/CD → Run workflow**:

- `target`: `both` (padrão), `server1` ou `server2` — útil para redeploy de uma instância só.
- `sha`: vazio usa o `HEAD`; informe o **SHA anterior** para fazer rollback das duas instâncias.

Mudanças só em arquivos `.md` não disparam deploy. Dois deploys nunca rodam ao mesmo tempo
(`concurrency: deploy`).

### Observações

- O deploy reinicia o container: um `POST /check` em andamento é abortado (até `REQUEST_TIMEOUT_S`). Prefira
  horários de baixo movimento se isso importar.
- Antes de usar o serviço `login` num servidor, lembre que um deploy concorrente vai falhar de propósito no
  pré-flight — é intencional, para não disputar o `profile/`.
- O build roda em cada servidor. Mudança em `requirements.txt` refaz o download de ~1,3 GB do Camoufox nos
  dois; mudança só em `app.py` reaproveita o cache e leva segundos. Se isso incomodar, o caminho é publicar a
  imagem no `ghcr.io` pelo CI e trocar o `build` por `pull` no script de deploy do `ci-cd.yml`.

---
## Rodar sem Docker (desenvolvimento)

Requer Python 3.12+ (testado em 3.14) e `pip`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt websockets
.venv/bin/python -m camoufox fetch           # baixa o navegador (demora)
cp .env.example .env                         # HEADLESS=0 abre a janela do navegador
.venv/bin/uvicorn app:app --port 8000 --env-file .env
```

- Com `HEADLESS=0` a janela do Camoufox abre; faça o login nela (a sessão fica em `profile/`).
- O captcha pelo dashboard (`/captcha/*`) só funciona com `HEADLESS=display` no Linux/Docker.
- Se aparecer `Failed to launch the browser process`, há outra instância usando `profile/`:
  `pkill -f "uvicorn app:app"; pkill -f "caches/camoufox/browsers"`.
- Rode **um** `uvicorn` por vez (sem `--workers`).

---

## Solução de problemas

| Sintoma | Causa provável e solução |
|---|---|
| `compose` recusa subir / API sem token | Defina `API_TOKEN` no `.env`. Sem ele a API fica aberta (e o log avisa). |
| `/session` → `logged_in: false` | Sessão caiu ou nunca foi feita. `/captcha/status` mostra `reason: "login"`. Use o [login pelo dashboard](#login-pelo-dashboard) (`POST /login/session`) ou refaça o [login](#login-no-mercado-livre). |
| `/check` → `captcha` | Use o [fluxo de captcha](#captcha-pelo-dashboard). O campo `detail` mostra se foi URL de bloqueio ou elemento da página. Se for frequente, aumente `MIN_GAP_S`. |
| 500 com `TargetClosedError ... browser has been closed` | O navegador do servidor fechou. A API reinicia o processo sozinha (≈15 s). Se não reiniciar, `docker compose restart verify`. |
| Link do noVNC → 404 | A sessão não está mais `pending` (resolvida, expirada, ou o container reiniciou). Gere outra. |
| Deploy: `Permission denied (publickey)` numa máquina, apesar do `ssh-copy-id` ter dito `key(s) added: 1` | Home gravável por grupo/outros: com `StrictModes yes` (padrão) o sshd ignora o `authorized_keys`. Confira com `ls -ld ~` e corrija com `chmod go-w ~`. O motivo exato aparece em `sudo tail -40 /var/log/auth.log`. |
| noVNC: "Falha ao conectar-se ao servidor" | Proxy sem suporte a WebSocket, ou sessão encerrada. Veja os headers do exemplo nginx e `docker compose logs verify`. |
| `Server is already active for display 99` | Trava velha do Xvfb. Já tratado em `start.sh`/`login.sh`; reconstrua a imagem. |
| `Failed to launch the browser process` | Outro container/processo usa o mesmo `profile/`. Pare o outro (`docker compose ps`, `pkill ...`). |
| `No space left on device` na build | Disco do Docker cheio. `docker builder prune` ou aumente o disco. |
| `exec format error` na build | Plataforma errada. Não force `linux/amd64` num Docker arm64 sem emulação. |
| Build muito lenta na 1ª vez | Normal: baixa ~1,3 GB do navegador (só na primeira vez). |
| 422 no `/check` | URL fora dos domínios permitidos do Mercado Livre. |
| 401 | Token ausente/errado (`X-Api-Token` ou `Authorization: Bearer`). |
| Comparação `igual` com preços diferentes | Esperado: os centavos são ignorados. |

---

## Testes e estrutura

```bash
.venv/bin/python test_parse.py    # testes unitários das regras (sem navegador); imprime "ok"
```

```
app.py              API: leitura de preço, cupons, cache, captcha/noVNC, segurança
login.py            Modo login: abre o navegador e espera o login (e o captcha de VERIFY_URL)
login.sh            Sobe tela virtual + VNC + noVNC e roda o login.py
start.sh            Sobe tela virtual + VNC e roda a API (usado pelo serviço verify)
Dockerfile          Imagem (Python + Camoufox + Xvfb + x11vnc + noVNC)
docker-compose.yml  Serviços: verify (API) e login (perfil "login", sob demanda)
requirements.txt    Dependências Python (websockets é instalado numa camada própria do Dockerfile)
test_parse.py       Testes unitários
.github/workflows/  ci-cd.yml: testes e deploy sequencial nas 2 instâncias
.env.example        Modelo de configuração
profile/            Sessão do navegador (NÃO versionar; contém a sessão da sua conta)
```

---

## Limitações e próximos passos

- **Uma conta** do Mercado Livre por instância (um `profile/`).
- **IP de datacenter** costuma levar mais captcha; se for frequente, considere proxy residencial (não implementado).
- O captcha só é resolvido por pessoas (via link); não há resolução automática.
- Adiados (não implementados): keepalive de sessão a cada 10 min e fingerprint fixo por perfil, leitura de preço
  pelo JSON interno do site (mais rápida), normalizar/expandir links (`meli.la`, `utm`), cupom "clicável" sem
  código e `GET /health`.
