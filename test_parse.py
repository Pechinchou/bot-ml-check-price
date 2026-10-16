import tempfile
from pathlib import Path

from app import coupon_failure, coupon_in_page, compare, discount, final_verdict, fresh, has_login_cookies, host_allowed, html_logged_in, is_login_url, login_step, parse_price, safe_static_path, session_valid

assert parse_price("R$ 1.299,90") == 1299.9
assert parse_price("199,00") == 199.0
assert parse_price("1.299") == 1299.0
assert compare(179.9, 199.9) == "menor"
assert compare(210, 199.9) == "maior"
assert compare(199.9, 199.9) == "igual"
assert compare(73.92, 73.00) == "igual"  # centavos ignorados
assert compare(72.99, 73.00) == "menor"
C = {"min_amount": 79, "cap_amount": 50, "discount_type": "PERCENT", "discount_value": 10,
     "expiration_date": "2099-01-01T00:00:00Z"}
assert discount(100, C) == ("ok", 90.0)
assert discount(1000, C) == ("ok", 950.0)  # teto R$50
assert discount(53.9, C) == ("coupon_min_not_met", 53.9)
assert discount(100, {**C, "expiration_date": "2020-01-01T00:00:00Z"})[0] == "coupon_expired"
assert fresh(0, 599, 600) and not fresh(0, 601, 600)
assert host_allowed("https://www.mercadolivre.com.br/p/MLB1") and host_allowed("https://meli.la/abc")
assert not host_allowed("http://169.254.169.254/") and not host_allowed("https://evil.com/mercadolivre.com.br")
assert not host_allowed("https://mercadolivre.com.br.evil.com/") and not host_allowed("file:///etc/passwd")
assert final_verdict(73.92, 73, 60.0) == (73.92, True)  # vitrine já com cupom: não aplica de novo
assert final_verdict(100, 50, 90.0) == (90.0, False)
assert final_verdict(100, 50, None) == (100, False)
assert final_verdict(2675.2, 2674, 2475.2, in_page=True) == (2675.2, True)  # cupom já na página, R$ 1 de diferença
assert coupon_in_page("R$ 2.675,20 no Pix com Cupom", 2675.2, 2675.2) and coupon_in_page("", 2675.2, 2875.2)
assert not coupon_in_page("R$ 100", 100, 100)
assert coupon_failure("Inserir Erro Confira se o cupom está correto") == "coupon_invalid"
assert coupon_failure("Este cupom já foi adicionado") == "coupon_unverified"
assert coupon_failure("O cupom esgotou. Há outros") == "coupon_sold_out"
assert coupon_failure("O cupom não está mais disponível.") == "coupon_expired"
with tempfile.TemporaryDirectory() as d:
    root = Path(d) / "novnc"; root.mkdir(); (root / "vnc.html").write_text("x"); (Path(d) / "segredo").write_text("s")
    assert safe_static_path(root, "vnc.html") and not safe_static_path(root, "../segredo")
    assert not safe_static_path(root, "nao-existe") and not safe_static_path(root, "/etc/passwd")
S = {"token": "abc", "status": "pending", "expires_at": 100}
assert session_valid(S, "abc", 99) and not session_valid(S, "abc", 101) and not session_valid(S, "xyz", 99)
assert not session_valid({**S, "status": "solved"}, "abc", 99) and not session_valid(None, "abc", 99)
assert is_login_url("https://www.mercadolivre.com.br/gz/account-verification?go=x") and is_login_url("https://www.mercadolivre.com/jms/mlb/lgz/login")
assert not is_login_url("https://www.mercadolivre.com.br/natura-homem/p/MLB45871745") and not is_login_url("https://www.mercadolivre.com.br/captcha/wall/x")
assert has_login_cookies([{"name": "ssid"}, {"name": "orguserid"}, {"name": "ftid"}])
assert not has_login_cookies([{"name": "ftid"}, {"name": "orguseridp"}, {"name": "_d2id"}])  # deslogado
assert html_logged_in('<a class="nav-header-user-myml">Leo</a>') and not html_logged_in('<a data-link-id="login">Entre</a>')
# nomes reais de uma conta logada (perfil de verdade) e de uma deslogada
assert has_login_cookies([{"name": n} for n in ("ftid", "dsid", "edsid", "nonce_sso", "orgnickp", "orguserid", "orguseridp", "ssid")])
# login_step: começa deslogado -> termina quando o menu aparece; com force começa logado -> precisa ver o logout antes
assert login_step(True, False) == (True, False) and login_step(True, True) == (True, True)
assert login_step(False, True) == (False, False)  # force: menu de antes do logout não conta
assert login_step(False, False) == (True, False) and login_step(*login_step(False, False)[:1], True) == (True, True)
print("ok")
