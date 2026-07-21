"""Domain: componer_mensaje (quitar ubicación + markup + footer)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from domain.message import DEFAULT_PHONE_PATTERNS, componer_mensaje, quitar_lineas  # noqa: E402

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
            # con TILDE (así lo publica el canal en el encabezado): antes no casaba y colaba la marca
            "👩🏻‍💻 IPRÓ PARTS 👩🏻‍💻\nA06 $100.000", "@IPRÓPARTS\nA06 $100.000",
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


class TelefonoTests(unittest.TestCase):
    def _quita(self, linea):
        # True si la línea (con un producto debajo) se elimina por contener teléfono.
        out = quitar_lineas(linea + "\nA06 4-64GB", DEFAULT_PHONE_PATTERNS)
        return linea not in out and "A06 4-64GB" in out

    def test_quita_telefonos_formateados_o_etiquetados(self):
        for tel in (
            "320 123 4567", "300-123-4567", "320.123.4567", "300  123  4567",  # formateados
            "300 - 123 - 4567", "WhatsApp +57 300 123 4567", "+573001234567",   # +57 / espaciado
            "Tel: (601) 234 5678", "Cel 350 1234567", "57 311 222 3344",        # parens / etiqueta
            "📞 Pedidos: 3201234567", "Cel 3001234567", "WhatsApp 3001234567",   # etiqueta + pelado
        ):
            self.assertTrue(self._quita(tel), f"no quitó: {tel!r}")

    def test_conserva_numeros_ambiguos_sin_senal_de_telefono(self):
        # 10 dígitos PEGADOS sin separador/indicativo/etiqueta: refs/costos/cantidades -> NO tocar
        for linea in (
            "Item 6012345678", "REF 3001234567", "Costo 3123456789", "3001234567",
            "A06 4-64GB $325.000", "iPhone 13 256GB $3.200.000", "REDMI 12 6-128GB",
            "INTEL CORE i5", "REF 1234567890", "05/06/2026",
        ):
            out = quitar_lineas(linea + "\nMARCA", DEFAULT_PHONE_PATTERNS)
            self.assertIn(linea, out, f"borró por error: {linea!r}")

    def test_componer_quita_telefono_y_conserva_lista(self):
        src = "IPRO PARTS\n📲 Pedidos: 320 123 4567\nSAMSUNG\nA06 4-64GB $325.000"
        out = componer_mensaje(src, markup_percentage=15, footer="")
        self.assertNotIn("320 123 4567", out)
        self.assertNotIn("Pedidos", out)               # la línea de contacto desaparece
        self.assertIn("SAMSUNG", out)
        self.assertIn("A06 4-64GB $374.000", out)

    def test_m3_telefono_en_linea_mixta_conserva_producto_y_precio(self):
        # M3: una línea que mezcla producto + teléfono + precio conserva el producto y el precio
        # (antes se borraba la línea entera con su precio). Solo se quita el número de teléfono.
        out = componer_mensaje("A06 4-64GB 300 123 4567 $325.000", markup_percentage=15, footer="")
        self.assertNotIn("300 123 4567", out)   # el teléfono se quita
        self.assertIn("A06 4-64GB", out)         # el producto se conserva
        self.assertIn("$374.000", out)           # y el precio (con markup) también


class ComponerConDesgloseTests(unittest.TestCase):
    def test_devuelve_mensaje_y_desglose(self):
        from domain.message import componer_con_desglose
        src = "UBICADOS aqui\nSAMSUNG A06 $325.000\n📲 Pedidos: 300 123 4567"
        msg, desglose = componer_con_desglose(src, markup_percentage=15, currency_symbols="$", footer="")
        self.assertNotIn("UBICADOS", msg)                 # limpieza aplicada
        self.assertIn("$374.000", msg)                    # markup en el mensaje final
        self.assertEqual(len(desglose), 1)
        self.assertEqual((desglose[0]["anterior"], desglose[0]["nuevo"]), ("$325.000", "$374.000"))

    def test_vacio_da_desglose_vacio(self):
        from domain.message import componer_con_desglose
        msg, desglose = componer_con_desglose("UBICADOS EN EL C.C", markup_percentage=15, footer="X")
        self.assertEqual((msg, desglose), ("", []))


class ComponerVacioTests(unittest.TestCase):
    def test_solo_ubicacion_no_difunde_solo_footer(self):
        # M4: si la limpieza vacía el cuerpo, devolver "" (no "solo footer", que sería spam).
        out = componer_mensaje("IPRO PARTS\nUBICADOS EN EL C.C", footer="📲 Pedidos: +57 300", markup_percentage=15)
        self.assertEqual(out, "")

    def test_texto_vacio_da_vacio(self):
        self.assertEqual(componer_mensaje("   \n\n  ", footer="X"), "")


if __name__ == "__main__":
    unittest.main()
