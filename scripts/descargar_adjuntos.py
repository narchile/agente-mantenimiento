#!/usr/bin/env python3
"""Descarga las imágenes adjuntas de un ticket de Jira (mejor esfuerzo).

Uso:
    python3 scripts/descargar_adjuntos.py TICKET [--destino DIR]

Usa JIRA_URL (https), JIRA_USERNAME y JIRA_API_TOKEN del entorno. Por cada imagen descargada imprime
una línea `<ruta absoluta>: <nombre original>`; si no hay imágenes imprime
`ADJUNTOS: ninguna imagen adjunta en el ticket`, y si no se pudo consultar Jira
`ADJUNTOS: no disponibles (<motivo>)`. El código de salida es 0 en esos casos; solo un uso inválido
(p. ej. un ID de ticket mal formado) termina con código distinto de cero. El token nunca se imprime.

Lo usan tanto agente_mantenimiento.py (importando `descargar_adjuntos`) como el sub-agente de Claude Code.
"""

import argparse
import base64
import http.client
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Límites de la descarga de imágenes adjuntas.
MAX_IMAGENES = 10
MAX_BYTES_IMAGEN = 10 * 1024 * 1024
TIMEOUT_HTTP = 20
EXTENSIONES_IMAGEN = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
EXTENSION_POR_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}


class _RedireccionSinAuth(urllib.request.HTTPRedirectHandler):
    """Sigue redirecciones pero no reenvía Authorization a otro host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        nuevo = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nuevo is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for nombre in list(nuevo.headers):
                if nombre.lower() == "authorization":
                    del nuevo.headers[nombre]
            nuevo.unredirected_hdrs.pop("Authorization", None)
        return nuevo


def _abrir(req: urllib.request.Request, timeout: float = TIMEOUT_HTTP):
    return urllib.request.build_opener(_RedireccionSinAuth()).open(req, timeout=timeout)


def _extension(nombre: str, mime: str) -> str | None:
    ext = Path(nombre).suffix.lstrip(".").lower()
    return ext if ext in EXTENSIONES_IMAGEN else EXTENSION_POR_MIME.get(mime.lower())


def _motivo_error(error: Exception) -> str:
    """Motivo corto, sin URL completa ni credenciales."""
    if isinstance(error, urllib.error.HTTPError):
        return f"Jira respondió HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return "no se pudo conectar con Jira (¿VPN?)"
    if isinstance(error, http.client.HTTPException):
        return "respuesta incompleta o inválida de Jira"
    if isinstance(error, (TimeoutError, OSError)):
        return "error de red o de disco al descargar adjuntos"
    return "respuesta inválida de Jira"


def descargar_adjuntos(ticket: str, destino: Path) -> tuple[list[tuple[Path, str]], str | None]:
    """Descarga las imágenes adjuntas del ticket en `destino` (mejor esfuerzo).

    Devuelve ([(ruta local, nombre original)], motivo_no_disponible o None). Solo usa GET sobre https;
    los archivos se guardan como <id><ext>, nunca con el nombre remoto. No imprime el token.
    """
    url = os.environ.get("JIRA_URL", "").strip().rstrip("/")
    usuario = os.environ.get("JIRA_USERNAME", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    if not (url and usuario and token):
        return [], "faltan variables JIRA_URL/JIRA_USERNAME/JIRA_API_TOKEN"
    if urllib.parse.urlsplit(url).scheme != "https":
        return [], "JIRA_URL debe usar https"
    netloc_jira = urllib.parse.urlsplit(url).netloc
    cabeceras = {
        "Authorization": "Basic " + base64.b64encode(f"{usuario}:{token}".encode()).decode(),
        "Accept": "application/json",
    }

    def pedir(destino_url: str) -> urllib.request.Request:
        return urllib.request.Request(destino_url, headers=cabeceras, method="GET")

    try:
        with _abrir(pedir(f"{url}/rest/api/3/issue/{urllib.parse.quote(ticket)}?fields=attachment"), TIMEOUT_HTTP) as r:
            adjuntos = json.loads(r.read(5 * 1024 * 1024).decode("utf-8")).get("fields", {}).get("attachment") or []
        if not isinstance(adjuntos, list):
            raise ValueError("attachment")
    except (urllib.error.URLError, OSError, ValueError, AttributeError, TypeError, http.client.HTTPException) as error:
        return [], _motivo_error(error)

    descargados: list[tuple[Path, str]] = []
    for adj in adjuntos:
        if len(descargados) >= MAX_IMAGENES:
            break
        try:
            mime = str(adj.get("mimeType", ""))
            nombre = str(adj.get("filename", ""))
            id_ = re.sub(r"[^A-Za-z0-9_-]", "", str(adj.get("id", "")))
            contenido = str(adj.get("content", ""))
            ext = _extension(nombre, mime)
            if not mime.lower().startswith("image/") or not id_ or not ext or not contenido:
                continue
            try:
                tamano = int(adj.get("size", 0))
            except (TypeError, ValueError):
                tamano = 0  # desconocido: el tope de bytes al leer sigue aplicando
            if tamano > MAX_BYTES_IMAGEN:
                continue
            destino_contenido = urllib.parse.urlsplit(contenido)
            if destino_contenido.scheme != "https" or destino_contenido.netloc != netloc_jira:
                continue  # nunca enviar credenciales a otro host o por http
            with _abrir(pedir(contenido), TIMEOUT_HTTP) as r:
                datos = r.read(MAX_BYTES_IMAGEN + 1)
            if len(datos) > MAX_BYTES_IMAGEN:
                continue
            ruta = destino / f"{id_}.{ext}"
            ruta.write_bytes(datos)
            descargados.append((ruta, nombre))
        except (urllib.error.URLError, OSError, ValueError, AttributeError, TypeError, http.client.HTTPException):
            continue
    return descargados, None


# Claves de Jira: letras y dígitos, guion y número (ej: PROJ-123). Estricto: el valor viaja a una URL.
TICKET_CLI_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")


def _ticket(valor: str) -> str:
    if not TICKET_CLI_RE.fullmatch(valor):
        raise argparse.ArgumentTypeError(f"'{valor}' no es un ID de ticket válido (ej: PROJ-123)")
    return valor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="descargar_adjuntos.py",
        description="Descarga las imágenes adjuntas de un ticket de Jira (requiere JIRA_URL, JIRA_USERNAME y JIRA_API_TOKEN).",
    )
    parser.add_argument("ticket", type=_ticket, help="ID del ticket (ej: PROJ-123)")
    parser.add_argument("--destino", type=Path, help="directorio de descarga (por defecto, un directorio temporal nuevo)")
    args = parser.parse_args(argv)

    try:
        destino = args.destino.expanduser() if args.destino else Path(tempfile.mkdtemp(prefix="agente_adjuntos_"))
        destino.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"ADJUNTOS: no disponibles ({_motivo_error(error)})")
        return 0
    destino = destino.resolve()

    descargados, motivo = descargar_adjuntos(args.ticket, destino)
    if motivo:
        print(f"ADJUNTOS: no disponibles ({motivo})")
    elif not descargados:
        print("ADJUNTOS: ninguna imagen adjunta en el ticket")
    else:
        for ruta, nombre in descargados:
            print(f"{ruta}: {nombre}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
