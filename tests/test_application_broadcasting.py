"""Application: BroadcastList (composición del mensaje + encolado, con fakes)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from application.broadcasting import BroadcastList  # noqa: E402


class FakeSubs:
    def __init__(self, ids):
        self._ids = ids

    def listar_activos(self):
        return list(self._ids)

    def registrar(self, *a):
        pass

    def marcar_inactivo(self, *a):
        pass


class FakeQueue:
    def __init__(self):
        self.calls = []

    def encolar(self, text, chat_ids, image_url=None, image_key=None, broadcast_id=None):
        self.calls.append((text, list(chat_ids), image_url, image_key))
        return 1


class FakeConfig:
    def __init__(self, **overrides):
        self.cfg = {
            "source_channel": "ch",
            "markup_percentage": 15.0,
            "currency_symbols": "$",
            "strip_patterns": ["ubicad"],
            "whatsapp_footer": "📲 WhatsApp 300",
            "image_url": "",
        }
        self.cfg.update(overrides)

    def get(self):
        return dict(self.cfg)

    def set(self, c):
        self.cfg.update(c)
        return dict(self.cfg)


class BroadcastListTests(unittest.TestCase):
    def test_compone_quita_ubicacion_markup_y_footer(self):
        queue = FakeQueue()
        bl = BroadcastList(FakeSubs(["1", "2"]), queue, FakeConfig())
        res = bl("UBICADOS aqui\nA06 4-64GB $100.000")

        self.assertEqual(res["batches"], 1)
        self.assertEqual(res["subscribers"], 2)
        self.assertIn("broadcast_id", res)
        text, ids, image_url, image_key = queue.calls[0]
        self.assertNotIn("UBICADOS", text)            # ubicación fuera
        self.assertIn("$115.000", text)               # markup aplicado
        self.assertTrue(text.endswith("📲 WhatsApp 300"))  # footer
        self.assertEqual(ids, ["1", "2"])
        self.assertIsNone(image_url)

    def test_pasa_image_url_si_configurada(self):
        queue = FakeQueue()
        BroadcastList(FakeSubs(["1"]), queue, FakeConfig(image_url="http://img/p.jpg"))("A $100.000")
        self.assertEqual(queue.calls[0][2], "http://img/p.jpg")

    def test_excluye_ids(self):
        queue = FakeQueue()
        BroadcastList(FakeSubs(["1", "2", "3"]), queue, FakeConfig(excluded_ids=["2"]))("A $100.000")
        self.assertEqual(queue.calls[0][1], ["1", "3"])  # 2 excluido

    def test_pasa_image_key(self):
        queue = FakeQueue()
        BroadcastList(FakeSubs(["1"]), queue, FakeConfig(image_key="images/broadcast.jpg"))("A $100.000")
        self.assertEqual(queue.calls[0][3], "images/broadcast.jpg")

    def test_reenvia_a_whatsapp_si_activo(self):
        class FakeWa:
            def __init__(self):
                self.calls = []

            def forward(self, text, image_url, exclude, *, mode="all", list_ids=None, broadcast_id=None, broadcasts_table=None):
                self.calls.append((text, image_url, list(exclude), mode, list(list_ids or [])))
                return {"accepted": True}

        wa = FakeWa()
        BroadcastList(FakeSubs(["1"]), FakeQueue(), FakeConfig(whatsapp_enabled=True, image_url="http://img"), whatsapp=wa)("A $100.000")
        self.assertEqual(len(wa.calls), 1)
        text, image_url, exclude, mode, list_ids = wa.calls[0]
        self.assertIn("$115.000", text)
        self.assertEqual(image_url, "http://img")

    def test_telegram_whitelist_solo_envia_a_listas_activas(self):
        queue = FakeQueue()
        cfg = FakeConfig(
            telegram_lists=[{"name": "VIP", "ids": ["1", "3"]}, {"name": "otros", "ids": ["2"]}],
            telegram_target={"mode": "only", "lists": ["VIP"]},
        )
        BroadcastList(FakeSubs(["1", "2", "3", "4"]), queue, cfg)("A $100.000")
        self.assertEqual(queue.calls[0][1], ["1", "3"])  # solo VIP, intersecado con contactos

    def test_telegram_blacklist_excluye_listas_activas(self):
        queue = FakeQueue()
        cfg = FakeConfig(
            telegram_lists=[{"name": "bloqueados", "ids": ["2", "4"]}],
            telegram_target={"mode": "except", "lists": ["bloqueados"]},
        )
        BroadcastList(FakeSubs(["1", "2", "3", "4"]), queue, cfg)("A $100.000")
        self.assertEqual(queue.calls[0][1], ["1", "3"])

    def test_whatsapp_recibe_modo_y_list_ids(self):
        class FakeWa:
            def __init__(self):
                self.calls = []

            def forward(self, text, image_url, exclude, *, mode="all", list_ids=None, broadcast_id=None, broadcasts_table=None):
                self.calls.append((mode, list(list_ids or [])))
                return {}

        wa = FakeWa()
        cfg = FakeConfig(
            whatsapp_enabled=True,
            whatsapp_lists=[{"name": "clientes", "ids": ["57300@s.whatsapp.net", "57301@s.whatsapp.net"]}],
            whatsapp_target={"mode": "only", "lists": ["clientes"]},
        )
        BroadcastList(FakeSubs(["1"]), FakeQueue(), cfg, whatsapp=wa)("A $100.000")
        mode, list_ids = wa.calls[0]
        self.assertEqual(mode, "only")
        self.assertEqual(list_ids, ["57300@s.whatsapp.net", "57301@s.whatsapp.net"])

    def test_envio_manual_texto_crudo_a_ambos_canales(self):
        class FakeWa:
            def __init__(self):
                self.calls = []

            def forward(self, text, image_url, exclude, *, mode="all", list_ids=None, broadcast_id=None, broadcasts_table=None):
                self.calls.append((text, broadcast_id))
                return {}

        class FakeBroadcasts:
            def __init__(self):
                self.jobs = []

            def crear(self, broadcast_id, text, source, channels, tg_total=0):
                self.jobs.append({"id": broadcast_id, "source": source, "channels": list(channels), "tg_total": tg_total})

        queue, wa, store = FakeQueue(), FakeWa(), FakeBroadcasts()
        # WhatsApp manual exige una lista activa en modo "only" (seguridad anti-mando-a-todos)
        cfg = FakeConfig(
            whatsapp_lists=[{"name": "clientes", "ids": ["57300@s.whatsapp.net"]}],
            whatsapp_target={"mode": "only", "lists": ["clientes"]},
        )
        bl = BroadcastList(FakeSubs(["1", "2"]), queue, cfg, whatsapp=wa, broadcasts=store)
        res = bl.enviar_manual("Hola mundo $100.000", telegram=True, whatsapp=True)

        # texto CRUDO (sin markup ni footer)
        self.assertEqual(queue.calls[0][0], "Hola mundo $100.000")
        self.assertEqual(queue.calls[0][1], ["1", "2"])
        self.assertEqual(wa.calls[0][0], "Hola mundo $100.000")
        # job registrado como manual, en ambos canales, con el mismo id propagado
        self.assertEqual(store.jobs[0]["source"], "manual")
        self.assertEqual(store.jobs[0]["channels"], ["telegram", "whatsapp"])
        self.assertEqual(res["broadcast_id"], store.jobs[0]["id"])
        self.assertEqual(wa.calls[0][1], res["broadcast_id"])

    def test_envio_manual_whatsapp_con_lista_elegida(self):
        # Elegir una lista en el compositor permite el envío aunque el target global sea "all".
        class FakeWa:
            def __init__(self):
                self.calls = []

            def forward(self, text, image_url, exclude, *, mode="all", list_ids=None, broadcast_id=None, broadcasts_table=None):
                self.calls.append((mode, list(list_ids or [])))
                return {"accepted": True}

        wa = FakeWa()
        cfg = FakeConfig(whatsapp_lists=[{"name": "Prueba", "ids": ["57300@s.whatsapp.net"]}])  # target global "all" por defecto
        bl = BroadcastList(FakeSubs(["1"]), FakeQueue(), cfg, whatsapp=wa)
        bl.enviar_manual("hola", telegram=False, whatsapp=True, whatsapp_list="Prueba")
        self.assertEqual(wa.calls[0], ("only", ["57300@s.whatsapp.net"]))

    def test_previsualizar_cuenta_destinatarios(self):
        cfg = FakeConfig(
            telegram_lists=[{"name": "VIP", "ids": ["1", "3"]}],
            whatsapp_lists=[{"name": "Prueba", "ids": ["57300@s.whatsapp.net", "57301@s.whatsapp.net"]}],
        )
        bl = BroadcastList(FakeSubs(["1", "2", "3", "4"]), FakeQueue(), cfg, whatsapp=object())
        prev = bl.previsualizar(telegram=True, whatsapp=True, telegram_list="VIP", whatsapp_list="Prueba")
        self.assertEqual(prev["telegram"], 2)  # 1 y 3 (VIP ∩ contactos)
        self.assertEqual(prev["whatsapp"], 2)

    def test_envio_manual_whatsapp_sin_lista_es_rechazado(self):
        # Sin lista activa de WhatsApp en modo "only", el envío manual a WhatsApp se rechaza.
        bl = BroadcastList(FakeSubs(["1"]), FakeQueue(), FakeConfig(), whatsapp=object(), broadcasts=None)
        with self.assertRaises(ValueError):
            bl.enviar_manual("hola", telegram=False, whatsapp=True)

    def test_no_reenvia_whatsapp_si_desactivado(self):
        class FakeWa:
            def __init__(self):
                self.calls = []

            def forward(self, *a, **kw):
                self.calls.append((a, kw))
                return {}

        wa = FakeWa()
        BroadcastList(FakeSubs(["1"]), FakeQueue(), FakeConfig(whatsapp_enabled=False), whatsapp=wa)("A $100.000")
        self.assertEqual(wa.calls, [])


class FakePlans:
    def __init__(self):
        self.creados = []

    def crear(self, plan_id, **kw):
        self.creados.append({"id": plan_id, **kw})


class SchedulerPathTests(unittest.TestCase):
    """Con scheduling_enabled + store de planes: NO se encola/forwarda; se crea un plan."""

    def test_canal_crea_plan_fraccionado_y_no_encola(self):
        queue, plans = FakeQueue(), FakePlans()
        cfg = FakeConfig(scheduling_enabled=True, batch_size=2)
        bl = BroadcastList(FakeSubs(["1", "2", "3"]), queue, cfg, plans=plans)
        res = bl("A06 $100.000")
        self.assertTrue(res["scheduled"])
        self.assertEqual(queue.calls, [])  # no se vuelca a SQS
        plan = plans.creados[0]
        self.assertEqual([len(l) for l in plan["tg_lotes"]], [2, 1])  # 3 contactos, lote 2 -> [2,1]
        self.assertFalse(plan["wa_enabled"])

    def test_manual_wa_resuelve_total_via_contar(self):
        class FakeWa:
            def contar(self, *, mode="all", list_ids=None, exclude=None):
                return 7

            def forward(self, *a, **k):
                return {}

        plans = FakePlans()
        cfg = FakeConfig(
            scheduling_enabled=True, batch_size=150,
            whatsapp_lists=[{"name": "c", "ids": ["57300@s.whatsapp.net"]}],
            whatsapp_target={"mode": "only", "lists": ["c"]},
        )
        bl = BroadcastList(FakeSubs(["1"]), FakeQueue(), cfg, whatsapp=FakeWa(), plans=plans)
        bl.enviar_manual("hola", telegram=True, whatsapp=True, whatsapp_list="c")
        plan = plans.creados[0]
        self.assertTrue(plan["wa_enabled"])
        self.assertTrue(plan["wa_resolved"])
        self.assertEqual(plan["wa_total"], 7)

    def test_fallback_legacy_si_no_hay_plans(self):
        # scheduling_enabled pero sin store de planes -> envío inmediato (compatibilidad)
        queue = FakeQueue()
        bl = BroadcastList(FakeSubs(["1"]), queue, FakeConfig(scheduling_enabled=True), plans=None)
        bl("A $100.000")
        self.assertEqual(len(queue.calls), 1)


if __name__ == "__main__":
    unittest.main()
