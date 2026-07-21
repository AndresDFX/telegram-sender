"""Adapters: SqsBroadcastQueue (lotes, PartialEnqueueError) e InlineBroadcastQueue."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from adapters.sqs import InlineBroadcastQueue, SqsBroadcastQueue, SqsQueueStats  # noqa: E402
from application.ports import PartialEnqueueError  # noqa: E402


class SqsQueueTests(unittest.TestCase):
    def test_encola_un_mensaje_por_lote(self):
        fake = MagicMock()
        q = SqsBroadcastQueue(queue_url="https://q", batch_size=100)
        chat_ids = [str(i) for i in range(250)]
        with patch.object(q, "_client", return_value=fake):
            n = q.encolar("lista", chat_ids)
        self.assertEqual(n, 3)
        self.assertEqual(fake.send_message.call_count, 3)
        bodies = [json.loads(c.kwargs["MessageBody"]) for c in fake.send_message.call_args_list]
        self.assertEqual([len(b["chat_ids"]) for b in bodies], [100, 100, 50])
        self.assertEqual([cid for b in bodies for cid in b["chat_ids"]], chat_ids)

    def test_fallo_persistente_lanza_partial(self):
        fake = MagicMock()
        fake.send_message.side_effect = RuntimeError("sqs down")
        q = SqsBroadcastQueue(queue_url="https://q", batch_size=100, max_retries=1, sleep=lambda _s: None)
        with patch.object(q, "_client", return_value=fake):
            with self.assertRaises(PartialEnqueueError) as ctx:
                q.encolar("x", ["1", "2"])
        self.assertEqual(ctx.exception.enqueued, 0)

    def test_sin_url_lanza(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BROADCAST_QUEUE_URL", None)
            with self.assertRaises(RuntimeError):
                SqsBroadcastQueue(queue_url=None).encolar("x", ["1"])


class SqsQueueStatsTests(unittest.TestCase):
    def test_profundidades_incluye_en_vuelo(self):
        # Devuelve en cola (visibles) Y en vuelo (NotVisible) para cola principal y DLQ.
        fake = MagicMock()

        def attrs(QueueUrl, AttributeNames):
            if QueueUrl == "https://main":
                return {"Attributes": {"ApproximateNumberOfMessages": "3", "ApproximateNumberOfMessagesNotVisible": "2"}}
            return {"Attributes": {"ApproximateNumberOfMessages": "5", "ApproximateNumberOfMessagesNotVisible": "1"}}

        fake.get_queue_attributes.side_effect = attrs
        s = SqsQueueStats(queue_url="https://main", dlq_url="https://dlq")
        with patch.object(s, "_client", return_value=fake):
            r = s.profundidades()
        self.assertEqual(r, {"broadcast": 3, "en_vuelo": 2, "dlq": 5, "dlq_en_vuelo": 1})
        # pidió ambos atributos (no solo el de visibles)
        _, kw = fake.get_queue_attributes.call_args
        self.assertIn("ApproximateNumberOfMessagesNotVisible", kw["AttributeNames"])

    def test_purgar_principal_ok(self):
        fake = MagicMock()
        s = SqsQueueStats(queue_url="https://main", dlq_url="https://dlq")
        with patch.object(s, "_client", return_value=fake):
            r = s.purgar_principal()
        self.assertEqual(r, {"ok": True, "purged": True})
        fake.purge_queue.assert_called_once_with(QueueUrl="https://main")

    def test_purgar_principal_rate_limit_no_revienta(self):
        # Un segundo purge en <60s (PurgeQueueInProgress) devuelve 'en_progreso', no una excepción.
        fake = MagicMock()
        fake.purge_queue.side_effect = RuntimeError("AWS.SimpleQueueService.PurgeQueueInProgress: Only one PurgeQueue ...")
        s = SqsQueueStats(queue_url="https://main")
        with patch.object(s, "_client", return_value=fake):
            r = s.purgar_principal()
        self.assertTrue(r.get("ok"))
        self.assertEqual(r.get("purge"), "en_progreso")

    def test_purgar_principal_sin_url(self):
        s = SqsQueueStats(queue_url=None)
        s._url = None
        self.assertEqual(s.purgar_principal(), {"error": "sin cola"})

    def test_dlq_purgar_rate_limit_no_revienta(self):
        fake = MagicMock()
        fake.purge_queue.side_effect = RuntimeError("PurgeQueueInProgress")
        s = SqsQueueStats(queue_url="https://main", dlq_url="https://dlq")
        with patch.object(s, "_client", return_value=fake):
            r = s.dlq_purgar()
        self.assertEqual(r.get("purge"), "en_progreso")


class InlineQueueTests(unittest.TestCase):
    def test_entrega_inmediata_con_imagen(self):
        entregados = []
        q = InlineBroadcastQueue(lambda text, ids, image_url=None: entregados.append((text, list(ids), image_url)))
        n = q.encolar("lista", ["1", "2"], image_url="http://img")
        self.assertEqual(n, 1)
        self.assertEqual(entregados, [("lista", ["1", "2"], "http://img")])


if __name__ == "__main__":
    unittest.main()
