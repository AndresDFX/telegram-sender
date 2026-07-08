"""Adapters: DynamoDB (suscriptores, dedup, high-water mark)."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from adapters import dynamodb  # noqa: E402

try:
    from botocore.exceptions import ClientError
    import boto3  # noqa: F401

    HAS_BOTO = True
except ImportError:  # pragma: no cover
    HAS_BOTO = False


class SubscriberRepoTests(unittest.TestCase):
    def test_registrar_upsert(self):
        table = MagicMock()
        repo = dynamodb.DynamoDbSubscriberRepository()
        with patch.object(dynamodb, "_table", return_value=table):
            repo.registrar("123", "active")
        kw = table.update_item.call_args.kwargs
        self.assertEqual(kw["Key"], {"chatId": "123"})
        self.assertEqual(kw["ExpressionAttributeValues"][":s"], "active")
        self.assertIn("if_not_exists(createdAt", kw["UpdateExpression"])

    @unittest.skipUnless(HAS_BOTO, "requiere boto3")
    def test_b6_marcar_inactivo_chat_inexistente_no_lanza(self):
        # B6: si el chat no existe, la ConditionExpression falla con CCF; debe ser no-op silencioso
        # (no propagar el stacktrace que ensuciaba los logs por un caso benigno).
        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        repo = dynamodb.DynamoDbSubscriberRepository()
        with patch.object(dynamodb, "_table", return_value=table):
            repo.marcar_inactivo("999")  # no debe lanzar
        # Otros errores SÍ se propagan.
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
        with patch.object(dynamodb, "_table", return_value=table):
            with self.assertRaises(ClientError):
                repo.marcar_inactivo("999")

    @unittest.skipUnless(HAS_BOTO, "requiere boto3")
    def test_listar_activos_pagina_y_filtra(self):
        from boto3.dynamodb.conditions import Key

        table = MagicMock()
        table.query.side_effect = [
            {"Items": [{"chatId": "1"}, {"chatId": "2"}], "LastEvaluatedKey": {"chatId": "2"}},
            {"Items": [{"chatId": "3"}]},
        ]
        repo = dynamodb.DynamoDbSubscriberRepository()
        with patch.object(dynamodb, "_table", return_value=table):
            ids = repo.listar_activos()
        self.assertEqual(ids, ["1", "2", "3"])
        first = table.query.call_args_list[0].kwargs
        self.assertEqual(first["IndexName"], "StatusIndex")
        self.assertEqual(first["KeyConditionExpression"], Key("status").eq("active"))
        self.assertEqual(table.query.call_args_list[1].kwargs["ExclusiveStartKey"], {"chatId": "2"})


@unittest.skipUnless(HAS_BOTO, "requiere botocore para ClientError")
class DedupStoreTests(unittest.TestCase):
    def test_marca_nueva_true(self):
        table = MagicMock()
        store = dynamodb.DynamoDbDedupStore(ttl_seconds=60)
        with patch.object(dynamodb, "_table", return_value=table), patch.object(
            dynamodb.time, "time", return_value=1000.0
        ):
            self.assertTrue(store.marcar("42"))
        item = table.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["updateId"], "42")
        self.assertIsInstance(item["expiresAt"], int)
        self.assertEqual(item["expiresAt"], 1060)

    def test_duplicado_false(self):
        table = MagicMock()
        table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem"
        )
        store = dynamodb.DynamoDbDedupStore()
        with patch.object(dynamodb, "_table", return_value=table):
            self.assertFalse(store.marcar("42"))

    def test_otro_error_no_propaga_fail_open(self):
        # Fallo de infra (permiso/throughput/tabla): marcar NO re-lanza, devuelve False.
        # Si re-lanzara, el worker reencolaría un lote YA ENTREGADO → reentrega-duplicado.
        table = MagicMock()
        table.put_item.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "PutItem"
        )
        store = dynamodb.DynamoDbDedupStore()
        with patch.object(dynamodb, "_table", return_value=table):
            self.assertFalse(store.marcar("42"))  # no lanza: fail-open simétrico con procesado()

    def test_m10_fallo_de_infra_emite_metrica_emf(self):
        # M10/M30: el fail-open ante infra debe dejar rastro MEDIBLE: un log EMF con la métrica
        # DedupInfraError (CloudWatch la extrae y el stack alarma por SNS). Duplicado real NO emite.
        import io
        import json as _json
        from contextlib import redirect_stdout

        table = MagicMock()
        table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "PutItem"
        )
        store = dynamodb.DynamoDbDedupStore()
        out = io.StringIO()
        with patch.object(dynamodb, "_table", return_value=table), redirect_stdout(out):
            store.marcar("42")
        emf = _json.loads(out.getvalue().strip())
        self.assertEqual(emf["DedupInfraError"], 1)
        self.assertEqual(emf["Component"], "dedup")
        self.assertEqual(emf["_aws"]["CloudWatchMetrics"][0]["Namespace"], "Replica/Backend")
        # Duplicado real (ConditionalCheckFailed): NO es fallo de infra, no emite métrica.
        table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem"
        )
        out2 = io.StringIO()
        with patch.object(dynamodb, "_table", return_value=table), redirect_stdout(out2):
            store.marcar("42")
        self.assertEqual(out2.getvalue(), "")


class ConfigStoreTests(unittest.TestCase):
    def test_get_mezcla_defaults_con_item(self):
        from decimal import Decimal

        table = MagicMock()
        table.get_item.return_value = {
            "Item": {"configId": "default", "source_channel": "otro", "markup_percentage": Decimal("20")}
        }
        store = dynamodb.DynamoDbConfigStore()
        with patch.object(dynamodb, "_table", return_value=table):
            cfg = store.get()
        self.assertEqual(cfg["source_channel"], "otro")        # override
        self.assertEqual(cfg["markup_percentage"], 20.0)        # Decimal -> float
        self.assertIsInstance(cfg["markup_percentage"], float)
        self.assertIn("strip_patterns", cfg)                    # default presente
        self.assertEqual(cfg["whatsapp_footer"], "")            # default

    def test_set_actualiza_solo_campos_validos(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {}}
        store = dynamodb.DynamoDbConfigStore()
        with patch.object(dynamodb, "_table", return_value=table):
            store.set({"source_channel": "nuevo", "campo_basura": "x"})
        kw = table.update_item.call_args.kwargs
        self.assertEqual(set(kw["ExpressionAttributeNames"].values()), {"source_channel"})  # ignora basura


class HwmStoreTests(unittest.TestCase):
    def test_obtener_y_guardar(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"updateId": "__hwm__ch", "value": 3289}}
        store = dynamodb.DynamoDbHighWaterMarkStore()
        with patch.object(dynamodb, "_table", return_value=table):
            self.assertEqual(store.obtener("ch"), 3289)
            store.guardar("ch", 3290)
        self.assertEqual(table.put_item.call_args.kwargs["Item"], {"updateId": "__hwm__ch", "value": 3290})

    def test_obtener_sin_item_none(self):
        table = MagicMock()
        table.get_item.return_value = {}
        store = dynamodb.DynamoDbHighWaterMarkStore()
        with patch.object(dynamodb, "_table", return_value=table):
            self.assertIsNone(store.obtener("ch"))


if __name__ == "__main__":
    unittest.main()
