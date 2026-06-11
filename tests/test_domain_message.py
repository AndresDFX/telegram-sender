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
        self.assertNotIn("IPRO PARTS", out)           # marca del canal fuente fuera
        self.assertIn("A06 4-64GB $374.000", out)     # markup aplicado
        self.assertNotIn("$325.000", out)
        self.assertTrue(out.endswith(footer))         # footer al final

    def test_quita_marca_ipro_parts(self):
        for variante in (
            "🔥 IPRO PARTS 🔥\nA06 $100.000", "IPROPARTS\nA06 $100.000", "ipro parts\nA06 $100.000",
            "ipro_parts\nA06 $100.000", "IPRO-PARTS\nA06 $100.000", "@iproparts\nA06 $100.000",
        ):
            out = componer_mensaje(variante, markup_percentage=0, footer="")
            self.assertNotIn("PARTS", out.upper())
            self.assertNotIn("IPROPARTS", out.upper())
            self.assertIn("A06", out)

    def test_quita_ubicacion_variada(self):
        src = (
            "IPRO PARTS\n"
            "Dirección: Cra 50 # 30-20\n"
            "Centro Comercial La Fortuna\n"
            "PISO 2 LOCAL C1-C4\n"
            "HORARIO: 8:30AM a 6:30PM\n"
            "\nSAMSUNG\nA06 4-64GB $325.000\nINTEL CORE i5"
        )
        out = componer_mensaje(src, markup_percentage=15, footer="")
        for fuera in ("IPRO", "Dirección", "Cra 50", "Centro Comercial", "PISO 2", "HORARIO"):
            self.assertNotIn(fuera, out)
        # producto y specs intactos (no se confunden con dirección/marca)
        self.assertIn("SAMSUNG", out)
        self.assertIn("A06 4-64GB $374.000", out)
        self.assertIn("INTEL CORE i5", out)

    def test_torre_solo_como_ubicacion_no_toca_producto(self):
        out = quitar_lineas(
            "TORRE A LOCAL 5\nSAMSUNG S24 TORRE Edition $5.600.000",
            ("\\btorre\\s+(?:\\d+|[a-z])\\b",),
        )
        self.assertNotIn("TORRE A LOCAL 5", out)               # ubicación fuera
        self.assertIn("S24 TORRE Edition", out)                # producto intacto

    def test_direccion_con_cardinal_intermedio(self):
        from domain.message import DEFAULT_LOCATION_PATTERNS
        out = quitar_lineas("Autopista Norte # 100\nA06 4-64GB", DEFAULT_LOCATION_PATTERNS)
        self.assertNotIn("Autopista", out)
        self.assertIn("A06 4-64GB", out)

    def test_sin_footer(self):
        out = componer_mensaje("A06 $100.000", markup_percentage=15, strip_patterns=(), footer="")
        self.assertEqual(out, "A06 $115.000")


if __name__ == "__main__":
    unittest.main()
