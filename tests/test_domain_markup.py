"""Domain: markup (formato colombiano, redondeo al mil hacia arriba, solo precios con $)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from domain.markup import aplicar_markup  # noqa: E402


class MarkupTests(unittest.TestCase):
    def test_redondea_al_mil_hacia_arriba(self):
        self.assertEqual(aplicar_markup("$325.000", 15), "$374.000")

    def test_millones_formato_colombiano(self):
        self.assertEqual(aplicar_markup("$1.150.000", 15), "$1.323.000")
        self.assertEqual(aplicar_markup("$1.250.000", 15), "$1.438.000")

    def test_precio_exacto_en_miles_no_sube_de_mas(self):
        self.assertEqual(aplicar_markup("$100.000", 15), "$115.000")

    def test_NO_toca_modelo_ni_specs(self):
        result = aplicar_markup("A06 4-64GB $325.000", 15)
        self.assertIn("A06 4-64GB", result)
        self.assertIn("$374.000", result)
        self.assertNotIn("$325.000", result)

    def test_NO_marca_numeros_sin_moneda(self):
        for original in ("A37 5G 6-128GB", "REDMI PAD 2 8-256GB", "05/06/2026", "3218354891", "8:30AM"):
            self.assertEqual(aplicar_markup(original, 15), original)

    def test_lista_multilinea(self):
        result = aplicar_markup("SAMSUNG\nA06 4-64GB $325.000\nA07 6-128GB $430.000", 15)
        self.assertIn("A06 4-64GB $374.000", result)
        self.assertIn("A07 6-128GB $495.000", result)

    def test_con_espacio_tras_simbolo(self):
        self.assertEqual(aplicar_markup("$ 100.000", 15), "$115.000")

    def test_reconoce_emojis_de_moneda(self):
        billete, dolar = "\U0001F4B8", "\U0001F4B2"  # 💸 y 💲 que usa el canal
        self.assertEqual(aplicar_markup(billete + "2.400.000", 15), billete + "2.760.000")
        self.assertEqual(aplicar_markup(dolar + "1.950.000", 15), dolar + "2.243.000")

    def test_default_15_y_sin_precios(self):
        self.assertEqual(aplicar_markup("$100.000"), "$115.000")  # default 15%
        self.assertEqual(aplicar_markup("sin precios"), "sin precios")


if __name__ == "__main__":
    unittest.main()
