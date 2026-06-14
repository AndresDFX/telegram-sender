"""Domain: resolución de destinatarios por listas de distribución."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from domain.recipients import (  # noqa: E402
    filtrar_destinatarios,
    ids_de_listas_activas,
    ids_excluidos_por_patron,
)

LISTS = [
    {"name": "VIP", "ids": ["1", "2"]},
    {"name": "mayoristas", "ids": ["3", "4"]},
    {"name": "vacia", "ids": []},
]
TODOS = ["1", "2", "3", "4", "5"]


class ExcluirPorPatronTests(unittest.TestCase):
    CONTACTOS = [
        {"chatId": "1", "name": "FAM Juan"},
        {"chatId": "2", "name": "María (familia)"},
        {"chatId": "3", "name": "Proveedor Norte"},
        {"chatId": "4", "name": "cliente #vip"},
        {"id": "5", "name": ""},
    ]

    def test_substring_case_insensitive(self):
        # "fam" coincide con "FAM Juan" y "familia" (sin distinguir mayúsculas)
        self.assertEqual(ids_excluidos_por_patron(self.CONTACTOS, ["fam"]), {"1", "2"})

    def test_simbolo_y_varios_patrones(self):
        self.assertEqual(ids_excluidos_por_patron(self.CONTACTOS, ["#", "norte"]), {"3", "4"})

    def test_acepta_id_o_chatId_y_ignora_sin_nombre(self):
        self.assertEqual(ids_excluidos_por_patron(self.CONTACTOS, ["cliente"]), {"4"})

    def test_patrones_vacios_no_excluye(self):
        self.assertEqual(ids_excluidos_por_patron(self.CONTACTOS, []), set())
        self.assertEqual(ids_excluidos_por_patron(self.CONTACTOS, ["", "  "]), set())


class RecipientsTests(unittest.TestCase):
    def test_modo_all_devuelve_todos(self):
        self.assertEqual(filtrar_destinatarios(TODOS, LISTS, {"mode": "all", "lists": []}), TODOS)

    def test_all_quita_excluidos_siempre(self):
        self.assertEqual(
            filtrar_destinatarios(TODOS, LISTS, {"mode": "all", "lists": ["VIP"]}, excluidos=["5"]),
            ["1", "2", "3", "4"],
        )

    def test_only_whitelist_union_de_listas_activas(self):
        self.assertEqual(
            filtrar_destinatarios(TODOS, LISTS, {"mode": "only", "lists": ["VIP", "mayoristas"]}),
            ["1", "2", "3", "4"],
        )

    def test_only_ignora_ids_que_no_son_contactos(self):
        listas = [{"name": "VIP", "ids": ["1", "999"]}]  # 999 no está en TODOS
        self.assertEqual(filtrar_destinatarios(TODOS, listas, {"mode": "only", "lists": ["VIP"]}), ["1"])

    def test_only_tambien_respeta_excluidos(self):
        self.assertEqual(
            filtrar_destinatarios(TODOS, LISTS, {"mode": "only", "lists": ["VIP"]}, excluidos=["2"]),
            ["1"],
        )

    def test_except_blacklist_quita_listas_activas(self):
        self.assertEqual(
            filtrar_destinatarios(TODOS, LISTS, {"mode": "except", "lists": ["VIP"]}),
            ["3", "4", "5"],
        )

    def test_target_vacio_o_invalido_es_all(self):
        self.assertEqual(filtrar_destinatarios(TODOS, LISTS, {}), TODOS)
        self.assertEqual(filtrar_destinatarios(TODOS, LISTS, {"mode": "raro", "lists": []}), TODOS)

    def test_ids_de_listas_activas_union(self):
        self.assertEqual(
            ids_de_listas_activas(LISTS, {"lists": ["VIP", "mayoristas"]}),
            {"1", "2", "3", "4"},
        )

    def test_conserva_tipo_int(self):
        listas = [{"name": "L", "ids": [2]}]
        self.assertEqual(filtrar_destinatarios([1, 2, 3], listas, {"mode": "only", "lists": ["L"]}), [2])


if __name__ == "__main__":
    unittest.main()
