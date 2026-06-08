"""Tests del markup: solo precios con $, formato colombiano, redondeo al mil hacia arriba."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from markup import aplicar_markup  # noqa: E402


class MarkupTests(unittest.TestCase):
    def test_redondea_al_mil_hacia_arriba(self):
        # 325.000 * 1.15 = 373.750 → arriba al mil = 374.000
        self.assertEqual(aplicar_markup("$325.000", 15), "$374.000")

    def test_millones_formato_colombiano(self):
        # 1.150.000 * 1.15 = 1.322.500 → 1.323.000
        self.assertEqual(aplicar_markup("$1.150.000", 15), "$1.323.000")
        # 1.250.000 * 1.15 = 1.437.500 → 1.438.000
        self.assertEqual(aplicar_markup("$1.250.000", 15), "$1.438.000")

    def test_precio_exacto_en_miles_no_sube_de_mas(self):
        # 100.000 * 1.15 = 115.000 (ya múltiplo de mil) → 115.000
        self.assertEqual(aplicar_markup("$100.000", 15), "$115.000")

    def test_NO_toca_modelo_ni_specs(self):
        original = "A06 4-64GB $325.000"
        result = aplicar_markup(original, 15)
        self.assertIn("A06 4-64GB", result)  # modelo y specs intactos
        self.assertIn("$374.000", result)
        self.assertNotIn("$325.000", result)

    def test_NO_marca_numeros_sin_moneda(self):
        for original in ("A37 5G 6-128GB", "REDMI PAD 2 8-256GB", "05/06/2026", "3218354891", "8:30AM"):
            self.assertEqual(aplicar_markup(original, 15), original)

    def test_lista_multilinea(self):
        original = "SAMSUNG\nA06 4-64GB $325.000\nA07 6-128GB $430.000"
        result = aplicar_markup(original, 15)
        self.assertIn("A06 4-64GB $374.000", result)   # 373.750 → 374.000
        self.assertIn("A07 6-128GB $495.000", result)   # 494.500 → 495.000
        self.assertIn("SAMSUNG", result)

    def test_con_espacio_tras_simbolo(self):
        self.assertEqual(aplicar_markup("$ 100.000", 15), "$115.000")

    def test_sin_precios_no_modifica(self):
        self.assertEqual(aplicar_markup("Mensaje sin precios"), "Mensaje sin precios")


if __name__ == "__main__":
    unittest.main()
