# ===== VERSIÓN DE PRUEBA: PRODUCTOS (después restaura monitor.py normal) =====
"""
Revisa la tienda Trendy UNA vez por ejecución (GitHub Actions la lanza
cada 10 minutos). Si aparece un producto de "calendario de adviento
luxury", avisa por Telegram. Recuerda lo ya avisado en estado.json.

Uso:  python monitor.py          -> revisión normal
      python monitor.py --test   -> solo envía un mensaje de prueba
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

BASE = "https://www.maquillajetrendyshop.com"

# Páginas a vigilar (agrega o quita las que quieras)
PAGINAS = [
    f"{BASE}/luxury?_q=luxury&map=ft",
    f"{BASE}/adviento?_q=adviento&map=ft",
    f"{BASE}/calendario?_q=calendario&map=ft",
    f"{BASE}/nueva-coleccion",
    f"{BASE}/",
]
# API de búsqueda de VTEX (opcional; si no responde, se ignora)
API = f"{BASE}/api/catalog_system/pub/products/search?ft="
TERMINOS_API = ["luxury", "adviento", "calendario"]

# Un producto cuenta si su nombre/enlace contiene alguna de estas palabras.
# El calendario se llamará "Luxury", así que es la principal.
# Si recibes demasiados avisos de otros productos, deja solo la lista
# de abajo con las palabras más específicas (por ejemplo "calendario luxury").
PALABRAS = ["hollywood"]  # PRUEBA: producto "Sombra Hollywood"

def coincide(texto: str) -> bool:
    t = texto.lower()
    return any(p in t for p in PALABRAS)

# Páginas donde se buscan también banners/enlaces/imágenes (no solo productos)
PAGINAS_BANNER = []  # PRUEBA: banners desactivados
# Enlaces que se ignoran en la búsqueda de banners (evita falsos avisos,
# por ejemplo el enlace "Términos Calendario 2026" del pie de página)
EXCLUIR = ["terminos", "términos", "politica", "política", "privacidad",
           "condiciones", "servicio-al-cliente", "habeas", "pqr"]

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ESTADO = "estado.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "es-CO,es;q=0.9",
}


def hora_colombia() -> str:
    """Hora actual en Colombia (UTC-5, sin horario de verano)."""
    return datetime.now(timezone(timedelta(hours=-5))).strftime("%H:%M:%S")


def telegram(msg: str) -> None:
    msg = f"{msg}\n🕒 Enviado a las {hora_colombia()} (Colombia)"
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": msg},
        timeout=15,
    )
    r.raise_for_status()


def cargar_estado() -> dict:
    try:
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"avisados": [], "errores": 0}


def guardar_estado(e: dict) -> None:
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(e, f, ensure_ascii=False, indent=2)


def productos_en_html(url: str) -> dict:
    """Devuelve {url_producto: nombre} de enlaces a productos que coinciden."""
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    hallados = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # En VTEX los productos terminan en /p
        if not re.search(r"/p(\?|$)", href):
            continue
        texto = a.get_text(" ", strip=True)
        if coincide(href + " " + texto):
            hallados[urljoin(BASE, href.split("?")[0])] = texto[:80] or href
    return hallados


def anuncios_en_html(url: str) -> dict:
    """Busca CUALQUIER enlace o imagen (banner, pop-up, menú) cuyo
    enlace, texto, título o 'alt' contenga las palabras clave.
    Devuelve {clave: descripción}."""
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    hallados = {}

    def excluido(t: str) -> bool:
        t = t.lower()
        return any(x in t for x in EXCLUIR)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        texto = a.get_text(" ", strip=True)
        partes = [href, texto, a.get("title", ""), a.get("aria-label", "")]
        for img in a.find_all("img"):
            partes += [img.get("alt", ""), img.get("src", "")]
        todo = " ".join(partes)
        if not excluido(todo) and coincide(todo):
            nombre = texto or a.get("title", "") or "banner/enlace"
            hallados[urljoin(BASE, href)] = nombre[:80]

    for img in soup.find_all("img"):
        alt, src = img.get("alt", ""), img.get("src", "")
        todo = alt + " " + src
        if not excluido(todo) and coincide(todo):
            hallados[urljoin(BASE, src)] = (alt or "imagen")[:80]
    return hallados


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
                if coincide(nombre + " " + link):
                    hallados[urljoin(BASE, link)] = nombre
        except Exception:
            continue  # la API es un extra; si falla no pasa nada
    return hallados


def main() -> None:
    if "--test" in sys.argv:
        telegram("✅ Prueba: tu monitor de Trendy puede enviarte mensajes.")
        print("Mensaje de prueba enviado.")
        return

    estado = cargar_estado()
    hallados, fallos = {}, 0

    for url in PAGINAS:
        try:
            hallados.update(productos_en_html(url))
        except Exception as e:
            fallos += 1
            print(f"[!] Error en {url}: {e}")
    hallados.update(productos_en_api())

    anuncios = {}
    for url in PAGINAS_BANNER:
        try:
            anuncios.update(anuncios_en_html(url))
        except Exception as e:
            print(f"[!] Error buscando banners en {url}: {e}")

    # Si TODAS las páginas fallaron, cuenta un error; si no, reinicia
    estado["errores"] = estado.get("errores", 0) + 1 if fallos == len(PAGINAS) else 0
    if estado["errores"] == 6:
        telegram("⚠️ El monitor de Trendy lleva 6 revisiones seguidas con error "
                 "(¿bloqueo de IP o cambio en la web?). Revisa GitHub Actions.")

    nuevos = {u: n for u, n in hallados.items() if u not in estado["avisados"]}
    for url, nombre in nuevos.items():
        telegram(f"🧪 PRUEBA de PRODUCTOS (todo funciona)\n{nombre}\n{url}")
        estado["avisados"].append(url)
        print(f"Aviso enviado: {nombre} -> {url}")

    nuevos_anuncios = {u: n for u, n in anuncios.items()
                       if u not in estado["avisados"] and u not in nuevos}
    for url, nombre in nuevos_anuncios.items():
        telegram(f"🔔 Posible anuncio/banner con tu palabra clave:\n{nombre}\n{url}")
        estado["avisados"].append(url)
        print(f"Aviso de banner enviado: {nombre} -> {url}")

    if not nuevos and not nuevos_anuncios:
        print("Sin novedades.")
    guardar_estado(estado)


if __name__ == "__main__":
    main()
