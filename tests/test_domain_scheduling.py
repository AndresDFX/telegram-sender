"""Domain: políticas de scheduling (tope de lote, ventana horaria, delay aleatorio)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from domain.scheduling import cap_batch_size, delay_aleatorio, total_lotes, ventana_abierta  # noqa: E402

H = 3600


class CapBatchSizeTests(unittest.TestCase):
    def test_tope_150(self):
        self.assertEqual(cap_batch_size(500), 150)
        self.assertEqual(cap_batch_size(150), 150)
        self.assertEqual(cap_batch_size(80), 80)

    def test_minimo_1(self):
        self.assertEqual(cap_batch_size(0), 1)
        self.assertEqual(cap_batch_size(-5), 1)

    def test_invalido_va_al_maximo(self):
        self.assertEqual(cap_batch_size("abc"), 150)
        self.assertEqual(cap_batch_size(None), 150)

    def test_baja_el_tope_configurable(self):
        self.assertEqual(cap_batch_size(99, maximo=50), 50)


class TotalLotesTests(unittest.TestCase):
    def test_division_techo(self):
        self.assertEqual(total_lotes(300, 150), 2)
        self.assertEqual(total_lotes(301, 150), 3)
        self.assertEqual(total_lotes(1, 150), 1)
        self.assertEqual(total_lotes(0, 150), 0)


class VentanaTests(unittest.TestCase):
    def test_deshabilitada_siempre_abierta(self):
        self.assertTrue(ventana_abierta(22 * H, enabled=False, start="08:00", end="20:00", tz_offset_min=0))

    def test_dentro_y_fuera_utc(self):
        self.assertTrue(ventana_abierta(10 * H, enabled=True, start="08:00", end="20:00", tz_offset_min=0))
        self.assertFalse(ventana_abierta(22 * H, enabled=True, start="08:00", end="20:00", tz_offset_min=0))
        self.assertFalse(ventana_abierta(6 * H, enabled=True, start="08:00", end="20:00", tz_offset_min=0))

    def test_cruza_medianoche(self):
        # ventana 22:00 -> 06:00
        self.assertTrue(ventana_abierta(23 * H, enabled=True, start="22:00", end="06:00", tz_offset_min=0))
        self.assertTrue(ventana_abierta(2 * H, enabled=True, start="22:00", end="06:00", tz_offset_min=0))
        self.assertFalse(ventana_abierta(12 * H, enabled=True, start="22:00", end="06:00", tz_offset_min=0))

    def test_offset_horario(self):
        # UTC 00:00 con offset -300 (UTC-5) -> 19:00 local -> dentro de 08:00-20:00
        self.assertTrue(ventana_abierta(0, enabled=True, start="08:00", end="20:00", tz_offset_min=-300))
        # UTC 02:00 con offset -300 -> 21:00 local -> fuera
        self.assertFalse(ventana_abierta(2 * H, enabled=True, start="08:00", end="20:00", tz_offset_min=-300))

    def test_ventana_degenerada_es_total(self):
        self.assertTrue(ventana_abierta(3 * H, enabled=True, start="08:00", end="08:00", tz_offset_min=0))


class DelayTests(unittest.TestCase):
    def test_en_rango(self):
        for _ in range(50):
            d = delay_aleatorio(1.0, 4.0)
            self.assertGreaterEqual(d, 1.0)
            self.assertLessEqual(d, 4.0)

    def test_min_mayor_que_max_se_ordena(self):
        d = delay_aleatorio(5.0, 2.0)
        self.assertGreaterEqual(d, 2.0)
        self.assertLessEqual(d, 5.0)

    def test_cero_o_negativo(self):
        self.assertEqual(delay_aleatorio(0, 0), 0.0)
        self.assertEqual(delay_aleatorio(-1, 0), 0.0)

    def test_usa_rng_inyectado(self):
        self.assertEqual(delay_aleatorio(1, 4, rng=lambda a, b: a), 1)


class CapBatchNoFiniteTests(unittest.TestCase):
    def test_inf_nan_no_revientan(self):  # A1: int(float('inf')) lanzaba OverflowError
        for v in ("inf", "1e400", "nan", "Infinity", "-inf"):
            self.assertEqual(cap_batch_size(v), 150)


class VentanaHoraInvalidaTests(unittest.TestCase):
    def test_minutos_invalidos_no_se_reinterpretan(self):
        # M1: "08:90" antes daba 9:30 (570 min); ahora es inválido -> fallback (start=00:00).
        # Con start inválido (->00:00) y end 20:00, las 06:00 caen DENTRO (antes caían fuera con 08:00).
        self.assertTrue(ventana_abierta(6 * H, enabled=True, start="08:90", end="20:00", tz_offset_min=0))


if __name__ == "__main__":
    unittest.main()
