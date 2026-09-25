#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alertas de robos en viviendas - Comunitat Valenciana
====================================================

Lee los feeds RSS de periódicos y buscadores de noticias, detecta noticias de
robos (por defecto, en viviendas y chalets) y las envía a Telegram.

Uso:
    python bot.py run                 # ejecución normal (envía a Telegram)
    python bot.py run --dry-run       # simulacro: no envía ni guarda nada
    python bot.py test-telegram       # manda un mensaje de prueba

Variables de entorno necesarias para enviar:
    TELEGRAM_BOT_TOKEN   token que te da @BotFather
    TELEGRAM_CHAT_ID     id del chat o grupo (varios, separados por comas)
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
import yaml

log = logging.getLogger("alertas")
BASE_DIR = Path(__file__).resolve().parent
UTC = timezone.utc
CV_GENERICA = "Comunitat Valenciana"
try:
    from zoneinfo import ZoneInfo
    HORA_LOCAL = ZoneInfo("Europe/Madrid")
except Exception:  # sin base de datos de zonas horarias: usamos UTC
    HORA_LOCAL = UTC


class ConfigError(Exception):
    pass


class FeedError(Exception):
    pass


class TelegramError(Exception):
    pass


# ----------------------------------------------------------------------
#  Utilidades de texto
# ----------------------------------------------------------------------
def norm(texto: str) -> str:
    """Minúsculas, sin tildes, solo letras/números separados por un espacio."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def limpiar_html(texto: str) -> str:
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    return re.sub(r"\s+", " ", html.unescape(texto)).strip()


def recortar(texto: str, maximo: int) -> str:
    texto = texto.strip()
    if len(texto) <= maximo:
        return texto
    corte = texto[:maximo].rsplit(" ", 1)[0].rstrip(" ,;:.-")
    return corte + "…"


def compilar_terminos(terminos) -> Optional[re.Pattern]:
    """'asalt*' -> prefijo; 'robo' -> palabra exacta. Todo sobre texto normalizado."""
    partes = []
    for t in terminos or []:
        t = str(t)
        comodin = t.endswith("*")
        base = norm(t.rstrip("*"))
        if not base:
            continue
        partes.append(r"\b" + re.escape(base) + (r"\w*" if comodin else r"\b"))
    return re.compile("|".join(partes)) if partes else None


STOP = set(
    "para como tras este esta estos estas entre sobre desde hasta donde cuando porque tambien segun "
    "otra otro otros otras cada todo todos toda todas pero sino aunque haya tiene tienen ante hace "
    "hacen habia sido esto eso mas muy".split()
)


def tokens_titulo(titulo: str) -> set[str]:
    return {w for w in norm(titulo).split() if len(w) > 3 and w not in STOP}


def titulos_similares(a: set[str], b: set[str], umbral: float) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    if inter == 0:
        return False
    if inter / len(a | b) >= umbral:
        return True
    return inter >= 4 and inter / min(len(a), len(b)) >= 0.8


_PARAMS_RUIDO = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|cmp$|ns_)")


def normalizar_url(url: str) -> str:
    p = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _PARAMS_RUIDO.match(k.lower())]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", "", urlencode(query), ""))


def id_url(url: str) -> str:
    return hashlib.sha1(normalizar_url(url).encode("utf-8")).hexdigest()[:16]


def hace_cuanto(delta: timedelta) -> str:
    s = max(int(delta.total_seconds()), 0)
    if s < 90:
        return "ahora mismo"
    m = s // 60
    if m < 60:
        return f"hace {m} min"
    h = m // 60
    if h < 24:
        return f"hace {h} h"
    d = h // 24
    return f"hace {d} día" + ("s" if d != 1 else "")


# ----------------------------------------------------------------------
#  Categorías y geografía
# ----------------------------------------------------------------------
@dataclass
class Categoria:
    clave: str
    etiqueta: str
    icono: str
    delitos: re.Pattern
    lugares: Optional[re.Pattern]                    # basta con que aparezcan en el texto
    lugares_cercanos: Optional[re.Pattern] = None    # solo cuentan si están cerca del delito
    distancia: int = 6                               # nº máximo de palabras entre delito y lugar_cercano

    def encaja(self, texto_n: str) -> bool:
        delitos = _posiciones(self.delitos, texto_n)
        if not delitos:
            return False
        if self.lugares is None and self.lugares_cercanos is None:
            return True
        if self.lugares is not None and self.lugares.search(texto_n):
            return True
        if self.lugares_cercanos is not None:
            for p in _posiciones(self.lugares_cercanos, texto_n):
                if any(abs(p - d) <= self.distancia for d in delitos):
                    return True
        return False


def _posiciones(patron: re.Pattern, texto: str) -> list[int]:
    """Posición (en nº de palabra) de cada coincidencia. El texto ya viene normalizado con un espacio entre palabras."""
    return [texto.count(" ", 0, m.start()) for m in patron.finditer(texto)]


def cargar_categorias(cfg: dict) -> list[Categoria]:
    cats = []
    for clave, c in (cfg.get("categorias") or {}).items():
        if not c.get("activa"):
            continue
        delitos = compilar_terminos(c.get("delitos"))
        if delitos is None:
            raise ConfigError(f"La categoría '{clave}' no tiene 'delitos'")
        cats.append(Categoria(
            clave, c.get("etiqueta", clave), c.get("icono", "🚨"), delitos,
            compilar_terminos(c.get("lugares")), compilar_terminos(c.get("lugares_cercanos")),
            int(c.get("distancia", 6)),
        ))
    if not cats:
        raise ConfigError("No hay ninguna categoría activa en config.yaml")
    return cats


@dataclass
class Filtros:
    """Reglas para descartar noticias que no son un robo real reciente."""
    exclusiones: Optional[re.Pattern] = None         # en titular o resumen
    exclusiones_titulo: Optional[re.Pattern] = None  # solo en el titular (juicios, consejos, estadísticas...)
    frases_ignoradas: Optional[re.Pattern] = None    # expresiones que se borran antes de buscar (p. ej. "como Pedro por su casa")
    porcentaje_titulo: bool = True                   # titular con "%" = estadística

    def descarta(self, titulo: str, titulo_n: str, texto_n: str) -> bool:
        if self.exclusiones is not None and self.exclusiones.search(texto_n):
            return True
        if self.exclusiones_titulo is not None and self.exclusiones_titulo.search(titulo_n):
            return True
        return self.porcentaje_titulo and "%" in titulo

    def limpiar(self, texto_n: str) -> str:
        if self.frases_ignoradas is None:
            return texto_n
        return re.sub(r" {2,}", " ", self.frases_ignoradas.sub(" ", texto_n)).strip()


def cargar_filtros(cfg: dict) -> Filtros:
    return Filtros(
        exclusiones=compilar_terminos(cfg.get("exclusiones")),
        exclusiones_titulo=compilar_terminos(cfg.get("exclusiones_titulo")),
        frases_ignoradas=compilar_terminos(cfg.get("frases_ignoradas")),
        porcentaje_titulo=bool(cfg.get("ajustes", {}).get("descartar_titulos_con_porcentaje", True)),
    )


@dataclass
class Lugar:
    provincia: Optional[str]
    municipio: Optional[str]
    en_cv: bool

    def texto(self, respaldo: Optional[str] = None) -> str:
        prov = self.provincia or respaldo
        if self.municipio and prov:
            return f"{prov} · {self.municipio}"
        return self.municipio or prov or CV_GENERICA


class Geografia:
    def __init__(self, cfg: dict):
        self.tabla: dict[str, tuple[str, str, bool]] = {}  # alias -> (nombre, provincia, es_provincia)
        for prov, alias in (cfg.get("provincias") or {}).items():
            for a in alias:
                self.tabla[norm(a)] = (prov, prov, True)
        for prov, items in (cfg.get("municipios") or {}).items():
            for item in items:
                nombres = [n.strip() for n in str(item).split("|") if n.strip()]
                for n in nombres:
                    self.tabla.setdefault(norm(n), (nombres[0], prov, False))
        alias_ordenados = sorted(self.tabla, key=len, reverse=True)
        self.patron = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in alias_ordenados) + r")\b") if alias_ordenados else None
        self.genericos = compilar_terminos(cfg.get("generico_cv"))
        self.falsos = compilar_terminos(cfg.get("falsos_lugares"))
        self.fuera = compilar_terminos(cfg.get("fuera_de_zona"))

    def localizar(self, titulo_n: str, cuerpo_n: str = "") -> Lugar:
        textos = [self.falsos.sub(" ", t) if self.falsos else t for t in (titulo_n, cuerpo_n)]
        aciertos = []  # (municipio_o_provincia, provincia, es_provincia)
        for texto in textos:
            if self.patron:
                for m in self.patron.finditer(texto):
                    aciertos.append(self.tabla[m.group(0)])
        for nombre, prov, es_prov in aciertos:
            if not es_prov:
                return Lugar(prov, nombre, True)
        if aciertos:
            return Lugar(aciertos[0][1], None, True)
        if self.genericos and any(self.genericos.search(t) for t in textos):
            return Lugar(None, None, True)
        return Lugar(None, None, False)

    def nombra_otra_zona(self, texto_n: str) -> bool:
        return bool(self.fuera and self.fuera.search(texto_n))


# ----------------------------------------------------------------------
#  Noticias
# ----------------------------------------------------------------------
@dataclass
class Noticia:
    titulo: str
    resumen: str
    url: str
    fuente: str
    publicada: Optional[datetime]
    ambito: str = "regional"
    agregador: bool = False
    provincia_defecto: Optional[str] = None
    origen: str = ""  # nombre de la fuente tal como aparece en config.yaml
    id: str = field(init=False)

    def __post_init__(self):
        self.id = id_url(self.url)


@dataclass
class Alerta:
    noticia: Noticia
    categoria: Categoria
    lugar: Lugar


def entrada_a_noticia(e, fuente: dict, prov_defecto: Optional[str], ahora: datetime,
                      agregador: Optional[bool] = None) -> Optional[Noticia]:
    titulo = limpiar_html(e.get("title", ""))
    url = (e.get("link") or "").strip()
    if not titulo or not url:
        return None
    # Una noticia es "de buscador" si la fuente entera es Google/Bing o si este feed concreto es una búsqueda
    # (p. ej. "site:levante-emv.com ..."). A esas se les aplican siempre las reglas estrictas de lugar.
    if agregador is None:
        agregador = fuente.get("tipo", "directa") == "agregador"
    nombre = fuente["nombre"]
    if agregador:
        src = e.get("source") or {}
        medio = src.get("title") if hasattr(src, "get") else None
        if medio:
            nombre = medio
            sufijo = f" - {medio}"
            if titulo.endswith(sufijo):
                titulo = titulo[: -len(sufijo)].rstrip()
        resumen = ""  # el "resumen" de Google/Bing solo repite titulares relacionados
    else:
        resumen = limpiar_html(e.get("summary") or e.get("description") or "")
    t = e.get("published_parsed") or e.get("updated_parsed")
    publicada = None
    if t:
        try:
            publicada = datetime(*t[:6], tzinfo=UTC)
        except (TypeError, ValueError):
            publicada = None
    if publicada and publicada > ahora + timedelta(hours=2):
        publicada = ahora  # fechas "del futuro" (zona horaria mal indicada en el feed)
    return Noticia(
        titulo=titulo,
        resumen=resumen,
        url=url,
        fuente=nombre,
        publicada=publicada,
        ambito="nacional" if agregador else fuente.get("ambito", "regional"),
        agregador=agregador,
        provincia_defecto=prov_defecto,
        origen=fuente["nombre"],
    )


# ----------------------------------------------------------------------
#  Descarga de feeds y autodescubrimiento
# ----------------------------------------------------------------------
def crear_sesion(cfg: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": cfg["ajustes"].get("user_agent", "AlertasRobosCV/1.0"),
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, text/html;q=0.7, */*;q=0.5",
            "Accept-Language": "es-ES,es;q=0.9",
        }
    )
    return s


def descargar(sesion: requests.Session, url: str, timeout: int = 20, reintentos: int = 2) -> bytes:
    motivo = "sin respuesta"
    for intento in range(reintentos + 1):
        try:
            r = sesion.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.content
            motivo = f"HTTP {r.status_code}"
            if r.status_code in (400, 401, 403, 404, 410):
                break
        except requests.RequestException as exc:
            motivo = type(exc).__name__
        if intento < reintentos:
            time.sleep(1.5 * (intento + 1))
    raise FeedError(motivo)


def leer_feed(sesion, url: str, timeout: int):
    d = feedparser.parse(descargar(sesion, url, timeout))
    if not d.entries and d.bozo:
        raise FeedError("la respuesta no es un feed RSS/Atom válido")
    return d


class _BuscadorEnlaces(HTMLParser):
    def __init__(self):
        super().__init__()
        self.declarados: list[str] = []
        self.anclas: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        href = a.get("href", "").strip()
        if not href:
            return
        if tag == "link" and "alternate" in a.get("rel", "").lower() and re.search(r"rss|atom|xml", a.get("type", "").lower()):
            self.declarados.append(href)
        elif tag == "a" and re.search(r"(rss|\.xml|/feed)", href.lower()):
            self.anclas.append(href)


def extraer_candidatos_rss(pagina_html: str, base_url: str) -> list[str]:
    p = _BuscadorEnlaces()
    try:
        p.feed(pagina_html)
    except Exception:  # HTML muy roto: nos quedamos con lo que se haya leído
        pass
    vistos, salida = set(), []
    for href in p.declarados + p.anclas:
        url = urljoin(base_url, href)
        if urlparse(url).scheme in ("http", "https") and url not in vistos:
            vistos.add(url)
            salida.append(url)

    def prioridad(u: str) -> int:
        u = u.lower()
        return 0 if re.search(r"suceso|successos|seguridad|sucesos", u) else 1

    return sorted(salida, key=prioridad)[:12]  # sorted es estable: conserva el orden original


def descubrir_feeds(sesion, paginas: list[str], timeout: int, maximo: int = 4) -> list[tuple[str, object]]:
    """Busca en las páginas indicadas enlaces RSS y devuelve los que funcionan: [(url, feed)]."""
    encontrados: list[tuple[str, object]] = []
    vistos: set[str] = set()
    for pagina in paginas:
        if len(encontrados) >= maximo:
            break
        try:
            contenido = descargar(sesion, pagina, timeout, reintentos=1)
        except FeedError as exc:
            log.info("   descubrir: no se pudo abrir %s (%s)", pagina, exc)
            continue
        for candidato in extraer_candidatos_rss(contenido.decode("utf-8", errors="replace"), pagina):
            if candidato in vistos or len(encontrados) >= maximo:
                continue
            vistos.add(candidato)
            try:
                encontrados.append((candidato, leer_feed(sesion, candidato, timeout)))
            except FeedError:
                continue
    return encontrados


def expandir_feed(item) -> tuple[str, Optional[str]]:
    """Convierte una entrada de 'feeds' en (url, provincia)."""
    if isinstance(item, str):
        return item, None
    prov = item.get("provincia")
    if "google_news" in item:
        return f"https://news.google.com/rss/search?q={quote_plus(item['google_news'])}&hl=es&gl=ES&ceid=ES:es", prov
    if "bing_news" in item:
        return f"https://www.bing.com/news/search?q={quote_plus(item['bing_news'])}&format=RSS&setmkt=es-ES", prov
    if "url" in item:
        return item["url"], prov
    raise ConfigError(f"Entrada de feed no válida: {item!r}")


def es_busqueda(item) -> bool:
    """True si la entrada de 'feeds' es una búsqueda de Google/Bing Noticias."""
    return isinstance(item, dict) and ("google_news" in item or "bing_news" in item)


# ----------------------------------------------------------------------
#  Estado (lo ya enviado)
# ----------------------------------------------------------------------
def estado_vacio() -> dict:
    # historial: robos detectados con municipio (para las oleadas) · oleadas: zona -> fecha del último aviso de oleada
    return {"version": 1, "creado": None, "enviadas": {}, "descubiertos": {}, "historial": [], "oleadas": {}}


def cargar_estado(ruta: Path) -> dict:
    if not ruta.exists():
        return estado_vacio()
    try:
        est = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"No se puede leer el estado {ruta}: {exc}")
    base = estado_vacio()
    base.update(est)
    return base


def guardar_estado(ruta: Path, estado: dict) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(ruta)


def podar_estado(estado: dict, ahora: datetime, dias: int, dias_historial: Optional[int] = None) -> None:
    limite = ahora - timedelta(days=dias)
    for k in [k for k, v in estado["enviadas"].items() if _fecha(v.get("t")) < limite]:
        del estado["enviadas"][k]
    if dias_historial is not None:
        limite_h = ahora - timedelta(days=dias_historial)
        estado["historial"] = [h for h in estado["historial"] if _fecha(h.get("t")) >= limite_h]
        for z in [z for z, t in estado["oleadas"].items() if _fecha(t) < limite_h]:
            del estado["oleadas"][z]


# ----------------------------------------------------------------------
#  Oleadas: varios robos en el mismo municipio en pocos días
# ----------------------------------------------------------------------
@dataclass
class ConfigOleadas:
    activa: bool = True
    umbral: int = 3          # nº de robos en el mismo municipio para avisar
    ventana_dias: int = 15   # ... dentro de estos días


def cargar_oleadas(cfg: dict) -> ConfigOleadas:
    o = cfg.get("oleadas") or {}
    return ConfigOleadas(
        activa=bool(o.get("activa", True)),
        umbral=max(2, int(o.get("umbral", 3))),
        ventana_dias=max(1, int(o.get("ventana_dias", 15))),
    )


def zona_de(lugar: "Lugar") -> Optional[str]:
    """Clave del municipio ('Valencia|Bétera'). Solo cuenta si la noticia nombra un municipio concreto."""
    return f"{lugar.provincia}|{lugar.municipio}" if lugar.municipio and lugar.provincia else None


def eventos_zona(estado: dict, zona: str, desde: datetime) -> list[dict]:
    return [h for h in estado["historial"] if h.get("zona") == zona and _fecha(h.get("t")) >= desde]


def oleada_activa(estado: dict, zona: str, desde: datetime) -> bool:
    """Ya se avisó de una oleada en esta zona dentro de la ventana (no se repite el resumen)."""
    return zona in estado["oleadas"] and _fecha(estado["oleadas"][zona]) >= desde


def formatear_oleada(zona: str, eventos: list[dict], ventana_dias: int) -> str:
    esc = html.escape
    provincia, municipio = zona.split("|", 1)
    ordenados = sorted(eventos, key=lambda h: h.get("t", ""))
    lineas = [f"⚠️ <b>Posible oleada de robos · {esc(provincia)} · {esc(municipio)}</b>",
              f"{len(eventos)} robos en viviendas en los últimos {ventana_dias} días:", ""]
    for h in ordenados[-10:]:
        dia = _fecha(h.get("t")).astimezone(HORA_LOCAL).strftime("%d/%m")
        lineas.append(f'• {dia} · <a href="{esc(h.get("url", ""), quote=True)}">{esc(h.get("titulo", ""))}</a>')
    lineas += ["", "<i>Puede haber varias noticias sobre un mismo robo.</i>"]
    return "\n".join(lineas)


def _fecha(texto: Optional[str]) -> datetime:
    try:
        return datetime.fromisoformat(texto)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)


# ----------------------------------------------------------------------
#  Recolección por fuente
# ----------------------------------------------------------------------
@dataclass
class SaludFuente:
    nombre: str
    feeds_ok: list = field(default_factory=list)   # (url, nº entradas, entrada más reciente)
    feeds_ko: list = field(default_factory=list)   # (url, motivo)
    descubiertos: bool = False
    via_buscador: bool = False                     # medio leído a través de Google/Bing Noticias
    coincidencias: int = 0


def _mas_reciente(d, ahora) -> Optional[datetime]:
    fechas = []
    for e in d.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if t:
            try:
                fechas.append(datetime(*t[:6], tzinfo=UTC))
            except (TypeError, ValueError):
                pass
    return min(max(fechas), ahora) if fechas else None


def recolectar_fuente(sesion, fuente: dict, cfg: dict, estado: dict, ahora: datetime) -> tuple[list[Noticia], SaludFuente]:
    timeout = cfg["ajustes"].get("timeout_segundos", 20)
    salud = SaludFuente(fuente["nombre"])
    prov_fuente = fuente.get("default_provincia")
    fuente_agregadora = fuente.get("tipo", "directa") == "agregador"
    validos: list[tuple[str, Optional[str], object, bool]] = []   # (url, provincia, feed, es_busqueda)

    for item in fuente.get("feeds") or []:
        url, prov = expandir_feed(item)
        try:
            validos.append((url, prov, leer_feed(sesion, url, timeout), fuente_agregadora or es_busqueda(item)))
        except FeedError as exc:
            salud.feeds_ko.append((url, str(exc)))

    paginas = fuente.get("descubrir") or []
    if not validos and paginas:
        # Los feeds autodetectados se recuerdan 24 h (o 6 h si no se encontró ninguno) para no repetir la búsqueda cada vez
        cache = estado["descubiertos"].get(fuente["nombre"])
        ttl = timedelta(hours=24 if cache and cache.get("feeds") else 6)
        usar_cache = bool(cache) and ahora - _fecha(cache.get("ts")) < ttl
        if usar_cache:
            for url in cache.get("feeds", []):
                try:
                    validos.append((url, None, leer_feed(sesion, url, timeout), fuente_agregadora))
                except FeedError as exc:
                    salud.feeds_ko.append((url, str(exc)))
        if not validos and (not usar_cache or cache.get("feeds")):
            log.info("   %s: buscando su RSS automáticamente...", fuente["nombre"])
            hallados = descubrir_feeds(sesion, paginas, timeout)
            estado["descubiertos"][fuente["nombre"]] = {"ts": ahora.isoformat(), "feeds": [u for u, _ in hallados]}
            validos.extend((u, None, d, fuente_agregadora) for u, d in hallados)
        salud.descubiertos = bool(validos)

    salud.via_buscador = bool(validos) and not fuente_agregadora and all(b for *_, b in validos)
    noticias: list[Noticia] = []
    for url, prov, d, busqueda in validos:
        salud.feeds_ok.append((url, len(d.entries), _mas_reciente(d, ahora)))
        for e in d.entries:
            n = entrada_a_noticia(e, fuente, prov or prov_fuente, ahora, agregador=busqueda)
            if n:
                noticias.append(n)
    return noticias, salud


# ----------------------------------------------------------------------
#  Filtrado
# ----------------------------------------------------------------------
def filtrar_noticias(noticias: list[Noticia], cats: list[Categoria], geo: Geografia, filtros: Filtros,
                     estado: dict, ahora: datetime, ajustes: dict, max_edad_h: float) -> list[Alerta]:
    umbral = ajustes.get("similitud_titulos", 0.5)
    limite_edad = timedelta(hours=max_edad_h)
    recientes = [set(v.get("tok", [])) for v in estado["enviadas"].values() if ahora - _fecha(v.get("t")) < timedelta(days=3)]
    aceptadas: list[Alerta] = []
    ids_vistos = set(estado["enviadas"])

    # Primero las fuentes directas (mejor enlace), luego los agregadores; dentro de cada grupo, por fecha
    orden = sorted(noticias, key=lambda n: (n.agregador, n.publicada or ahora))
    for n in orden:
        if n.id in ids_vistos:
            continue
        if n.publicada and ahora - n.publicada > limite_edad:
            continue
        titulo_n = norm(n.titulo)
        texto_n = norm(f"{n.titulo}. {n.resumen[:600]}")
        if filtros.descarta(n.titulo, titulo_n, texto_n):
            continue
        limpio = filtros.limpiar(texto_n)
        categoria = next((c for c in cats if c.encaja(limpio)), None)
        if categoria is None:
            continue
        lugar = geo.localizar(titulo_n, norm(n.resumen[:600]))
        if not lugar.en_cv:
            if n.ambito == "nacional":
                continue  # fuente general o buscador: exigimos que LA PROPIA NOTICIA nombre un lugar real
                          # de la Comunitat. Que la búsqueda incluyera "Alicante" no basta: Google/Bing pueden
                          # devolver resultados de otras provincias que solo tocan el tema por encima.
            if geo.nombra_otra_zona(texto_n):
                continue
        elif n.ambito == "nacional" and not lugar.municipio and not lugar.provincia and n.provincia_defecto:
            lugar = Lugar(n.provincia_defecto, None, True)  # "Comunitat Valenciana" genérico + pista de la búsqueda
        tok = tokens_titulo(n.titulo)
        if any(titulos_similares(tok, otro, umbral) for otro in recientes):
            ids_vistos.add(n.id)
            continue
        recientes.append(tok)
        ids_vistos.add(n.id)
        aceptadas.append(Alerta(n, categoria, lugar))
    return aceptadas


# ----------------------------------------------------------------------
#  Mensajes y notificadores
# ----------------------------------------------------------------------
def formatear_mensaje(a: Alerta, ahora: datetime, extra: Optional[str] = None) -> str:
    n = a.noticia
    esc = html.escape
    lineas = [f"{a.categoria.icono} <b>{esc(a.categoria.etiqueta)}</b> · {esc(a.lugar.texto(n.provincia_defecto))}"]
    if extra:
        lineas.append(f"<b>{esc(extra)}</b>")
    lineas.append("")
    lineas.append(f"<b>{esc(n.titulo)}</b>")
    if n.resumen:
        ini_t, ini_r = norm(n.titulo)[:40], norm(n.resumen)[:40]
        if ini_t != ini_r:
            lineas.append(esc(recortar(n.resumen, 260)))
    lineas.append("")
    pie = f"📰 {esc(n.fuente)}"
    if n.publicada:
        pie += f" · {hace_cuanto(ahora - n.publicada)}"
    lineas.append(pie)
    lineas.append(f'<a href="{esc(n.url, quote=True)}">Leer la noticia</a>')
    return "\n".join(lineas)


class Notificador:
    errores = 0

    def enviar(self, texto: str) -> bool:
        raise NotImplementedError


class ConsolaNotificador(Notificador):
    def enviar(self, texto: str) -> bool:
        print("-" * 60)
        print(re.sub(r"</?(b|a)[^>]*>", "", html.unescape(texto)))
        return True


class TelegramNotificador(Notificador):
    def __init__(self, token: str, chat_ids: list[str], sesion: Optional[requests.Session] = None,
                 vista_previa: bool = False, pausa: float = 0.6):
        self.token, self.chat_ids, self.vista_previa, self.pausa = token, chat_ids, vista_previa, pausa
        self.sesion = sesion or requests.Session()
        self.errores = 0

    def _ocultar(self, texto: str) -> str:
        return texto.replace(self.token, "***")

    def _enviar_uno(self, chat_id: str, texto: str) -> None:
        payload = {
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": not self.vista_previa},
        }
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for _ in range(3):
            try:
                r = self.sesion.post(url, json=payload, timeout=20)
            except requests.RequestException as exc:
                raise TelegramError(type(exc).__name__)  # sin detalle: podría incluir el token en la URL
            if r.status_code == 200:
                return
            try:
                info = r.json()
            except ValueError:
                info = {}
            if r.status_code == 429:
                time.sleep(min(int(info.get("parameters", {}).get("retry_after", 5)), 30))
                continue
            raise TelegramError(self._ocultar(f"HTTP {r.status_code}: {info.get('description', 'sin detalle')}"))
        raise TelegramError("límite de envíos de Telegram (429) tras varios intentos")

    def enviar(self, texto: str) -> bool:
        alguno = False
        for chat in self.chat_ids:
            try:
                self._enviar_uno(chat, texto)
                alguno = True
            except TelegramError as exc:
                self.errores += 1
                log.error("Telegram (chat %s): %s", chat, exc)
            time.sleep(self.pausa)  # Telegram admite ~1 mensaje/segundo por chat
        return alguno


def notificador_desde_entorno(sesion, cfg) -> TelegramNotificador:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chats:
        raise ConfigError("Faltan las variables TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID (secretos de GitHub).")
    return TelegramNotificador(token, chats, sesion, cfg["ajustes"].get("vista_previa_enlace", False))


# ----------------------------------------------------------------------
#  Ejecución
# ----------------------------------------------------------------------
def cargar_config(ruta: Path) -> dict:
    try:
        cfg = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"No se puede leer {ruta}: {exc}")
    cfg.setdefault("ajustes", {})
    if not cfg.get("fuentes"):
        raise ConfigError("config.yaml no tiene 'fuentes'")
    return cfg


def resumen_markdown(salud: list[SaludFuente], ahora: datetime, enviadas: int, candidatas: int, oleadas: int = 0) -> str:
    filas = ["## Resumen de la ejecución", "",
             f"Noticias de robos detectadas: **{candidatas}** · avisos enviados: **{enviadas}** · avisos de oleada: **{oleadas}**", "",
             "| Fuente | Estado | Entradas | Última noticia | Coincidencias |", "|---|---|---|---|---|"]
    for s in salud:
        if s.feeds_ok:
            entradas = sum(n for _, n, _ in s.feeds_ok)
            fechas = [f for _, _, f in s.feeds_ok if f]
            ultima = hace_cuanto(ahora - max(fechas)) if fechas else "?"
            estado = (f"OK ({len(s.feeds_ok)} feed{'s' if len(s.feeds_ok) != 1 else ''}"
                      + (", autodetectado" if s.descubiertos else "")
                      + (", vía Google Noticias" if s.via_buscador else "") + ")")
            filas.append(f"| {s.nombre} | {estado} | {entradas} | {ultima} | {s.coincidencias} |")
        else:
            motivo = s.feeds_ko[0][1] if s.feeds_ko else "no se encontró ningún feed"
            filas.append(f"| {s.nombre} | SIN FEED ({motivo}) | 0 | - | 0 |")
    ko = [(s.nombre, u, m) for s in salud for u, m in s.feeds_ko if s.feeds_ok]
    if ko:
        filas += ["", "Feeds que fallaron (la fuente sigue activa por otros feeds):", ""]
        filas += [f"- {n}: {u} → {m}" for n, u, m in ko]
    return "\n".join(filas) + "\n"


def ejecutar(cfg: dict, ruta_estado: Path, dry_run: bool = False, max_edad_h: Optional[float] = None,
             ahora: Optional[datetime] = None, sesion=None, notificador: Optional[Notificador] = None) -> int:
    ahora = ahora or datetime.now(UTC)
    ajustes = cfg["ajustes"]
    max_edad_h = max_edad_h if max_edad_h is not None else ajustes.get("max_edad_horas", 36)
    sesion = sesion or crear_sesion(cfg)
    cats = cargar_categorias(cfg)
    geo = Geografia(cfg)
    filtros = cargar_filtros(cfg)
    estado = cargar_estado(ruta_estado)
    primera_vez = estado.get("creado") is None
    if notificador is None:
        notificador = ConsolaNotificador() if dry_run else notificador_desde_entorno(sesion, cfg)

    log.info("Categorías activas: %s", ", ".join(c.clave for c in cats))
    todas: list[Noticia] = []
    salud: list[SaludFuente] = []
    for fuente in cfg["fuentes"]:
        noticias, s = recolectar_fuente(sesion, fuente, cfg, estado, ahora)
        todas.extend(noticias)
        salud.append(s)
        if s.feeds_ok:
            log.info("OK   %-40s %3d entradas (%d feed/s)", s.nombre, sum(n for _, n, _ in s.feeds_ok), len(s.feeds_ok))
        else:
            log.warning("FALLA %-39s %s", s.nombre, "; ".join(f"{u} → {m}" for u, m in s.feeds_ko) or "ningún feed encontrado")

    fuentes_ok = sum(1 for s in salud if s.feeds_ok)
    if fuentes_ok == 0:
        log.error("Ninguna fuente respondió. Revisa la conexión o config.yaml.")
        return 2

    alertas = filtrar_noticias(todas, cats, geo, filtros, estado, ahora, ajustes, max_edad_h)
    por_nombre = {s.nombre: s for s in salud}
    for a in alertas:
        por_nombre[a.noticia.origen].coincidencias += 1

    a_enviar = sorted(alertas, key=lambda a: a.noticia.publicada or ahora)
    silenciadas: list[Alerta] = []
    if primera_vez and not dry_run:
        maximo = ajustes.get("primera_ejecucion_max", 5)
        recientes = sorted(a_enviar, key=lambda a: a.noticia.publicada or ahora, reverse=True)[:maximo]
        elegidas = {id(a) for a in recientes}
        silenciadas = [a for a in a_enviar if id(a) not in elegidas]
        a_enviar = [a for a in a_enviar if id(a) in elegidas]
        bienvenida = (f"✅ <b>Bot de alertas activo</b>\nVigilo {fuentes_ok} fuentes de noticias de la Comunitat Valenciana "
                      f"({', '.join(c.etiqueta.lower() for c in cats)}). Te aviso en cuanto salga algo nuevo.")
        notificador.enviar(bienvenida)

    limite = ajustes.get("max_alertas_por_ejecucion", 15)
    if len(a_enviar) > limite:
        log.warning("Hay %d avisos; se envían %d ahora y el resto en la siguiente ejecución.", len(a_enviar), limite)
    ole = cargar_oleadas(cfg)
    desde = ahora - timedelta(days=ole.ventana_dias)
    enviadas = oleadas_enviadas = 0
    for a in a_enviar[:limite]:
        zona = zona_de(a.lugar) if ole.activa else None
        total = len(eventos_zona(estado, zona, desde)) + 1 if zona else 0
        ya_avisada = bool(zona) and oleada_activa(estado, zona, desde)
        extra = None
        if zona and ya_avisada and total >= ole.umbral:
            extra = f"⚠️ Oleada activa: {total} robos en {a.lugar.municipio} en los últimos {ole.ventana_dias} días"
        if notificador.enviar(formatear_mensaje(a, ahora, extra)):
            enviadas += 1
            _registrar(estado, a, ahora)
            _registrar_historial(estado, a, ahora)
            if zona and not ya_avisada and total >= ole.umbral:
                if notificador.enviar(formatear_oleada(zona, eventos_zona(estado, zona, desde), ole.ventana_dias)):
                    estado["oleadas"][zona] = ahora.isoformat()
                    oleadas_enviadas += 1
    for a in silenciadas:
        _registrar(estado, a, ahora, silenciada=True)
        _registrar_historial(estado, a, ahora)

    if not dry_run:
        if estado["creado"] is None:
            estado["creado"] = ahora.isoformat()
        podar_estado(estado, ahora, ajustes.get("retencion_dias", 10), ole.ventana_dias + 1)
        guardar_estado(ruta_estado, estado)

    log.info("Resumen: %d fuentes activas de %d · %d noticias de robos nuevas · %d avisos enviados · %d oleadas",
             fuentes_ok, len(salud), len(alertas), enviadas, oleadas_enviadas)
    md = resumen_markdown(salud, ahora, enviadas, len(alertas), oleadas_enviadas)
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if destino:
        try:
            with open(destino, "a", encoding="utf-8") as f:
                f.write(md)
        except OSError:
            pass
    elif dry_run:
        print("\n" + md)
    return 1 if getattr(notificador, "errores", 0) else 0


def _registrar(estado: dict, a: Alerta, ahora: datetime, silenciada: bool = False) -> None:
    n = a.noticia
    estado["enviadas"][n.id] = {
        "t": ahora.isoformat(),
        "titulo": n.titulo[:160],
        "url": n.url,
        "fuente": n.fuente,
        "tok": sorted(tokens_titulo(n.titulo)),
        **({"silenciada": True} if silenciada else {}),
    }


def _registrar_historial(estado: dict, a: Alerta, ahora: datetime) -> None:
    zona = zona_de(a.lugar)
    n = a.noticia
    if not zona or any(h.get("id") == n.id for h in estado["historial"]):
        return
    estado["historial"].append({
        "t": (n.publicada or ahora).isoformat(),
        "zona": zona,
        "id": n.id,
        "titulo": n.titulo[:160],
        "url": n.url,
        "fuente": n.fuente,
    })


def probar_telegram(cfg: dict) -> int:
    sesion = crear_sesion(cfg)
    n = notificador_desde_entorno(sesion, cfg)
    ok = n.enviar("✅ <b>Prueba correcta</b>\nEl bot de alertas de robos puede escribirte en este chat.")
    print("Mensaje de prueba enviado." if ok and not n.errores else "No se pudo enviar el mensaje (mira el error de arriba).")
    return 0 if ok and not n.errores else 1


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Alertas de robos en viviendas - Comunitat Valenciana")
    p.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="busca noticias y envía los avisos")
    r.add_argument("--dry-run", action="store_true", help="simulacro: muestra los avisos sin enviarlos ni guardar nada")
    r.add_argument("--max-edad-horas", type=float, default=None, help="ignora noticias más antiguas (por defecto, config.yaml)")
    r.add_argument("--estado", default=None, help="ruta del fichero de estado")
    sub.add_parser("test-telegram", help="envía un mensaje de prueba")
    args = p.parse_args(argv)
    args.cmd = args.cmd or "run"

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        cfg = cargar_config(Path(args.config))
        if args.cmd == "test-telegram":
            return probar_telegram(cfg)
        ruta = Path(getattr(args, "estado", None) or BASE_DIR / cfg["ajustes"].get("archivo_estado", "state/state.json"))
        return ejecutar(cfg, ruta, dry_run=getattr(args, "dry_run", False), max_edad_h=getattr(args, "max_edad_horas", None))
    except ConfigError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
