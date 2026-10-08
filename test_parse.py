import tempfile
from pathlib import Path

from app import coupon_failure, compare, discount, final_verdict, fresh, host_allowed, parse_price, safe_static_path, session_valid

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
print("ok")
