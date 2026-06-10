"""Domain: componer_mensaje (quitar ubicación + markup + footer)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from domain.message import componer_mensaje, quitar_lineas  # noqa: E402

LISTA = """IPRO PARTS
📣 UBICADOS EN EL C.C LA FORTUNA
SÓTANO LOCAL C1-C4
🕒 HORARIO: 8:30AM A 6:30PM

SAMSUNG
A06 4-64GB $325.000"""


class QuitarLineasTests(unittest.TestCase):
    def test_quita_lineas_de_ubicacion(self):
        out = quitar_lineas(LISTA, ("ubicad", r"s[oó]tano", "horario", "fortuna", r"local\s+c\d"))
        self.assertNotIn("UBICADOS", out)
        self.assertNotIn("SÓTANO", out)
        self.assertNotIn("HORARIO", out)
        self.assertIn("SAMSUNG", out)
        self.assertIn("A06 4-64GB $325.000", out)

    def test_sin_patrones_no_cambia(self):
        self.assertEqual(quitar_lineas("hola\nmundo", ()), "hola\nmundo")


class ComponerMensajeTests(unittest.TestCase):
    def test_pipeline_completo(self):
        footer = "📲 Pedidos WhatsApp: +57 300 000 0000"
        out = componer_mensaje(LISTA, markup_percentage=15, footer=footer)
        self.assertNotIn("UBICADOS", out)             # ubicación fuera
        self.assertIn("A06 4-64GB $374.000", out)     # markup aplicado
        self.assertNotIn("$325.000", out)
        self.assertTrue(out.endswith(footer))         # footer al final

    def test_sin_footer(self):
        out = componer_mensaje("A06 $100.000", markup_percentage=15, strip_patterns=(), footer="")
        self.assertEqual(out, "A06 $115.000")


if __name__ == "__main__":
    unittest.main()
