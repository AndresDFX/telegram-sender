"""Adapter: derivación de estado del BroadcastStore (lógica pura, sin DynamoDB)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from adapters.dynamodb import DynamoDbBroadcastStore  # noqa: E402

E = DynamoDbBroadcastStore._estado


def job(**kw):
    base = {
        "channels": ["telegram"],
        "tg_total": 0,
        "tg_sent": 0,
        "tg_failed": 0,
        "wa_total": 0,
        "wa_sent": 0,
        "wa_failed": 0,
        "wa_started": False,
    }
    base.update(kw)
    return base


class EstadoTests(unittest.TestCase):
    def test_queued_sin_progreso(self):
        self.assertEqual(E(job(tg_total=10)), "queued")

    def test_sending_parcial(self):
        self.assertEqual(E(job(tg_total=10, tg_sent=4)), "sending")

    def test_done_completo_sin_fallos(self):
        self.assertEqual(E(job(tg_total=10, tg_sent=10)), "done")

    def test_partial_completo_con_fallos(self):
        self.assertEqual(E(job(tg_total=10, tg_sent=7, tg_failed=3)), "partial")

    def test_whatsapp_pendiente_es_sending(self):
        # canal whatsapp pero el servicio aún no reportó wa_total → en curso
        self.assertEqual(E(job(channels=["whatsapp"], wa_started=False)), "sending")

    def test_ambos_canales_done(self):
        j = job(channels=["telegram", "whatsapp"], tg_total=5, tg_sent=5, wa_total=3, wa_sent=3, wa_started=True)
        self.assertEqual(E(j), "done")

    def test_ambos_canales_uno_en_curso(self):
        j = job(channels=["telegram", "whatsapp"], tg_total=5, tg_sent=5, wa_total=3, wa_sent=1, wa_started=True)
        self.assertEqual(E(j), "sending")

    def test_sin_destinatarios_es_done_no_queued(self):
        # job solo-telegram con 0 destinatarios: terminal (no atascado en queued)
        self.assertEqual(E(job(channels=["telegram"], tg_total=0)), "done")

    def test_todo_fallido_es_failed(self):
        self.assertEqual(E(job(channels=["telegram"], tg_total=5, tg_failed=5, tg_sent=0)), "failed")


class IncrTelegramFechasTests(unittest.TestCase):
    def test_incr_telegram_sella_first_y_last_sent_at(self):
        # Cada entrega Telegram sella last_sent_at y, la primera vez, first_sent_at (if_not_exists).
        store = DynamoDbBroadcastStore.__new__(DynamoDbBroadcastStore)
        kw = {}

        class _T:
            def update_item(self, **k):
                kw.update(k)

        store._t = lambda: _T()
        store.incr_telegram("b1", sent=5, failed=0)
        expr = kw["UpdateExpression"]
        self.assertIn("ADD tg_sent", expr)
        self.assertIn("last_sent_at = :now", expr)
        self.assertIn("first_sent_at = if_not_exists(first_sent_at, :now)", expr)
        self.assertIn(":now", kw["ExpressionAttributeValues"])


class ScanConsistenteTests(unittest.TestCase):
    def test_scan_todo_usa_consistent_read(self):
        # Tras borrar, el re-listado NO debe devolver el item borrado: Scan con ConsistentRead=True.
        store = DynamoDbBroadcastStore.__new__(DynamoDbBroadcastStore)
        llamadas = []

        class _T:
            def scan(self, **kw):
                llamadas.append(kw)
                return {"Items": []}

        store._t = lambda: _T()
        store._scan_todo()
        self.assertTrue(llamadas and llamadas[0].get("ConsistentRead") is True)


class RegistrarErrorTests(unittest.TestCase):
    def test_b18_dos_escrituras_add_siempre_y_last_error_condicional(self):
        # B18: registrar_error hace (1) ADD error_reasons SIN condición de orden y (2) SET last_error
        # condicional al timestamp (último gana por recencia, no last-writer-wins ciego).
        store = DynamoDbBroadcastStore.__new__(DynamoDbBroadcastStore)
        llamadas = []

        class _T:
            def update_item(self, **kw):
                llamadas.append(kw)

        store._t = lambda: _T()
        store.registrar_error("b1", "Telegram — boom")
        self.assertEqual(len(llamadas), 2)
        add, set_le = llamadas
        self.assertIn("ADD error_reasons", add["UpdateExpression"])
        self.assertNotIn("last_error_at", add["ConditionExpression"])  # el ADD no depende del orden
        self.assertIn("SET last_error", set_le["UpdateExpression"])
        self.assertIn("last_error_at <= :t", set_le["ConditionExpression"])  # solo si es >= al guardado


class BorrarTerminadosTests(unittest.TestCase):
    def _store(self, jobs):
        store = DynamoDbBroadcastStore.__new__(DynamoDbBroadcastStore)
        store._scan_todo = lambda: jobs
        borrados = []

        class _T:
            def delete_item(self, Key):
                borrados.append(Key["id"])

        store._t = lambda: _T()
        return store, borrados

    def test_m8_no_borra_job_de_plan_activo(self):
        import time
        viejo = int(time.time()) - DynamoDbBroadcastStore._EDAD_TERMINAL - 100
        # Job incompleto y viejo → _estado lo daría 'partial' (terminal por EDAD) → borrable...
        store, borrados = self._store([job(id="b1", tg_total=10, tg_sent=3, created_at=viejo)])
        # ...pero su plan sigue EN VUELO (excluir_ids) → NO se borra (M8).
        self.assertEqual(store.borrar_terminados(excluir_ids=["b1"]), 0)
        self.assertEqual(borrados, [])
        # Sin excluir, el mismo job sí se borra (comportamiento normal de limpieza).
        store2, borrados2 = self._store([job(id="b1", tg_total=10, tg_sent=3, created_at=viejo)])
        self.assertEqual(store2.borrar_terminados(), 1)
        self.assertEqual(borrados2, ["b1"])


class MetricasTests(unittest.TestCase):
    def test_agrega_enviados_fallidos_y_tasa(self):
        import time as _t

        store = DynamoDbBroadcastStore(table_name="x")
        now = int(_t.time())
        store._scan_todo = lambda: [
            {"created_at": now, "tg_sent": 8, "tg_failed": 2, "wa_sent": 5, "wa_failed": 0},
            {"created_at": now - 86400, "tg_sent": 10, "tg_failed": 0, "wa_sent": 0, "wa_failed": 0},
            {"created_at": now - 40 * 86400, "tg_sent": 99, "tg_failed": 99},  # fuera de los 30 días
        ]
        m = store.metricas(30)
        self.assertEqual(m["jobs"], 2)                 # el viejo se excluye
        self.assertEqual(m["enviados"], 8 + 5 + 10)    # 23
        self.assertEqual(m["fallidos"], 2)
        self.assertEqual(m["tasa_exito"], round(23 / 25 * 100, 1))
        self.assertEqual(m["telegram"]["enviados"], 18)
        self.assertEqual(m["whatsapp"]["enviados"], 5)
        self.assertEqual(len(m["serie"]), 2)           # dos días distintos


class EstructuraStoresTests(unittest.TestCase):
    """Guardas contra mezclar métodos entre clases (un editor mal anclado lo causó una vez)."""

    def test_plan_y_audit_listar_no_se_mezclan(self):
        import inspect

        from adapters.dynamodb import DynamoDbAuditStore, DynamoDbPlanStore

        self.assertIn("_META", inspect.getsource(DynamoDbPlanStore.listar))   # listar de planes
        self.assertNotIn("_META", inspect.getsource(DynamoDbAuditStore.listar))  # listar de auditoría
        self.assertTrue(hasattr(DynamoDbPlanStore, "cancelar_pendientes"))
        self.assertTrue(hasattr(DynamoDbAuditStore, "registrar"))


if __name__ == "__main__":
    unittest.main()
