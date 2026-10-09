"""Tests for the Jira attachment download helper (no network: _abrir is mocked)."""
import http.client
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "scripts"))
sys.path.insert(0, str(BASE_DIR))

import agente_mantenimiento as agente  # noqa: E402
import descargar_adjuntos as mod  # noqa: E402

ENV = {
    "JIRA_URL": "https://jira.example.com",
    "JIRA_USERNAME": "user@example.com",
    "JIRA_API_TOKEN": "s3cr3t-token",
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def attachment(id_, filename, mime="image/png", size=10, content=None):
    return {
        "id": str(id_),
        "filename": filename,
        "mimeType": mime,
        "size": size,
        "content": content or f"https://jira.example.com/rest/api/3/attachment/content/{id_}",
    }


def fake_jira(attachments, blobs=None):
    """Returns a fake _abrir serving the issue listing and attachment contents."""
    blobs = blobs or {}
    pedidos = []

    def abrir(req, timeout=None):
        pedidos.append(req)
        url = req.full_url
        if "/rest/api/3/issue/" in url:
            return FakeResponse(json.dumps({"fields": {"attachment": attachments}}).encode())
        for adj in attachments:
            if url == adj["content"]:
                return FakeResponse(blobs.get(adj["id"], b"PNGDATA-" + adj["id"].encode()))
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    abrir.pedidos = pedidos
    return abrir


class DescargarAdjuntos(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.destino = Path(tmp.name)

    def descargar(self, abrir, env=ENV):
        with mock.patch.dict("os.environ", env, clear=True), mock.patch.object(mod, "_abrir", abrir):
            return mod.descargar_adjuntos("PROJ-1", self.destino)

    def test_falta_alguna_variable(self):
        for faltante in ENV:
            with self.subTest(faltante=faltante):
                env = {k: v for k, v in ENV.items() if k != faltante}
                archivos, motivo = self.descargar(fake_jira([]), env)
                self.assertEqual(archivos, [])
                self.assertIn("JIRA_URL/JIRA_USERNAME/JIRA_API_TOKEN", motivo)

    def test_url_no_https(self):
        abrir = fake_jira([])
        archivos, motivo = self.descargar(abrir, {**ENV, "JIRA_URL": "http://jira.example.com"})
        self.assertEqual(archivos, [])
        self.assertIn("https", motivo)
        self.assertEqual(abrir.pedidos, [])

    def test_descarga_feliz_guarda_como_id_ext(self):
        abrir = fake_jira([attachment(101, "captura.PNG"), attachment(102, "foto.jpeg", "image/jpeg")])
        archivos, motivo = self.descargar(abrir)
        self.assertIsNone(motivo)
        self.assertEqual([(p.name, n) for p, n in archivos], [("101.png", "captura.PNG"), ("102.jpeg", "foto.jpeg")])
        self.assertEqual((self.destino / "101.png").read_bytes(), b"PNGDATA-101")
        self.assertEqual(len(abrir.pedidos), 3)
        for req in abrir.pedidos:
            self.assertEqual(req.get_method(), "GET")
            self.assertTrue(req.get_header("Authorization").startswith("Basic "))
        self.assertIn("fields=attachment", abrir.pedidos[0].full_url)

    def test_nombre_remoto_malicioso_no_se_usa_como_ruta(self):
        abrir = fake_jira([attachment(7, "../../etc/passwd.png")])
        archivos, _ = self.descargar(abrir)
        self.assertEqual([p for p, _ in archivos], [self.destino / "7.png"])
        self.assertEqual([p.name for p in self.destino.iterdir()], ["7.png"])

    def test_extension_desconocida_usa_mime(self):
        abrir = fake_jira([attachment(8, "sin_extension", "image/webp"), attachment(9, "x.exe", "image/gif")])
        archivos, _ = self.descargar(abrir)
        self.assertEqual([p.name for p, _ in archivos], ["8.webp", "9.gif"])

    def test_omite_adjuntos_que_no_son_imagen(self):
        abrir = fake_jira([attachment(1, "doc.pdf", "application/pdf"), attachment(2, "a.png")])
        archivos, motivo = self.descargar(abrir)
        self.assertIsNone(motivo)
        self.assertEqual([p.name for p, _ in archivos], ["2.png"])

    def test_limita_a_diez_imagenes(self):
        abrir = fake_jira([attachment(i, f"i{i}.png") for i in range(1, 16)])
        archivos, _ = self.descargar(abrir)
        self.assertEqual(len(archivos), 10)
        self.assertEqual(len(list(self.destino.iterdir())), 10)

    def test_omite_imagenes_de_mas_de_10_mb_por_size(self):
        abrir = fake_jira([attachment(1, "big.png", size=10 * 1024 * 1024 + 1), attachment(2, "ok.png")])
        archivos, _ = self.descargar(abrir)
        self.assertEqual([p.name for p, _ in archivos], ["2.png"])

    def test_omite_si_los_bytes_reales_exceden_el_limite(self):
        blobs = {"1": b"x" * (10 * 1024 * 1024 + 1)}
        abrir = fake_jira([attachment(1, "liar.png", size=5), attachment(2, "ok.png")], blobs)
        archivos, _ = self.descargar(abrir)
        self.assertEqual([p.name for p, _ in archivos], ["2.png"])
        self.assertFalse((self.destino / "1.png").exists())

    def test_error_http_devuelve_motivo_sin_token(self):
        def abrir(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        archivos, motivo = self.descargar(abrir)
        self.assertEqual(archivos, [])
        self.assertIn("401", motivo)
        self.assertNotIn(ENV["JIRA_API_TOKEN"], motivo)

    def test_error_de_red_y_json_invalido(self):
        def red(req, timeout=None):
            raise urllib.error.URLError("boom")

        def malo(req, timeout=None):
            return FakeResponse(b"<html>not json")

        for abrir in (red, malo):
            with self.subTest(abrir=abrir.__name__):
                archivos, motivo = self.descargar(abrir)
                self.assertEqual(archivos, [])
                self.assertTrue(motivo)
                self.assertNotIn(ENV["JIRA_API_TOKEN"], motivo)

    def test_fallo_en_una_descarga_conserva_las_demas(self):
        base = fake_jira([attachment(1, "a.png"), attachment(2, "b.png")])

        def abrir(req, timeout=None):
            if req.full_url.endswith("/1"):
                raise urllib.error.URLError("boom")
            return base(req, timeout)

        archivos, _ = self.descargar(abrir)
        self.assertEqual([p.name for p, _ in archivos], ["2.png"])


    def test_content_en_otro_host_no_se_pide_ni_recibe_el_token(self):
        evil = "https://evil.example.net/rest/api/3/attachment/content/1"
        abrir = fake_jira([attachment(1, "a.png", content=evil), attachment(2, "b.png")])
        archivos, motivo = self.descargar(abrir)
        self.assertIsNone(motivo)
        self.assertEqual([p.name for p, _ in archivos], ["2.png"])
        self.assertNotIn(evil, [req.full_url for req in abrir.pedidos])
        for req in abrir.pedidos:
            self.assertTrue(req.full_url.startswith("https://jira.example.com/"))

    def test_content_http_no_https_se_omite(self):
        plain = "http://jira.example.com/rest/api/3/attachment/content/1"
        abrir = fake_jira([attachment(1, "a.png", content=plain), attachment(2, "b.png")])
        archivos, _ = self.descargar(abrir)
        self.assertEqual([p.name for p, _ in archivos], ["2.png"])
        self.assertNotIn(plain, [req.full_url for req in abrir.pedidos])

    def test_content_https_del_mismo_host_se_descarga(self):
        same = "https://jira.example.com/secure/attachment/1/a.png"
        abrir = fake_jira([attachment(1, "a.png", content=same)])
        archivos, _ = self.descargar(abrir)
        self.assertEqual([p.name for p, _ in archivos], ["1.png"])
        self.assertEqual(abrir.pedidos[-1].full_url, same)

    def test_incomplete_read_en_el_pedido_del_issue(self):
        def abrir(req, timeout=None):
            raise http.client.IncompleteRead(b"par")

        archivos, motivo = self.descargar(abrir)
        self.assertEqual(archivos, [])
        self.assertTrue(motivo)
        self.assertNotIn(ENV["JIRA_API_TOKEN"], motivo)

    def test_incomplete_read_en_un_adjunto_conserva_los_demas(self):
        base = fake_jira([attachment(1, "a.png"), attachment(2, "b.png")])

        def abrir(req, timeout=None):
            if req.full_url.endswith("/1"):
                raise http.client.IncompleteRead(b"par")
            return base(req, timeout)

        archivos, _ = self.descargar(abrir)
        self.assertEqual([p.name for p, _ in archivos], ["2.png"])

    def test_size_invalido_no_aborta_los_demas(self):
        for size in (None, "abc", [], {}):
            with self.subTest(size=size):
                abrir = fake_jira([attachment(1, "a.png", size=size), attachment(2, "b.png")])
                archivos, motivo = self.descargar(abrir)
                self.assertIsNone(motivo)
                self.assertIn("2.png", [p.name for p, _ in archivos])


class RedireccionSinAuth(unittest.TestCase):
    def redirigir(self, destino):
        req = urllib.request.Request("https://jira.example.com/a", headers={"Authorization": "Basic abc"})
        manejador = mod._RedireccionSinAuth()
        return manejador.redirect_request(req, None, 302, "Found", {}, destino)

    def test_quita_authorization_si_cambia_el_host(self):
        nuevo = self.redirigir("https://media.example.net/file")
        self.assertIsNone(nuevo.get_header("Authorization"))

    def test_conserva_authorization_en_el_mismo_host(self):
        nuevo = self.redirigir("https://jira.example.com/b")
        self.assertEqual(nuevo.get_header("Authorization"), "Basic abc")


class MensajeConAdjuntos(unittest.TestCase):
    def mensaje(self, **extra):
        return agente.construir_mensaje(
            "PROJ-1", [], Path("/out/qa.md"), Path("/out/dev.md"), "jira-x", "cmd {ruta}", **extra
        )

    def test_lista_las_imagenes(self):
        texto = self.mensaje(adjuntos=[(Path("/tmp/x/1.png"), "captura.png")])
        self.assertIn("ADJUNTOS (imágenes del ticket, léelas con Read):", texto)
        self.assertIn("- /tmp/x/1.png: captura.png", texto)

    def test_no_disponibles_con_motivo(self):
        texto = self.mensaje(motivo_adjuntos="faltan variables")
        self.assertIn("ADJUNTOS: no disponibles (faltan variables)", texto)

    def test_sin_imagenes_y_sin_motivo(self):
        texto = self.mensaje(adjuntos=[])
        self.assertIn("ADJUNTOS: ninguna imagen adjunta en el ticket", texto)

    def test_firma_anterior_sigue_funcionando(self):
        self.assertIn("ADJUNTOS", self.mensaje())


class MainConAdjuntos(unittest.TestCase):
    def correr(self, resultado):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        salida = Path(tmp.name) / "salida"
        capturado = {}

        def falso_run(comando, **kwargs):
            capturado["comando"] = comando
            capturado["dir"] = capturado.get("dir")
            for nombre in ("analisis_qa_PROJ-1.md", "analisis_dev_PROJ-1.md"):
                (salida / nombre).write_text("x", encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        def falso_descargar(ticket, destino):
            capturado["dir"] = destino
            return resultado(destino)

        err = io.StringIO()
        argv = ["agente", "PROJ-1", "--salida", str(salida), "--jira-mcp", "j"]
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(agente.subprocess, "run", side_effect=falso_run), \
                mock.patch.object(agente, "descargar_adjuntos", side_effect=falso_descargar), \
                mock.patch("sys.stderr", err), mock.patch("sys.stdout", io.StringIO()):
            agente.main()
        return capturado, err.getvalue()

    def test_con_imagenes_agrega_el_directorio_y_lo_limpia(self):
        def resultado(destino):
            (destino / "1.png").write_bytes(b"x")
            return [(destino / "1.png", "a.png")], None

        capturado, err = self.correr(resultado)
        comando = capturado["comando"]
        self.assertIn(str(capturado["dir"]), comando)
        self.assertTrue(any("1.png: a.png" in parte for parte in comando))
        self.assertNotIn("imágenes del ticket", err)
        self.assertFalse(capturado["dir"].exists())

    def test_no_disponible_avisa_y_no_agrega_directorio(self):
        capturado, err = self.correr(lambda destino: ([], "faltan variables"))
        self.assertNotIn(str(capturado["dir"]), capturado["comando"])
        self.assertIn("AVISO", err)
        self.assertIn("faltan variables", err)
        self.assertFalse(capturado["dir"].exists())


if __name__ == "__main__":
    unittest.main()
