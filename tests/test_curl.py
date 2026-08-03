"""Tests de la extraccion de sesion desde un comando cURL."""

from __future__ import annotations

from fantasyhelper.adapters.mister.curl import (
    cookie_header,
    extract_cookies,
    update_env_token,
)

CURL_BASH = """curl 'https://mister.mundodeportivo.com/standings' \\
  -H 'accept: */*' \\
  -H 'cookie: PHPSESSID=abc123; user=42; _ga=GA1.2.999; OptanonConsent=xyz' \\
  -H 'x-requested-with: XMLHttpRequest' \\
  --compressed"""

CURL_CMD = (
    'curl "https://mister.mundodeportivo.com/team" ^\n'
    '  -H "accept: */*" ^\n'
    '  -H "Cookie: PHPSESSID=abc123; user=42" ^\n'
    '  -H "x-requested-with: XMLHttpRequest"'
)


def test_extrae_cookies_de_curl_bash():
    cookies = extract_cookies(CURL_BASH)
    assert cookies == {"PHPSESSID": "abc123", "user": "42"}


def test_extrae_cookies_de_curl_cmd():
    # La variante de Windows usa comillas dobles y ^ como continuacion de linea.
    cookies = extract_cookies(CURL_CMD)
    assert cookies == {"PHPSESSID": "abc123", "user": "42"}


def test_descarta_cookies_de_analitica():
    cookies = extract_cookies(CURL_BASH)
    assert "_ga" not in cookies
    assert "OptanonConsent" not in cookies


def test_forma_corta_con_b():
    cookies = extract_cookies("curl https://x -b 'PHPSESSID=zzz; foo=bar'")
    assert cookies == {"PHPSESSID": "zzz", "foo": "bar"}


def test_sin_cookies_devuelve_vacio():
    assert extract_cookies("curl https://x -H 'accept: */*'") == {}


def test_cookie_header_reconstruye_la_cabecera():
    assert cookie_header({"a": "1", "b": "2"}) == "a=1; b=2"


def test_update_env_crea_el_fichero(tmp_path):
    env = tmp_path / ".env"
    assert update_env_token(env, "a=1") is False
    assert env.read_text(encoding="utf-8").strip() == "MISTER_TOKEN=a=1"


def test_update_env_reemplaza_sin_tocar_el_resto(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "MISTER_EMAIL=yo@ejemplo.com\nMISTER_TOKEN=viejo\nFH_SEASON=2026-27\n",
        encoding="utf-8",
    )

    assert update_env_token(env, "nuevo=1") is True

    contenido = env.read_text(encoding="utf-8")
    assert "MISTER_TOKEN=nuevo=1" in contenido
    assert "MISTER_EMAIL=yo@ejemplo.com" in contenido
    assert "FH_SEASON=2026-27" in contenido
    assert "viejo" not in contenido


def test_update_env_anade_si_no_existe_la_clave(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FH_SEASON=2026-27\n", encoding="utf-8")

    assert update_env_token(env, "t=1") is False

    contenido = env.read_text(encoding="utf-8")
    assert "FH_SEASON=2026-27" in contenido
    assert "MISTER_TOKEN=t=1" in contenido
