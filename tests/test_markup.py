"""Tests unitarios para la lógica de markup."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from markup import aplicar_markup  # noqa: E402


class MarkupTests(unittest.TestCase):
    def test_aplica_quince_por_ciento(self):
        original = "Zapatillas $100.00 en stock"
        result = aplicar_markup(original, porcentaje=15)
        self.assertIn("115.00", result)
        self.assertNotIn("100.00", result)

    def test_formato_europeo(self):
        original = "Precio: 1.250,50"
        result = aplicar_markup(original, porcentaje=10)
        self.assertIn("1.375,55", result)

    def test_sin_precios_no_modifica(self):
        original = "Mensaje sin números de precio"
        self.assertEqual(aplicar_markup(original), original)


if __name__ == "__main__":
    unittest.main()
