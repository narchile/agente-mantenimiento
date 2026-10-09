# Agente de Mantenimiento y QA

Analiza un ticket de Jira contra uno o más repositorios locales y genera dos reportes Markdown, uno por audiencia:

| Reporte | Para | Contenido |
|---|---|---|
| `analisis_qa_<TICKET>.md` | QA | Casos de prueba (Given-When-Then) o reporte de bug, según el tipo de ticket |
| `analisis_dev_<TICKET>.md` | Desarrollo | Estado de implementación, archivos afectados con evidencia `ruta:línea`, riesgo de regresión y preguntas de desambiguación para el cliente |

El agente es de **solo lectura**: no modifica código ni escribe en Jira.

## Requisitos

* Python 3.9 o superior.
* Claude Code instalado (comando `claude` en el `PATH`).
* Un servidor MCP de Jira configurado en Claude Code que exponga las herramientas `jira_get_issue` y `jira_search`.
* El MCP de CodeGraph y su CLI `codegraph`, para analizar el código de los repositorios.
* Opcional: el MCP de Context7, para consultar documentación de librerías externas.

## Uso

```bash
# Con la variable de entorno configurada una vez (nombre del servidor MCP de Jira):
export AGENTE_JIRA_MCP=<servidor>

# Un repositorio
python agente_mantenimiento.py PROJ-123 --repo ~/repos/mi-componente

# Varios repositorios (el ticket siempre va antes de --repo)
python agente_mantenimiento.py PROJ-123 --repo ~/repos/componente-a ~/repos/componente-b

# Sin código local: el análisis se basa solo en el ticket
python agente_mantenimiento.py PROJ-123
```

### Opciones

| Opción | Descripción | Por defecto |
|---|---|---|
| `--repo RUTA [RUTA ...]` | Repositorios locales a analizar. Acepta rutas absolutas, relativas o con `~`, y puede repetirse. Cada repo debe tener un nombre de carpeta distinto | ninguno |
| `--jira-mcp SERVIDOR` | Servidor MCP de Jira. **Obligatorio**, por flag o por `AGENTE_JIRA_MCP` | — |
| `--salida DIR` | Directorio donde se escriben los reportes | `./analisis` (directorio actual) |
| `--modelo MODELO` | Modelo de Claude a usar | `claude-sonnet-5-5` |
| `--comando-indexado CMD` | Comando para indexar con CodeGraph un repo sin índice; `{ruta}` se reemplaza por la ruta del repo | `codegraph init -y {ruta}` |

Si ya existe un reporte para el ticket, no se sobrescribe: ambos archivos reciben el mismo sufijo numérico (`_1`, `_2`, …).

### Sin repositorios locales

Sin `--repo` el script avisa y el agente trabaja solo con el ticket. El reporte de desarrollo es menos detallado: el estado de implementación, los archivos y el riesgo quedan marcados `[NO VERIFICADO]`, y las preguntas salen solo de vacíos del propio ticket. El reporte de QA casi no cambia.

### Imágenes adjuntas

Para que el agente vea las imágenes del ticket, define estas variables de entorno (API REST de Jira, con VPN si corresponde):

```bash
export JIRA_URL=https://tu-empresa.atlassian.net   # debe ser https
export JIRA_USERNAME=tu.mail@empresa.com
export JIRA_API_TOKEN=<token de API de Atlassian>
```

Es de mejor esfuerzo: si faltan variables o la descarga falla, el script avisa por stderr y el análisis continúa; el reporte de QA indica que las imágenes no se revisaron y marca `[NO VERIFICADO]` lo que dependa de ellas. Se descargan como máximo 10 imágenes de hasta 10 MB cada una, en un directorio temporal que se elimina al terminar. Solo se hacen peticiones GET y el token nunca se imprime ni se reenvía a otro host en redirecciones.

## Cómo funciona

1. `agente_mantenimiento.py` valida las entradas, calcula las rutas de salida y arma el mensaje de la tarea.
2. Lanza `claude -p` en modo headless con `agents/agente-mantenimiento.md` como prompt de sistema, y con una lista acotada de herramientas preautorizadas. Todo lo demás, incluidas las escrituras en Jira, queda denegado.
3. El agente lee el ticket, analiza cada repositorio con CodeGraph y escribe los dos reportes siguiendo las plantillas de `plantillas/`.
4. El script verifica que ambos archivos existan y muestra sus rutas.

### Seguridad

* El permiso para indexar se deriva de `--comando-indexado`: se rechazan plantillas con metacaracteres de shell, intérpretes (`sh`, `bash`, `env`, …) o sin comando antes de `{ruta}`.
* El agente solo recibe acceso a los repositorios indicados, al directorio de salida y a este proyecto.
* El agente lee texto del ticket, que es contenido no confiable. Por eso las herramientas de escritura de Jira están denegadas explícitamente y el resto de permisos es mínimo.

## Usarlo como sub-agente de Claude Code

Este repositorio es también un plugin de Claude Code: `agents/agente-mantenimiento.md` es la definición del sub-agente y `scripts/descargar_adjuntos.py` viaja con él. El agente lo invoca con `${CLAUDE_PLUGIN_ROOT}/scripts/descargar_adjuntos.py`, que Claude Code sustituye por la ruta del plugin, así que no hay rutas absolutas que editar.

### Instalarlo

El repositorio incluye un marketplace local (`.claude-plugin/marketplace.json`). Para instalar el plugin de forma permanente:

```
claude plugin marketplace add /ruta/a/agente-mantenimiento
claude plugin install agente-mantenimiento@agente-mantenimiento-local
```

Reinicia Claude Code después de instalar. El agente queda disponible como `agente-mantenimiento:agente-mantenimiento`.

Para probarlo solo en una sesión, sin instalar, usa `claude --plugin-dir /ruta/a/agente-mantenimiento`.

**Si antes copiaste el agente a `~/.claude/agents/`, bórralo.** No lo uses suelto: fuera del plugin `${CLAUDE_PLUGIN_ROOT}` no se resuelve, el comando de adjuntos falla y el reporte marca las imágenes como no revisadas.

### Configurar el servidor Jira

El frontmatter de `agents/agente-mantenimiento.md` trae el placeholder `TU_SERVIDOR_JIRA` en `tools`. **Hay que reemplazarlo** por el nombre de tu servidor MCP de Jira (el que aparece en `mcp__<servidor>__jira_get_issue`); `tools` es una lista cerrada y es lo que mantiene el acceso a Jira en solo lectura (solo `jira_get_issue` y `jira_search`). Si queda el placeholder, el sub-agente no recibe herramientas de Jira y termina con `ERROR: no se pudo leer el ticket`.

Reemplaza las dos ocurrencias (`jira_get_issue` y `jira_search`):

```
mcp__TU_SERVIDOR_JIRA__jira_get_issue  ->  mcp__<servidor>__jira_get_issue
mcp__TU_SERVIDOR_JIRA__jira_search     ->  mcp__<servidor>__jira_search
```

* Con `--plugin-dir`, edítalo en el archivo del repo.
* Con el plugin instalado, edita la copia instalada (`~/.claude/plugins/cache/agente-mantenimiento-local/agente-mantenimiento/<versión>/agents/agente-mantenimiento.md`) y reinicia Claude Code. **Esa edición se pierde con cada `claude plugin update`**: hay que repetirla.

Con el script (`--jira-mcp`) ese frontmatter se ignora: el script arma sus propios permisos. El sub-agente conserva `Bash` en `tools` porque también lo usa para `git`, `date`, `mkdir`/`ls`, el comando de indexado y la descarga de adjuntos.

### Imágenes del ticket

Para leer las imágenes hacen falta `JIRA_URL`, `JIRA_USERNAME` y `JIRA_API_TOKEN` exportadas en tu shell; sin ellas el agente continúa y marca como `[NO VERIFICADO]` lo que dependa de las imágenes.

### Descargar adjuntos (CLI)

`scripts/descargar_adjuntos.py` es el módulo compartido por el script y el sub-agente, y también se puede ejecutar solo:

```
python3 scripts/descargar_adjuntos.py TICKET [--destino DIR]
```

* `TICKET` debe tener forma `PROJ-123`; si no, falla con error de uso (código 2).
* Sin `--destino` usa un directorio temporal nuevo (`agente_adjuntos_*`).
* Salida, una línea por imagen: `<ruta absoluta>: <nombre original>`. Si no hay imágenes: `ADJUNTOS: ninguna imagen adjunta en el ticket`; si no se pudo consultar Jira: `ADJUNTOS: no disponibles (<motivo>)`. En todos estos casos el código de salida es 0.
* Solo https y mismo host de `JIRA_URL`, hasta 10 imágenes de 10 MB, guardadas como `<id>.<ext>`; el token nunca se imprime.

## Estructura

```
.claude-plugin/plugin.json         # manifiesto del plugin
.claude-plugin/marketplace.json    # marketplace local para instalarlo
agents/agente-mantenimiento.md     # prompt del agente / definición de sub-agente
scripts/descargar_adjuntos.py      # descarga de imágenes del ticket (módulo + CLI)
agente_mantenimiento.py            # runner: validación, permisos y llamada a claude
plantillas/
  plantilla_caso_prueba.md         # formato de los casos de prueba
  plantilla_bug.md                 # formato de los reportes de bug
```

## Limitaciones

* El análisis es estático: no ejecuta tests ni la aplicación.
* Toda afirmación sobre el código lleva evidencia `ruta:línea`; lo que no se pudo verificar se marca `[NO VERIFICADO]`.
* El prompt está pensado para repositorios de Angular (Angular Elements / Web Components).

---

Desarrollado con [Claude Code](https://claude.com/claude-code) y [Gentle AI](https://github.com/Gentleman-Programming/gentle-ai).
