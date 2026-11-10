"""
MONITOR TRENDY - Calendario de adviento "Luxury"
================================================
Se ejecuta UNA vez por llamada (GitHub Actions lo lanza cada ~10 min) y:

 1. Busca productos cuyo nombre/enlace contenga las PALABRAS clave.
 2. Busca banners, enlaces e imágenes con esas palabras en la portada.
 3. Avisa si aparece en la portada un BANNER NUEVO (aunque no tenga las
    palabras clave), por si el calendario se anuncia con otro nombre.
 4. Sigue el ESTADO del calendario: si aparece pero "No disponible", te avisa en
    cuanto pase a disponible (y repite el aviso varias veces).
 5. Vigila los ENLACES DIRECTOS del calendario (PRODUCTOS_DIRECTOS), aunque la tienda
    los oculte de las búsquedas: avisa si responden, si dejan de responder o si
    pasan a disponible.
    También lee el bloque principal de esa página, te manda UN informe de lo que ve
    y te avisa si ese bloque cambia (aunque no entienda el texto nuevo).
 6. Te avisa si él mismo deja de funcionar (páginas que no cargan, 0 productos
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

# Nueva colección ordenada por fecha de lanzamiento (lo más reciente primero),
# para que un lanzamiento nuevo quede entre los productos que se leen
NUEVA = f"{BASE}/nueva-coleccion?order=OrderByReleaseDateDESC"

# Páginas donde se buscan productos
PAGINAS = [
    f"{BASE}/luxury?_q=luxury&map=ft",
    f"{BASE}/adviento?_q=adviento&map=ft",
    f"{BASE}/calendario?_q=calendario&map=ft",
    NUEVA,
    f"{BASE}/",
]
# Páginas donde se buscan banners / enlaces / imágenes
PAGINAS_BANNER = [f"{BASE}/"]
# Páginas que SIEMPRE deben mostrar productos (si salen con 0, algo falla)
PAGINAS_CON_PRODUCTOS = [NUEVA, f"{BASE}/"]

# Enlaces DIRECTOS al calendario: se vigilan aunque la tienda los oculte de las
# búsquedas. El primero es el principal (el que ya sabes que existe); los demás
# son variantes probables y solo avisan si llegan a existir.
PRODUCTOS_DIRECTOS = [
    f"{BASE}/calendario-de-adviento-2026-wonderland-b/p",
    f"{BASE}/calendario-de-adviento-2026-wonderland-a/p",
    f"{BASE}/calendario-de-adviento-2026-wonderland/p",
]

# API de búsqueda de VTEX (extra; si no responde se ignora)
API = f"{BASE}/api/catalog_system/pub/products/search?ft="
TERMINOS_API = ["luxury", "adviento", "calendario"]

# Palabras clave (ignora mayúsculas, tildes y %20)
PALABRAS = ["luxury", "adviento", "calendario", "advent", "wonderland"]
# Enlaces que se ignoran (evita falsos avisos, p. ej. "Términos Calendario 2026")
EXCLUIR = ["terminos", "condiciones", "politica", "privacidad", "habeas",
           "servicio-al-cliente", "enlaces-de-interes", "pqr"]

AVISAR_BANNERS_NUEVOS = True      # avisa si aparece un banner nuevo en la portada
AVISAR_PRODUCTOS_NUEVOS = True   # avisa de CUALQUIER producto nuevo en Nueva colección
LATIDO_HORA = 8                   # mensaje diario "sigo activo" desde esta hora (Colombia); None = desactivado
MAX_AVISOS_NOVEDAD = 5            # máximo de avisos de novedad por ejecución
DIAGNOSTICO_DIRECTO = True       # envía UNA vez un informe de lo que ve en la página directa
MAX_AVISOS_CAMBIO = 5             # máximo de avisos "la página cambió" por enlace
REPETIR_DISPONIBLE = 6            # cuántas veces repite el aviso "¡DISPONIBLE!" (una por revisión)

# Cómo saber si un producto se puede comprar (se busca en el texto de su tarjeta)
NEGATIVOS = ["no disponible", "agotado", "sin stock", "sin existencias", "sold out"]
POSITIVOS = ["agregar al carrito", "anadir al carrito", "comprar", "add to cart"]
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
    "banners_vistos": None, "productos_vistos": None, "fuente_nueva": "",
    "ultimo_latido": "", "disponibilidad": {}, "alertas_disp": {}, "directos": {},
    "diag_directo": {}, "huella_directo": {},
}
PRIORIDAD = {"desconocido": 0, "no_disponible": 1, "disponible": 2}
ETIQUETA = {"disponible": "DISPONIBLE ✅", "no_disponible": "No disponible todavía ⏳",
            "desconocido": "estado no determinado"}


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
    for k in ("fallos_pagina", "disponibilidad", "alertas_disp", "directos", "diag_directo", "huella_directo"):
        if not isinstance(e.get(k), dict):
            e[k] = {}
    return e


def guardar_estado(e: dict) -> None:
    e["avisados"] = e["avisados"][-500:]
    for k in ("banners_vistos", "productos_vistos"):
        if isinstance(e.get(k), list):
            e[k] = e[k][-1000:]
    for k in ("disponibilidad", "alertas_disp"):
        if len(e.get(k, {})) > 200:
            e[k] = dict(list(e[k].items())[-200:])
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


def disponibilidad_texto(texto: str) -> str:
    """'disponible' / 'no_disponible' / 'desconocido' según el texto de la tarjeta."""
    t = norm(texto)
    if any(n in t for n in NEGATIVOS):
        return "no_disponible"
    if any(p in t for p in POSITIVOS):
        return "disponible"
    return "desconocido"


def texto_tarjeta(a) -> str:
    """Texto de la 'tarjeta' del producto (incluye el botón Agregar/No disponible).
    Sube por el HTML mientras la caja siga conteniendo UN solo producto."""
    nodo = mejor = a
    for _ in range(8):
        padre = nodo.parent
        if padre is None or padre.name in ("body", "html", "[document]"):
            break
        hrefs = {re.split(r"[?#]", x["href"])[0]
                 for x in padre.find_all("a", href=True) if RE_PROD.search(x["href"])}
        if len(hrefs) > 1 or len(padre.get_text(" ", strip=True)) > 400:
            break
        nodo = mejor = padre
    return mejor.get_text(" ", strip=True)


def extraer_productos(html: str) -> dict:
    """{url_producto: {nombre, full, disp, tarjeta}} de TODOS los productos."""
    soup = BeautifulSoup(html, "html.parser")
    res = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not RE_PROD.search(href):
            continue
        texto = a.get_text(" ", strip=True)
        alts = " ".join(i.get("alt", "") for i in a.find_all("img"))
        clave = urljoin(BASE, re.split(r"[?#]", href)[0])
        tarjeta = texto_tarjeta(a)
        nuevo = {
            "nombre": limpiar_nombre(texto or alts) or "",
            "full": f"{href} {texto} {alts}",
            "disp": disponibilidad_texto(tarjeta),
            "tarjeta": tarjeta[:160],
        }
        if clave not in res:
            res[clave] = nuevo
            continue
        previo = res[clave]
        previo["full"] += " " + nuevo["full"]
        if not previo["nombre"]:
            previo["nombre"] = nuevo["nombre"]
        if PRIORIDAD[nuevo["disp"]] > PRIORIDAD[previo["disp"]]:
            previo["disp"], previo["tarjeta"] = nuevo["disp"], nuevo["tarjeta"]
    for clave, p in res.items():
        p["nombre"] = p["nombre"] or clave[-80:]
    return res


def fusionar(destino: dict, url: str, p: dict) -> None:
    """Junta dos lecturas del mismo producto; gana la más 'disponible'."""
    if url not in destino:
        destino[url] = dict(p)
    elif PRIORIDAD[p["disp"]] > PRIORIDAD[destino[url]["disp"]]:
        destino[url]["disp"], destino[url]["tarjeta"] = p["disp"], p["tarjeta"]


def disponibilidad_en_pagina_producto(url: str) -> str:
    """Respaldo: lee el dato 'availability' (InStock/OutOfStock) de la página del producto."""
    try:
        html = descargar(url)
    except Exception:
        return "desconocido"
    t = " ".join(re.findall(r'"availability"\s*:\s*"([^"]+)"', html)).lower()
    if "instock" in t:
        return "disponible"
    if "outofstock" in t or "soldout" in t or "discontinued" in t:
        return "no_disponible"
    return "desconocido"


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


def hay_stock(p: dict) -> bool:
    """True si algún vendedor del producto (JSON de la API de VTEX) tiene unidades."""
    for it in p.get("items") or []:
        for sel in it.get("sellers") or []:
            of = sel.get("commertialOffer") or {}
            if (of.get("AvailableQuantity") or 0) > 0 and of.get("IsAvailable", True):
                return True
    return False


def productos_en_api() -> dict:
    """{url: {nombre, disp, tarjeta}} desde la API de VTEX (extra; si falla se ignora)."""
    hallados = {}
    for term in TERMINOS_API:
        try:
            r = requests.get(f"{API}{quote(term)}&_from=0&_to=49", headers=HEADERS, timeout=20)
            if r.status_code != 200:
                continue
            for p in r.json():
                nombre = p.get("productName", "")
                link = p.get("link") or ""
                if not coincide(f"{nombre} {link}"):
                    continue
                hay = hay_stock(p)
                clave = urljoin(BASE, re.split(r"[?#]", link)[0])
                fusionar(hallados, clave, {
                    "nombre": nombre, "full": f"{link} {nombre}",
                    "disp": "disponible" if hay else "no_disponible",
                    "tarjeta": "(según la API de la tienda)"})
        except Exception:
            continue
    return hallados


# Compatibilidad con los scripts probar_*.py
def productos_en_html(url: str) -> dict:
    return {u: p["nombre"] for u, p in extraer_productos(descargar(url)).items()
            if coincide(p["full"])}


def anuncios_en_html(url: str) -> dict:
    return extraer_anuncios(descargar(url))


# -------------------------------------------------------- enlaces directos
def revisar_directo(url: str) -> dict:
    """Lee la página directa del producto y su estado por varias vías."""
    slug = url.rstrip("/").split("/")[-2]
    info = {"http": 0, "disp": "desconocido", "nombre": slug, "fuentes": "", "html": ""}
    html = ""
    for _ in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            info["http"] = r.status_code
            if r.status_code == 200:
                html = r.text
            if r.status_code < 500:
                break
        except Exception:
            info["http"] = 0
        time.sleep(2)
    info["html"] = html
    senales = []
    if html:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        if m and m.group(1).strip():
            info["nombre"] = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
        t = " ".join(re.findall(r'"availability"\s*:\s*"([^"]+)"', html)).lower()
        if "instock" in t:
            senales.append(("disponible", "página"))
        elif any(x in t for x in ("outofstock", "soldout", "discontinued")):
            senales.append(("no_disponible", "página"))
    try:
        r = requests.get(f"{BASE}/api/catalog_system/pub/products/search/{slug}/p",
                         headers=HEADERS, timeout=20)
        if r.status_code == 200:
            lista = r.json()
            if isinstance(lista, list) and lista:
                senales.append(("disponible" if hay_stock(lista[0]) else "no_disponible", "API"))
    except Exception:
        pass
    if senales:
        de_pagina = [d for d, f in senales if f == "página"]
        # Lo que dice la página del producto manda sobre la API
        info["disp"] = de_pagina[0] if de_pagina else max(
            (d for d, _ in senales), key=lambda d: PRIORIDAD[d])
    info["fuentes"] = ", ".join(f"{f}={d}" for d, f in senales) or "sin datos legibles"
    return info


def bloque_principal(html: str) -> str:
    """Texto del bloque del producto: sube desde el título (h1) hasta el primer
    contenedor que tenga un botón (donde suelen estar precio y compra)."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not h1:
        return ""
    nodo = h1
    for _ in range(8):
        padre = nodo.parent
        if padre is None or padre.name in ("body", "html", "[document]"):
            break
        nodo = padre
        if padre.find("button"):
            break
    return re.sub(r"\s+", " ", nodo.get_text(" ", strip=True))[:400]


def diagnostico_pagina(html: str, bloque: str) -> str:
    """Resumen de lo que contiene la página (para ajustar la lectura)."""
    def n(patron):
        return len(re.findall(patron, html, re.I))
    soup = BeautifulSoup(html, "html.parser")
    botones = []
    for b in soup.find_all("button"):
        x = re.sub(r"\s+", " ", b.get_text(" ", strip=True))[:40]
        if x and x not in botones:
            botones.append(x)
    cant = sorted(set(re.findall(r'"AvailableQuantity"\s*:\s*(\d+)', html)))[:6]
    n_disp = n('"availability"')
    n_ld = n("application/ld.json")
    return (f"Tamaño: {len(html)} caracteres\n"
            f"JSON-LD: {n_ld} | availability: {n_disp}\n"
            f"AvailableQuantity: {n('AvailableQuantity')} (valores {cant})\n"
            f"'No disponible': {n('no disponible')} | 'Agotado': {n('agotado')} | "
            f"'Agregar al carrito': {n('agregar al carrito')} | 'Comprar': {n('comprar')} | "
            f"'Avísame': {n('av[ií]same')}\n"
            f"Botones: {botones[:10]}\n"
            f"Bloque principal: {bloque[:220]}")


def completar_directo(estado: dict, url: str, info: dict, principal: bool) -> None:
    """Lee el bloque principal de la página, manda el informe (una vez) y avisa
    si ese bloque cambia (por ejemplo, 'No Disponible' -> 'Agregar al carrito')."""
    html = info.get("html") or ""
    if info["http"] != 200 or not html:
        return
    bloque = bloque_principal(html)
    d_bloque = disponibilidad_texto(bloque) if bloque else "desconocido"
    if bloque:
        info["fuentes"] += f"{', ' if info['fuentes'] and info['fuentes'] != 'sin datos legibles' else ''}bloque={d_bloque}"
        if info["fuentes"].startswith("sin datos legibles, "):
            info["fuentes"] = info["fuentes"][len("sin datos legibles, "):]
    if info["disp"] == "desconocido" and d_bloque != "desconocido":
        info["disp"] = d_bloque                       # solo si no hay datos mejores
    if DIAGNOSTICO_DIRECTO and principal and url not in estado["diag_directo"]:
        telegram(f"🔬 Informe de la página del calendario (para ajustar el monitor)\n"
                 f"{diagnostico_pagina(html, bloque)}")
        estado["diag_directo"][url] = True
    # Aviso ante cualquier cambio del bloque, aunque no se entienda el texto
    if bloque:
        h = estado["huella_directo"].get(url)
        if h is None:
            estado["huella_directo"][url] = {"texto": bloque, "avisos": 0}
        elif h["texto"] != bloque:
            if h["avisos"] < MAX_AVISOS_CAMBIO:
                telegram(f"🔔 La página del calendario CAMBIÓ:\n{url}\n"
                         f"Antes: {h['texto'][:150]}\nAhora: {bloque[:150]}")
                h["avisos"] += 1
            h["texto"] = bloque


def avisos_directo(estado: dict, url: str, info: dict, principal: bool) -> None:
    """Avisa cuando el enlace directo empieza o deja de responder."""
    http = info["http"]
    if http == 0 or http >= 500:
        return                                    # fallo pasajero: no se toma como cambio
    ok = http == 200
    prev = estado["directos"].get(url)
    if prev is None:
        if principal or ok:
            telegram(f"🔎 Enlace directo del calendario\n{url}\nRespuesta de la tienda: HTTP {http}\n"
                     f"Estado: {ETIQUETA[info['disp']]}\nSeñales: {info['fuentes']}")
    else:
        antes_ok = prev.get("http") == 200
        if ok and not antes_ok:
            telegram(f"🔄 El enlace directo ahora RESPONDE (antes: HTTP {prev.get('http')}).\n"
                     f"{url}\nEstado: {ETIQUETA[info['disp']]}")
        elif antes_ok and not ok:
            telegram(f"⚠️ El enlace directo dejó de responder (HTTP {http}). ¿Lo retiraron?\n{url}\n"
                     "Sigo vigilando por si vuelve.")
    estado["directos"][url] = {"http": http}


def procesar_disponibilidad(estado: dict, url: str, p: dict) -> None:
    """Máquina de estados: avisa de apariciones y de cambios de disponibilidad."""
    nombre = p["nombre"]
    disp = p["disp"]
    if disp == "desconocido":
        disp = disponibilidad_en_pagina_producto(url)   # respaldo: página del producto
    p["disp"] = disp
    previo = estado["disponibilidad"].get(url)
    if disp == "disponible":
        if previo != "disponible":
            estado["alertas_disp"][url] = 0              # empieza un ciclo de avisos
        n = estado["alertas_disp"].get(url, 0)
        if n < REPETIR_DISPONIBLE:
            telegram(f"🚨🚨 ¡DISPONIBLE AHORA! 🚨🚨\n{nombre}\n{url}\n"
                     f"(aviso {n + 1} de {REPETIR_DISPONIBLE})\nTarjeta: {p['tarjeta'][:100]}")
            estado["alertas_disp"][url] = n + 1
            print(f"Aviso DISPONIBLE: {nombre} -> {url}")
        if url not in estado["avisados"]:
            estado["avisados"].append(url)
    else:
        if url not in estado["avisados"]:
            telegram(f"🎄 ¡Posible calendario de adviento!\n{nombre}\n"
                     f"Estado: {ETIQUETA[disp]}\n{url}\n"
                     "Te avisaré en cuanto esté disponible.")
            estado["avisados"].append(url)
            print(f"Aviso de producto: {nombre} -> {url}")
        elif disp == "no_disponible" and previo == "disponible":
            telegram(f"ℹ️ Volvió a 'No disponible' (se agotó o se retiró):\n{nombre}\n{url}\n"
                     "Seguiré vigilando por si vuelve a aparecer.")
    if disp != "desconocido":
        estado["disponibilidad"][url] = disp


# ---------------------------------------------------------------------- main
def main() -> None:
    if "--test" in sys.argv:
        telegram("✅ Prueba: tu monitor de Trendy puede enviarte mensajes.")
        print("Mensaje de prueba enviado.")
        return

    estado = cargar_estado()
    if estado.get("fuente_nueva") != NUEVA:
        # Cambió la página de Nueva colección (o su orden): se vuelve a memorizar
        # en silencio para no recibir avisos falsos de productos que ya existían.
        estado["productos_vistos"] = None
        estado["fuente_nueva"] = NUEVA
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
        for u, p in prods.items():
            if coincide(p["full"]):
                fusionar(hallados, u, p)
    # La API solo se usa si la página no mostró el producto, o si su tarjeta no
    # se pudo leer: lo que ves en la tarjeta ("No Disponible") manda sobre la API.
    for u, p in productos_en_api().items():
        if u not in hallados:
            hallados[u] = dict(p)
        elif hallados[u]["disp"] == "desconocido":
            hallados[u]["disp"], hallados[u]["tarjeta"] = p["disp"], p["tarjeta"]

    # 3) Salud: las páginas con productos no deben salir vacías
    vacias = [u for u in PAGINAS_CON_PRODUCTOS if u in productos_por_pagina
              and not productos_por_pagina[u]]
    estado["ceros"] = estado["ceros"] + 1 if vacias else 0
    if vacias and debe_alertar(estado["ceros"], 3):
        avisar_seguro(f"⚠️ Llevo {estado['ceros']} revisiones seguidas leyendo 0 productos en:\n"
                      + "\n".join(vacias) + "\n(¿la tienda cambió o bloquea las lecturas?)")

    # 4) Enlaces directos + avisos de productos y su DISPONIBILIDAD
    candidatos = dict(hallados)
    for i, durl in enumerate(PRODUCTOS_DIRECTOS):
        info = revisar_directo(durl)
        completar_directo(estado, durl, info, principal=(i == 0))
        avisos_directo(estado, durl, info, principal=(i == 0))
        if info["http"] == 200 or info["disp"] != "desconocido":
            fusionar(candidatos, durl, {"nombre": info["nombre"], "full": durl,
                                        "disp": info["disp"], "tarjeta": info["fuentes"]})
    for url, p in candidatos.items():
        procesar_disponibilidad(estado, url, p)

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
    nueva = NUEVA
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
                telegram(f"🆕 Producto nuevo en Nueva colección:\n{actuales[u]['nombre']}\n{u}")
                enviados += 1

    # 8) Mensaje diario "sigo activo"
    if LATIDO_HORA is not None:
        ahora = ahora_colombia()
        hoy = ahora.strftime("%Y-%m-%d")
        if ahora.hour >= LATIDO_HORA and estado["ultimo_latido"] != hoy:
            n_portada = len(productos_por_pagina.get(portada, {}))
            n_nueva = len(productos_por_pagina.get(nueva, {}))
            lineas = "".join(f"\n• {p['nombre'][:45]}: {ETIQUETA[p['disp']]}" for p in candidatos.values())
            if avisar_seguro(f"✅ Monitor Trendy activo ({hoy}).\n"
                             f"Leído ahora: {n_portada} productos en portada, {n_nueva} en Nueva "
                             f"colección, {n_banners} banners.\nBuscando: {', '.join(PALABRAS)}\n"
                             f"Productos con tus palabras:{lineas or ' ninguno'}"):
                estado["ultimo_latido"] = hoy

    guardar_estado(estado)
    print("Sin novedades." if _enviados[0] == 0 else f"Revisión terminada ({_enviados[0]} aviso(s) enviado(s)).")


if __name__ == "__main__":
    main()
