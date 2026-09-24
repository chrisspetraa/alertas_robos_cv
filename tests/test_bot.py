# -*- coding: utf-8 -*-
"""Pruebas del bot. Ejecutar con:  python -m pytest -q   (desde la carpeta del proyecto)"""
import copy
import email.utils
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402

UTC = timezone.utc
AHORA = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
CONFIG = bot.cargar_config(Path(__file__).resolve().parent.parent / "config.yaml")


@pytest.fixture
def cfg():
    return copy.deepcopy(CONFIG)


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch):
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def noticia(titulo, resumen="", ambito="regional", prov=None, agregador=False, hace_horas=1, url=None, fuente="Medio", origen="Fuente"):
    return bot.Noticia(
        titulo=titulo, resumen=resumen, url=url or f"https://ejemplo.es/{abs(hash(titulo))}",
        fuente=fuente, publicada=AHORA - timedelta(hours=hace_horas), ambito=ambito, agregador=agregador,
        provincia_defecto=prov, origen=origen,
    )


def clasificar(cfg, noticias, estado=None):
    cats = bot.cargar_categorias(cfg)
    return bot.filtrar_noticias(
        noticias, cats, bot.Geografia(cfg), bot.compilar_terminos(cfg["exclusiones"]),
        estado or bot.estado_vacio(), AHORA, cfg["ajustes"], cfg["ajustes"]["max_edad_horas"],
    )


# ---------------------------------------------------------------- texto
def test_norm_quita_tildes_y_signos():
    assert bot.norm("  Xàbia, L'Eliana: ¡Ñandú!  ") == "xabia l eliana nandu"


def test_comodines_y_palabra_exacta():
    pat = bot.compilar_terminos(["asalt*", "robo"])
    assert pat.search(bot.norm("Asaltaron una casa"))
    assert pat.search(bot.norm("un robo"))
    assert not pat.search(bot.norm("un robot"))  # 'robo' solo casa como palabra completa


def test_normalizar_url_quita_rastreo():
    a = bot.normalizar_url("https://Ejemplo.es/noticia/?utm_source=x&id=3#frag")
    b = bot.normalizar_url("https://ejemplo.es/noticia?id=3")
    assert a == b


def test_hace_cuanto():
    assert bot.hace_cuanto(timedelta(seconds=30)) == "ahora mismo"
    assert bot.hace_cuanto(timedelta(minutes=25)) == "hace 25 min"
    assert bot.hace_cuanto(timedelta(hours=3)) == "hace 3 h"
    assert bot.hace_cuanto(timedelta(hours=49)) == "hace 2 días"
    assert bot.hace_cuanto(timedelta(seconds=-500)) == "ahora mismo"


# ---------------------------------------------------------------- clasificación
@pytest.mark.parametrize("titulo,resumen,lugar", [
    ("Detenidos dos hombres por robar en chalets de Torrevieja", "", "Alicante · Torrevieja"),
    ("Misterio en un pueblo de Castellón: varias vecinas denuncian robos",
     "Tres vecinas de Xert comenzaron a detectar que las joyas guardadas en sus viviendas estaban desapareciendo", "Castellón · Xert"),
    ("Dos detenidos por 14 robos en viviendas de Alicante, Elche, Elda y Villena", "", "Alicante · Elche"),
    ("Cae una banda que asaltaba domicilios en Godella y Rocafort", "", "Valencia · Godella"),
    ("Saqueos en viviendas durante la evacuación por el incendio de Castellón", "", "Castellón"),
])
def test_detecta_robos_en_viviendas(cfg, titulo, resumen, lugar):
    res = clasificar(cfg, [noticia(titulo, resumen)])
    assert len(res) == 1
    assert res[0].categoria.clave == "viviendas"
    assert res[0].lugar.texto() == lugar


def test_acepta_noticia_que_nombra_alicante_y_murcia(cfg):
    t = "Detienen a un hombre que fingía interés en viviendas en Alicante y Murcia para robar dinero y joyas"
    assert len(clasificar(cfg, [noticia(t)])) == 1


@pytest.mark.parametrize("titulo,resumen", [
    ("Detienen a tres carteristas que robaban a turistas en Valencia", ""),          # sin vivienda
    ("Robo de identidad: así usan tus datos para comprar en tu casa", ""),          # exclusión
    ("El Villarreal denuncia un robo arbitral en su propia casa", ""),              # deporte
    ("Atracan una farmacia de Elche", ""),                                          # comercios desactivado
    ("El precio de la vivienda sube en Valencia", ""),                              # sin delito
    ("Nuevo robot aspirador para tu casa en Alicante", ""),                         # 'robot' no es 'robo'
])
def test_descarta_lo_que_no_es(cfg, titulo, resumen):
    assert clasificar(cfg, [noticia(titulo, resumen)]) == []


def test_categoria_comercios_se_activa_desde_config(cfg):
    n = noticia("Roban en un bar de Elche durante la noche")
    assert clasificar(cfg, [n]) == []
    cfg["categorias"]["comercios"]["activa"] = True
    res = clasificar(cfg, [n])
    assert len(res) == 1 and res[0].categoria.clave == "comercios"


def test_no_hay_categorias_activas(cfg):
    for c in cfg["categorias"].values():
        c["activa"] = False
    with pytest.raises(bot.ConfigError):
        bot.cargar_categorias(cfg)


# ---------------------------------------------------------------- geografía
def test_fuente_regional_sin_lugar_se_acepta_como_comunitat(cfg):
    res = clasificar(cfg, [noticia("Detenido un hombre por robar en varios chalets")])
    assert len(res) == 1 and res[0].lugar.texto() == "Comunitat Valenciana"


def test_fuente_regional_con_provincia_por_defecto(cfg):
    res = clasificar(cfg, [noticia("Detenido un hombre por robar en varios chalets", prov="Alicante")])
    assert res[0].lugar.texto("Alicante") == "Alicante"


def test_fuente_regional_descarta_otras_zonas(cfg):
    assert clasificar(cfg, [noticia("Roban en un chalet de Marbella")]) == []


def test_fuente_nacional_exige_lugar_de_la_comunitat(cfg):
    t = "Detenido un hombre por robar en varios chalets"
    assert clasificar(cfg, [noticia(t, ambito="nacional")]) == []
    assert len(clasificar(cfg, [noticia(t, ambito="nacional", prov="Valencia")])) == 1  # consulta por provincia
    assert len(clasificar(cfg, [noticia(t + " de Benidorm", ambito="nacional")])) == 1


def test_nacional_con_pista_de_provincia_sigue_descartando_otras_zonas(cfg):
    assert clasificar(cfg, [noticia("Roban en una vivienda de Madrid", ambito="nacional", prov="Valencia")]) == []


def test_falsos_lugares(cfg):
    assert clasificar(cfg, [noticia("Roban en una vivienda de Valencia de Alcántara", ambito="nacional")]) == []
    assert clasificar(cfg, [noticia("Roban en una vivienda de Elche de la Sierra", ambito="nacional")]) == []


def test_alias_valencianos(cfg):
    geo = bot.Geografia(cfg)
    assert geo.localizar(bot.norm("Robos en chalets de Xàbia")).texto() == "Alicante · Xàbia"
    assert geo.localizar(bot.norm("Robos en chalets de Jávea")).texto() == "Alicante · Xàbia"
    assert geo.localizar(bot.norm("Robos en Orihuela Costa")).texto() == "Alicante · Orihuela Costa"
    assert geo.localizar(bot.norm("Robos en la Comunidad Valenciana")).texto() == "Comunitat Valenciana"
    assert not geo.localizar(bot.norm("Robos en Zaragoza")).en_cv


# ---------------------------------------------------------------- duplicados y antigüedad
def test_misma_noticia_en_dos_medios_se_envia_una_vez(cfg):
    a = noticia("Detenidos dos hombres por robar en chalets de Torrevieja", fuente="Levante", url="https://a.es/1")
    b = noticia("Dos hombres detenidos por robar en chalets de Torrevieja", fuente="Las Provincias", url="https://b.es/2")
    assert len(clasificar(cfg, [a, b])) == 1


def test_noticias_distintas_se_envian_ambas(cfg):
    a = noticia("Detenidos dos hombres por robar en chalets de Torrevieja")
    b = noticia("Roban en un chalet de Benidorm mientras sus dueños estaban de vacaciones")
    assert len(clasificar(cfg, [a, b])) == 2


def test_se_prefiere_el_enlace_del_medio_frente_al_agregador(cfg):
    ag = noticia("Detenidos dos hombres por robar en chalets de Torrevieja", agregador=True, ambito="nacional",
                 prov="Alicante", url="https://news.google.com/rss/articles/xyz", hace_horas=3)
    di = noticia("Detenidos dos hombres por robar en chalets de Torrevieja", url="https://levante-emv.com/a", hace_horas=1)
    res = clasificar(cfg, [ag, di])
    assert [a.noticia.url for a in res] == ["https://levante-emv.com/a"]


def test_no_repite_lo_ya_enviado_ni_lo_parecido(cfg):
    n = noticia("Detenidos dos hombres por robar en chalets de Torrevieja")
    est = bot.estado_vacio()
    est["enviadas"][n.id] = {"t": AHORA.isoformat(), "tok": sorted(bot.tokens_titulo(n.titulo))}
    assert clasificar(cfg, [n], est) == []
    otra = noticia("Dos hombres detenidos por robar en chalets de Torrevieja", url="https://otro.es/x")
    assert clasificar(cfg, [otra], est) == []


def test_ignora_noticias_antiguas(cfg):
    assert clasificar(cfg, [noticia("Roban en un chalet de Benidorm", hace_horas=48)]) == []
    assert len(clasificar(cfg, [noticia("Roban en un chalet de Benidorm", hace_horas=30)])) == 1


def test_poda_de_estado():
    est = bot.estado_vacio()
    est["enviadas"]["viejo"] = {"t": (AHORA - timedelta(days=30)).isoformat()}
    est["enviadas"]["nuevo"] = {"t": (AHORA - timedelta(days=1)).isoformat()}
    bot.podar_estado(est, AHORA, 10)
    assert list(est["enviadas"]) == ["nuevo"]


# ---------------------------------------------------------------- mensajes
def test_mensaje_escapa_html_y_lleva_enlace(cfg):
    n = noticia("Roban en un chalet de <Benidorm> & Altea", resumen="Resumen con <b>etiquetas</b> & más. " * 20,
                url="https://ejemplo.es/a?x=1&y=2", fuente="Diario <X>")
    a = clasificar(cfg, [n])[0]
    msg = bot.formatear_mensaje(a, AHORA)
    assert "&lt;Benidorm&gt; &amp; Altea" in msg
    assert "<script" not in msg and "<b>etiquetas</b>" not in msg
    assert 'href="https://ejemplo.es/a?x=1&amp;y=2"' in msg
    assert "hace 1 h" in msg and "Diario &lt;X&gt;" in msg
    assert msg.startswith("🏠 <b>Robo en vivienda</b> · Alicante · Benidorm")
    assert len(msg) < 1200


def test_mensaje_no_repite_el_titulo_como_resumen(cfg):
    t = "Roban en un chalet de Benidorm mientras sus dueños dormían"
    a = clasificar(cfg, [noticia(t, resumen=t + ". Más texto.")])[0]
    assert bot.formatear_mensaje(a, AHORA).count("Roban en un chalet") == 1


# ---------------------------------------------------------------- feeds y descubrimiento
def rss(items):
    partes = []
    for titulo, url, desc, fecha in items:
        partes.append(f"<item><title>{escape(titulo)}</title><link>{url}</link><description>{escape(desc)}</description>"
                      f"<pubDate>{email.utils.format_datetime(fecha)}</pubDate></item>")
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>' + "".join(partes) + "</channel></rss>").encode()


PAGINA_HTML = """<html><head>
<link rel="alternate" type="application/rss+xml" href="/rss/portada.xml">
<link rel="alternate" type="application/rss+xml" href="/rss/sucesos.xml">
<link rel="stylesheet" href="/x.css"></head><body><a href="/feed/otros.xml">rss</a> <a href="/contacto">c</a></body></html>"""


def test_extraer_candidatos_rss_prioriza_sucesos():
    c = bot.extraer_candidatos_rss(PAGINA_HTML, "https://medio.es/sucesos/")
    assert c[0] == "https://medio.es/rss/sucesos.xml"
    assert set(c) == {"https://medio.es/rss/sucesos.xml", "https://medio.es/rss/portada.xml", "https://medio.es/feed/otros.xml"}


def test_expandir_feed(cfg):
    url, prov = bot.expandir_feed({"google_news": 'robo "chalet" Valencia when:1d', "provincia": "Valencia"})
    assert url.startswith("https://news.google.com/rss/search?q=robo+%22chalet%22+Valencia+when%3A1d&hl=es")
    assert prov == "Valencia"
    assert bot.expandir_feed("https://a.es/rss") == ("https://a.es/rss", None)
    assert bot.expandir_feed({"url": "https://a.es/rss", "provincia": "Alicante"}) == ("https://a.es/rss", "Alicante")
    assert "bing.com/news/search" in bot.expandir_feed({"bing_news": "robo chalet"})[0]
    with pytest.raises(bot.ConfigError):
        bot.expandir_feed({"otra": 1})


class Envios(bot.Notificador):
    def __init__(self):
        self.textos = []

    def enviar(self, texto):
        self.textos.append(texto)
        return True


@pytest.fixture
def mundo(monkeypatch, cfg):
    """Internet simulado: un diccionario url -> bytes."""
    web = {}

    def falso_descargar(sesion, url, timeout=20, reintentos=2):
        if url not in web:
            raise bot.FeedError("HTTP 404")
        return web[url]

    monkeypatch.setattr(bot, "descargar", falso_descargar)
    cfg["fuentes"] = [
        {"nombre": "Diario A", "feeds": ["https://a.es/rss"]},
        {"nombre": "Diario roto", "feeds": ["https://roto.es/rss"]},
        {"nombre": "Diario B", "feeds": [], "descubrir": ["https://b.es/sucesos/"]},
    ]
    web["https://a.es/rss"] = rss([
        ("Detenidos dos hombres por robar en chalets de Torrevieja", "https://a.es/1", "La Guardia Civil detuvo a dos hombres.", AHORA - timedelta(hours=2)),
        ("Accidente en la A-7 a la altura de Benidorm", "https://a.es/2", "Sin heridos graves.", AHORA - timedelta(hours=1)),
        ("Roban en una vivienda de Godella y se llevan las joyas", "https://a.es/3", "", AHORA - timedelta(minutes=20)),
    ])
    web["https://b.es/sucesos/"] = PAGINA_HTML.encode()
    web["https://b.es/rss/sucesos.xml"] = rss([
        ("Asaltan un chalet en Xàbia con los dueños dentro", "https://b.es/9", "Ocurrió de madrugada.", AHORA - timedelta(minutes=45)),
    ])
    return web, cfg


def test_ejecucion_completa(mundo, tmp_path):
    web, cfg = mundo
    ruta = tmp_path / "state" / "state.json"
    envios = Envios()

    # 1ª ejecución: bienvenida + como mucho 'primera_ejecucion_max' avisos
    cfg["ajustes"]["primera_ejecucion_max"] = 2
    codigo = bot.ejecutar(cfg, ruta, ahora=AHORA, sesion=object(), notificador=envios)
    assert codigo == 0
    assert "Bot de alertas activo" in envios.textos[0] and "Vigilo 2 fuentes" in envios.textos[0]
    avisos = envios.textos[1:]
    assert len(avisos) == 2
    assert "Godella" in avisos[-1] or "Godella" in avisos[0]
    estado = json.loads(ruta.read_text(encoding="utf-8"))
    assert estado["creado"] and len(estado["enviadas"]) == 3          # 2 enviadas + 1 silenciada
    assert sum(1 for v in estado["enviadas"].values() if v.get("silenciada")) == 1
    assert "Diario B" in estado["descubiertos"]

    # 2ª ejecución sin novedades: no se repite nada
    envios.textos.clear()
    assert bot.ejecutar(cfg, ruta, ahora=AHORA + timedelta(minutes=10), sesion=object(), notificador=envios) == 0
    assert envios.textos == []

    # Llega una noticia nueva: solo se envía esa
    web["https://a.es/rss"] = rss([
        ("Roban en una casa de campo de Calpe mientras la familia estaba en la playa", "https://a.es/4", "", AHORA + timedelta(minutes=5)),
        ("Roban en una vivienda de Godella y se llevan las joyas", "https://a.es/3", "", AHORA - timedelta(minutes=20)),
    ])
    assert bot.ejecutar(cfg, ruta, ahora=AHORA + timedelta(minutes=20), sesion=object(), notificador=envios) == 0
    assert len(envios.textos) == 1 and "Calpe" in envios.textos[0]


def test_descubrimiento_se_recuerda_en_cache(mundo, tmp_path, monkeypatch):
    web, cfg = mundo
    llamadas = []
    original = bot.descargar
    monkeypatch.setattr(bot, "descargar", lambda s, u, timeout=20, reintentos=2: (llamadas.append(u), original(s, u, timeout, reintentos))[1])
    ruta = tmp_path / "s.json"
    bot.ejecutar(cfg, ruta, ahora=AHORA, sesion=object(), notificador=Envios())
    assert llamadas.count("https://b.es/sucesos/") == 1
    llamadas.clear()
    bot.ejecutar(cfg, ruta, ahora=AHORA + timedelta(hours=1), sesion=object(), notificador=Envios())
    assert "https://b.es/sucesos/" not in llamadas           # usa los feeds recordados
    assert "https://b.es/rss/sucesos.xml" in llamadas
    llamadas.clear()
    bot.ejecutar(cfg, ruta, ahora=AHORA + timedelta(hours=25), sesion=object(), notificador=Envios())
    assert "https://b.es/sucesos/" in llamadas               # a las 24 h vuelve a buscar


def test_descubrimiento_fallido_no_se_repite_cada_ejecucion(mundo, tmp_path, monkeypatch):
    web, cfg = mundo
    del web["https://b.es/sucesos/"]
    llamadas = []
    original = bot.descargar
    monkeypatch.setattr(bot, "descargar", lambda s, u, timeout=20, reintentos=2: (llamadas.append(u), original(s, u, timeout, reintentos))[1])
    ruta = tmp_path / "s.json"
    bot.ejecutar(cfg, ruta, ahora=AHORA, sesion=object(), notificador=Envios())
    assert llamadas.count("https://b.es/sucesos/") == 1
    llamadas.clear()
    bot.ejecutar(cfg, ruta, ahora=AHORA + timedelta(hours=1), sesion=object(), notificador=Envios())
    assert "https://b.es/sucesos/" not in llamadas


def test_dry_run_no_envia_ni_guarda(mundo, tmp_path, capsys):
    web, cfg = mundo
    ruta = tmp_path / "s.json"
    assert bot.ejecutar(cfg, ruta, dry_run=True, ahora=AHORA, sesion=object()) == 0
    assert not ruta.exists()
    salida = capsys.readouterr().out
    assert "Robo en vivienda" in salida and "Resumen de la ejecución" in salida and "SIN FEED" in salida


def test_si_no_responde_ninguna_fuente_devuelve_error(mundo, tmp_path):
    web, cfg = mundo
    web.clear()
    assert bot.ejecutar(cfg, tmp_path / "s.json", ahora=AHORA, sesion=object(), notificador=Envios()) == 2


def test_fecha_del_futuro_se_ajusta(mundo, tmp_path):
    web, cfg = mundo
    web["https://a.es/rss"] = rss([("Roban en una vivienda de Godella", "https://a.es/f", "", AHORA + timedelta(hours=10))])
    envios = Envios()
    bot.ejecutar(cfg, tmp_path / "s.json", ahora=AHORA, sesion=object(), notificador=envios)
    assert any("ahora mismo" in t for t in envios.textos)


def test_agregador_usa_medio_original_y_quita_sufijo(cfg):
    xml = ('<?xml version="1.0"?><rss version="2.0"><channel><title>g</title><item>'
           "<title>Detenidos por robar en chalets de Benidorm - Levante-EMV</title><link>https://news.google.com/rss/articles/abc</link>"
           '<pubDate>Sat, 19 Sep 2026 10:00:00 GMT</pubDate><source url="https://levante-emv.com">Levante-EMV</source>'
           "<description>&lt;a&gt;otro titular sobre robo&lt;/a&gt;</description></item></channel></rss>").encode()
    import feedparser
    e = feedparser.parse(xml).entries[0]
    n = bot.entrada_a_noticia(e, {"nombre": "Google Noticias", "tipo": "agregador", "ambito": "nacional"}, "Alicante", AHORA)
    assert n.titulo == "Detenidos por robar en chalets de Benidorm" and n.fuente == "Levante-EMV"
    assert n.resumen == "" and n.origen == "Google Noticias" and n.agregador


# ---------------------------------------------------------------- Telegram
class Respuesta:
    def __init__(self, codigo, datos=None):
        self.status_code, self._datos = codigo, datos or {}

    def json(self):
        return self._datos


class SesionFalsa:
    def __init__(self, respuestas):
        self.respuestas, self.peticiones = list(respuestas), []

    def post(self, url, json=None, timeout=None):
        self.peticiones.append((url, json))
        r = self.respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_telegram_envia_a_varios_chats():
    s = SesionFalsa([Respuesta(200), Respuesta(200)])
    t = bot.TelegramNotificador("TOKEN123", ["111", "-222"], s)
    assert t.enviar("hola") and t.errores == 0
    assert [p[1]["chat_id"] for p in s.peticiones] == ["111", "-222"]
    assert s.peticiones[0][1]["parse_mode"] == "HTML" and s.peticiones[0][1]["link_preview_options"] == {"is_disabled": True}
    assert s.peticiones[0][0] == "https://api.telegram.org/botTOKEN123/sendMessage"


def test_telegram_reintenta_ante_429():
    s = SesionFalsa([Respuesta(429, {"parameters": {"retry_after": 2}}), Respuesta(200)])
    t = bot.TelegramNotificador("TOKEN123", ["111"], s)
    assert t.enviar("hola") and len(s.peticiones) == 2


def test_telegram_error_no_filtra_el_token(caplog):
    s = SesionFalsa([Respuesta(401, {"description": "Unauthorized TOKEN123"}), Exception("no debe llegar")])
    t = bot.TelegramNotificador("TOKEN123", ["111"], s)
    assert t.enviar("hola") is False and t.errores == 1
    assert "TOKEN123" not in caplog.text and "401" in caplog.text


def test_telegram_fallo_de_red_no_filtra_el_token(caplog):
    import requests
    s = SesionFalsa([requests.ConnectionError("fallo en https://api.telegram.org/botTOKEN123/sendMessage")])
    t = bot.TelegramNotificador("TOKEN123", ["111"], s)
    assert t.enviar("hola") is False
    assert "TOKEN123" not in caplog.text and "ConnectionError" in caplog.text


def test_ejecucion_devuelve_1_si_telegram_falla(mundo, tmp_path):
    web, cfg = mundo
    s = SesionFalsa([Respuesta(400, {"description": "chat not found"})] * 10)
    t = bot.TelegramNotificador("TOKEN123", ["111"], s)
    ruta = tmp_path / "s.json"
    assert bot.ejecutar(cfg, ruta, ahora=AHORA, sesion=object(), notificador=t) == 1
    assert json.loads(ruta.read_text())["enviadas"] == {}   # nada se marca como enviado si Telegram falló


def test_faltan_variables_de_entorno(cfg, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(bot.ConfigError):
        bot.notificador_desde_entorno(None, cfg)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " 1 , -2 ")
    assert bot.notificador_desde_entorno(None, cfg).chat_ids == ["1", "-2"]


def test_config_real_es_valida_y_las_fuentes_tienen_nombre(cfg):
    nombres = [f["nombre"] for f in cfg["fuentes"]]
    assert len(nombres) == len(set(nombres))
    for f in cfg["fuentes"]:
        assert f.get("feeds") or f.get("descubrir"), f["nombre"]
        for item in f.get("feeds") or []:
            bot.expandir_feed(item)
