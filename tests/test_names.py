from fantasyhelper.utils.names import match_key, slugify


def test_slugify_quita_acentos_y_puntuacion():
    assert slugify("César Tárrega") == "cesar-tarrega"
    assert slugify("Vinícius Jr.") == "vinicius-jr"
    assert slugify("N'Golo Kanté") == "n-golo-kante"


def test_match_key_ignora_sufijos():
    # El mismo jugador escrito distinto en dos fuentes debe dar la misma clave.
    assert match_key("Vinícius Jr.") == match_key("Vinicius Junior")
    assert match_key("Rodrigo Sr") == match_key("Rodrigo")


def test_match_key_no_colisiona_entre_jugadores_distintos():
    assert match_key("Javi Guerra") != match_key("Javi Galan")
