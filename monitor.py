"""
MONITOR TRENDY - Calendario de adviento "Luxury"
================================================
Se ejecuta UNA vez por llamada (GitHub Actions lo lanza cada ~10 min) y:

 1. Busca productos cuyo nombre/enlace contenga las PALABRAS clave.
 2. Busca banners, enlaces e imágenes con esas palabras en la portada.
 3. Avisa si aparece en la portada un BANNER NUEVO (aunque no tenga las
    palabras clave), por si el calendario se anuncia con otro nombre.
 4. Te avisa si él mismo deja de funcionar (páginas que no cargan, 0 productos
    leídos...) y te manda un mensaje diario de "sigo activo".

Recuerda lo ya avisado en estado.json para no repetir mensajes.

Uso:  python monitor.py          -> revisión normal
      python monitor.py --test   -> solo envía un mensaje de prueba
"""
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urljoin

import requests
from bs4 import BeautifulSoup

# =====================  CONFIGURACIÓN  =====================
BASE = "https://www.maquillajetrendyshop.com"

# Páginas donde se buscan productos
PAGINAS = [
    f"{BASE}/luxury?_q=luxury&map=ft",
    f"{BASE}/adviento?_q=adviento&map=ft",
    f"{BASE}/calendario?_q=calendario&map=ft",
    f"{BASE}/nueva-coleccion",
    f"{BASE}/",
]
# Páginas donde se buscan banners / enlaces / imágenes
PAGINAS_BANNER = [f"{BASE}/"]
# Páginas que SIEMPRE deben mostrar productos (si salen con 0, algo falla)
PAGINAS_CON_PRODUCTOS = [f"{BASE}/nueva-coleccion", f"{BASE}/"]

# API de búsqueda de VTEX (extra; si no responde se ignora)
API = f"{BASE}/api/catalog_system/pub/products/search?ft="
TERMINOS_API = ["luxury", "adviento", "calendario"]

# Palabras clave (ignora mayúsculas, tildes y %20)
PALABRAS = ["luxury", "adviento", "calendario", "advent"]
# Enlaces que se ignoran (evita falsos avisos, p. ej. "Términos Calendario 2026")
EXCLUIR = ["terminos", "condiciones", "politica", "privacidad", "habeas",
           "servicio-al-cliente", "enlaces-de-interes", "pqr"]

AVISAR_BANNERS_NUEVOS = True      # avisa si aparece un banner nuevo en la portada
AVISAR_PRODUCTOS_NUEVOS = False   # avisa de CUALQUIER producto nuevo en Nueva colección
LATIDO_HORA = 8                   # mensaje diario "sigo activo" desde esta hora (Colombia); None = desactivado
MAX_AVISOS_NOVEDAD = 5            # máximo de avisos de novedad por ejecución
# ===========================================================

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ESTADO = "estado.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "es-CO,es;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
RE_PROD = re.compile(r"/p(\?|#|$)")
ESTADO_BASE = {
    "avisados": [], "fallos_pagina": {}, "ceros": 0,
    "banners_vistos": None, "productos_vistos": None, "ultimo_latido": "",
}


# ---------------------------------------------------------------- utilidades
def ahora_colombia() -> datetime:
    return datetime.now(timezone(timedelta(hours=-5)))   # Colombia: UTC-5 fijo


def hora_colombia() -> str:
    return ahora_colombia().strftime("%H:%M:%S")


def norm(texto: str) -> str:
    """Minúsculas, sin tildes y con %20 convertido en espacio."""
    t = unicodedata.normalize("NFKD", unquote(texto or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def coincide(texto: str) -> bool:
    t = norm(texto)
    return any(norm(p) in t for p in PALABRAS)


def excluido(texto: str) -> bool:
    t = norm(texto)
    return any(norm(x) in t for x in EXCLUIR)


_enviados = [0]


def telegram(msg: str) -> None:
    """Envía a Telegram con 3 intentos. Si falla, lanza error (así el aviso
    NO se marca como enviado y se reintenta en la siguiente ejecución)."""
    msg = f"{msg}\n🕒 Enviado a las {hora_colombia()} (Colombia)"
    ultimo = None
    for i in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": msg},
                timeout=15,
            )
            r.raise_for_status()
            _enviados[0] += 1
            return
        except Exception as e:
            ultimo = e
            time.sleep(2 * (i + 1))
    raise ultimo


def avisar_seguro(msg: str) -> bool:
    """Para avisos de salud: si Telegram falla, no detiene el monitor."""
    try:
        telegram(msg)
        return True
    except Exception as e:
        print(f"[!] No se pudo enviar el aviso de salud: {e}")
        return False


def debe_alertar(n: int, primera: int) -> bool:
    """Alerta en la 'primera' vez y luego cada 36 revisiones (~6 h)."""
    return n == primera or (n > primera and (n - primera) % 36 == 0)


def cargar_estado() -> dict:
    e = json.loads(json.dumps(ESTADO_BASE))
    try:
        with open(ESTADO, encoding="utf-8") as f:
            e.update(json.load(f))
    except Exception:
        pass
    if not isinstance(e.get("fallos_pagina"), dict):
        e["fallos_pagina"] = {}
    return e


def guardar_estado(e: dict) -> None:
    e["avisados"] = e["avisados"][-500:]
    for k in ("banners_vistos", "productos_vistos"):
        if isinstance(e.get(k), list):
            e[k] = e[k][-1000:]
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(e, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------- lectura
_cache: dict = {}


def descargar(url: str, intentos: int = 3) -> str:
    """Descarga una página (con reintentos). Cada URL se pide una sola vez."""
    if url in _cache:
        return _cache[url]
    ultimo = None
    for i in range(intentos):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            _cache[url] = r.text
            return r.text
        except Exception as e:
            ultimo = e
            time.sleep(2 * (i + 1))
    raise ultimo


def limpiar_nombre(t: str) -> str:
    """Quita precio, 'Agregar al carrito', etiquetas y repeticiones del nombre."""
    t = re.sub(r"Agregar al carrito", " ", t or "", flags=re.I)
    t = re.sub(r"\$\s?[\d\.\,]+", " ", t)
    t = re.sub(r"\b(NUEVO|OFERTA)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    m = re.match(r"^(.{6,}?)(?:\s+\1)+(?:\s|$)", t)     # "Nombre Nombre Nombre" -> "Nombre"
    if m:
        t = m.group(1)
    return t[:80]


def extraer_productos(html: str) -> dict:
    """{url_producto: (nombre_corto, texto_completo)} de TODOS los productos."""
    soup = BeautifulSoup(html, "html.parser")
    res = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not RE_PROD.search(href):
            continue
        texto = a.get_text(" ", strip=True)
        alts = " ".join(i.get("alt", "") for i in a.find_all("img"))
        clave = urljoin(BASE, re.split(r"[?#]", href)[0])
        res[clave] = (limpiar_nombre(texto or alts) or href[:80], f"{href} {texto} {alts}")
    return res


def extraer_anuncios(html: str) -> dict:
    """Enlaces/imágenes (que no sean productos) con palabras clave."""
    soup = BeautifulSoup(html, "html.parser")
    res = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if RE_PROD.search(href):
            continue
        texto = a.get_text(" ", strip=True)
        imgs = a.find_all("img")
        partes = [href, texto, a.get("title", ""), a.get("aria-label", "")]
        for img in imgs:
            partes += [img.get("alt", ""), img.get("src", "")]
        todo = " ".join(partes)
        if excluido(todo) or not coincide(todo):
            continue
        alts = [i.get("alt") for i in imgs if i.get("alt")]
        nombre = texto or a.get("title", "") or (alts[0] if alts else "banner/enlace")
        res[urljoin(BASE, href)] = nombre[:80]
    for img in soup.find_all("img"):
        if img.find_parent("a"):
            continue
        alt, src = img.get("alt", ""), img.get("src", "")
        todo = f"{alt} {src}"
        if not excluido(todo) and coincide(todo):
            res[urljoin(BASE, src)] = (alt or "imagen")[:80]
    return res


def extraer_banners(html: str) -> dict:
    """Banners = enlaces que contienen solo una imagen. {src: (alt, enlace)}"""
    soup = BeautifulSoup(html, "html.parser")
    res = {}
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True):
            continue
        for img in a.find_all("img"):
            src = (img.get("src") or img.get("data-src") or "").strip()
            if not src or src.startswith("data:"):
                continue
            res[src.split("?")[0]] = (img.get("alt", "") or "", urljoin(BASE, a["href"]))
    return res


def productos_en_api() -> dict:
    hallados = {}
    for term in TERMINOS_API:
        try:
            r = requests.get(API + quote(term), headers=HEADERS, timeout=20)
            if r.status_code != 200:
                continue
            for p in r.json():
                nombre = p.get("productName", "")
                link = p.get("link") or ""
                if coincide(f"{nombre} {link}"):
                    hallados[urljoin(BASE, link)] = nombre
        except Exception:
            continue   # la API es un extra; si falla no pasa nada
    return hallados


# Compatibilidad con los scripts probar_*.py
def productos_en_html(url: str) -> dict:
    return {u: n for u, (n, full) in extraer_productos(descargar(url)).items()
            if coincide(full)}


def anuncios_en_html(url: str) -> dict:
    return extraer_anuncios(descargar(url))


# ---------------------------------------------------------------------- main
def main() -> None:
    if "--test" in sys.argv:
        telegram("✅ Prueba: tu monitor de Trendy puede enviarte mensajes.")
        print("Mensaje de prueba enviado.")
        return

    estado = cargar_estado()
    urls = list(dict.fromkeys(PAGINAS + PAGINAS_BANNER))

    # 1) Descargar todas las páginas
    paginas, fp = {}, estado["fallos_pagina"]
    for url in urls:
        try:
            paginas[url] = descargar(url)
            fp[url] = 0
        except Exception as e:
            fp[url] = fp.get(url, 0) + 1
            print(f"[!] No se pudo leer {url}: {e}")
            if debe_alertar(fp[url], 6):
                avisar_seguro(f"⚠️ Llevo {fp[url]} revisiones seguidas sin poder leer:\n{url}\n"
                              "(¿bloqueo de IP o cambio en la web?). Revisa GitHub Actions.")

    # 2) Productos
    productos_por_pagina = {u: extraer_productos(paginas[u]) for u in PAGINAS if u in paginas}
    hallados = {}
    for prods in productos_por_pagina.values():
        for u, (nombre, full) in prods.items():
            if coincide(full):
                hallados[u] = nombre
    hallados.update(productos_en_api())

    # 3) Salud: las páginas con productos no deben salir vacías
    vacias = [u for u in PAGINAS_CON_PRODUCTOS if u in productos_por_pagina
              and not productos_por_pagina[u]]
    estado["ceros"] = estado["ceros"] + 1 if vacias else 0
    if vacias and debe_alertar(estado["ceros"], 3):
        avisar_seguro(f"⚠️ Llevo {estado['ceros']} revisiones seguidas leyendo 0 productos en:\n"
                      + "\n".join(vacias) + "\n(¿la tienda cambió o bloquea las lecturas?)")

    # 4) Avisos por palabra clave: productos
    for url, nombre in hallados.items():
        if url in estado["avisados"]:
            continue
        telegram(f"🎄 ¡Posible calendario de adviento!\n{nombre}\n{url}")
        estado["avisados"].append(url)
        print(f"Aviso de producto: {nombre} -> {url}")

    # 5) Avisos por palabra clave: banners/enlaces/imágenes
    anuncios = {}
    for url in PAGINAS_BANNER:
        if url in paginas:
            anuncios.update(extraer_anuncios(paginas[url]))
    for url, nombre in anuncios.items():
        if url in estado["avisados"] or url in hallados:
            continue
        telegram(f"🔔 Posible anuncio/banner con tu palabra clave:\n{nombre}\n{url}")
        estado["avisados"].append(url)
        print(f"Aviso de banner: {nombre} -> {url}")

    # 6) Banners nuevos en la portada (aunque no tengan las palabras clave)
    n_banners = 0
    portada = f"{BASE}/"
    if AVISAR_BANNERS_NUEVOS and portada in paginas:
        actuales = extraer_banners(paginas[portada])
        n_banners = len(actuales)
        if estado["banners_vistos"] is None:
            estado["banners_vistos"] = list(actuales)      # primera vez: solo se memoriza
        else:
            nuevos = [s for s in actuales if s not in estado["banners_vistos"]]
            enviados = 0
            for src in nuevos:
                alt, enlace = actuales[src]
                estado["banners_vistos"].append(src)
                if coincide(f"{alt} {enlace} {src}") and not excluido(f"{alt} {enlace} {src}"):
                    continue                               # ya lo cubre el aviso por palabra clave
                if enviados < MAX_AVISOS_NOVEDAD:
                    telegram(f"🖼️ Banner NUEVO en la portada:\n{alt or '(sin descripción)'}\n"
                             f"Enlace: {enlace}\nImagen: {src}")
                    enviados += 1
            if len(nuevos) > enviados and enviados >= MAX_AVISOS_NOVEDAD:
                telegram(f"🖼️ Hay {len(nuevos) - enviados} banners nuevos más en la portada: {portada}")

    # 7) Productos nuevos en Nueva colección (opcional)
    nueva = f"{BASE}/nueva-coleccion"
    if AVISAR_PRODUCTOS_NUEVOS and nueva in productos_por_pagina and productos_por_pagina[nueva]:
        actuales = productos_por_pagina[nueva]
        if estado["productos_vistos"] is None:
            estado["productos_vistos"] = list(actuales)
        else:
            nuevos = [u for u in actuales if u not in estado["productos_vistos"]]
            enviados = 0
            for u in nuevos:
                estado["productos_vistos"].append(u)
                if u in hallados or enviados >= MAX_AVISOS_NOVEDAD:
                    continue
                telegram(f"🆕 Producto nuevo en Nueva colección:\n{actuales[u][0]}\n{u}")
                enviados += 1

    # 8) Mensaje diario "sigo activo"
    if LATIDO_HORA is not None:
        ahora = ahora_colombia()
        hoy = ahora.strftime("%Y-%m-%d")
        if ahora.hour >= LATIDO_HORA and estado["ultimo_latido"] != hoy:
            n_portada = len(productos_por_pagina.get(portada, {}))
            n_nueva = len(productos_por_pagina.get(nueva, {}))
            if avisar_seguro(f"✅ Monitor Trendy activo ({hoy}).\n"
                             f"Leído ahora: {n_portada} productos en portada, {n_nueva} en Nueva "
                             f"colección, {n_banners} banners.\nBuscando: {', '.join(PALABRAS)}"):
                estado["ultimo_latido"] = hoy

    guardar_estado(estado)
    print("Sin novedades." if _enviados[0] == 0 else f"Revisión terminada ({_enviados[0]} aviso(s) enviado(s)).")


if __name__ == "__main__":
    main()
