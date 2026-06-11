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

    # --- robustez: el original no siempre llega igual ---
    def test_apostrofo_miles(self):
        self.assertEqual(aplicar_markup("$1'150.000", 15), "$1.323.000")   # apóstrofo recto
        self.assertEqual(aplicar_markup("$1’150.000", 15), "$1.323.000")   # apóstrofo tipográfico
        self.assertEqual(aplicar_markup("$1'150'000", 15), "$1.323.000")   # apóstrofo en ambos grupos

    def test_sin_separador_de_miles(self):
        self.assertEqual(aplicar_markup("$325000", 15), "$374.000")

    def test_palabra_cop(self):
        self.assertEqual(aplicar_markup("COP 325.000", 15), "$374.000")
        self.assertEqual(aplicar_markup("COP $325.000", 15), "$374.000")
        self.assertEqual(aplicar_markup("325.000 COP", 15), "$374.000")
        self.assertEqual(aplicar_markup("cop 1.150.000", 15), "$1.323.000")

    def test_simbolo_despues_del_numero(self):
        self.assertEqual(aplicar_markup("325.000$", 15), "$374.000")

    def test_conserva_simbolo_emoji_lider(self):
        billete = "\U0001F4B8"
        self.assertEqual(aplicar_markup(billete + " 1.000.000", 15), billete + "1.150.000")

    def test_no_confunde_cop_dentro_de_palabra(self):
        # 'COP' dentro de otra palabra (SCOPE) no debe activarse como moneda
        self.assertEqual(aplicar_markup("SCOPE 8-256GB", 15), "SCOPE 8-256GB")

    def test_no_marca_telefono_con_puntos_sin_moneda(self):
        for original in ("320.123.456", "320.123.4567", "Cra 50 # 30-20", "01.02.2026"):
            self.assertEqual(aplicar_markup(original, 15), original)

    def test_simbolo_no_cruza_salto_de_linea(self):
        # "325.000" (sin símbolo) seguido de línea con "$400.000": el símbolo-después NO debe
        # cruzar el \n y robar el "$" de la línea siguiente (dejaría 400.000 sin markup).
        out = aplicar_markup("A06 325.000\n$400.000", 15)
        self.assertIn("$460.000", out)            # 400.000 SÍ recibe markup
        self.assertIn("325.000", out)             # 325.000 sin moneda queda igual
        self.assertNotIn("$374.000", out)         # no se reinterpretó 325.000 como precio

    def test_coma_de_miles_formato_us(self):
        self.assertEqual(aplicar_markup("$325,000", 15), "$374.000")
        self.assertEqual(aplicar_markup("$1,150,000", 15), "$1.323.000")

    def test_no_corrompe_precio_con_cero_a_la_izquierda(self):
        # precio mal formado: se deja intacto en vez de producir un valor erróneo
        self.assertEqual(aplicar_markup("$0325.000", 15), "$0325.000")
        self.assertEqual(aplicar_markup("$00325.000", 15), "$00325.000")

    def test_lista_realista_mixta(self):
        src = "IPHONE 15 $5'600.000\nA06 4-64GB 325.000$\nCOP 1.150.000"
        out = aplicar_markup(src, 15)
        self.assertIn("$6.440.000", out)
        self.assertIn("$374.000", out)
        self.assertIn("$1.323.000", out)


if __name__ == "__main__":
    unittest.main()
