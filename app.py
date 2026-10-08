import asyncio
import json
import os
import logging
import re
import secrets
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from camoufox.async_api import AsyncCamoufox
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from pathlib import Path
from urllib.parse import urlparse

# Seletores do ML isolados aqui: se o layout mudar, é só ajustar estas constantes.
SEL_PRICE_META = 'meta[itemprop="price"]'
SEL_PRICE_FRACTION = ".ui-pdp-price__second-line .andes-money-amount__fraction"
SEL_PRICE_CENTS = ".ui-pdp-price__second-line .andes-money-amount__cents"
SEL_LOGGED_IN = "a.nav-header-user-myml, .nav-header-user-myml, [data-js='user-menu']"
COUPONS_URL = "https://www.mercadolivre.com.br/cupons/active"
COUPONS_TTL_S = 300
STORE_TTL_S = 600  # cache da leitura da loja por URL
_h = os.getenv("HEADLESS", "0").lower()
HEADLESS = "virtual" if _h == "virtual" else _h in ("1", "true", "yes")  # virtual = Xvfb (só Linux/Docker); headless: não dá p/ resolver captcha
# display = navegador visível numa tela virtual fixa (DISPLAY, ex. :99) espelhada por VNC: captcha resolvido remotamente (/captcha/*)
SOLVE_REMOTE = _h == "display"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
CAPTCHA_SESSION_TTL_S = float(os.getenv("CAPTCHA_SESSION_TTL_S", "900"))
NOVNC_ROOT = Path(os.getenv("NOVNC_ROOT", "/usr/share/novnc"))
VNC_ADDR = ("127.0.0.1", 5900)  # x11vnc só em localhost; quem expõe é a ponte /vnc/{token}/websockify
ADD_COUPON_URL = "https://www.mercadolivre.com.br/cupons"
SEL_ADD_COUPON_LINK = "text=Inserir código >> visible=true"
SEL_ADD_COUPON_INPUT = "input[placeholder*='código do cupom' i]"
SEL_CAPTCHA = "iframe[src*='recaptcha'], .g-recaptcha, #captcha, [class*='captcha']"
CAPTCHA_WAIT_S = 120
BLOCK_PATHS = ("/gz/account-verification", "/captcha/wall", "/verify/traffic", "/verify/captcha")
ALLOWED_HOSTS = ("mercadolivre.com.br", "mercadolivre.com", "meli.la")  # evita SSRF: o navegador do servidor só abre o ML
API_TOKEN = os.getenv("API_TOKEN", "")
SLOTS = int(os.getenv("SLOTS", "1"))
MIN_GAP_S = float(os.getenv("MIN_GAP_S", "10"))  # intervalo mínimo entre aberturas de produto (reduz recaptcha)
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "150"))
ELIGIBLE_TTL_S = 1800

browser_ctx = None
slots = asyncio.Semaphore(SLOTS)  # ponytail: navegador único; escalar com mais perfis só se precisar
gap_lock, last_start = asyncio.Lock(), 0.0
eligible_cache: dict[tuple, float] = {}  # (item_id, code) -> ts; só guarda "elegível"
captcha_free = asyncio.Event()
captcha_free.set()
coupons_cache = (0.0, {})  # (timestamp, {code: cupom})
captcha_state = {"needed": False, "blocked_url": None, "since": None, "session": None}  # 1 sessão de resolução por vez
store_cache: dict[str, tuple[float, dict]] = {}  # ponytail: sem limpeza ativa, só sobrescreve; adicionar varredura se houver muitas URLs distintas
store_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # 1 consulta por URL, mesmo em lote


def parse_price(text: str) -> float:
    """'R$ 1.299,90' -> 1299.9 ; '199' -> 199.0"""
    digits = re.sub(r"[^\d,]", "", text.replace(".", ""))
    return float(digits.replace(",", "."))


def fresh(ts: float, now: float, ttl: float) -> bool:
    return now - ts < ttl


def compare(final: float, informed: float) -> str:
    """Ignora os centavos: compara só a parte inteira (73,92 == 73,00)."""
    final, informed = int(final), int(informed)
    if final == informed:
        return "igual"
    return "menor" if final < informed else "maior"


def host_allowed(url: str) -> bool:
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(host == d or host.endswith("." + d) for d in ALLOWED_HOSTS)


def final_verdict(price: float, informed: float, coupon_final: float | None) -> tuple[float, bool]:
    """Vitrine vence o cupom: se o informado já bate com a vitrine, o ML já exibe o preço com cupom
    (aplicar de novo daria desconto em dobro). Retorna (preço final, bateu_com_a_vitrine)."""
    matched = int(price) == int(informed)
    return (price if matched or coupon_final is None else coupon_final), matched


def coupon_failure(msg: str) -> str:
    """Status a partir da mensagem do site ao inserir o código. Só é 'inválido' quando ele rejeita o código;
    qualquer resposta desconhecida é inconclusiva."""
    m = msg.lower()
    if "confira se o cupom" in m:
        return "coupon_invalid"
    if "esgotou" in m:
        return "coupon_sold_out"
    if "não está mais disponível" in m:
        return "coupon_expired"
    return "coupon_unverified"


def safe_static_path(root: Path, rel: str) -> Path | None:
    """Arquivo dentro de root (bloqueia ../ e links para fora); None se não existe."""
    p = (root / rel).resolve()
    return p if p.is_relative_to(root.resolve()) and p.is_file() else None


def session_valid(session: dict | None, token: str, now: float) -> bool:
    """O token da URL do noVNC só vale para a sessão pendente e não expirada."""
    return bool(session and session["status"] == "pending" and now < session["expires_at"]
                and secrets.compare_digest(session["token"], token))


class Item(BaseModel):
    url: str
    price: float
    coupon: str | None = None

    @field_validator("url")
    @classmethod
    def url_must_be_ml(cls, v):
        if not host_allowed(v):
            raise ValueError("url deve ser do Mercado Livre")
        return v.strip()


def require_token(authorization: str = Header(None), x_api_token: str = Header(None)):
    if not API_TOKEN:
        return  # sem token configurado: aberto (dev local)
    given = x_api_token or (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(given.encode(), API_TOKEN.encode()):
        raise HTTPException(401, "token inválido")


@asynccontextmanager
async def lifespan(_):
    global browser_ctx
    if not API_TOKEN:
        logging.getLogger("uvicorn.error").warning("API_TOKEN não definido: API sem autenticação")
    # headed + perfil persistente: permite login manual (2FA) e resolver captcha na janela
    async with AsyncCamoufox(headless=HEADLESS, persistent_context=True, user_data_dir="profile", locale="pt-BR",
                             # fechar a última aba (ex. pela tela do noVNC) não pode encerrar o navegador do servidor
                             firefox_user_prefs={"browser.tabs.closeWindowWithLastTab": False}) as ctx:
        browser_ctx = ctx
        closing = {"on": False}

        def on_browser_close(*_):
            # navegador fechou/morreu (janela fechada no noVNC, falta de memória, crash): sem ele toda chamada dá 500.
            # Encerra o processo para o Docker (restart: unless-stopped) subir tudo de novo.
            if not closing["on"]:
                logging.getLogger("uvicorn.error").error("navegador fechou: encerrando o processo para reiniciar")
                os._exit(1)

        ctx.on("close", on_browser_close)
        try:
            yield
        finally:
            closing["on"] = True  # desligamento normal não é falha


app = FastAPI(lifespan=lifespan)  # /docs e /vnc/* sem token da API (o /vnc usa o token da sessão)
api = APIRouter(dependencies=[Depends(require_token)])


async def has_captcha(page) -> str:
    """O que indicou bloqueio/captcha (vazio = nada); o motivo vai no `detail` da resposta."""
    if "captcha" in page.url or any(p in page.url for p in BLOCK_PATHS):
        return f"url: {page.url}"
    el = await page.query_selector(SEL_CAPTCHA)
    return f"elemento: {(await el.evaluate('e => e.outerHTML'))[:160]}" if el else ""


async def wait_captcha(page) -> bool:
    """Pausa todas as requisições até o usuário resolver o captcha na janela."""
    captcha_free.clear()
    try:
        for _ in range(CAPTCHA_WAIT_S):
            if not await has_captcha(page):
                return True
            await asyncio.sleep(1)
        return False
    finally:
        captcha_free.set()


async def read_price(page) -> float | None:
    meta = await page.query_selector(SEL_PRICE_META)
    if meta and (v := await meta.get_attribute("content")):
        return float(v)
    frac = await page.query_selector(SEL_PRICE_FRACTION)
    if not frac:
        return None
    cents = await page.query_selector(SEL_PRICE_CENTS)
    return parse_price(f"{await frac.inner_text()},{(await cents.inner_text()) if cents else '00'}")


def discount(price: float, c: dict) -> tuple[str, float]:
    """Regra do cupom (da página /cupons/active) aplicada ao preço -> (status, preço final)."""
    if datetime.fromisoformat(c["expiration_date"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
        return "coupon_expired", price
    if price < c["min_amount"]:
        return "coupon_min_not_met", price
    off = price * c["discount_value"] / 100 if c["discount_type"] == "PERCENT" else c["discount_value"]
    return "ok", round(price - min(off, c["cap_amount"]), 2)


def parse_coupons(html: str) -> dict:
    """Objetos de cupom ({campaign_id, code, ...}) embutidos como JSON na página -> {CÓDIGO: cupom}."""
    found, dec, i = {}, json.JSONDecoder(), 0
    while (i := html.find('{"campaign_id"', i)) != -1:
        try:
            obj, i = dec.raw_decode(html, i)
        except ValueError:
            i += 1
            continue
        if "code" in obj and "discount_type" in obj:
            found[obj["code"].upper()] = obj
    return found


async def load_coupons(force: bool = False) -> dict:
    """Cupons ativos da conta logada; os dados vêm como JSON embutido na página."""
    global coupons_cache
    ts, cache = coupons_cache
    if not force and time.time() - ts < COUPONS_TTL_S:
        return cache
    page = await browser_ctx.new_page()
    try:
        await page.goto(COUPONS_URL, wait_until="domcontentloaded")
        await page.wait_for_selector("text=Meus cupons", timeout=15000)
        found = parse_coupons(await page.content())
    finally:
        await page.close()
    coupons_cache = (time.time(), found)
    return found


async def add_coupon(code: str) -> str:
    """Insere o código na conta (modal 'Inserir código' em /cupons); devolve o texto do modal."""
    page = await browser_ctx.new_page()
    try:
        await page.goto(ADD_COUPON_URL, wait_until="domcontentloaded")
        await page.click(SEL_ADD_COUPON_LINK)
        await page.fill(SEL_ADD_COUPON_INPUT, code)
        await page.get_by_role("button", name="Inserir", exact=True).click()
        await page.wait_for_timeout(2500)
        # sobe do input até o menor ancestral com texto curto: o corpo do modal com a mensagem do site
        return await page.eval_on_selector(SEL_ADD_COUPON_INPUT, """el => {
            while (el.parentElement && el.innerText.length < 40) el = el.parentElement;
            return el.innerText.replace(/\\s+/g, ' ').trim();
        }""")
    finally:
        await page.close()


coupon_lock = asyncio.Lock()  # evita inserir o mesmo código várias vezes em lote


async def get_coupon(code: str) -> tuple[dict | None, str]:
    code = code.strip().upper()
    async with coupon_lock:
        if c := (await load_coupons()).get(code):
            return c, ""
        msg = await add_coupon(code)
        return (await load_coupons(force=True)).get(code), msg


async def coupon_eligible(item_id: str | None, price: float, loyalty: str, code: str) -> bool | None:
    """O cupom aparece na lista de cupons deste produto (/cupons/pdp)? None = inconclusivo."""
    key = (item_id, code)
    if not item_id:
        return None
    if key in eligible_cache and fresh(eligible_cache[key], time.time(), ELIGIBLE_TTL_S):
        return True
    page = await browser_ctx.new_page()
    try:
        await page.goto(f"https://www.mercadolivre.com.br/cupons/pdp?item_id={item_id}&unit_price={price}"
                        f"&quantity=1&loyalty={loyalty}&new_version=true", wait_until="domcontentloaded")
        html = await page.content()
    except Exception:
        return None
    finally:
        await page.close()
    if '"groupings"' not in html:  # lista não carregou: não dá para afirmar nada
        return None
    ok = f'"{code}"' in html
    if ok:
        eligible_cache[key] = time.time()
    return ok


async def pace():
    """Garante MIN_GAP_S entre o início de duas leituras de produto."""
    global last_start
    async with gap_lock:
        await asyncio.sleep(max(0.0, last_start + MIN_GAP_S - time.time()))
        last_start = time.time()


async def fetch_store(url: str) -> dict:
    """Abre o produto e lê preço + item_id da página; ou {"status": erro}."""
    if (cs := captcha_state["session"]) and cs["status"] == "pending":  # alguém está resolvendo: não enfileira
        return {"status": "captcha", "detail": "verificação em andamento"}
    await captcha_free.wait()
    async with slots:
        await pace()
        page = await browser_ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if not host_allowed(page.url):
                return {"status": "invalid_link"}
            if "login" in page.url or "registration" in page.url:
                return {"status": "login_required"}
            if reason := await has_captcha(page):
                if SOLVE_REMOTE:  # o dashboard mostra o alerta (/captcha/status) e abre uma sessão (/captcha/session)
                    captcha_state.update(needed=True, blocked_url=url, since=captcha_state["since"] or time.time())
                    return {"status": "captcha", "detail": reason}
                if HEADLESS or not await wait_captcha(page):  # headless: ninguém para resolver, não espera
                    return {"status": "captcha", "detail": reason}
            price, html = await read_price(page), await page.content()
            if price is None:
                return {"status": "not_found"}
            captcha_state.update(needed=False, since=None)  # leitura normal: o bloqueio passou
            ids = Counter(re.findall(r'"item_id":"(MLB\d+)"', html))  # a oferta vencedora é a mais citada
            loyalty = re.search(r'"loyalty_level":(\d+)', html)  # a rota de cupons do produto exige o nível
            return {"status": "ok", "price": price, "item_id": ids.most_common(1)[0][0] if ids else None,
                    "loyalty": loyalty.group(1) if loyalty else "0"}
        except Exception as e:  # um item com erro não derruba o batch
            return {"status": "error", "detail": str(e)}
        finally:
            await page.close()


async def get_store(url: str) -> tuple[dict, bool]:
    """(leitura da loja, veio_do_cache). Só leituras com sucesso são cacheadas."""
    url = url.strip()
    async with store_locks[url]:
        hit = store_cache.get(url)
        if hit and fresh(hit[0], time.time(), STORE_TTL_S):
            return hit[1], True
        store = await fetch_store(url)
        if store["status"] == "ok":
            store_cache[url] = (time.time(), store)
        return store, False


async def check(item: Item) -> dict:
    store, cached = await get_store(item.url)
    if store["status"] != "ok":
        return store
    price, with_coupon = store["price"], None
    out = {"status": "ok", "store_price": price, "price_matches": int(price) == int(item.price),
           "coupon_applied": False, "price_with_coupon": None, "cached": cached}
    if item.coupon:
        try:
            c, msg = await get_coupon(item.coupon)
        except Exception as e:  # lista de cupons não carregou: inconclusivo, não "inválido"
            c, msg = None, f"erro ao consultar cupons: {e}"
        if c is None:
            out.update(status=coupon_failure(msg), detail=msg)
        else:
            out["coupon_eligible"] = await coupon_eligible(store["item_id"], price, store["loyalty"], c["code"].upper())
            if out["coupon_eligible"] is False:
                out["status"] = "coupon_not_applicable"
            else:
                out["status"], final = discount(price, c)
                if out["status"] == "ok":
                    with_coupon = final
                    out.update(coupon_applied=True, price_with_coupon=final)
    final, out["matched_showcase"] = final_verdict(price, item.price, with_coupon)
    out["comparison"] = compare(final, item.price)
    return out


async def run(item: Item) -> dict:
    try:
        return await asyncio.wait_for(check(item), REQUEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"status": "timeout"}


@api.get("/session")
async def session():
    page = await browser_ctx.new_page()
    try:
        await page.goto("https://www.mercadolivre.com.br", wait_until="domcontentloaded")
        return {"logged_in": await page.query_selector(SEL_LOGGED_IN) is not None}
    finally:
        await page.close()


@api.post("/check")
async def check_one(item: Item):
    return await run(item)


@api.post("/check/batch")
async def check_batch(items: list[Item]):
    return await asyncio.gather(*(run(i) for i in items))


# --- Resolver captcha pelo dashboard: sessão com link do noVNC (ponte VNC servida pela própria API) ---

class CaptchaReq(BaseModel):
    url: str | None = None


def vnc_url(token: str) -> str:
    # noVNC 1.6 resolve `path` relativo à URL da página (/vnc/{token}/), então basta "websockify"
    return f"{PUBLIC_BASE_URL}/vnc/{token}/vnc.html?autoconnect=1&resize=scale&path=websockify"


async def watch_session(s: dict):
    """Resolvida quando a página sai da parede de captcha e mostra produto/menu logado; expira após o TTL."""
    page = s["page"]
    try:
        while time.time() < s["expires_at"]:
            await asyncio.sleep(3)
            if not await has_captcha(page) and await page.query_selector(f"{SEL_PRICE_META}, {SEL_LOGGED_IN}"):
                await asyncio.sleep(5)  # deixa os cookies serem gravados no perfil
                s["status"] = "solved"
                captcha_state.update(needed=False, since=None)
                return
        s["status"] = "expired"
    except Exception as e:
        s.update(status="expired", detail=str(e))
    finally:
        try:
            await page.close()
        except Exception:
            pass


@api.get("/captcha/status")
async def captcha_status():
    s = captcha_state["session"]
    return {"needed": captcha_state["needed"], "blocked_url": captcha_state["blocked_url"],
            "since": captcha_state["since"], "session": {"id": s["id"], "status": s["status"]} if s else None}


@api.post("/captcha/session")
async def captcha_session(req: CaptchaReq | None = None):
    s = captcha_state["session"]
    if not (s and s["status"] == "pending"):
        url = (req.url if req and req.url else captcha_state["blocked_url"]) or "https://www.mercadolivre.com.br/"
        if not host_allowed(url):
            raise HTTPException(422, "url deve ser do Mercado Livre")
        page = await browser_ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.bring_to_front()
        except Exception as e:
            await page.close()
            raise HTTPException(502, f"não abriu a página: {e}")
        s = {"id": secrets.token_hex(8), "token": secrets.token_urlsafe(32), "status": "pending", "detail": "",
             "expires_at": time.time() + CAPTCHA_SESSION_TTL_S, "page": page}
        captcha_state["session"] = s
        s["task"] = asyncio.create_task(watch_session(s))  # referência forte: senão o GC pode matar a tarefa
    return {"id": s["id"], "url": vnc_url(s["token"]), "expires_in": int(s["expires_at"] - time.time())}


@api.get("/captcha/session/{sid}")
async def captcha_session_status(sid: str):
    s = captcha_state["session"]
    if not s or s["id"] != sid:
        raise HTTPException(404, "sessão não encontrada")
    return {"status": s["status"], "detail": s["detail"]}


def active_session(token: str) -> dict | None:
    s = captcha_state["session"]
    return s if session_valid(s, token, time.time()) else None


@app.get("/vnc/{token}/{path:path}")
async def vnc_static(token: str, path: str):
    """Cliente noVNC (arquivos de /usr/share/novnc); a autorização é o token da sessão na URL."""
    if not active_session(token):
        raise HTTPException(404)
    f = safe_static_path(NOVNC_ROOT, path or "vnc.html")
    if not f:
        raise HTTPException(404)
    return FileResponse(f)


@app.websocket("/vnc/{token}/websockify")
async def vnc_bridge(ws: WebSocket, token: str):
    """Ponte WebSocket <-> x11vnc (equivalente ao websockify): repassa bytes RFB nos dois sentidos."""
    if not active_session(token):
        await ws.close(code=1008)
        return
    # só ecoa o subprotocolo se o cliente pediu: o Chrome recusa o handshake se a resposta trouxer um que ele não ofereceu
    await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
    reader, writer = await asyncio.open_connection(*VNC_ADDR)

    async def up():
        while True:
            writer.write(await ws.receive_bytes())
            await writer.drain()

    async def down():
        while data := await reader.read(65536):
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)  # ponytail: a conexão aberta sobrevive ao fim da sessão até o cliente fechar
    finally:
        for t in tasks:
            t.cancel()
        writer.close()


app.include_router(api)
