"""Modo login: abre o Camoufox visível (na tela virtual do container, acessível via noVNC) e espera
você entrar no Mercado Livre. A sessão fica salva em profile/."""
import asyncio
import os

from camoufox.async_api import AsyncCamoufox

from app import SEL_LOGGED_IN

LOGIN_TIMEOUT_S = 900
VERIFY_URL = os.getenv("VERIFY_URL", "")  # produto p/ abrir depois do login e resolver o captcha da verificação


async def solve_captcha(page):
    """Abre o produto e espera sair da parede de captcha (você resolve na tela do noVNC)."""
    await page.goto(VERIFY_URL, wait_until="domcontentloaded")
    print("Resolva o captcha/verificação na tela do noVNC...", flush=True)
    for _ in range(LOGIN_TIMEOUT_S // 3):
        if "captcha" not in page.url and await page.query_selector('meta[itemprop="price"]'):
            print("VERIFICAÇÃO OK: produto abriu sem captcha", flush=True)
            return
        await asyncio.sleep(3)
    raise SystemExit("tempo esgotado sem passar pela verificação")


async def main():
    async with AsyncCamoufox(headless=False, persistent_context=True, user_data_dir="profile", locale="pt-BR") as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.mercadolivre.com.br/", wait_until="domcontentloaded")  # /login não existe: clique em "Entre" na home
        print("Aguardando login na tela virtual (noVNC)...", flush=True)
        for _ in range(LOGIN_TIMEOUT_S // 3):
            if await page.query_selector(SEL_LOGGED_IN):
                print("LOGIN OK", flush=True)
                if VERIFY_URL:
                    await solve_captcha(page)
                await page.wait_for_timeout(5000)  # deixa os cookies serem gravados no perfil
                print("SESSÃO SALVA em profile/", flush=True)
                return
            await asyncio.sleep(3)
        raise SystemExit("tempo esgotado sem login")


asyncio.run(main())
