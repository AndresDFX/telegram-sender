"""Tests de dynamodb_client: alta de suscriptor y lógica de dedup (ramas)."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

import dynamodb_client  # noqa: E402

try:
    from botocore.exceptions import ClientError

    HAS_BOTOCORE = True
except ImportError:  # pragma: no cover
    HAS_BOTOCORE = False

try:
    import boto3  # noqa: F401

    HAS_BOTO3 = True
except ImportError:  # pragma: no cover
    HAS_BOTO3 = False


class RegistrarSuscriptorTests(unittest.TestCase):
    def test_upsert_con_status_y_createdat(self):
        table = MagicMock()
        with patch.object(dynamodb_client, "_table", return_value=table):
            dynamodb_client.registrar_suscriptor("123", "active")
        kwargs = table.update_item.call_args.kwargs
        self.assertEqual(kwargs["Key"], {"chatId": "123"})
        self.assertEqual(kwargs["ExpressionAttributeValues"][":status"], "active")
        self.assertIn("if_not_exists(createdAt", kwargs["UpdateExpression"])


class BorrarUpdateTests(unittest.TestCase):
    def test_delete_por_clave(self):
        table = MagicMock()
        with patch.object(dynamodb_client, "_processed_table", return_value=table):
            dynamodb_client.borrar_update_procesado("99")
        table.delete_item.assert_called_once_with(Key={"updateId": "99"})


@unittest.skipUnless(HAS_BOTOCORE, "requiere botocore para construir ClientError")
class DedupTests(unittest.TestCase):
    def test_primera_marca_devuelve_true_con_ttl_futuro(self):
        table = MagicMock()
        with patch.object(dynamodb_client, "_processed_table", return_value=table), patch.object(
            dynamodb_client.time, "time", return_value=1000.0
        ):
            self.assertTrue(dynamodb_client.marcar_update_procesado("100", ttl_seconds=60))
        item = table.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["updateId"], "100")
        # expiresAt debe ser epoch en segundos (int) y futuro, o el TTL no purga.
        self.assertIsInstance(item["expiresAt"], int)
        self.assertEqual(item["expiresAt"], 1060)
        self.assertIn(
            "attribute_not_exists", table.put_item.call_args.kwargs["ConditionExpression"]
        )

    def test_duplicado_devuelve_false(self):
        table = MagicMock()
        err = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem"
        )
        table.put_item.side_effect = err
        with patch.object(dynamodb_client, "_processed_table", return_value=table):
            self.assertFalse(dynamodb_client.marcar_update_procesado("100"))

    def test_otro_error_se_propaga(self):
        table = MagicMock()
        err = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "PutItem"
        )
        table.put_item.side_effect = err
        with patch.object(dynamodb_client, "_processed_table", return_value=table):
            with self.assertRaises(ClientError):
                dynamodb_client.marcar_update_procesado("100")


@unittest.skipUnless(HAS_BOTO3, "requiere boto3 para boto3.dynamodb.conditions.Key")
class ObtenerActivosTests(unittest.TestCase):
    def test_pagina_y_consulta_el_gsi(self):
        table = MagicMock()
        table.query.side_effect = [
            {"Items": [{"chatId": "1"}, {"chatId": "2"}], "LastEvaluatedKey": {"chatId": "2"}},
            {"Items": [{"chatId": "3"}]},
        ]
        with patch.object(dynamodb_client, "_table", return_value=table):
            ids = dynamodb_client.obtener_usuarios_activos()

        self.assertEqual(ids, ["1", "2", "3"])  # concatena ambas páginas
        self.assertEqual(table.query.call_count, 2)
        first = table.query.call_args_list[0].kwargs
        self.assertEqual(first["IndexName"], "StatusIndex")
        # La segunda página continúa desde la clave de la primera.
        self.assertEqual(table.query.call_args_list[1].kwargs["ExclusiveStartKey"], {"chatId": "2"})

    def test_filtra_por_status_active(self):
        from boto3.dynamodb.conditions import Key

        table = MagicMock()
        table.query.return_value = {"Items": []}
        with patch.object(dynamodb_client, "_table", return_value=table):
            dynamodb_client.obtener_usuarios_activos()
        cond = table.query.call_args.kwargs["KeyConditionExpression"]
        self.assertEqual(cond, Key("status").eq("active"))


if __name__ == "__main__":
    unittest.main()
