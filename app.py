import asyncio
import json
import os
import logging
import re
import secrets
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from contextlib import asynccontextmanager, contextmanager
from importlib.metadata import version as pkg_version

from camoufox.async_api import AsyncCamoufox
from camoufox.fingerprints import generate_fingerprint
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Seletores do ML isolados aqui (e em READ_PRODUCT_JS): se o layout mudar, é só ajustar estes pontos.
SEL_SOCIAL_PRODUCT = "a.poly-component__link--action-link"  # "Ir para produto" na vitrine do afiliado (/social/...)
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
INELIGIBLE_TTL_S = 60  # "não elegível" muda mais (estoque/regra do cupom): cache curto, só p/ não repetir dentro do lote
MAX_BATCH = int(os.getenv("MAX_BATCH", "100"))  # itens por /check/batch; acima disso 413
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "200"))  # itens em andamento+esperando; cheio = 503 com Retry-After (em vez de pendurar até estourar)
TRACKING_PARAMS = ("matt_", "utm_", "reco_")  # prefixos
TRACKING_NAMES = {"source", "ref", "tracking_id", "sid", "gclid", "fbclid", "forceinapp", "search_layout", "position",
                  "polycard_client", "type", "is_advertising", "ad_domain", "ad_position", "ad_click_id", "click1"}
SESSION_CACHE_TTL_S = float(os.getenv("SESSION_CACHE_TTL_S", "30"))  # /session: resposta reaproveitada p/ não abrir uma janela por chamada
SESSION_TIMEOUT_S = float(os.getenv("SESSION_TIMEOUT_S", "25"))
WATCHDOG_INTERVAL_S = float(os.getenv("WATCHDOG_INTERVAL_S", "60"))
WATCHDOG_TIMEOUT_S = float(os.getenv("WATCHDOG_TIMEOUT_S", "20"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "15"))  # abas abertas acima disso = vazamento (cada uma é um processo do Firefox)
FINGERPRINT_FILE = Path("profile/fingerprint.json")
# Cópia do MEMORY_PREFS de apps/verify-bot/src/scrape/browser.ts (repo promo-feed). Ficam gravadas no perfil: tirar uma daqui
# não volta o valor antigo, é preciso apagar a linha em profile/prefs.js.
FIREFOX_PREFS = {
    "dom.ipc.keepProcessesAlive.web": 0, "dom.ipc.keepProcessesAlive.webIsolated": 0,  # sem processo quente após fechar aba
    "dom.ipc.processPrelaunch.enabled": False,
    "dom.ipc.processCount": 1, "dom.ipc.processCount.webIsolated": 1,  # default 8; lemos uma página por vez
    "browser.cache.memory.capacity": 32768,
    "browser.sessionhistory.max_total_viewers": 0, "browser.sessionhistory.max_entries": 3,  # bfcache: nunca voltamos página
    "image.mem.surface_cache.max_size_kb": 32768,
    "browser.cache.disk.capacity": 51200,
    "places.history.enabled": False,
    "browser.sessionstore.resume_from_crash": False, "browser.sessionstore.interval": 1_800_000,
    "network.predictor.enabled": False, "network.prefetch-next": False,
    "toolkit.telemetry.enabled": False, "datareporting.healthreport.uploadEnabled": False,
}
# Preço do meta, preço visível, área de preço e ids do produto numa só ida ao navegador (antes: ~8 + o HTML inteiro p/ Python)
READ_PRODUCT_JS = """() => {
    const html = document.documentElement.outerHTML;
    const q = s => document.querySelector(s);
    return {
        meta: q('meta[itemprop="price"]')?.content ?? null,
        fraction: q('.ui-pdp-price__second-line .andes-money-amount__fraction')?.innerText ?? null,
        cents: q('.ui-pdp-price__second-line .andes-money-amount__cents')?.innerText ?? null,
        area: q('.ui-pdp-price')?.innerText ?? '',
        ids: [...html.matchAll(/"item_id":"(MLB\\d+)"/g)].map(m => m[1]),
        loyalty: (html.match(/"loyalty_level":(\\d+)/) || [])[1] ?? null,
    };
}"""

browser_ctx = None
main_page = None  # janela única mostrada ao operador no noVNC (cada new_page() abriria outra janela por cima)
read_pages: asyncio.Queue = asyncio.Queue()  # abas de leitura livres (None = ainda não criada); limita SLOTS leituras em paralelo
for _ in range(SLOTS):  # ponytail: navegador único; escalar com mais perfis só se precisar
    read_pages.put_nowait(None)
gap_lock, last_start = asyncio.Lock(), 0.0
eligible_cache: dict[tuple, float] = {}  # (item_id, code) -> ts; "elegível"
ineligible_cache: dict[tuple, float] = {}  # (item_id, code) -> ts; "não elegível" (TTL curto)
pending = 0  # itens admitidos (esperando ou em leitura); limitado por MAX_QUEUE
stats: Counter = Counter()  # contadores p/ /metrics
captcha_free = asyncio.Event()
captcha_free.set()
coupons_cache = (0.0, {})  # (timestamp, {code: cupom})
session_cache = (0.0, None)  # (timestamp, logged_in)
session_lock = asyncio.Lock()  # uma checagem por vez: chamadas simultâneas esperam e reaproveitam o resultado
captcha_state = {"needed": False, "reason": None, "blocked_url": None, "since": None, "session": None}  # reason: "captcha"|"login"; 1 sessão por vez
store_cache: dict[str, tuple[float, dict]] = {}  # varrido por sweep_caches() a cada ciclo do watchdog
store_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # 1 consulta por URL, mesmo em lote


def parse_price(text: str) -> float:
    """'R$ 1.299,90' -> 1299.9 ; '199' -> 199.0"""
    digits = re.sub(r"[^\d,]", "", text.replace(".", ""))
    return float(digits.replace(",", "."))


def parse_product_read(raw: dict) -> tuple[float | None, float | None, str, str | None, str]:
    """Resultado de READ_PRODUCT_JS -> (preço do meta ou visível, preço visível, texto da área, item_id, loyalty)."""
    shown = parse_price(f"{raw['fraction']},{raw['cents'] or '00'}") if raw.get("fraction") else None
    price = float(raw["meta"]) if raw.get("meta") else shown
    ids = Counter(raw.get("ids") or [])  # a oferta vencedora é a mais citada
    return price, shown, raw.get("area") or "", ids.most_common(1)[0][0] if ids else None, raw.get("loyalty") or "0"


def stored_fingerprint(path: Path, camoufox_version: str, make) -> dict:
    """Impressão digital fixa entre reinícios (senão o ML via um computador novo a cada um). O arquivo guarda a versão do
    Camoufox: formato de outra versão derruba o launch, então é regerado se ela mudar."""
    try:
        saved = json.loads(path.read_text())
        if saved.get("camoufox") == camoufox_version:
            return saved["fingerprint"]
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    fp = make()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"camoufox": camoufox_version, "fingerprint": fp}))
    return fp


async def bounded(coro, timeout: float):
    """Como wait_for, mas NÃO espera o cancelamento: com o Firefox travado, o `finally: page.close()` da tarefa
    nunca volta e o wait_for pendurava junto (preso no session_lock/read_pages)."""
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
        sweep_caches(time.time())
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


def norm_url(url: str) -> str:
    """Chave de cache: tira fragmento e parâmetros de rastreio (matt_*, utm_*, source...), que não mudam o produto.
    Variação/oferta (wid, variation...) fica. Outro host (ex. meli.la) só perde o fragmento."""
    u = urlparse(url.strip())
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
         if not k.lower().startswith(TRACKING_PARAMS) and k.lower() not in TRACKING_NAMES]
    return urlunparse(u._replace(query=urlencode(q), fragment=""))


def sweep_caches(now: float):
    """Remove entradas vencidas e locks ociosos (sem isso as URLs distintas se acumulam para sempre)."""
    for cache, ttl in ((store_cache, STORE_TTL_S), (eligible_cache, ELIGIBLE_TTL_S), (ineligible_cache, INELIGIBLE_TTL_S)):
        for k in [k for k, v in cache.items() if not fresh(v[0] if isinstance(v, tuple) else v, now, ttl)]:
            cache.pop(k, None)
    for k in [k for k, lock in store_locks.items() if not lock.locked()]:
        store_locks.pop(k, None)  # sem await entre o lookup e o acquire: lock livre não tem espera


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


def is_social_page(url: str) -> bool:
    """meli.la de afiliado cai na vitrine do perfil (/social/<perfil>), não na página do produto."""
    return urlparse(url).path.startswith("/social/")


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


def coupon_in_page(area_text: str, price: float, shown: float | None) -> bool:
    """A vitrine já traz um cupom aplicado: o rótulo "com Cupom" na área de preço, ou o preço do meta menor que
    o exibido. ponytail: se a página tiver outro cupom que não o pedido, não empilha nem troca; revisar se ocorrer."""
    return "com cupom" in area_text.lower() or (shown is not None and price < shown - 0.5)


def final_verdict(price: float, informed: float, coupon_final: float | None, in_page: bool = False) -> tuple[float, bool]:
    """Vitrine vence o cupom: se o informado já bate com a vitrine (ou a página já mostra o cupom), o ML já exibe
    o preço com cupom (aplicar de novo daria desconto em dobro). Retorna (preço final, bateu_com_a_vitrine)."""
    matched = in_page or int(price) == int(informed)
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
                             os="macos", firefox_user_prefs=FIREFOX_PREFS,
                             fingerprint=stored_fingerprint(FINGERPRINT_FILE, pkg_version("camoufox"),
                                                            lambda: generate_fingerprint(os="macos"))) as ctx:
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


async def read_page():
    """Aba de leitura reaproveitada: uma janela por slot em vez de uma por /check. Recriada se foi fechada."""
    page = await read_pages.get()
    if page is None or page.is_closed():
        page = await light_page()
    return page


async def release_read_page(page):
    """Devolve a aba em about:blank (o JS do ML não segue rodando); se não der, descarta e o próximo slot cria outra."""
    ok = False
    try:
        await page.goto("about:blank")
        ok = True
    except BaseException:  # inclui cancelamento por timeout: o slot não pode se perder
        asyncio.ensure_future(page.close())
    finally:
        read_pages.put_nowait(page if ok else None)


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
    if key in ineligible_cache and fresh(ineligible_cache[key], time.time(), INELIGIBLE_TTL_S):
        return False
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
    (eligible_cache if ok else ineligible_cache)[key] = time.time()
    return ok


async def pace():
    """Garante MIN_GAP_S entre o início de duas leituras de produto."""
    global last_start
    async with gap_lock:  # só reserva o horário; dorme fora do lock (leituras em outros slots não ficam presas atrás dele)
        start = max(time.time(), last_start + MIN_GAP_S)
        last_start = start
    await asyncio.sleep(max(0.0, start - time.time()))


async def bounded_from_start(make, timeout: float):
    """O prazo só começa quando `make(started)` chama started.set() (já com slot e ritmo liberados): tempo de fila não conta.
    Se o chamador desistir (cancelado), a leitura é cancelada junto e o slot volta."""
    started = asyncio.Event()
    task = asyncio.ensure_future(make(started))
    waiter = asyncio.ensure_future(started.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if not task.done():
            await asyncio.wait({task}, timeout=timeout)
        if not task.done():
            task.cancel()
            raise asyncio.TimeoutError
        return task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        waiter.cancel()


async def fetch_store(url: str) -> dict:
    try:
        return await bounded_from_start(lambda started: read_store(url, started), REQUEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"status": "timeout"}


async def read_store(url: str, started: asyncio.Event) -> dict:
    """Abre o produto e lê preço + item_id da página; ou {"status": erro}."""
    if (cs := captcha_state["session"]) and cs["status"] == "pending":  # alguém está resolvendo: não enfileira
        return {"status": "captcha", "detail": "verificação em andamento"}
    await captcha_free.wait()
    page = await read_page()
    try:
        await pace()
        started.set()
        await page.goto(url, wait_until="domcontentloaded")
        if host_allowed(page.url) and is_social_page(page.url):  # vitrine do afiliado: segue o "Ir para produto"
            href = await page.eval_on_selector(SEL_SOCIAL_PRODUCT, "a => a.href") if await page.query_selector(SEL_SOCIAL_PRODUCT) else None
            if not href or not host_allowed(href):
                return {"status": "not_found", "detail": "vitrine do afiliado sem link de produto"}
            await page.goto(href, wait_until="domcontentloaded")
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
        price, shown, area_text, item_id, loyalty = parse_product_read(await page.evaluate(READ_PRODUCT_JS))
        if price is None:
            return {"status": "not_found"}
        in_page = coupon_in_page(area_text, price, shown)
        logging.getLogger("uvicorn.error").info("preço: meta/lido=%s visível=%s cupom_na_página=%s texto=%r",
                                                price, shown, in_page, area_text[:200])  # diagnóstico do "com Cupom"
        # leitura normal: o bloqueio passou. Captcha de OUTRA url não se apaga (o alerta sumia com o item ainda bloqueado)
        if captcha_state["reason"] != "captcha" or captcha_state["blocked_url"] == url \
                or time.time() - (captcha_state["since"] or 0) > 600:
            clear_blocked()
        return {"status": "ok", "price": price, "item_id": item_id, "loyalty": loyalty, "coupon_in_page": in_page}
    except Exception as e:  # um item com erro não derruba o batch
        return {"status": "error", "detail": str(e)}
    finally:
        await release_read_page(page)


async def get_store(url: str) -> tuple[dict, float | None]:
    """(leitura da loja, idade em segundos se veio do cache, senão None). Só leituras com sucesso são cacheadas."""
    url = norm_url(url)
    async with store_locks[url]:
        hit = store_cache.get(url)
        if hit and fresh(hit[0], time.time(), STORE_TTL_S):
            return hit[1], time.time() - hit[0]
        store = await fetch_store(url)
        if store["status"] == "ok":
            store_cache[url] = (time.time(), store)
        return store, None


async def check(item: Item) -> dict:
    store, age = await get_store(item.url)
    if store["status"] != "ok":
        return store
    price, with_coupon = store["price"], None
    out = {"status": "ok", "store_price": price, "price_matches": int(price) == int(item.price),
           "coupon_applied": False, "price_with_coupon": None, "cached": age is not None,
           "cache_age_s": None if age is None else round(age)}
    if item.coupon:
        try:
            c, msg = await bounded(get_coupon(item.coupon), REQUEST_TIMEOUT_S)
        except Exception as e:  # lista de cupons não carregou: inconclusivo, não "inválido"
            c, msg = None, f"erro ao consultar cupons: {e}"
        if c is None:
            out.update(status=coupon_failure(msg), detail=msg)
        else:
            out["coupon_eligible"] = await bounded(
                coupon_eligible(store["item_id"], price, store["loyalty"], c["code"].upper()), REQUEST_TIMEOUT_S)
            if out["coupon_eligible"] is False:
                out["status"] = "coupon_not_applicable"
            else:
                out["status"], final = discount(price, c)
                if out["status"] == "ok":
                    with_coupon = final
                    out.update(coupon_applied=True, price_with_coupon=final)
    in_page = store.get("coupon_in_page", False)
    if in_page and out["status"] == "ok":  # a vitrine já tem o cupom: não desconta de novo
        out.update(coupon_applied=False, price_with_coupon=price if item.coupon else None)
        with_coupon = None
    final, out["matched_showcase"] = final_verdict(price, item.price, with_coupon, in_page)
    out["comparison"] = compare(final, item.price)
    return out


async def run(item: Item) -> dict:
    """Prazo (REQUEST_TIMEOUT_S) vale por etapa de navegador, não inclui a espera na fila; a fila é limitada em admit()."""
    t0 = time.time()
    try:
        out = await check(item)
    except asyncio.TimeoutError:
        out = {"status": "timeout"}
    stats[out["status"]] += 1
    stats["cache_hit"] += bool(out.get("cached"))
    logging.getLogger("uvicorn.error").info("item status=%s total=%.1fs cache=%s", out["status"], time.time() - t0,
                                            out.get("cached", False))
    return out


@contextmanager
def admit(n: int):
    """Reserva n posições da fila; cheia = 503 com Retry-After (o chamador reenvia) em vez de esperar até estourar."""
    global pending
    if pending + n > MAX_QUEUE:
        stats["rejected_busy"] += n
        raise HTTPException(503, f"fila cheia ({pending}/{MAX_QUEUE})", headers={"Retry-After": str(min(300, int(MIN_GAP_S * max(1, pending) / SLOTS)))})
    pending += n
    try:
        yield
    finally:
        pending -= n


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
    if await logged_in_quick():  # evidência positiva por HTTP: dispensa renderizar a home (pesada)
        return True
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
    with admit(1):
        return await run(item)


@api.post("/check/batch")
async def check_batch(items: list[Item]):
    if len(items) > MAX_BATCH:
        raise HTTPException(413, f"máximo de {MAX_BATCH} itens por lote")
    with admit(len(items)):
        return await asyncio.gather(*(run(i) for i in items))


@api.get("/metrics")
async def metrics():
    return {"pending": pending, "max_queue": MAX_QUEUE, "slots": SLOTS, "store_cache": len(store_cache),
            "store_locks": len(store_locks), **stats}


@app.get("/health")  # sem token (healthcheck do Docker): abre/fecha uma aba de verdade
async def health():
    try:
        await bounded(probe_browser(), 10)
    except Exception as e:
        raise HTTPException(503, f"navegador não responde: {type(e).__name__}")
    return {"ok": True}


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
    ineligible_cache.clear()


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
