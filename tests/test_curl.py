"""Tests de la extraccion de sesion desde un comando cURL."""

from __future__ import annotations

from fantasyhelper.adapters.mister.curl import (
    extract_header,
    extract_session,
    unescape,
    update_env_var,
)

CURL_BASH = """curl 'https://mister.mundodeportivo.com/standings' \\
  -H 'accept: */*' \\
  -H 'cookie: PHPSESSID=abc123; authenticated=true; _ga=GA1.2.999; OptanonConsent=xyz' \\
  -H 'x-auth: 455c5cfd8cde21e6966f890f810a9d35' \\
  -H 'x-requested-with: XMLHttpRequest' \\
  --compressed"""

#: Forma que genera Chromium en Windows: ^ escapando casi todo y como
#: continuacion de linea. Es la que llega en la practica.
CURL_CMD = (
    'curl --url ^"https://mister.mundodeportivo.com/market^" ^\n'
    '  -X ^"POST^" ^\n'
    '  -b ^"g_state=^{^\\^"i_l^\\^":0^}; authenticated=true; '
    'PHPSESSID=5c027c933b164c8930ee5001acd11860; euconsent-v2=CPxurkA^" ^\n'
    '  -H ^"x-auth: 455c5cfd8cde21e6966f890f810a9d35^" ^\n'
    '  -H ^"x-requested-with: XMLHttpRequest^"'
)


def test_unescape_deshace_los_circunflejos_de_cmd():
    assert unescape('^"hola^"') == '"hola"'
    assert unescape("^{^}") == "{}"
    assert unescape("a ^\n  b") == "a b"


def test_extrae_sesion_de_curl_bash():
    sesion = extract_session(CURL_BASH)
    assert sesion.cookies == {"PHPSESSID": "abc123", "authenticated": "true"}
    assert sesion.headers["x-auth"] == "455c5cfd8cde21e6966f890f810a9d35"
    assert sesion.is_usable()


def test_extrae_sesion_de_curl_cmd_windows():
    sesion = extract_session(CURL_CMD)
    assert sesion.cookies["PHPSESSID"] == "5c027c933b164c8930ee5001acd11860"
    assert sesion.cookies["authenticated"] == "true"
    assert sesion.headers["x-auth"] == "455c5cfd8cde21e6966f890f810a9d35"


def test_descarta_cookies_de_analitica_y_consentimiento():
    cookies = extract_session(CURL_BASH).cookies
    assert "_ga" not in cookies
    assert "OptanonConsent" not in cookies

    cookies = extract_session(CURL_CMD).cookies
    assert "euconsent-v2" not in cookies
    # g_state es de Google Sign-In y su valor JSON lleva llaves y comillas:
    # ni autentica ni conviene arrastrarlo.
    assert "g_state" not in cookies


def test_cookie_header_reconstruye_la_cabecera():
    sesion = extract_session(CURL_BASH)
    assert sesion.cookie_header == "PHPSESSID=abc123; authenticated=true"


def test_sin_cookies_no_es_usable():
    sesion = extract_session("curl https://x -H 'accept: */*'")
    assert not sesion.is_usable()


def test_extract_header_es_insensible_a_mayusculas():
    assert extract_header(CURL_BASH, "X-Auth") == "455c5cfd8cde21e6966f890f810a9d35"
    assert extract_header(CURL_BASH, "no-existe") is None


def test_update_env_crea_el_fichero(tmp_path):
    env = tmp_path / ".env"
    assert update_env_var(env, "MISTER_TOKEN", "a=1") is False
    assert env.read_text(encoding="utf-8").strip() == "MISTER_TOKEN=a=1"


def test_update_env_reemplaza_sin_tocar_el_resto(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "MISTER_XAUTH=hash\nMISTER_TOKEN=viejo\nFH_SEASON=2026-27\n", encoding="utf-8"
    )

    assert update_env_var(env, "MISTER_TOKEN", "nuevo=1") is True

    contenido = env.read_text(encoding="utf-8")
    assert "MISTER_TOKEN=nuevo=1" in contenido
    assert "MISTER_XAUTH=hash" in contenido
    assert "FH_SEASON=2026-27" in contenido
    assert "viejo" not in contenido


def test_update_env_anade_si_no_existe_la_clave(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FH_SEASON=2026-27\n", encoding="utf-8")

    assert update_env_var(env, "MISTER_XAUTH", "hash") is False

    contenido = env.read_text(encoding="utf-8")
    assert "FH_SEASON=2026-27" in contenido
    assert "MISTER_XAUTH=hash" in contenido
