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
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, field_validator
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
    async with AsyncCamoufox(headless=HEADLESS, persistent_context=True, user_data_dir="profile", locale="pt-BR") as ctx:
        browser_ctx = ctx
        yield


app = FastAPI(lifespan=lifespan, dependencies=[Depends(require_token)])


async def has_captcha(page) -> bool:
    return "captcha" in page.url or any(p in page.url for p in BLOCK_PATHS) \
        or await page.query_selector(SEL_CAPTCHA) is not None


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
            if await has_captcha(page) and (HEADLESS or not await wait_captcha(page)):
                return {"status": "captcha"}  # headless: ninguém para resolver, não espera
            price, html = await read_price(page), await page.content()
            if price is None:
                return {"status": "not_found"}
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


@app.get("/session")
async def session():
    page = await browser_ctx.new_page()
    try:
        await page.goto("https://www.mercadolivre.com.br", wait_until="domcontentloaded")
        return {"logged_in": await page.query_selector(SEL_LOGGED_IN) is not None}
    finally:
        await page.close()


@app.post("/check")
async def check_one(item: Item):
    return await run(item)


@app.post("/check/batch")
async def check_batch(items: list[Item]):
    return await asyncio.gather(*(run(i) for i in items))
