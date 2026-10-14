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
HOME_URL = "https://www.mercadolivre.com.br/"
LOGIN_MARKERS = ("login", "registration", "/gz/account-verification")  # páginas que pedem login (não são captcha)
BLOCK_PATHS = ("/gz/account-verification", "/captcha/wall", "/verify/traffic", "/verify/captcha")
ALLOWED_HOSTS = ("mercadolivre.com.br", "mercadolivre.com", "meli.la")  # evita SSRF: o navegador do servidor só abre o ML
API_TOKEN = os.getenv("API_TOKEN", "")
SLOTS = int(os.getenv("SLOTS", "1"))
MIN_GAP_S = float(os.getenv("MIN_GAP_S", "10"))  # intervalo mínimo entre aberturas de produto (reduz recaptcha)
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "150"))
ELIGIBLE_TTL_S = 1800
SESSION_CACHE_TTL_S = float(os.getenv("SESSION_CACHE_TTL_S", "30"))  # /session: resposta reaproveitada p/ não abrir uma janela por chamada
SESSION_TIMEOUT_S = float(os.getenv("SESSION_TIMEOUT_S", "25"))
WATCHDOG_INTERVAL_S = float(os.getenv("WATCHDOG_INTERVAL_S", "60"))
WATCHDOG_TIMEOUT_S = float(os.getenv("WATCHDOG_TIMEOUT_S", "20"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "15"))  # abas abertas acima disso = vazamento (cada uma é um processo do Firefox)

browser_ctx = None
main_page = None  # janela única mostrada ao operador no noVNC (cada new_page() abriria outra janela por cima)
slots = asyncio.Semaphore(SLOTS)  # ponytail: navegador único; escalar com mais perfis só se precisar
gap_lock, last_start = asyncio.Lock(), 0.0
eligible_cache: dict[tuple, float] = {}  # (item_id, code) -> ts; só guarda "elegível"
captcha_free = asyncio.Event()
captcha_free.set()
coupons_cache = (0.0, {})  # (timestamp, {code: cupom})
session_cache = (0.0, None)  # (timestamp, logged_in)
session_lock = asyncio.Lock()  # uma checagem por vez: chamadas simultâneas esperam e reaproveitam o resultado
captcha_state = {"needed": False, "reason": None, "blocked_url": None, "since": None, "session": None}  # reason: "captcha"|"login"; 1 sessão por vez
store_cache: dict[str, tuple[float, dict]] = {}  # ponytail: sem limpeza ativa, só sobrescreve; adicionar varredura se houver muitas URLs distintas
store_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # 1 consulta por URL, mesmo em lote


def parse_price(text: str) -> float:
    """'R$ 1.299,90' -> 1299.9 ; '199' -> 199.0"""
    digits = re.sub(r"[^\d,]", "", text.replace(".", ""))
    return float(digits.replace(",", "."))


async def bounded(coro, timeout: float):
    """Como wait_for, mas NÃO espera o cancelamento: com o Firefox travado, o `finally: page.close()` da tarefa
    nunca volta e o wait_for pendurava junto (preso no session_lock/slots)."""
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        raise asyncio.TimeoutError
    return task.result()


async def probe_browser():
    s = captcha_state["session"]
    if s and s["status"] == "pending" and not s["page"].is_closed():
        await s["page"].evaluate("1")  # new_page() abriria uma janela por cima da tela do operador
        return
    page = await browser_ctx.new_page()
    await page.close()


async def watchdog():
    """Navegador pendurado não dispara o evento `close`: se abrir/fechar uma aba falhar 2x seguidas, reinicia o processo."""
    fails = 0
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_S)
        urls = [p.url[:80] for p in browser_ctx.pages]
        if len(urls) > 4:  # normal: 1 janela do operador + até ~3 leituras/cupons em andamento
            logging.getLogger("uvicorn.error").warning("watchdog: %d abas abertas: %s", len(urls), urls)
        if len(urls) > MAX_PAGES:
            logging.getLogger("uvicorn.error").error("watchdog: abas demais (vazamento): reiniciando")
            os._exit(1)
        try:
            await bounded(probe_browser(), WATCHDOG_TIMEOUT_S)
            fails = 0
        except Exception:
            fails += 1
            logging.getLogger("uvicorn.error").error("watchdog: navegador não responde (%d/2)", fails)
            if fails >= 2:
                os._exit(1)  # Docker (restart: unless-stopped) sobe tudo de novo


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


def is_login_url(url: str) -> bool:
    return any(m in url for m in LOGIN_MARKERS)


def flag_blocked(reason: str, url: str):
    """O dashboard vê isso em /captcha/status: reason=captcha (resolver a verificação) ou login (entrar na conta)."""
    captcha_state.update(needed=True, reason=reason, blocked_url=url, since=captcha_state["since"] or time.time())


def clear_blocked():
    captcha_state.update(needed=False, reason=None, since=None)


def html_logged_in(html: str) -> bool:
    """A home logada traz o menu do usuário; a deslogada traz o link "Entre"."""
    return "nav-header-user-myml" in html


def has_login_cookies(cookies: list[dict]) -> bool:
    """Cookies que só existem com a conta logada (a deslogada tem `ftid` e `orguseridp`, mas não estes)."""
    names = {c["name"] for c in cookies}
    return {"ssid", "orguserid"} <= names


def login_step(seen_out: bool, menu: bool) -> tuple[bool, bool]:
    """(já_viu_deslogado, terminou). Só termina com o menu presente DEPOIS de ter visto a tela deslogada:
    em `force` a pessoa já começa logada, e o menu de antes do logout não pode contar como login novo."""
    return seen_out or not menu, menu and seen_out


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
    global browser_ctx, main_page
    if not API_TOKEN:
        logging.getLogger("uvicorn.error").warning("API_TOKEN não definido: API sem autenticação")
    # headed + perfil persistente: permite login manual (2FA) e resolver captcha na janela
    async with AsyncCamoufox(headless=HEADLESS, persistent_context=True, user_data_dir="profile", locale="pt-BR",
                             # fechar a última aba (ex. pela tela do noVNC) não pode encerrar o navegador do servidor
                             firefox_user_prefs={"browser.tabs.closeWindowWithLastTab": False}) as ctx:
        browser_ctx = ctx
        main_page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        closing = {"on": False}

        def on_browser_close(*_):
            # navegador fechou/morreu (janela fechada no noVNC, falta de memória, crash): sem ele toda chamada dá 500.
            # Encerra o processo para o Docker (restart: unless-stopped) subir tudo de novo.
            if not closing["on"]:
                logging.getLogger("uvicorn.error").error("navegador fechou: encerrando o processo para reiniciar")
                os._exit(1)

        ctx.on("close", on_browser_close)
        wd = asyncio.create_task(watchdog())
        try:
            yield
        finally:
            wd.cancel()
            closing["on"] = True  # desligamento normal não é falha


app = FastAPI(lifespan=lifespan)  # /docs e /vnc/* sem token da API (o /vnc usa o token da sessão)
api = APIRouter(dependencies=[Depends(require_token)])


async def light_page():
    """Página para leitura em segundo plano: bloqueia imagens, fontes e mídia (a home do ML é pesada e a VM tem pouca CPU)."""
    page = await browser_ctx.new_page()
    try:
        await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font")
                         else route.continue_())
    except BaseException:
        await page.close()  # senão a aba vazaria (cada uma é um processo do Firefox)
        raise
    return page


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
    page = await light_page()
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
    page = await light_page()
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
        page = await light_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if not host_allowed(page.url):
                return {"status": "invalid_link"}
            if is_login_url(page.url):  # sessão caiu: o ML manda p/ /gz/account-verification ou login
                flag_blocked("login", HOME_URL)
                return {"status": "login_required", "detail": f"url: {page.url}"}
            if reason := await has_captcha(page):
                logging.getLogger("uvicorn.error").warning("captcha: %s (%s)", url, reason)
                if SOLVE_REMOTE:  # o dashboard mostra o alerta (/captcha/status) e abre uma sessão (/captcha/session)
                    flag_blocked("captcha", url)
                    return {"status": "captcha", "detail": reason}
                if HEADLESS or not await wait_captcha(page):  # headless: ninguém para resolver, não espera
                    return {"status": "captcha", "detail": reason}
            price, html = await read_price(page), await page.content()
            if price is None:
                return {"status": "not_found"}
            # leitura normal: o bloqueio passou. Captcha de OUTRA url não se apaga (o alerta sumia com o item ainda bloqueado)
            if captcha_state["reason"] != "captcha" or captcha_state["blocked_url"] == url \
                    or time.time() - (captcha_state["since"] or 0) > 600:
                clear_blocked()
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
        return await bounded(check(item), REQUEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"status": "timeout"}


async def logged_in_quick() -> bool | None:
    """Home por requisição com os cookies do navegador (não abre janela). Só serve como evidência POSITIVA:
    o menu pode ser montado no navegador e faltar no HTML inicial, então a ausência dele não prova deslogado (None)."""
    try:
        r = await browser_ctx.request.get(HOME_URL, timeout=15000)
        return True if html_logged_in(await r.text()) else None
    except Exception:
        return None


async def menu_on(page) -> bool:
    """O menu do usuário está na página? É o que o navegador mostra de verdade (cookies velhos não enganam)."""
    try:
        return bool(page and await page.query_selector(SEL_LOGGED_IN))
    except Exception:
        return False  # janela fechada/navegando


async def menu_on_any() -> bool:
    """O ML pode terminar o login (pós-2FA) em outra aba/janela: olha todas, não só a do operador."""
    return any([await menu_on(p) for p in list(browser_ctx.pages)])


async def operator_page(s: dict):
    """Janela do operador da sessão; se ela foi fechada (o ML troca de janela após o 2FA), adota outra aberta."""
    global main_page
    if s["page"].is_closed():
        alive = [p for p in browser_ctx.pages if not p.is_closed()]
        s["page"] = main_page = alive[-1] if alive else await browser_ctx.new_page()
    return s["page"]


async def logged_in_signal(page) -> bool:
    """Sinais baratos de login, sem navegar nem abrir janelas."""
    try:
        if page and await page.query_selector(SEL_LOGGED_IN):
            return True
    except Exception:
        pass  # janela fechada/navegando: segue para os outros sinais
    if has_login_cookies(await browser_ctx.cookies()):
        return True
    return bool(await logged_in_quick())


async def is_logged_in() -> bool:
    page = await light_page()
    try:
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        # o header é montado depois do DOMContentLoaded: espera o menu (logado) ou o link "Entre" (deslogado)
        await page.wait_for_selector(f"{SEL_LOGGED_IN}, a[data-link-id='login']", timeout=12000)
        return await page.query_selector(SEL_LOGGED_IN) is not None
    finally:
        await page.close()


@api.get("/session")
async def session(refresh: bool = False):
    """refresh=true ignora o cache de SESSION_CACHE_TTL_S."""
    global session_cache
    s = captcha_state["session"]
    if s and s["status"] == "pending":  # alguém usa a tela: responde sem abrir janela (menu na janela do operador)
        logged = await menu_on(main_page)
    else:
        async with session_lock:
            ts, cached = session_cache
            if not refresh and cached is not None and fresh(ts, time.time(), SESSION_CACHE_TTL_S):
                return {"logged_in": cached, "cached": True}
            try:
                logged = await bounded(is_logged_in(), SESSION_TIMEOUT_S)
            except Exception as e:  # trava/timeout (CPU saturada, home lenta): responde em vez de pendurar o chamador
                if cached is not None:  # último valor conhecido, marcado como antigo
                    return {"logged_in": cached, "cached": True, "stale": True}
                raise HTTPException(504, f"não deu para checar o login: {type(e).__name__}")
            session_cache = (time.time(), logged)
    if not logged:
        flag_blocked("login", HOME_URL)  # deslogado: o dashboard já mostra o alerta, sem esperar um /check falhar
    elif captcha_state["reason"] == "login":
        clear_blocked()
    return {"logged_in": logged, "cached": False}


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


async def end_session(s: dict, status: str, detail: str = ""):
    """Encerra a sessão. A janela do operador é reaproveitada: volta a about:blank em vez de fechar."""
    s.update(status=status, detail=detail)
    try:
        await s["page"].goto("about:blank")
    except Exception:
        pass


def clear_account_caches():
    """Depois de um login a conta pode ser outra: os caches (cupons, produtos, elegibilidade) ficam inválidos."""
    global coupons_cache, session_cache
    coupons_cache = (0.0, {})
    session_cache = (0.0, None)
    store_cache.clear()
    eligible_cache.clear()


async def watch_session(s: dict):
    """Captcha: resolvida quando a página sai da parede de captcha/login E a conta está logada."""
    try:
        while time.time() < s["expires_at"]:
            await asyncio.sleep(3)
            page = await operator_page(s)
            if await has_captcha(page) or is_login_url(page.url):
                continue
            if await logged_in_signal(page):
                await asyncio.sleep(5)  # deixa os cookies serem gravados no perfil
                clear_blocked()
                return await end_session(s, "solved")
        await end_session(s, "expired")
    except Exception as e:
        await end_session(s, "expired", str(e))


async def watch_login(s: dict):
    """Login: resolvida quando o MENU DO USUÁRIO aparece na janela do operador (e, com force, só depois de a pessoa
    ter saído da conta). Não navega nem abre janelas; cookies sozinhos não bastam (podem estar velhos)."""
    seen_out = not s["force"]  # sem force a pessoa começa deslogada; com force ainda está logada
    try:
        while time.time() < s["expires_at"]:
            await asyncio.sleep(3)
            await operator_page(s)
            urls = [p.url[:100] for p in browser_ctx.pages]
            if urls != s.get("urls"):  # diagnóstico: mostra para onde o ML levou o login (2FA abre aba nova?)
                s["urls"] = urls
                logging.getLogger("uvicorn.error").info("login: abas %s", urls)
            seen_out, done = login_step(seen_out, await menu_on_any())
            if done:
                await asyncio.sleep(5)  # deixa os cookies serem gravados no perfil
                clear_blocked()
                clear_account_caches()
                return await end_session(s, "solved")
        await end_session(s, "expired")
    except Exception as e:
        await end_session(s, "expired", str(e))


@api.get("/captcha/status")
async def captcha_status():
    s = captcha_state["session"]
    return {"needed": captcha_state["needed"], "reason": captcha_state["reason"],
            "blocked_url": captcha_state["blocked_url"], "since": captcha_state["since"],
            "session": {"id": s["id"], "kind": s["kind"], "status": s["status"]} if s else None}


async def open_session(kind: str, url: str, force: bool = False) -> dict:
    """Abre (ou devolve) a sessão do operador na janela única. kind: "captcha" ou "login"."""
    global main_page
    s = captcha_state["session"]
    if s and s["status"] == "pending":
        return s
    if main_page is None or main_page.is_closed():
        main_page = await browser_ctx.new_page()
    page = main_page
    try:
        for p in list(browser_ctx.pages):  # janelas vazias sobrando cobririam a do operador (leituras em andamento ficam)
            if p is not page and p.url == "about:blank":
                await p.close()
        await page.goto(url, wait_until="domcontentloaded")
        if kind == "login":  # a home tem o link "Entre" (login real do ML, com client_id): vai direto ao href
            try:
                href = await page.get_attribute('a[data-link-id="login"]', "href", timeout=8000)
                if href and host_allowed(href):
                    await page.goto(href, wait_until="domcontentloaded")
            except Exception:
                pass  # fica na home: a pessoa clica em "Entre"
        await page.bring_to_front()
    except Exception as e:
        raise HTTPException(502, f"não abriu a página: {e}")
    s = {"id": secrets.token_hex(8), "kind": kind, "token": secrets.token_urlsafe(32), "status": "pending",
         "detail": "", "expires_at": time.time() + CAPTCHA_SESSION_TTL_S, "page": page, "force": force}
    captcha_state["session"] = s
    s["task"] = asyncio.create_task((watch_login if kind == "login" else watch_session)(s))  # referência forte: senão o GC pode matar a tarefa
    return s


def session_reply(s: dict) -> dict:
    return {"id": s["id"], "kind": s["kind"], "url": vnc_url(s["token"]), "expires_in": int(s["expires_at"] - time.time())}


@api.post("/captcha/session")
async def captcha_session(req: CaptchaReq | None = None):
    url = (req.url if req and req.url else captcha_state["blocked_url"]) or HOME_URL
    if not host_allowed(url):
        raise HTTPException(422, "url deve ser do Mercado Livre")
    return session_reply(await open_session("captcha", url))


class LoginReq(BaseModel):
    force: bool = False  # abrir mesmo logado (trocar de conta)


@api.post("/login/session")
async def login_session(req: LoginReq | None = None):
    s = captcha_state["session"]
    if not (s and s["status"] == "pending") and not (req and req.force):
        if await is_logged_in():
            return {"already_logged_in": True}
    return session_reply(await open_session("login", HOME_URL, force=bool(req and req.force)))


@api.get("/login/session/{sid}")
async def login_session_status(sid: str):
    s = captcha_state["session"]
    if not s or s["id"] != sid or s["kind"] != "login":
        raise HTTPException(404, "sessão não encontrada")
    return {"status": s["status"], "logged_in": s["status"] == "solved", "detail": s["detail"]}


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
