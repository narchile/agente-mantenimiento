"""Pruebas del script agente_mantenimiento.py (sin invocar a `claude`)."""
import contextlib
import io
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import agente_mantenimiento as agente  # noqa: E402


def falla(funcion, *args):
    """Ejecuta funcion y devuelve el mensaje de error de fallar(); rompe si no falla."""
    caso = unittest.TestCase()
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), caso.assertRaises(SystemExit) as salida:
        funcion(*args)
    caso.assertEqual(salida.exception.code, 1)
    return stderr.getvalue()


class ValidarJiraMcp(unittest.TestCase):
    def test_acepta_nombres_validos(self):
        for nombre in ("atlassian-local", "jira_2", "ABC"):
            with self.subTest(nombre=nombre):
                agente.validar_jira_mcp(nombre)

    def test_rechaza_caracteres_que_romperian_el_identificador(self):
        for nombre in ("a b", "a__b/c", "a;b", "", "a.b"):
            with self.subTest(nombre=nombre):
                self.assertIn("--jira-mcp", falla(agente.validar_jira_mcp, nombre))


class ValidarComandoIndexado(unittest.TestCase):
    def test_acepta_el_valor_por_defecto(self):
        agente.validar_comando_indexado(agente.COMANDO_INDEXADO_POR_DEFECTO)

    def test_rechaza_comandos_inseguros_o_mal_formados(self):
        casos = {
            "sin marcador": "codegraph init",
            "marcador repetido": "codegraph {ruta} {ruta}",
            "pipe": "codegraph init {ruta} | tee x",
            "sustitucion": "codegraph init $(whoami) {ruta}",
            "interprete": "bash run.sh {ruta}",
            "interprete con ruta": "/bin/sh run.sh {ruta}",
            "dash c": "codegraph -c x {ruta}",
            "sin comando": "{ruta}",
            "comillas sin cerrar": "codegraph 'init {ruta}",
        }
        for nombre, comando in casos.items():
            with self.subTest(caso=nombre):
                self.assertIn("--comando-indexado", falla(agente.validar_comando_indexado, comando))


class ValidarEntradas(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def crear(self, *partes):
        ruta = self.tmp.joinpath(*partes)
        ruta.mkdir(parents=True)
        return ruta

    def test_sin_repos_devuelve_lista_vacia(self):
        self.assertEqual(agente.validar_entradas("PROJ-1", []), [])

    def test_ticket_vacio_o_con_formato_invalido(self):
        self.assertIn("falta ID_TICKET", falla(agente.validar_entradas, "", []))
        for ticket in ("proj-1", "PROJ1", "PROJ-", "1-23", "PROJ-1 x"):
            with self.subTest(ticket=ticket):
                self.assertIn("formato válido", falla(agente.validar_entradas, ticket, []))

    def test_repo_inexistente_o_vacio(self):
        self.assertIn("no existe", falla(agente.validar_entradas, "PROJ-1", [str(self.tmp / "nada")]))
        self.assertIn("vacía", falla(agente.validar_entradas, "PROJ-1", [""]))

    def test_elimina_duplicados_y_resuelve_rutas(self):
        repo = self.crear("wc-a")
        rutas = agente.validar_entradas("PROJ-1", [str(repo), str(repo / ".." / "wc-a")])
        self.assertEqual(rutas, [repo])

    def test_rechaza_repos_con_el_mismo_nombre(self):
        a = self.crear("x", "wc")
        b = self.crear("y", "wc")
        self.assertIn("comparten el nombre 'wc'", falla(agente.validar_entradas, "PROJ-1", [str(a), str(b)]))


class RutasSalida(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_primer_uso_sin_sufijo_y_crea_el_directorio(self):
        destino = self.dir / "nuevo" / "sub"
        qa, dev = agente.rutas_salida("PROJ-1", destino)
        self.assertEqual((qa.name, dev.name), ("analisis_qa_PROJ-1.md", "analisis_dev_PROJ-1.md"))
        self.assertTrue(destino.is_dir())

    def test_usa_el_primer_sufijo_libre_para_ambos_archivos(self):
        (self.dir / "analisis_qa_PROJ-1.md").touch()
        self.assertEqual(agente.rutas_salida("PROJ-1", self.dir)[0].name, "analisis_qa_PROJ-1_1.md")
        # Con solo uno de los dos archivos ocupado, el par completo salta de sufijo.
        (self.dir / "analisis_dev_PROJ-1_1.md").touch()
        qa, dev = agente.rutas_salida("PROJ-1", self.dir)
        self.assertEqual((qa.name, dev.name), ("analisis_qa_PROJ-1_2.md", "analisis_dev_PROJ-1_2.md"))

    def test_directorio_inutilizable_falla(self):
        archivo = self.dir / "es_un_archivo"
        archivo.touch()
        self.assertIn("--salida", falla(agente.rutas_salida, "PROJ-1", archivo / "sub"))


class Herramientas(unittest.TestCase):
    def test_permisos_de_jira_son_solo_lectura(self):
        permitidas, denegadas = agente.herramientas("jira-x", agente.COMANDO_INDEXADO_POR_DEFECTO)
        self.assertIn("mcp__jira-x__jira_get_issue", permitidas)
        self.assertIn("mcp__jira-x__jira_search", permitidas)
        for escritura in ("add_comment", "create_issue", "transition_issue", "update_issue"):
            self.assertIn(f"mcp__jira-x__jira_{escritura}", denegadas)
            self.assertNotIn(f"mcp__jira-x__jira_{escritura}", permitidas)
        self.assertIn("Edit", denegadas)

    def test_permiso_de_indexado_se_deriva_del_prefijo(self):
        permitidas, _ = agente.herramientas("j", "codegraph init -y {ruta}")
        self.assertIn("Bash(codegraph init -y:*)", permitidas)
        permitidas, _ = agente.herramientas("j", "mi-indexador --repo {ruta} --rapido")
        self.assertIn("Bash(mi-indexador --repo:*)", permitidas)


class ConstruirMensaje(unittest.TestCase):
    def mensaje(self, repos):
        return agente.construir_mensaje(
            "PROJ-1", repos, Path("/out/qa.md"), Path("/out/dev.md"), "jira-x", "idx {ruta}"
        )

    def test_con_repos_lista_nombre_y_ruta(self):
        texto = self.mensaje([Path("/r/wc-a"), Path("/r/wc-b")])
        self.assertIn("REPOSITORIOS (2), nombre: ruta:", texto)
        self.assertIn("- wc-a: /r/wc-a", texto)
        self.assertIn("COMANDO_INDEXADO", texto)
        self.assertNotIn(agente.MODO_SIN_REPO, texto)

    def test_sin_repos_usa_el_marcador_que_el_prompt_reconoce(self):
        texto = self.mensaje([])
        self.assertIn(agente.MODO_SIN_REPO, texto)
        self.assertIn(agente.MODO_SIN_REPO, agente.PROMPT_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("COMANDO_INDEXADO", texto)

    def test_incluye_ticket_mcp_y_rutas_de_salida(self):
        texto = self.mensaje([])
        for esperado in ("ID_TICKET: PROJ-1", "jira-x", "/out/qa.md", "/out/dev.md"):
            self.assertIn(esperado, texto)


class PromptComoPlugin(unittest.TestCase):
    def test_el_comando_de_adjuntos_apunta_a_un_script_que_existe(self):
        prompt = agente.PROMPT_FILE.read_text(encoding="utf-8")
        self.assertNotIn("TU_RUTA_A_DESCARGAR_ADJUNTOS", prompt)
        relativa = re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/(scripts/descargar_adjuntos\.py)", prompt)
        self.assertIsNotNone(relativa, "el prompt debe citar el script con ${CLAUDE_PLUGIN_ROOT}")
        self.assertTrue((BASE_DIR / relativa.group(1)).is_file())


class LeerPrompt(unittest.TestCase):
    def test_quita_el_frontmatter(self):
        cuerpo = agente.leer_prompt()
        self.assertFalse(cuerpo.startswith("---"))
        self.assertNotIn("\nmodel: sonnet", cuerpo.split("\n\n", 1)[0])

    def test_conserva_el_cuerpo_completo(self):
        completo = agente.PROMPT_FILE.read_text(encoding="utf-8")
        cuerpo = agente.leer_prompt()
        self.assertTrue(completo.endswith(cuerpo))
        self.assertIn("## Valores por Defecto", cuerpo)
        self.assertIn("## Plantillas por Defecto", cuerpo)


class Main(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "wc-a"
        self.repo.mkdir()
        self.salida = self.tmp / "salida"

    def correr(self, *argv, env_mcp=None, escribir_reportes=True):
        """Ejecuta main() con subprocess.run mockeado; devuelve (comando, stdout, stderr)."""
        capturado = {}

        def falso_run(comando, **kwargs):
            capturado["comando"] = comando
            if escribir_reportes:
                for nombre in ("analisis_qa_PROJ-1.md", "analisis_dev_PROJ-1.md"):
                    (self.salida / nombre).write_text("x", encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        entorno = {} if env_mcp is None else {"AGENTE_JIRA_MCP": env_mcp}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", entorno, clear=True), \
                mock.patch.object(sys, "argv", ["agente", *argv]), \
                mock.patch.object(agente.subprocess, "run", side_effect=falso_run), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                agente.main()
                codigo = 0
            except SystemExit as salida:
                codigo = salida.code
        return capturado.get("comando"), out.getvalue(), err.getvalue(), codigo

    def test_flujo_feliz_con_repo(self):
        comando, out, _, codigo = self.correr(
            "PROJ-1", "--repo", str(self.repo), "--salida", str(self.salida), "--jira-mcp", "jira-x"
        )
        self.assertEqual(codigo, 0)
        self.assertEqual(comando[0], "claude")
        self.assertIn("mcp__jira-x__jira_get_issue", comando)
        self.assertIn(str(self.repo), comando)
        self.assertIn("Análisis de QA generado", out)

    def test_jira_mcp_desde_variable_de_entorno(self):
        comando, _, _, codigo = self.correr("PROJ-1", "--salida", str(self.salida), env_mcp="desde-env")
        self.assertEqual(codigo, 0)
        self.assertIn("mcp__desde-env__jira_search", comando)

    def test_sin_repo_avisa_y_sigue(self):
        _, _, err, codigo = self.correr("PROJ-1", "--salida", str(self.salida), "--jira-mcp", "jira-x")
        self.assertEqual(codigo, 0)
        self.assertIn("AVISO", err)

    def test_falta_jira_mcp(self):
        comando, _, err, codigo = self.correr("PROJ-1", "--salida", str(self.salida))
        self.assertEqual(codigo, 1)
        self.assertIsNone(comando)
        self.assertIn("AGENTE_JIRA_MCP", err)

    def test_ticket_escrito_despues_de_repo_se_detecta(self):
        comando, _, err, codigo = self.correr(
            "--repo", str(self.repo), "PROJ-1", "--salida", str(self.salida), "--jira-mcp", "jira-x"
        )
        self.assertEqual(codigo, 1)
        self.assertIsNone(comando)
        self.assertIn("ID_TICKET debe ir antes de --repo", err)

    def test_falla_si_el_agente_no_genera_los_archivos(self):
        _, _, err, codigo = self.correr(
            "PROJ-1", "--salida", str(self.salida), "--jira-mcp", "jira-x", escribir_reportes=False
        )
        self.assertEqual(codigo, 1)
        self.assertIn("sin generar los archivos esperados", err)

    def test_salida_por_defecto_es_analisis_en_el_directorio_actual(self):
        cwd = self.tmp / "trabajo"
        cwd.mkdir()
        original = Path.cwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, original)

        def falso_run(comando, **kwargs):
            for nombre in ("analisis_qa_PROJ-1.md", "analisis_dev_PROJ-1.md"):
                (cwd / "analisis" / nombre).write_text("x", encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["agente", "PROJ-1", "--jira-mcp", "j"]), \
                mock.patch.object(agente.subprocess, "run", side_effect=falso_run), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            agente.main()
        self.assertTrue((cwd / "analisis" / "analisis_qa_PROJ-1.md").is_file())

    def test_falla_si_claude_devuelve_error(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["agente", "PROJ-1", "--salida", str(self.salida), "--jira-mcp", "j"]), \
                mock.patch.object(agente.subprocess, "run", return_value=mock.Mock(returncode=3, stdout="", stderr="")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as salida:
            agente.main()
        self.assertEqual(salida.exception.code, 1)
        self.assertIn("código 3", err.getvalue())


class EjecutarClaudeTests(unittest.TestCase):
    def ejecutar(self, falso_run, stdout=None, stderr=None):
        """Llama a ejecutar_claude con subprocess.run falso; devuelve (código, stdout, stderr)."""
        out = stdout if stdout is not None else io.StringIO()
        err = stderr if stderr is not None else io.StringIO()
        with mock.patch.object(agente, "leer_prompt", return_value="p"), \
                mock.patch.object(agente.subprocess, "run", side_effect=falso_run), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            codigo = agente.ejecutar_claude(
                mensaje="m", repos=[], salida_dir=Path("."), modelo="modelo", permitidas=[], denegadas=[]
            )
        return codigo, out, err

    @staticmethod
    def escribiendo(stdout=b"", stderr=b"", returncode=0, error=None):
        def falso_run(_comando, **kwargs):
            kwargs["stdout"].write(stdout)
            kwargs["stderr"].write(stderr)
            if error:
                raise error
            return mock.Mock(returncode=returncode)
        return falso_run

    def test_la_salida_de_claude_se_muestra_despues_de_apagar_el_spinner(self):
        eventos = []

        @contextlib.contextmanager
        def falso_indicador(_mensaje):
            eventos.append("spinner on")
            yield
            eventos.append("spinner off")

        class Salida(io.StringIO):
            def write(self, texto):
                eventos.append(f"escribe {texto}")
                return super().write(texto)

        with mock.patch.object(agente, "indicador_progreso", falso_indicador):
            codigo, _, _ = self.ejecutar(
                self.escribiendo(b"reporte", b"aviso"), stdout=Salida(), stderr=Salida()
            )
        self.assertEqual(codigo, 0)
        self.assertEqual(eventos, ["spinner on", "spinner off", "escribe reporte", "escribe aviso"])

    def test_con_codigo_distinto_de_cero_igual_muestra_stderr(self):
        codigo, _, err = self.ejecutar(self.escribiendo(stderr=b"fallo de MCP", returncode=3))
        self.assertEqual(codigo, 3)
        self.assertEqual(err.getvalue(), "fallo de MCP")

    def test_si_se_interrumpe_con_ctrl_c_muestra_lo_ya_escrito(self):
        out, err = io.StringIO(), io.StringIO()
        falso_run = self.escribiendo(b"parcial", b"error parcial", error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.ejecutar(falso_run, stdout=out, stderr=err)
        self.assertEqual((out.getvalue(), err.getvalue()), ("parcial", "error parcial"))

    def test_un_destino_que_no_soporta_el_encoding_no_rompe_la_corrida(self):
        crudo = io.BytesIO()
        out = io.TextIOWrapper(crudo, encoding="ascii")
        codigo, _, _ = self.ejecutar(self.escribiendo("análisis ✔".encode("utf-8")), stdout=out)
        out.flush()
        self.assertEqual(codigo, 0)
        self.assertEqual(crudo.getvalue(), b"an?lisis ?")


class IndicadorProgresoTests(unittest.TestCase):
    def test_sin_terminal_escribe_lineas_periodicas_en_stderr(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            with agente.indicador_progreso("Analizando", intervalo_log=0.01):
                time.sleep(0.1)
        self.assertIn("Analizando (00:00)", err.getvalue())
        self.assertNotIn("\r", err.getvalue())

    def test_con_terminal_anima_y_limpia_la_linea_al_terminar(self):
        err = io.StringIO()
        err.isatty = lambda: True
        with mock.patch.object(sys, "stderr", err):
            with agente.indicador_progreso("Analizando", intervalo_tty=0.01):
                time.sleep(0.1)
        salida = err.getvalue()
        self.assertIn("Analizando (00:00)", salida)
        self.assertTrue(salida.endswith("\r\033[K"))

    def test_detiene_el_hilo_aunque_el_bloque_falle(self):
        antes = threading.active_count()
        with mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(ValueError), agente.indicador_progreso("x", intervalo_log=0.01):
                raise ValueError
        self.assertEqual(threading.active_count(), antes)


if __name__ == "__main__":
    unittest.main()
