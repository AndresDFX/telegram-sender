"""Tests del encolado SQS: división en lotes y forma del payload."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

import sqs_client  # noqa: E402


class SqsClientTests(unittest.TestCase):
    def test_chunk_divide_en_lotes(self):
        lotes = list(sqs_client._chunk([str(i) for i in range(250)], 100))
        self.assertEqual([len(l) for l in lotes], [100, 100, 50])

    def test_encola_un_mensaje_por_lote(self):
        fake = MagicMock()
        chat_ids = [str(i) for i in range(250)]
        with patch.object(sqs_client, "_client", return_value=fake):
            encolados = sqs_client.encolar_lotes(
                "lista", chat_ids, queue_url="https://q", batch_size=100
            )

        self.assertEqual(encolados, 3)
        self.assertEqual(fake.send_message.call_count, 3)

        bodies = [json.loads(c.kwargs["MessageBody"]) for c in fake.send_message.call_args_list]
        self.assertEqual([len(b["chat_ids"]) for b in bodies], [100, 100, 50])
        self.assertEqual([b["batch_index"] for b in bodies], [0, 1, 2])
        self.assertTrue(all(b["text"] == "lista" for b in bodies))
        # Todos los chatIds quedan cubiertos exactamente una vez.
        cubiertos = [cid for b in bodies for cid in b["chat_ids"]]
        self.assertEqual(cubiertos, chat_ids)

    def test_sin_queue_url_lanza(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BROADCAST_QUEUE_URL", None)
            with self.assertRaises(RuntimeError):
                sqs_client.encolar_lotes("lista", ["1"], queue_url=None)


if __name__ == "__main__":
    unittest.main()
