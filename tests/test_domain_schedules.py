"""Domain: cálculo del próximo disparo de mensajes programados (once/daily/weekly)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from domain.schedules import hhmm, proximo_run  # noqa: E402

# 2024-01-01 00:00:00 UTC (lunes, weekday()==0)
LUN_0000 = 1704067200
H = 3600
D = 86400


class HhmmTests(unittest.TestCase):
    def test_validos(self):
        self.assertEqual(hhmm("09:00"), (9, 0))
        self.assertEqual(hhmm("23:59"), (23, 59))
        self.assertEqual(hhmm("0:5"), (0, 5))

    def test_invalidos(self):
        for v in ("99:99", "24:00", "9", "", "ab:cd", None, "12:60"):
            self.assertIsNone(hhmm(v))


class OnceTests(unittest.TestCase):
    def test_once_no_se_recalcula(self):
        self.assertIsNone(proximo_run("once", "", [], 0, LUN_0000))


class DailyTests(unittest.TestCase):
    def test_hoy_mismo_dia_si_falta(self):
        # base 08:00 UTC -> próximo 09:00 hoy
        self.assertEqual(proximo_run("daily", "09:00", None, 0, LUN_0000 + 8 * H), LUN_0000 + 9 * H)

    def test_siguiente_dia_si_ya_paso(self):
        # base 10:00 -> mañana 09:00
        self.assertEqual(proximo_run("daily", "09:00", None, 0, LUN_0000 + 10 * H), LUN_0000 + D + 9 * H)

    def test_exacto_salta_al_siguiente(self):
        # base == 09:00 exacto -> estrictamente posterior -> mañana
        self.assertEqual(proximo_run("daily", "09:00", None, 0, LUN_0000 + 9 * H), LUN_0000 + D + 9 * H)

    def test_zona_horaria_utc_menos_5(self):
        # 09:00 local UTC-5 == 14:00 UTC. base 08:00 UTC (=03:00 local) -> hoy 14:00 UTC
        self.assertEqual(proximo_run("daily", "09:00", None, -300, LUN_0000 + 8 * H), LUN_0000 + 14 * H)


class WeeklyTests(unittest.TestCase):
    def test_mismo_dia_lunes(self):
        # base lunes 00:00, día=lunes(0), 09:00 -> hoy 09:00
        self.assertEqual(proximo_run("weekly", "09:00", [0], 0, LUN_0000), LUN_0000 + 9 * H)

    def test_miercoles_desde_lunes(self):
        # base lunes 00:00, día=miércoles(2) -> +2 días 09:00
        self.assertEqual(proximo_run("weekly", "09:00", [2], 0, LUN_0000), LUN_0000 + 2 * D + 9 * H)

    def test_envuelve_a_la_semana_siguiente(self):
        # base sábado 12:00 (LUN+5d+12h), día=lunes(0) -> lunes siguiente (LUN+7d) 09:00
        base = LUN_0000 + 5 * D + 12 * H
        self.assertEqual(proximo_run("weekly", "09:00", [0], 0, base), LUN_0000 + 7 * D + 9 * H)

    def test_sin_dias_es_none(self):
        self.assertIsNone(proximo_run("weekly", "09:00", [], 0, LUN_0000))

    def test_hora_invalida_es_none(self):
        self.assertIsNone(proximo_run("daily", "99:99", None, 0, LUN_0000))

    def test_dias_malformados_no_revientan(self):
        # M2: días no numéricos antes lanzaban ValueError; ahora se ignoran (None si no queda ninguno).
        self.assertIsNone(proximo_run("weekly", "09:00", ["lunes", None, "x"], 0, LUN_0000))
        # con un día válido mezclado, sigue funcionando
        self.assertEqual(proximo_run("weekly", "09:00", ["x", 0], 0, LUN_0000), LUN_0000 + 9 * H)


if __name__ == "__main__":
    unittest.main()
