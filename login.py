"""Modo login: abre o Camoufox visível (na tela virtual do container, acessível via noVNC) e espera
você entrar no Mercado Livre. A sessão fica salva em profile/."""
import asyncio

from camoufox.async_api import AsyncCamoufox

from app import SEL_LOGGED_IN

LOGIN_TIMEOUT_S = 900


async def main():
    async with AsyncCamoufox(headless=False, persistent_context=True, user_data_dir="profile", locale="pt-BR") as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.mercadolivre.com.br/", wait_until="domcontentloaded")  # /login não existe: clique em "Entre" na home
        print("Aguardando login na tela virtual (noVNC)...", flush=True)
        for _ in range(LOGIN_TIMEOUT_S // 3):
            if await page.query_selector(SEL_LOGGED_IN):
                await page.wait_for_timeout(5000)  # deixa os cookies serem gravados no perfil
                print("LOGIN OK: sessão salva em profile/", flush=True)
                return
            await asyncio.sleep(3)
        raise SystemExit("tempo esgotado sem login")


asyncio.run(main())
