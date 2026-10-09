"""Tests for the descargar_adjuntos CLI (no network: _abrir is mocked)."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "scripts"))
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import descargar_adjuntos as mod  # noqa: E402
from test_adjuntos import ENV, attachment, fake_jira  # noqa: E402


class Cli(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()  # /var -> /private/var on macOS

    def correr(self, argv, abrir=None, env=ENV):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(mod, "_abrir", abrir or fake_jira([])), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            try:
                codigo = mod.main(argv)
            except SystemExit as salida:
                codigo = salida.code
        return codigo, out.getvalue(), err.getvalue()

    def test_una_linea_por_imagen_con_ruta_absoluta(self):
        abrir = fake_jira([attachment(1, "a b.png"), attachment(2, "x.pdf", "application/pdf"), attachment(3, "c.jpg", "image/jpeg")])
        codigo, out, _ = self.correr(["PROJ-1", "--destino", str(self.tmp)], abrir)
        self.assertEqual(codigo, 0)
        self.assertEqual(out.splitlines(), [f"{self.tmp / '1.png'}: a b.png", f"{self.tmp / '3.jpg'}: c.jpg"])
        self.assertTrue(all(Path(l.split(": ")[0]).is_absolute() for l in out.splitlines()))

    def test_sin_imagenes(self):
        codigo, out, _ = self.correr(["PROJ-1", "--destino", str(self.tmp)])
        self.assertEqual(codigo, 0)
        self.assertEqual(out, "ADJUNTOS: ninguna imagen adjunta en el ticket\n")

    def test_no_disponibles_por_variables_faltantes(self):
        codigo, out, _ = self.correr(["PROJ-1", "--destino", str(self.tmp)], env={})
        self.assertEqual(codigo, 0)
        self.assertEqual(out, "ADJUNTOS: no disponibles (faltan variables JIRA_URL/JIRA_USERNAME/JIRA_API_TOKEN)\n")

    def test_no_disponibles_nunca_imprime_el_token(self):
        import urllib.error

        def abrir(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        codigo, out, err = self.correr(["PROJ-1", "--destino", str(self.tmp)], abrir)
        self.assertEqual(codigo, 0)
        self.assertEqual(out, "ADJUNTOS: no disponibles (Jira respondió HTTP 401)\n")
        self.assertNotIn(ENV["JIRA_API_TOKEN"], out + err)

    def test_ticket_invalido_es_error_de_uso(self):
        for malo in ("not a ticket", "PROJ", "PROJ-", "-1", "PROJ-1; rm", "../PROJ-1", "PROJ-1\n"):
            with self.subTest(ticket=malo):
                codigo, out, err = self.correr([malo])
                self.assertEqual(codigo, 2)
                self.assertEqual(out, "")
                self.assertIn("usage", err.lower())

    def test_ticket_valido_en_minusculas_o_mixto(self):
        for ok in ("PROJ-1", "proj-22", "Ab1-300"):
            with self.subTest(ticket=ok):
                codigo, _, _ = self.correr([ok, "--destino", str(self.tmp)])
                self.assertEqual(codigo, 0)

    def test_destino_por_defecto_es_un_tempdir_nuevo(self):
        abrir = fake_jira([attachment(5, "a.png")])
        codigo, out, _ = self.correr(["PROJ-1"], abrir)
        self.assertEqual(codigo, 0)
        ruta = Path(out.split(": ")[0])
        self.addCleanup(lambda: ruta.exists() and ruta.unlink() or ruta.parent.rmdir())
        self.assertTrue(ruta.is_file())
        self.assertTrue(ruta.parent.name.startswith("agente_adjuntos_"))
        self.assertEqual(ruta.name, "5.png")

    def test_destino_inexistente_se_crea(self):
        destino = self.tmp / "nuevo" / "sub"
        codigo, out, _ = self.correr(["PROJ-1", "--destino", str(destino)], fake_jira([attachment(1, "a.png")]))
        self.assertEqual(codigo, 0)
        self.assertTrue((destino / "1.png").is_file())

    def test_destino_no_utilizable_es_no_disponible_con_exit_0(self):
        archivo = self.tmp / "archivo"
        archivo.write_text("x")
        codigo, out, _ = self.correr(["PROJ-1", "--destino", str(archivo / "sub")], fake_jira([attachment(1, "a.png")]))
        self.assertEqual(codigo, 0)
        self.assertTrue(out.startswith("ADJUNTOS: no disponibles ("))


if __name__ == "__main__":
    unittest.main()
