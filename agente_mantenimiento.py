#!/usr/bin/env python3
"""Agente de Mantenimiento y QA.

Uso:
    python agente_mantenimiento.py <ID_TICKET> --repo <RUTA> [<RUTA> ...] [--salida DIR] [--modelo M]
    python agente_mantenimiento.py <ID_TICKET>    # sin repositorios locales

Valida las entradas, calcula la ruta de salida y delega el análisis a Claude Code
en modo headless (`claude -p`), usando como system prompt el archivo
`agents/agente-mantenimiento.md`. Los MCPs (Jira, CodeGraph, Context7)
son los ya configurados en Claude Code.

`--repo` acepta una o más rutas a repositorios locales (también puede repetirse). Sin `--repo` el agente
no analiza código local y el reporte de desarrollo es menos detallado.

Imágenes adjuntas (opcional, mejor esfuerzo): si están definidas JIRA_URL (https), JIRA_USERNAME y
JIRA_API_TOKEN, el script (vía scripts/descargar_adjuntos.py) descarga las imágenes del ticket por la API REST de Jira y se las pasa al agente.
Sin ellas, o si la descarga falla, el análisis continúa y el reporte indica que no se revisaron las imágenes.
"""

import argparse
import itertools
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# descargar_adjuntos.py vive en scripts/ para que el plugin lo empaquete junto al agente y lo use como CLI.
sys.path.insert(0, str(BASE_DIR / "scripts"))

from descargar_adjuntos import descargar_adjuntos  # noqa: E402

PROMPT_FILE = BASE_DIR / "agents" / "agente-mantenimiento.md"
PLANTILLAS_DIR = BASE_DIR / "plantillas"

# Valores por defecto de las opciones --salida, --modelo y --comando-indexado.
SALIDA_POR_DEFECTO = Path("analisis")  # relativo al directorio de trabajo actual
MODELO_POR_DEFECTO = "claude-sonnet-5-5"
# Por defecto se usa el CLI oficial de CodeGraph; otro indexador se indica con --comando-indexado.
COMANDO_INDEXADO_POR_DEFECTO = "codegraph init -y {ruta}"

# Marcador que el prompt (sección "Modo sin Repositorio Local") reconoce literalmente:
# si se cambia aquí, hay que cambiarlo también en agents/agente-mantenimiento.md.
MODO_SIN_REPO = "Modo de análisis: SIN REPO LOCAL"

# Intérpretes y envoltorios que no pueden ser el ejecutable del comando de indexado:
# el permiso de Bash se deriva de su prefijo y convertiría al agente en un shell abierto.
INTERPRETES_PROHIBIDOS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "env", "eval", "exec", "sudo", "xargs", "nohup"}
CARACTERES_SHELL = set(";&|<>`$\n\\")

TICKET_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def herramientas(jira_mcp: str, comando_indexado: str) -> tuple[list[str], list[str]]:
    """Herramientas preautorizadas y denegadas.

    El modo headless no puede pedir permisos: se preautoriza solo lo que el prompt
    necesita. Todo lo demás (incluidas las escrituras en Jira) queda denegado. El permiso del
    indexado se deriva del texto anterior a {ruta} (ver validar_comando_indexado).
    """
    prefijo_indexado = comando_indexado.split("{ruta}")[0].strip()
    permitidas = [
        f"mcp__{jira_mcp}__jira_get_issue",
        f"mcp__{jira_mcp}__jira_search",
        "mcp__codegraph__*",
        "mcp__context7__*",
        "Read",
        "Glob",
        "Grep",
        "Write",
        "Bash(git:*)",
        "Bash(date:*)",
        "Bash(ls:*)",
        "Bash(mkdir:*)",
        f"Bash({prefijo_indexado}:*)",
    ]
    denegadas = [
        f"mcp__{jira_mcp}__jira_add_comment",
        f"mcp__{jira_mcp}__jira_create_issue",
        f"mcp__{jira_mcp}__jira_transition_issue",
        f"mcp__{jira_mcp}__jira_update_issue",
        "Edit",
        "NotebookEdit",
    ]
    return permitidas, denegadas


def fallar(mensaje: str) -> None:
    print(f"ERROR: {mensaje}", file=sys.stderr)
    sys.exit(1)


def validar_jira_mcp(nombre: str) -> None:
    """El nombre se interpola en identificadores de herramientas (mcp__<nombre>__...)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", nombre):
        fallar(f"--jira-mcp '{nombre}' no es un nombre de servidor válido (solo letras, números, '-' y '_')")


def validar_comando_indexado(comando: str) -> None:
    """Falla si la plantilla no permite derivar un permiso de Bash acotado.

    El permiso preautorizado es `Bash(<texto antes de {ruta}>:*)`, así que ese prefijo
    debe ser un comando concreto, sin metacaracteres de shell ni intérpretes.
    """
    if comando.count("{ruta}") != 1:
        fallar("--comando-indexado debe incluir exactamente una vez el marcador {ruta}")
    if any(c in CARACTERES_SHELL for c in comando):
        fallar("--comando-indexado no puede contener metacaracteres de shell (; & | < > ` $ \\ ni saltos de línea)")
    try:
        prefijo = shlex.split(comando.split("{ruta}")[0])
    except ValueError as error:
        fallar(f"--comando-indexado tiene comillas sin cerrar ({error})")
    if not prefijo:
        fallar("--comando-indexado debe empezar con el comando a ejecutar, antes de {ruta}")
    if Path(prefijo[0]).name in INTERPRETES_PROHIBIDOS or "-c" in prefijo:
        fallar(f"--comando-indexado no puede usar '{prefijo[0]}' ni '-c': el permiso resultante sería demasiado amplio")


def validar_entradas(ticket: str, repos: list[str]) -> list[Path]:
    """Valida el ticket y cada --repo; devuelve las rutas absolutas sin duplicados."""
    if not ticket:
        fallar("falta ID_TICKET")
    if not TICKET_RE.match(ticket):
        fallar(f"ID_TICKET '{ticket}' no tiene formato válido (ej: PROJ-123)")
    rutas: list[Path] = []
    for repo in repos:
        if not repo:
            fallar("--repo no puede ser una ruta vacía")
        ruta = Path(repo).expanduser().resolve()
        if not ruta.is_dir():
            fallar(f"el repositorio '{repo}' no existe o no es un directorio ({ruta})")
        if ruta not in rutas:
            rutas.append(ruta)
    por_nombre: dict[str, Path] = {}
    for ruta in rutas:
        if ruta.name in por_nombre:
            fallar(
                f"dos repositorios comparten el nombre '{ruta.name}' ({por_nombre[ruta.name]} y {ruta}); "
                "los reportes los distinguen por nombre, usa carpetas con nombres distintos"
            )
        por_nombre[ruta.name] = ruta
    return rutas


def rutas_salida(ticket: str, directorio: Path) -> tuple[Path, Path]:
    """Par (qa, dev) con el primer sufijo libre para ambos: '', _1, _2, ..."""
    try:
        directorio.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        fallar(f"no se puede usar --salida '{directorio}': {error.strerror or error}")
    n = 0
    while True:
        sufijo = f"_{n}" if n else ""
        qa = directorio / f"analisis_qa_{ticket}{sufijo}.md"
        dev = directorio / f"analisis_dev_{ticket}{sufijo}.md"
        if not qa.exists() and not dev.exists():
            return qa, dev
        n += 1


def construir_mensaje(
    ticket: str,
    repos: list[Path],
    salida_qa: Path,
    salida_dev: Path,
    jira_mcp: str,
    comando_indexado: str,
    adjuntos: list[tuple[Path, str]] | None = None,
    motivo_adjuntos: str | None = None,
) -> str:
    if adjuntos:
        lista_adjuntos = "\n".join(f"- {ruta}: {nombre}" for ruta, nombre in adjuntos)
        bloque_adjuntos = f"ADJUNTOS (imágenes del ticket, léelas con Read):\n{lista_adjuntos}\n"
    elif adjuntos is not None and motivo_adjuntos is None:
        bloque_adjuntos = "ADJUNTOS: ninguna imagen adjunta en el ticket\n"
    else:
        bloque_adjuntos = f"ADJUNTOS: no disponibles ({motivo_adjuntos or 'no se intentó la descarga'})\n"
    if not repos:
        contexto_repos = (
            "REPOSITORIOS: (ninguno)\n"
            f"{MODO_SIN_REPO} (el usuario no indicó ningún --repo; "
            "no accedas a repositorios locales)\n"
        )
    else:
        lista = "\n".join(f"- {repo.name}: {repo}" for repo in repos)
        contexto_repos = (
            f"REPOSITORIOS ({len(repos)}), nombre: ruta:\n{lista}\n"
            f"COMANDO_INDEXADO (para un repo sin .codegraph/; reemplaza {{ruta}} por la ruta del repo): {comando_indexado}\n"
        )
    return (
        f"ID_TICKET: {ticket}\n"
        f"JIRA_MCP (servidor MCP de Jira a usar): {jira_mcp}\n"
        f"{contexto_repos}"
        f"{bloque_adjuntos}"
        f"Fecha de análisis (ya obtenida, AAAA-MM-DD): {date.today().isoformat()}\n"
        f"Plantilla de caso de prueba: {PLANTILLAS_DIR / 'plantilla_caso_prueba.md'}\n"
        f"Plantilla de bug: {PLANTILLAS_DIR / 'plantilla_bug.md'}\n"
        f"Archivo de salida de QA (ruta exacta ya resuelta, no la cambies): {salida_qa}\n"
        f"Archivo de salida de desarrollo (ruta exacta ya resuelta, no la cambies): {salida_dev}\n\n"
        "Ejecuta el flujo de trabajo completo y escribe los dos análisis en los archivos de salida indicados."
    )


def leer_prompt() -> str:
    """Devuelve el cuerpo del prompt sin el frontmatter YAML del sub-agente."""
    texto = PROMPT_FILE.read_text(encoding="utf-8")
    coincidencia = FRONTMATTER_RE.match(texto)
    return texto[coincidencia.end():] if coincidencia else texto


@contextmanager
def indicador_progreso(mensaje: str, intervalo_tty: float = 0.2, intervalo_log: float = 30.0):
    """Muestra en stderr que el análisis sigue en curso mientras dura el bloque.

    En una terminal anima un spinner con el tiempo transcurrido; si stderr no es una terminal
    (redirigido a un archivo o a un pipe) escribe una línea cada `intervalo_log` segundos.
    """
    fin = threading.Event()
    interactivo = sys.stderr.isatty()
    inicio = time.monotonic()

    def transcurrido() -> str:
        minutos, segundos = divmod(int(time.monotonic() - inicio), 60)
        return f"{minutos:02d}:{segundos:02d}"

    def animar() -> None:
        for cuadro in itertools.cycle("|/-\\"):
            if interactivo:
                sys.stderr.write(f"\r{cuadro} {mensaje} ({transcurrido()})")
                sys.stderr.flush()
            if fin.wait(intervalo_tty if interactivo else intervalo_log):
                return
            if not interactivo:
                print(f"{mensaje} ({transcurrido()})", file=sys.stderr, flush=True)

    hilo = threading.Thread(target=animar, daemon=True)
    hilo.start()
    try:
        yield
    finally:
        fin.set()
        hilo.join()
        if interactivo:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


def volcar_salida(archivo, destino) -> None:
    """Escribe en `destino` lo que el subproceso dejó en `archivo`, sin fallar por el encoding del destino."""
    archivo.seek(0)
    texto = archivo.read().decode("utf-8", errors="replace")
    try:
        destino.write(texto)
    except UnicodeEncodeError:
        codificacion = destino.encoding or "utf-8"
        destino.write(texto.encode(codificacion, errors="replace").decode(codificacion))
    destino.flush()


def ejecutar_claude(
    mensaje: str,
    repos: list[Path],
    salida_dir: Path,
    modelo: str,
    permitidas: list[str],
    denegadas: list[str],
    adjuntos_dir: Path | None = None,
) -> int:
    comando = [
        "claude",
        "-p",
        mensaje,
        "--model",
        modelo,
        "--append-system-prompt",
        leer_prompt(),
        "--allowedTools",
        *permitidas,
        "--disallowedTools",
        *denegadas,
        "--add-dir",
        *map(str, repos),
        str(salida_dir),
        str(BASE_DIR),
        *([str(adjuntos_dir)] if adjuntos_dir else []),
        "--output-format",
        "text",
    ]
    try:
        # La salida de claude va a archivos temporales y se vuelca al apagar el spinner, para que no se mezclen en
        # la terminal. Así no hay salida en vivo (stdout y stderr salen como dos bloques), pero si se interrumpe
        # con Ctrl+C lo ya escrito igual se muestra.
        with tempfile.TemporaryFile() as salida, tempfile.TemporaryFile() as errores:
            try:
                with indicador_progreso("Analizando el ticket con Claude, puede tardar varios minutos"):
                    resultado = subprocess.run(comando, cwd=BASE_DIR, check=False, stdout=salida, stderr=errores)
            finally:
                volcar_salida(salida, sys.stdout)
                volcar_salida(errores, sys.stderr)
        return resultado.returncode
    except FileNotFoundError:
        fallar("no se encontró el comando 'claude' en el PATH")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Genera los reportes de QA y de desarrollo de un ticket de Jira, con o sin repositorios locales."
    )
    parser.add_argument("ticket", nargs="?", default="", help="ID del ticket de Jira (ej: PROJ-123)")
    parser.add_argument(
        "--repo",
        nargs="+",
        action="extend",
        default=[],
        metavar="RUTA",
        help="Ruta de uno o más repositorios locales a analizar (ej: ~/repos/mi-web-component); "
        "acepta varios. Sin --repo no se analiza código local",
    )
    parser.add_argument(
        "--salida",
        type=Path,
        default=SALIDA_POR_DEFECTO,
        metavar="DIR",
        help="Directorio donde se escriben los reportes (por defecto: %(default)s)",
    )
    parser.add_argument("--modelo", default=MODELO_POR_DEFECTO, help="Modelo de Claude a usar (por defecto: %(default)s)")
    parser.add_argument(
        "--jira-mcp",
        default=os.environ.get("AGENTE_JIRA_MCP"),
        metavar="SERVIDOR",
        help="Nombre del servidor MCP de Jira configurado en Claude Code; debe exponer jira_get_issue y "
        "jira_search. Obligatorio, también por la variable de entorno AGENTE_JIRA_MCP",
    )
    parser.add_argument(
        "--comando-indexado",
        default=COMANDO_INDEXADO_POR_DEFECTO,
        metavar="COMANDO",
        help="Comando para indexar con CodeGraph un repo sin índice; {ruta} se reemplaza por la ruta del repo "
        "(por defecto: '%(default)s')",
    )
    args = parser.parse_args()

    if not PROMPT_FILE.is_file():
        fallar(f"no se encuentra el prompt del agente: {PROMPT_FILE}")
    if not args.jira_mcp:
        fallar("falta el servidor MCP de Jira: usa --jira-mcp o la variable de entorno AGENTE_JIRA_MCP")
    validar_jira_mcp(args.jira_mcp)
    validar_comando_indexado(args.comando_indexado)

    ticket = args.ticket.strip()
    repos = [r.strip() for r in args.repo]
    # `--repo` consume todos los valores que le siguen, incluido un ticket escrito después de él.
    # Un valor con forma de ticket que no es un directorio existente es ese caso (con o sin ticket
    # previo); si existe como directorio se trata como una ruta más, que validar_entradas valida.
    for r in repos:
        if TICKET_RE.match(r) and not Path(r).expanduser().is_dir():
            fallar(f"'{r}' parece un ID de ticket y no una ruta existente: el ID_TICKET debe ir antes de --repo "
                   "(ej: agente_mantenimiento.py PROJ-123 --repo ~/repos/wc-a ~/repos/wc-b)")
    rutas = validar_entradas(ticket, repos)
    if not rutas:
        print("AVISO: sin --repo, el análisis no incluye código local (reporte de desarrollo menos detallado).", file=sys.stderr)

    salida_dir = args.salida.expanduser().resolve()
    salida_qa, salida_dev = rutas_salida(ticket, salida_dir)
    permitidas, denegadas = herramientas(args.jira_mcp, args.comando_indexado)

    adjuntos_tmp = Path(tempfile.mkdtemp(prefix="agente_adjuntos_"))
    try:
        adjuntos, motivo = descargar_adjuntos(ticket, adjuntos_tmp)
        if motivo:
            print(f"AVISO: no se descargaron las imágenes del ticket ({motivo}); el análisis continúa sin ellas.", file=sys.stderr)
        mensaje = construir_mensaje(
            ticket, rutas, salida_qa, salida_dev, args.jira_mcp, args.comando_indexado, adjuntos, motivo
        )
        codigo = ejecutar_claude(
            mensaje, rutas, salida_dir, args.modelo, permitidas, denegadas, adjuntos_tmp if adjuntos else None
        )
    finally:
        shutil.rmtree(adjuntos_tmp, ignore_errors=True)
    if codigo != 0:
        fallar(f"claude terminó con código {codigo}")
    faltantes = [str(s) for s in (salida_qa, salida_dev) if not s.is_file()]
    if faltantes:
        fallar(f"el agente terminó sin generar los archivos esperados: {', '.join(faltantes)}")

    print(f"Análisis de QA generado: {salida_qa}")
    print(f"Análisis de desarrollo generado: {salida_dev}")


if __name__ == "__main__":
    main()
