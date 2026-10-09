"""Las plantillas por defecto embebidas en el prompt deben coincidir con plantillas/."""
import re
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPT = (BASE_DIR / "agents" / "agente-mantenimiento.md").read_text(encoding="utf-8")


def embebida(nombre: str) -> str:
    patron = rf"<!-- BEGIN {re.escape(nombre)} -->\n(.*?)\n<!-- END {re.escape(nombre)} -->"
    coincidencia = re.search(patron, PROMPT, re.DOTALL)
    if coincidencia is None:
        raise AssertionError(f"el prompt no incluye la plantilla embebida {nombre}")
    return coincidencia.group(1)


PLANTILLAS_ESPERADAS = {"plantilla_caso_prueba.md", "plantilla_bug.md"}


class PlantillasEmbebidas(unittest.TestCase):
    def test_mismo_conjunto_de_plantillas_en_archivos_y_prompt(self):
        archivos = {a.name for a in (BASE_DIR / "plantillas").glob("*.md")}
        marcadas = set(re.findall(r"<!-- BEGIN (.+?) -->", PROMPT))
        self.assertEqual(archivos, PLANTILLAS_ESPERADAS)
        self.assertEqual(marcadas, archivos)

    def test_coinciden_con_los_archivos(self):
        for nombre in sorted(PLANTILLAS_ESPERADAS):
            with self.subTest(plantilla=nombre):
                archivo = BASE_DIR / "plantillas" / nombre
                self.assertEqual(embebida(nombre), archivo.read_text(encoding="utf-8").rstrip("\n"))


if __name__ == "__main__":
    unittest.main()
