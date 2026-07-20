"""Application: BroadcastList (composición del mensaje + encolado, con fakes)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from application.broadcasting import BroadcastList  # noqa: E402


class FakeSubs:
    def __init__(self, ids, names=None):
        self._ids = ids
        self._names = names or {}

    def listar_activos(self):
        return list(self._ids)

    def listar_todos(self):
        return [{"chatId": str(i), "name": self._names.get(str(i), "")} for i in self._ids]

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
            # WhatsApp "listo" por defecto (enviar_manual exige conectado); los tests que prueban
            # el caso desactivado hacen override con whatsapp_enabled=False.
            "whatsapp_enabled": True,
            "whatsapp_service_url": "http://wa.local",
            "whatsapp_token": "tok",
        }
        self.cfg.update(overrides)

    def get(self):
        return dict(self.cfg)

    def set(self, c):
        self.cfg.update(c)
        return dict(self.cfg)


class FakePlansEspera:
    def __init__(self):
        self.created = []

    def crear(self, bid, **kw):
        self.created.append((bid, kw))


class FakePreview:
    """Enviador de PREVIEW a Mensajes Guardados (userbot 'me')."""

    def __init__(self):
        self.sent = []

    def enviar(self, chat_id, text):
        self.sent.append((chat_id, text))
        return None


def _auto(ids, **over):
    """FakeConfig con una lista 'Auto' (= ids dados) elegida para el envío automático de Telegram.
    A12: el envío automático exige una lista elegida; sin ella un post se trata como captura."""
    over.setdefault("telegram_lists", [{"name": "Auto", "ids": [str(i) for i in ids]}])
    over.setdefault("auto_telegram_list", "Auto")
    return FakeConfig(**over)


class ExclusionPatronTelegramTests(unittest.TestCase):
    # NOTA (A12): el envío AUTOMÁTICO es siempre por LISTA elegida (modo 'only') y NO aplica el patrón
    # de nombre. La auto-exclusión por patrón solo actúa en envíos AMPLIOS (manual sin lista / 'all'),
    # por eso estos tests ejercen _destinatarios_telegram con target amplio (su entrada real ahora).
    def test_canal_excluye_por_patron_de_nombre(self):
        subs = FakeSubs(["1", "2", "3"], names={"1": "FAM Juan", "2": "Cliente", "3": "María"})
        cfg = FakeConfig(currency_symbols="$💸💲", telegram_exclude_patterns=["fam"])
        bl = BroadcastList(subs, FakeQueue(), cfg)
        dest = bl._destinatarios_telegram(cfg.get(), {"mode": "all", "lists": []})
        self.assertEqual(set(dest), {"2", "3"})  # "FAM Juan" auto-excluido

    def test_ids_ad_hoc_ignoran_el_patron_seleccion_explicita(self):
        # Selección EXPLÍCITA por id/número: se envía a EXACTAMENTE esos contactos, AUNQUE su
        # nombre coincida con un patrón (el usuario los eligió por número; un cambio de nombre no
        # debe sacarlos). El patrón solo auto-excluye en envíos amplios (all/except).
        queue = FakeQueue()
        subs = FakeSubs(["1", "2", "3"], names={"1": "FAM Juan", "2": "Cliente", "3": "María"})
        cfg = FakeConfig(telegram_exclude_patterns=["FAM"])
        bl = BroadcastList(subs, queue, cfg)
        clientes = bl._tg_clientes(cfg.get(), True, None, ["1", "2"])
        self.assertEqual(set(clientes), {"1", "2"})  # "1" SÍ se incluye pese al patrón (elegido a mano)

    def test_lista_only_ignora_patron_envia_por_numero(self):
        # Enviar a una LISTA (mode only) valida por id/número: un miembro cuyo nombre coincide con
        # un patrón IGUAL recibe (si se renombró a algo que matchea, no se cae de la lista).
        subs = FakeSubs(["1", "2"], names={"1": "FAM Juan", "2": "Cliente"})
        cfg = FakeConfig(telegram_exclude_patterns=["FAM"],
                         telegram_lists=[{"name": "VIP", "ids": ["1", "2"]}],
                         telegram_target={"mode": "only", "lists": ["VIP"]})
        bl = BroadcastList(subs, FakeQueue(), cfg)
        self.assertEqual(set(bl._destinatarios_telegram(cfg.get())), {"1", "2"})  # "1" (FAM) incluido

    def test_canal_all_sigue_excluyendo_por_patron(self):
        # En envío AMPLIO (all) el patrón SÍ auto-excluye (guardrail intacto).
        subs = FakeSubs(["1", "2"], names={"1": "FAM Juan", "2": "Cliente"})
        cfg = FakeConfig(telegram_exclude_patterns=["FAM"], telegram_target={"mode": "all", "lists": []})
        bl = BroadcastList(subs, FakeQueue(), cfg)
        self.assertEqual(set(bl._destinatarios_telegram(cfg.get())), {"2"})  # "1" excluido en modo all

    def test_sin_patron_no_excluye(self):
        subs = FakeSubs(["1", "2"], names={"1": "FAM Juan", "2": "Cliente"})
        cfg = FakeConfig(currency_symbols="$💸💲")
        bl = BroadcastList(subs, FakeQueue(), cfg)
        self.assertEqual(set(bl._destinatarios_telegram(cfg.get(), {"mode": "all", "lists": []})), {"1", "2"})

    def test_excepcion_incluye_pese_al_patron(self):
        # "1" (FAM Juan) coincide con el patrón pero está exento -> se incluye
        subs = FakeSubs(["1", "2", "3"], names={"1": "FAM Juan", "2": "Cliente", "3": "FAM Ana"})
        cfg = FakeConfig(currency_symbols="$💸💲", telegram_exclude_patterns=["fam"],
                         telegram_pattern_exceptions=["1"])
        bl = BroadcastList(subs, FakeQueue(), cfg)
        dest = bl._destinatarios_telegram(cfg.get(), {"mode": "all", "lists": []})
        self.assertEqual(set(dest), {"1", "2"})  # "3" (FAM Ana) sigue excluido


class BroadcastListTests(unittest.TestCase):
    def test_compone_quita_ubicacion_markup_y_footer(self):
        queue = FakeQueue()
        bl = BroadcastList(FakeSubs(["1", "2"]), queue, _auto(["1", "2"]))
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
        BroadcastList(FakeSubs(["1"]), queue, _auto(["1"], image_url="http://img/p.jpg"))("A $100.000")
        self.assertEqual(queue.calls[0][2], "http://img/p.jpg")

    def test_envio_apagado_solo_captura_no_crea_plan_ni_envia(self):
        # RECOPILACIÓN ≠ ENVÍO. Con el ENVÍO automático apagado solo se RECOPILA: no se crea plan,
        # no se encola, y se previsualiza a Mensajes Guardados (userbot 'me'). No se reenvía después.
        plans, queue, prev = FakePlansEspera(), FakeQueue(), FakePreview()
        bl = BroadcastList(
            FakeSubs(["1", "2"]), queue,
            FakeConfig(scheduling_enabled=True, sending_enabled=False), plans=plans, preview_sender=prev,
        )
        res = bl("A06 $100.000")
        self.assertTrue(res.get("captured"))
        self.assertEqual(plans.created, [])        # NO crea plan (no se vacía cola al activar)
        self.assertEqual(queue.calls, [])          # NO envía
        self.assertEqual(len(prev.sent), 1)        # previsualizada en Mensajes Guardados
        self.assertEqual(prev.sent[0][0], "me")
        self.assertIn("$115.000", prev.sent[0][1])  # markup aplicado en la preview

    def test_captura_guarda_comparador_de_precios(self):
        # La difusión del canal guarda el desglose anterior→nuevo por producto (para verlo en Actividad).
        class FakeBroadcasts:
            def __init__(self): self.price_diff = None
            def crear(self, broadcast_id, text, source, channels, tg_total=0, price_diff=None):
                self.price_diff = price_diff
            def registrar_error(self, bid, msg): pass
        store = FakeBroadcasts()
        cfg = FakeConfig(sending_enabled=False, currency_symbols="$", markup_percentage=15, whatsapp_footer="")
        BroadcastList(FakeSubs(["1"]), FakeQueue(), cfg, broadcasts=store)("SAMSUNG A06 $325.000")
        self.assertTrue(store.price_diff)
        self.assertEqual((store.price_diff[0]["anterior"], store.price_diff[0]["nuevo"]), ("$325.000", "$374.000"))

    def test_captura_con_imagen_anota_la_foto(self):
        # Post con imagen y caption mínimo ("📌"): la CAPTURA anota que hay imagen para que en el
        # panel no parezca una captura vacía/corrupta. El envío a contactos NO lleva la nota.
        class FakeBroadcasts:
            def __init__(self): self.textos = []
            def crear(self, broadcast_id, text, source, channels, tg_total=0, price_diff=None): self.textos.append(text)
            def registrar_error(self, bid, msg): pass
        store = FakeBroadcasts()
        bl = BroadcastList(FakeSubs(["1"]), FakeQueue(), FakeConfig(sending_enabled=False), broadcasts=store)
        res = bl("(OJO) 📌 $100.000", tiene_imagen=True)
        self.assertTrue(res.get("captured"))
        self.assertIn("incluye una imagen", store.textos[0])   # la nota queda en el job
        # Envío automático activo: el texto difundido NO lleva la nota interna.
        queue = FakeQueue()
        bl2 = BroadcastList(FakeSubs(["1"]), queue, _auto(["1"]), broadcasts=FakeBroadcasts())
        bl2("A $100.000", tiene_imagen=True)
        self.assertNotIn("incluye una imagen", queue.calls[0][0])

    def test_b16_preview_fallido_se_registra_en_el_job(self):
        # B16: si el preview de una captura NO se entrega (FloodWait/sesión), se deja constancia en el
        # job (registrar_error) para que el panel lo muestre (antes solo quedaba en logs).
        class FailPreview:
            def enviar(self, chat_id, text):
                return type("R", (), {"ok": False, "blocked": True})()
            def desconectar(self): pass
        class FakeBroadcasts:
            def __init__(self): self.errores = []
            def crear(self, broadcast_id, text, source, channels, tg_total=0, price_diff=None): pass
            def registrar_error(self, bid, msg): self.errores.append(msg)
        store = FakeBroadcasts()
        res = BroadcastList(FakeSubs(["1"]), FakeQueue(), FakeConfig(sending_enabled=False),
                            broadcasts=store, preview_sender=FailPreview())("A $100.000")
        self.assertTrue(res.get("captured"))
        self.assertFalse(res.get("preview_sent"))
        self.assertTrue(any("Preview" in e for e in store.errores))

    def test_envio_apagado_inline_no_envia_pero_captura(self):
        queue = FakeQueue()
        res = BroadcastList(FakeSubs(["1"]), queue, FakeConfig(sending_enabled=False))("A $100.000")
        self.assertTrue(res.get("captured"))
        self.assertEqual(queue.calls, [])          # no envía mientras el ENVÍO esté apagado

    def test_envio_auto_usa_lista_seleccionada_por_canal(self):
        # Con el envío activo, la difusión del canal va SOLO a la lista elegida (auto_telegram_list).
        queue = FakeQueue()
        cfg = FakeConfig(telegram_lists=[{"name": "Auto", "ids": ["1", "3"]}], auto_telegram_list="Auto")
        BroadcastList(FakeSubs(["1", "2", "3"]), queue, cfg)("A $100.000")
        self.assertEqual(set(queue.calls[0][1]), {"1", "3"})  # "2" fuera (no está en la lista Auto)

    def test_m18_dedup_key_da_broadcast_id_determinista(self):
        # M18: con dedup_key (update_id) el broadcast_id es DETERMINISTA → un reintento del webhook
        # reusa el mismo id y sobrescribe el plan (no crea otro → no duplica la difusión).
        cfg = FakeConfig(telegram_lists=[{"name": "T", "ids": ["1"]}], auto_telegram_list="T", whatsapp_enabled=False)
        bl = BroadcastList(FakeSubs(["1"]), FakeQueue(), cfg)
        r1 = bl("A $100.000", dedup_key="555")
        r2 = bl("A $100.000", dedup_key="555")
        self.assertEqual(r1["broadcast_id"], r2["broadcast_id"])  # mismo id en el reintento
        # Sin dedup_key, ids distintos (aleatorios).
        r3 = bl("A $100.000")
        r4 = bl("A $100.000")
        self.assertNotEqual(r3["broadcast_id"], r4["broadcast_id"])

    def test_m25_auto_lista_inexistente_registra_error(self):
        # M25: envío automático activo con lista elegida que ya NO existe (borrada/renombrada) →
        # resuelve a 0 destinatarios; en vez de cerrar como 'enviado-vacío' en silencio, registra error.
        class FakeBroadcasts:
            def __init__(self): self.errores = []
            def crear(self, broadcast_id, text, source, channels, tg_total=0, price_diff=None): pass
            def registrar_error(self, bid, msg): self.errores.append(msg)
        store = FakeBroadcasts()
        cfg = FakeConfig(telegram_lists=[{"name": "VIP", "ids": ["1"]}],
                         auto_telegram_list="NoExiste", whatsapp_enabled=False)
        BroadcastList(FakeSubs(["1", "2"]), FakeQueue(), cfg, broadcasts=store)("A $100.000")
        self.assertTrue(any("automático" in e for e in store.errores))

    def test_m6_fallo_encolar_cierra_whatsapp_y_relanza(self):
        # M6: si encolar a SQS lanza, el canal WhatsApp NO debe quedar sin arrancar; se intenta WA y
        # se re-lanza (para la compensación de dedup del receiver).
        class FakeBroadcasts:
            def __init__(self): self.errores = []
            def crear(self, broadcast_id, text, source, channels, tg_total=0, price_diff=None): pass
            def registrar_error(self, bid, msg): self.errores.append(msg)
            def marcar_whatsapp_fallido(self, bid): pass
        class QueueBoom:
            def encolar(self, *a, **k): raise RuntimeError("SQS caído")
        class FakeWa:
            def __init__(self): self.calls = []
            def forward(self, *a, **k): self.calls.append(1); return {"accepted": True}
        store, wa = FakeBroadcasts(), FakeWa()
        cfg = FakeConfig(telegram_lists=[{"name": "T", "ids": ["1"]}], auto_telegram_list="T",
                         whatsapp_enabled=True, whatsapp_lists=[{"name": "W", "ids": ["57300@s.whatsapp.net"]}],
                         auto_whatsapp_list="W")
        bl = BroadcastList(FakeSubs(["1"]), QueueBoom(), cfg, whatsapp=wa, broadcasts=store)
        with self.assertRaises(RuntimeError):
            bl("A $100.000")
        self.assertEqual(len(wa.calls), 1)   # WhatsApp se intentó pese al fallo de Telegram
        self.assertTrue(store.errores)        # error registrado en el job

    def test_a12_envio_activo_sin_lista_captura_no_difunde(self):
        # A12: envío automático ACTIVO pero sin auto_telegram_list ni auto_whatsapp_list → NO difunde
        # a 'todos'; se trata como captura (registra + preview) en vez de inundar la agenda.
        queue, prev = FakeQueue(), FakePreview()
        bl = BroadcastList(FakeSubs(["1", "2", "3"]), queue, FakeConfig(whatsapp_enabled=False), preview_sender=prev)
        res = bl("A $100.000")
        self.assertTrue(res.get("captured"))
        self.assertTrue(res.get("sin_lista"))
        self.assertEqual(queue.calls, [])           # NO difunde a nadie
        self.assertEqual(len(prev.sent), 1)         # se previsualiza en Mensajes Guardados

    def test_excluye_ids(self):
        queue = FakeQueue()
        # excluded_ids aplica también en modo 'only' (auto-lista): "2" se cae aunque esté en la lista.
        BroadcastList(FakeSubs(["1", "2", "3"]), queue, _auto(["1", "2", "3"], excluded_ids=["2"]))("A $100.000")
        self.assertEqual(queue.calls[0][1], ["1", "3"])  # 2 excluido

    def test_pasa_image_key(self):
        queue = FakeQueue()
        BroadcastList(FakeSubs(["1"]), queue, _auto(["1"], image_key="images/broadcast.jpg"))("A $100.000")
        self.assertEqual(queue.calls[0][3], "images/broadcast.jpg")

    def test_reenvia_a_whatsapp_si_activo(self):
        class FakeWa:
            def __init__(self):
                self.calls = []

            def forward(self, text, image_url, exclude, *, mode="all", list_ids=None, broadcast_id=None, broadcasts_table=None, exclude_patterns=None, **kwargs):
                self.calls.append((text, image_url, list(exclude), mode, list(list_ids or [])))
                return {"accepted": True}

        wa = FakeWa()
        cfg = FakeConfig(whatsapp_enabled=True, image_url="http://img",
                         whatsapp_lists=[{"name": "WA", "ids": ["57300@s.whatsapp.net"]}], auto_whatsapp_list="WA")
        BroadcastList(FakeSubs(["1"]), FakeQueue(), cfg, whatsapp=wa)("A $100.000")
        self.assertEqual(len(wa.calls), 1)
        text, image_url, exclude, mode, list_ids = wa.calls[0]
        self.assertIn("$115.000", text)
        self.assertEqual(image_url, "http://img")

    def test_telegram_whitelist_solo_envia_a_listas_activas(self):
        queue = FakeQueue()
        # Envío automático por LISTA elegida (auto_telegram_list="VIP"): solo VIP ∩ contactos.
        cfg = FakeConfig(
            telegram_lists=[{"name": "VIP", "ids": ["1", "3"]}, {"name": "otros", "ids": ["2"]}],
            auto_telegram_list="VIP",
        )
        BroadcastList(FakeSubs(["1", "2", "3", "4"]), queue, cfg)("A $100.000")
        self.assertEqual(queue.calls[0][1], ["1", "3"])  # solo VIP, intersecado con contactos

    def test_telegram_blacklist_excluye_listas_activas(self):
        # El modo 'except' (blacklist) es propio del envío AMPLIO (manual): el automático es por lista.
        # Se ejercita _destinatarios_telegram con target except (su entrada real).
        cfg = FakeConfig(telegram_lists=[{"name": "bloqueados", "ids": ["2", "4"]}])
        bl = BroadcastList(FakeSubs(["1", "2", "3", "4"]), FakeQueue(), cfg)
        dest = bl._destinatarios_telegram(cfg.get(), {"mode": "except", "lists": ["bloqueados"]})
        self.assertEqual(sorted(dest), ["1", "3"])

    def test_whatsapp_recibe_modo_y_list_ids(self):
        class FakeWa:
            def __init__(self):
                self.calls = []

            def forward(self, text, image_url, exclude, *, mode="all", list_ids=None, broadcast_id=None, broadcasts_table=None, exclude_patterns=None, **kwargs):
                self.calls.append((mode, list(list_ids or [])))
                return {}

        wa = FakeWa()
        cfg = FakeConfig(
            whatsapp_enabled=True,
            whatsapp_lists=[{"name": "clientes", "ids": ["57300@s.whatsapp.net", "57301@s.whatsapp.net"]}],
            auto_whatsapp_list="clientes",
        )
        BroadcastList(FakeSubs(["1"]), FakeQueue(), cfg, whatsapp=wa)("A $100.000")
        mode, list_ids = wa.calls[0]
        self.assertEqual(mode, "only")
        self.assertEqual(list_ids, ["57300@s.whatsapp.net", "57301@s.whatsapp.net"])

    def test_envio_manual_texto_crudo_a_ambos_canales(self):
        class FakeWa:
            def __init__(self):
                self.calls = []

            def forward(self, text, image_url, exclude, *, mode="all", list_ids=None, broadcast_id=None, broadcasts_table=None, exclude_patterns=None, **kwargs):
                self.calls.append((text, broadcast_id))
                return {}

        class FakeBroadcasts:
            def __init__(self):
                self.jobs = []

            def crear(self, broadcast_id, text, source, channels, tg_total=0, price_diff=None):
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

            def forward(self, text, image_url, exclude, *, mode="all", list_ids=None, broadcast_id=None, broadcasts_table=None, exclude_patterns=None, **kwargs):
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
        cfg = _auto(["1", "2", "3"], scheduling_enabled=True, batch_size=2)
        bl = BroadcastList(FakeSubs(["1", "2", "3"]), queue, cfg, plans=plans)
        res = bl("A06 $100.000")
        self.assertTrue(res["scheduled"])
        self.assertEqual(queue.calls, [])  # no se vuelca a SQS
        plan = plans.creados[0]
        self.assertEqual([len(l) for l in plan["tg_lotes"]], [2, 1])  # 3 contactos, lote 2 -> [2,1]
        self.assertFalse(plan["wa_enabled"])

    def test_manual_wa_resuelve_total_via_contar(self):
        class FakeWa:
            def contar(self, *, mode="all", list_ids=None, exclude=None, exclude_patterns=None, pattern_exceptions=None):
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

    def test_manual_programado_guarda_not_before(self):
        plans = FakePlans()
        cfg = FakeConfig(scheduling_enabled=True)
        bl = BroadcastList(FakeSubs(["1", "2"]), FakeQueue(), cfg, plans=plans)
        res = bl.enviar_manual("hola", telegram=True, whatsapp=False, scheduled_at=1750000000)
        self.assertTrue(res["scheduled"])
        self.assertEqual(plans.creados[0]["not_before"], 1750000000)

    def test_diferido_crea_plan_aunque_el_fraccionado_este_apagado(self):
        # Bug: con scheduling_enabled=False, un scheduled_at futuro se ignoraba y el envío salía YA
        # aunque el panel dijera "programado". Ahora, con store de planes, un diferido SIEMPRE crea plan.
        plans, queue = FakePlans(), FakeQueue()
        cfg = FakeConfig(scheduling_enabled=False)
        bl = BroadcastList(FakeSubs(["1", "2"]), queue, cfg, plans=plans)
        res = bl.enviar_manual("hola", telegram=True, whatsapp=False, scheduled_at=1750000000)
        self.assertTrue(res["scheduled"])
        self.assertEqual(queue.calls, [])                       # NO se envió de inmediato
        self.assertEqual(plans.creados[0]["not_before"], 1750000000)

    def test_diferido_sin_store_de_planes_se_rechaza(self):
        # Sin almacén de planes no se puede diferir: rechazar claro en vez de enviar ya "en silencio".
        cfg = FakeConfig(scheduling_enabled=False)
        bl = BroadcastList(FakeSubs(["1"]), FakeQueue(), cfg, plans=None)
        with self.assertRaises(ValueError):
            bl.enviar_manual("hola", telegram=True, whatsapp=False, scheduled_at=1750000000)

    def test_manual_etiqueta_source_manual(self):
        # El plan manual se marca source="manual" (el dispatcher/worker lo dejan pasar en pausa).
        plans = FakePlans()
        cfg = FakeConfig(scheduling_enabled=True)
        bl = BroadcastList(FakeSubs(["1", "2"]), FakeQueue(), cfg, plans=plans)
        bl.enviar_manual("hola", telegram=True, whatsapp=False)
        self.assertEqual(plans.creados[0]["source"], "manual")

    def test_canal_etiqueta_source_channel(self):
        plans = FakePlans()
        cfg = _auto(["1"], scheduling_enabled=True)
        bl = BroadcastList(FakeSubs(["1"]), FakeQueue(), cfg, plans=plans)
        bl("A06 $100.000")
        self.assertEqual(plans.creados[0]["source"], "channel")

    def test_manual_sale_aunque_este_pausado(self):
        # La PAUSA solo frena lo automático: el envío MANUAL crea su plan igual (sin lanzar error).
        plans = FakePlans()
        cfg = FakeConfig(scheduling_enabled=True, sending_enabled=False)
        bl = BroadcastList(FakeSubs(["1", "2"]), FakeQueue(), cfg, plans=plans)
        res = bl.enviar_manual("hola", telegram=True, whatsapp=False)
        self.assertTrue(res["scheduled"])
        self.assertEqual(plans.creados[0]["source"], "manual")

    def test_manual_propaga_image_key_para_refirmar(self):
        # La imagen subida viaja como image_key (clave S3) en el plan, para re-firmar al despachar.
        plans = FakePlans()
        cfg = FakeConfig(scheduling_enabled=True)
        bl = BroadcastList(FakeSubs(["1", "2"]), FakeQueue(), cfg, plans=plans)
        bl.enviar_manual("hola", telegram=True, whatsapp=False, image_key="images/abc.jpg")
        self.assertEqual(plans.creados[0]["image_key"], "images/abc.jpg")
        self.assertEqual(plans.creados[0]["wa_image_key"], "images/abc.jpg")

    def test_manual_whatsapp_no_configurado_se_rechaza(self):
        # Pedir WhatsApp sin el servicio CONFIGURADO (sin URL/token) falla RUIDOSAMENTE (no silencio).
        # No depende de whatsapp_enabled (ese es el auto-reenvío del canal, no el envío manual).
        cfg = FakeConfig(scheduling_enabled=True, whatsapp_service_url="", whatsapp_token="",
                         whatsapp_lists=[{"name": "c", "ids": ["57300@s.whatsapp.net"]}],
                         whatsapp_target={"mode": "only", "lists": ["c"]})
        bl = BroadcastList(FakeSubs(["1"]), FakeQueue(), cfg, whatsapp=object(), plans=FakePlans())
        with self.assertRaises(ValueError) as ctx:
            bl.enviar_manual("hola", telegram=False, whatsapp=True, whatsapp_list="c")
        self.assertIn("no está configurado", str(ctx.exception))

    def test_manual_sin_destinatarios_se_rechaza_con_motivo(self):
        # Si los patrones excluyen a todos, el manual no "no envía en silencio": avisa la causa.
        subs = FakeSubs(["1", "2"], names={"1": "FAM Uno", "2": "FAM Dos"})
        cfg = FakeConfig(scheduling_enabled=True, telegram_exclude_patterns=["fam"])
        bl = BroadcastList(subs, FakeQueue(), cfg, plans=FakePlans())
        with self.assertRaises(ValueError) as ctx:
            bl.enviar_manual("hola", telegram=True, whatsapp=False)
        self.assertIn("patrones de exclusión", str(ctx.exception))

    def test_fallback_legacy_si_no_hay_plans(self):
        # scheduling_enabled pero sin store de planes -> envío inmediato (compatibilidad)
        queue = FakeQueue()
        bl = BroadcastList(FakeSubs(["1"]), queue, _auto(["1"], scheduling_enabled=True), plans=None)
        bl("A $100.000")
        self.assertEqual(len(queue.calls), 1)


if __name__ == "__main__":
    unittest.main()
