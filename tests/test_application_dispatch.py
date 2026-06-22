"""Application: DispatchCampaigns (libera un lote por tick, secuencial, con ventana)."""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from application.dispatch import DispatchCampaigns  # noqa: E402

logging.disable(logging.CRITICAL)


def _plan(**over):
    p = {
        "pid": "p1", "sk": "PLAN", "status": "pending", "created_at": 1, "broadcast_id": "b1",
        "text": "hola", "batch_size": 150,
        "tg_total": 300, "tg_batches": 2, "tg_next": 0, "tg_dispatched": 0,
        "wa_enabled": False, "wa_resolved": True, "wa_total": 0, "wa_batches": 0, "wa_next": 0, "wa_dispatched": 0,
        "wa_mode": "all", "wa_list_ids": [], "wa_exclude": [], "wa_text": "hola", "wa_image_url": "",
        "image_url": "", "image_key": "",
        "in_flight": "", "in_flight_at": 0, "in_flight_channel": "", "in_flight_target": 0,
    }
    p.update(over)
    return p


class FakePlans:
    def __init__(self, plan, claim=True):
        self.plan = plan
        self.dispatched = []
        self.cleared = 0
        self.finalized = None
        self.resolved = None
        self.claim = claim  # False simula que un cancel concurrente ganó (ConditionExpression)

    def activos(self):
        return [self.plan] if self.plan and self.plan["status"] in ("pending", "running") else []

    def ids_lote_tg(self, pid, idx):
        return [f"tg{idx}-{i}" for i in range(150 if idx == 0 else 150)]

    def registrar_dispatch(self, plan_id, *, channel, index, n, target, now, prev_log=None):
        if not self.claim:
            return False  # cancelado en carrera: el dispatcher debe abortar sin encolar
        self.dispatched.append({"ch": channel, "idx": index, "n": n, "target": target, "now": now})
        return True

    def limpiar_inflight(self, plan_id):
        self.cleared += 1
        self.plan["in_flight"] = ""

    def finalizar(self, plan_id, status="done"):
        self.finalized = status
        self.plan["status"] = status

    def resolver_wa(self, plan_id, wa_total, batch_size):
        self.resolved = (wa_total, batch_size)


class FakeBroadcasts:
    def __init__(self, prog=None):
        self.prog = prog or {"tg": 0, "wa": 0}

    def progreso(self, bid):
        return dict(self.prog)


class FakeQueue:
    def __init__(self):
        self.calls = []

    def encolar_uno(self, text, chat_ids, image_url=None, image_key=None, broadcast_id=None, batch_index=0, pid=None, manual=False):
        self.calls.append({"text": text, "n": len(chat_ids), "bid": broadcast_id, "idx": batch_index, "pid": pid, "manual": manual})


class FakeWa:
    def __init__(self, count=0):
        self.calls = []
        self.count = count

    def contar(self, *, mode="all", list_ids=None, exclude=None, exclude_patterns=None, pattern_exceptions=None):
        return self.count

    def forward(self, text, image_url, exclude, **kw):
        self.calls.append({"text": text, **kw})
        return {"accepted": True}


class FakeConfig:
    def __init__(self, **over):
        self.cfg = {
            "window_enabled": False, "window_start": "08:00", "window_end": "20:00", "window_tz": 0,
            "wa_delay_min": 3000, "wa_delay_max": 9000, "batch_size": 150,
        }
        self.cfg.update(over)

    def get(self):
        return dict(self.cfg)


def _disp(plans, broadcasts=None, queue=None, wa=None, config=None, now=1000, stale=900, image_store=None):
    return DispatchCampaigns(
        plans=plans, broadcasts=broadcasts or FakeBroadcasts(), queue=queue or FakeQueue(),
        whatsapp=wa or FakeWa(), config=config or FakeConfig(), broadcasts_table="x-broadcasts",
        stale_seconds=stale, now=lambda: now, image_store=image_store,
    )


class DispatchTests(unittest.TestCase):
    def test_sin_planes(self):
        self.assertEqual(_disp(FakePlans(None))(), {"planes": 0})

    def test_plan_programado_a_futuro_se_difiere(self):
        plans, queue = FakePlans(_plan(not_before=999999)), FakeQueue()
        res = _disp(plans, queue=queue, now=1000)()  # not_before 999999 > now 1000
        self.assertTrue(res.get("diferido_horario"))
        self.assertEqual(queue.calls, [])
        self.assertEqual(plans.dispatched, [])

    def test_pausado_no_despacha_automatico(self):
        # En pausa, un plan AUTOMÁTICO (source channel/ausente) no se despacha.
        plans, queue = FakePlans(_plan()), FakeQueue()
        res = _disp(plans, queue=queue, config=FakeConfig(sending_enabled=False))()
        self.assertEqual(res, {"paused": True})
        self.assertEqual(queue.calls, [])  # nada se libera
        self.assertEqual(plans.dispatched, [])

    def test_pausado_si_despacha_manual(self):
        # En pausa, un plan MANUAL (source="manual") SÍ se despacha: la pausa es solo para automáticos.
        plans, queue = FakePlans(_plan(source="manual")), FakeQueue()
        res = _disp(plans, queue=queue, config=FakeConfig(sending_enabled=False))()
        self.assertEqual(res.get("despachado"), "TG#0")
        self.assertEqual(len(queue.calls), 1)
        self.assertTrue(queue.calls[0]["manual"])  # el lote viaja marcado como manual

    def test_despacha_primer_lote_tg(self):
        plans, queue = FakePlans(_plan()), FakeQueue()
        res = _disp(plans, queue=queue)()
        self.assertEqual(res["despachado"], "TG#0")
        self.assertEqual(queue.calls[0]["bid"], "b1")
        self.assertEqual(plans.dispatched[0], {"ch": "tg", "idx": 0, "n": 150, "target": 150, "now": 1000})

    def test_cancelado_en_carrera_no_encola(self):
        plans = FakePlans(_plan(), claim=False)  # el claim condicional falla (cancel ganó)
        queue = FakeQueue()
        res = _disp(plans, queue=queue)()
        self.assertEqual(res, {"plan": "p1", "cancelado": True})
        self.assertEqual(queue.calls, [])  # NO se libera el lote
        self.assertEqual(plans.dispatched, [])

    def test_espera_si_lote_en_vuelo_no_termino(self):
        plans = FakePlans(_plan(status="running", in_flight="TG#0", in_flight_channel="tg",
                                in_flight_target=150, in_flight_at=999, tg_next=1, tg_dispatched=150))
        bc = FakeBroadcasts({"tg": 80, "wa": 0})  # 80 < 150 -> sigue enviando
        res = _disp(plans, broadcasts=bc)()
        self.assertEqual(res["esperando"], "TG#0")
        self.assertEqual(plans.dispatched, [])
        self.assertEqual(plans.cleared, 0)

    def test_libera_siguiente_cuando_lote_termina(self):
        plans = FakePlans(_plan(status="running", in_flight="TG#0", in_flight_channel="tg",
                                in_flight_target=150, in_flight_at=999, tg_next=1, tg_dispatched=150))
        bc = FakeBroadcasts({"tg": 150, "wa": 0})  # 150 >= 150 -> terminó
        queue = FakeQueue()
        res = _disp(plans, broadcasts=bc, queue=queue)()
        self.assertEqual(plans.cleared, 1)
        self.assertEqual(res["despachado"], "TG#1")
        self.assertEqual(plans.dispatched[0]["target"], 300)  # 150 + 150

    def test_lote_estancado_se_libera_por_timeout(self):
        plans = FakePlans(_plan(status="running", in_flight="TG#0", in_flight_channel="tg",
                                in_flight_target=150, in_flight_at=1, tg_next=1, tg_dispatched=150))
        bc = FakeBroadcasts({"tg": 10, "wa": 0})  # no llegó al target pero...
        res = _disp(plans, broadcasts=bc, now=2000, stale=900)()  # 2000-1 > 900 -> estancado
        self.assertEqual(plans.cleared, 1)
        self.assertEqual(res["despachado"], "TG#1")

    def test_b17_inflight_at_cero_no_se_abandona(self):
        # B17: in_flight_at==0 (escritura parcial/legacy) NO debe contar como antigüedad gigante y
        # abandonar un lote recién reclamado; se espera (no se libera) hasta tener un timestamp real.
        plans = FakePlans(_plan(status="running", in_flight="TG#0", in_flight_channel="tg",
                                in_flight_target=150, in_flight_at=0, tg_next=1, tg_dispatched=150))
        bc = FakeBroadcasts({"tg": 10, "wa": 0})
        res = _disp(plans, broadcasts=bc, now=10**9, stale=900)()  # now enorme, pero in_flight_at=0
        self.assertEqual(plans.cleared, 0)             # NO se abandona
        self.assertEqual(res["esperando"], "TG#0")

    def test_m29_estancado_registra_error(self):
        # M29: al liberar un lote estancado se registra la causa en el job (no queda colgado sin motivo).
        class Broad(FakeBroadcasts):
            def __init__(self): super().__init__({"tg": 10, "wa": 0}); self.error=None
            def registrar_error(self, bid, msg): self.error=msg
        plans = FakePlans(_plan(status="running", in_flight="TG#0", in_flight_channel="tg",
                                in_flight_target=150, in_flight_at=1, tg_next=1, tg_dispatched=150))
        b = Broad()
        _disp(plans, broadcasts=b, now=2000, stale=900)()
        self.assertEqual(plans.cleared, 1)
        self.assertIsNotNone(b.error)
        self.assertIn("estancado", b.error)

    def test_ventana_cerrada_difiere(self):
        plans = FakePlans(_plan())
        cfg = FakeConfig(window_enabled=True, window_start="08:00", window_end="20:00", window_tz=0)
        res = _disp(plans, config=cfg, now=22 * 3600)()  # 22:00 UTC fuera
        self.assertEqual(res["diferido"], "fuera de ventana")
        self.assertEqual(plans.dispatched, [])

    def test_manual_inmediato_salta_ventana_cerrada(self):
        # "Enviar en el momento" (manual, not_before=0) IGNORA la ventana: sale aunque esté cerrada.
        plans, queue = FakePlans(_plan(source="manual")), FakeQueue()
        cfg = FakeConfig(window_enabled=True, window_start="08:00", window_end="20:00", window_tz=0)
        res = _disp(plans, queue=queue, config=cfg, now=22 * 3600)()  # 22:00 UTC fuera de ventana
        self.assertEqual(res.get("despachado"), "TG#0")  # sale igual
        self.assertEqual(len(queue.calls), 1)

    def test_manual_programado_respeta_ventana(self):
        # Un manual PROGRAMADO (not_before>0, ya vencido) SÍ respeta la ventana (no es "en el momento").
        plans = FakePlans(_plan(source="manual", not_before=1))
        cfg = FakeConfig(window_enabled=True, window_start="08:00", window_end="20:00", window_tz=0)
        res = _disp(plans, config=cfg, now=22 * 3600)()  # vencido pero fuera de ventana
        self.assertEqual(res["diferido"], "fuera de ventana")
        self.assertEqual(plans.dispatched, [])

    def test_ventana_tg_cerrada_no_frena_a_wa(self):
        # Horarios INDEPENDIENTES: TG fuera de su ventana, WA dentro -> se despacha WA igual.
        plans = FakePlans(_plan(wa_enabled=True, wa_resolved=True, wa_total=150, wa_batches=1))
        cfg = FakeConfig(tg_window_enabled=True, tg_window_start="08:00", tg_window_end="20:00",
                         wa_window_enabled=False, window_tz=0)
        wa = FakeWa()
        res = _disp(plans, wa=wa, config=cfg, now=22 * 3600)()  # 22:00 UTC: TG cerrado, WA 24h
        self.assertEqual(res["despachado"], "WA#0")
        self.assertEqual(len(wa.calls), 1)

    def test_ventana_wa_cerrada_no_frena_a_tg(self):
        plans, queue = FakePlans(_plan(wa_enabled=True, wa_resolved=True, wa_total=150, wa_batches=1)), FakeQueue()
        cfg = FakeConfig(wa_window_enabled=True, wa_window_start="08:00", wa_window_end="20:00",
                         tg_window_enabled=False, window_tz=0)
        res = _disp(plans, queue=queue, config=cfg, now=22 * 3600)()  # 22:00: WA cerrado, TG 24h
        self.assertEqual(res["despachado"], "TG#0")
        self.assertEqual(len(queue.calls), 1)

    def test_whatsapp_no_aceptado_marca_fallido(self):
        # RC-C: si el servicio WhatsApp no acepta (sin 'accepted'), se marca fallido y se registra error.
        class Wa:
            def ping(self): pass
            def forward(self, *a, **k): return {}  # sin 'accepted'
        class Broad(FakeBroadcasts):
            def __init__(self): super().__init__(); self.fallido=None; self.error=None
            def marcar_whatsapp_fallido(self, bid): self.fallido=bid
            def registrar_error(self, bid, msg): self.error=msg
        plans = FakePlans(_plan(tg_next=2, tg_dispatched=300, wa_enabled=True, wa_resolved=True, wa_total=150, wa_batches=1))
        b = Broad()
        res = _disp(plans, broadcasts=b, wa=Wa())()
        self.assertTrue(res.get("wa_no_aceptado"))
        self.assertEqual(b.fallido, "b1")
        self.assertIsNotNone(b.error)

    def test_whatsapp_imagen_se_refirma_desde_key(self):
        # RC-B: la URL de imagen de WhatsApp se RE-FIRMA desde wa_image_key al despachar.
        captured = {}
        class Wa:
            def ping(self): pass
            def forward(self, text, image_url, exclude, **k): captured['url']=image_url; return {"accepted": True}
        class IS:
            def url_temporal(self, key, expira=3600): return "https://fresh/"+key
        plans = FakePlans(_plan(tg_next=2, tg_dispatched=300, wa_enabled=True, wa_resolved=True,
                                wa_total=150, wa_batches=1, wa_image_url="https://stale/x", wa_image_key="images/x.jpg"))
        _disp(plans, wa=Wa(), image_store=IS())()
        self.assertEqual(captured['url'], "https://fresh/images/x.jpg")

    def test_b5_refirma_imagen_falla_no_despacha_y_reintenta(self):
        # B5: si la re-firma de la imagen WA falla y hay wa_image_key, NO se despacha con la URL caduca
        # ni se avanza el cursor; se reintenta el próximo tick (esperando_wa).
        class ISBoom:
            def url_temporal(self, key, expira=3600): raise RuntimeError("S3 caído")
        wa = FakeWa()
        plans = FakePlans(_plan(tg_next=2, tg_dispatched=300, wa_enabled=True, wa_resolved=True,
                                wa_total=150, wa_batches=1, wa_image_url="https://stale/x", wa_image_key="images/x.jpg"))
        res = _disp(plans, wa=wa, image_store=ISBoom())()
        self.assertTrue(res.get("esperando_wa"))
        self.assertEqual(wa.calls, [])             # NO se envió con URL caduca
        self.assertEqual(plans.dispatched, [])     # cursor NO avanzó (se reintenta)

    def test_whatsapp_caido_no_bloquea_telegram(self):
        # Si WhatsApp no resuelve su total (servicio caído), Telegram debe salir igual (independencia).
        class WaCaido:
            def ping(self): pass
            def contar(self, **k): raise RuntimeError("WhatsApp caído")
            def forward(self, *a, **k): raise RuntimeError("WhatsApp caído")
        plans, queue = FakePlans(_plan(wa_enabled=True, wa_resolved=False)), FakeQueue()
        res = _disp(plans, queue=queue, wa=WaCaido())()
        self.assertEqual(res["despachado"], "TG#0")  # TG sale aunque WhatsApp esté caído
        self.assertEqual(len(queue.calls), 1)

    def test_a4_encolar_falla_libera_inflight_y_no_propaga(self):
        # A4: si encolar_uno lanza DESPUÉS de reclamar el lote, NO debe propagar (perdería el lote
        # en silencio y colgaría el cursor 900s). Se libera el in_flight y se registra el error.
        class QueueBoom:
            def encolar_uno(self, *a, **k):
                raise RuntimeError("SQS caído")
        class Broad(FakeBroadcasts):
            def __init__(self): super().__init__(); self.error=None
            def registrar_error(self, bid, msg): self.error=msg
        plans = FakePlans(_plan())
        b = Broad()
        res = _disp(plans, broadcasts=b, queue=QueueBoom())()
        self.assertTrue(res.get("tg_encolar_fallido"))
        self.assertEqual(plans.cleared, 1)        # in_flight liberado (no cuelga 900s)
        self.assertIsNotNone(b.error)             # error visible en el job

    def test_a13_forward_excepcion_marca_fallido_y_libera(self):
        # A13: si forward lanza (Render dormido/caído) tras reclamar el lote WA, NO debe propagar;
        # cae en "no aceptado", marca el job WhatsApp fallido y libera el in_flight.
        class WaBoom:
            def ping(self): pass
            def forward(self, *a, **k): raise RuntimeError("Render caído")
        class Broad(FakeBroadcasts):
            def __init__(self): super().__init__(); self.fallido=None; self.error=None
            def marcar_whatsapp_fallido(self, bid): self.fallido=bid
            def registrar_error(self, bid, msg): self.error=msg
        plans = FakePlans(_plan(tg_next=2, tg_dispatched=300, wa_enabled=True, wa_resolved=True,
                                wa_total=150, wa_batches=1))
        b = Broad()
        res = _disp(plans, broadcasts=b, wa=WaBoom())()
        self.assertTrue(res.get("wa_no_aceptado"))
        self.assertEqual(b.fallido, "b1")
        self.assertEqual(plans.cleared, 1)        # in_flight liberado

    def test_finaliza_cuando_no_queda_nada(self):
        plans = FakePlans(_plan(status="running", tg_next=2, tg_dispatched=300))  # tg agotado, wa off
        res = _disp(plans)()
        self.assertEqual(res["finalizado"], True)
        self.assertEqual(plans.finalized, "done")

    def test_despacha_whatsapp_con_offset_limit(self):
        # TG agotado; WhatsApp habilitado con 250 destinatarios, lote 150 -> WA#0 offset 0 limit 150.
        plans = FakePlans(_plan(status="running", tg_next=2, tg_dispatched=300,
                                wa_enabled=True, wa_resolved=True, wa_total=250, wa_batches=2,
                                wa_next=0, wa_dispatched=0, wa_mode="only", wa_list_ids=["a", "b"]))
        wa = FakeWa()
        res = _disp(plans, wa=wa)()
        self.assertEqual(res["despachado"], "WA#0")
        self.assertEqual(wa.calls[0]["offset"], 0)
        self.assertEqual(wa.calls[0]["limit"], 150)
        self.assertEqual(wa.calls[0]["bc_total"], 250)
        self.assertEqual(wa.calls[0]["delay_min_ms"], 3000)
        self.assertEqual(plans.dispatched[0], {"ch": "wa", "idx": 0, "n": 150, "target": 150, "now": 1000})

    def test_resuelve_wa_perezosamente(self):
        plans = FakePlans(_plan(status="running", tg_next=2, tg_dispatched=300,
                                wa_enabled=True, wa_resolved=False, wa_total=0, wa_batches=0))
        wa = FakeWa(count=200)
        _disp(plans, wa=wa)()
        self.assertEqual(plans.resolved, (200, 150))  # se resolvió y luego despachó


if __name__ == "__main__":
    unittest.main()
