"""Adapters DynamoDB: suscriptores, dedup y high-water mark.

``boto3``/``botocore`` se importan de forma perezosa para que las pruebas unitarias
puedan importar este módulo sin tenerlos instalados (el runtime Lambda los trae).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from application.ports import DedupStore, HighWaterMarkStore, SubscriberRepository
from domain.models import ACTIVE, INACTIVE


def _table(name: str, endpoint: str | None):
    import boto3

    kwargs = {"endpoint_url": endpoint} if endpoint else {}
    return boto3.resource("dynamodb", **kwargs).Table(name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DynamoDbSubscriberRepository(SubscriberRepository):
    def __init__(self, table_name: str | None = None, status_index: str | None = None, endpoint: str | None = None):
        self._name = table_name or os.environ.get("SUBSCRIBERS_TABLE", "SubscriptoresTelegram")
        self._index = status_index or os.environ.get("SUBSCRIBERS_STATUS_INDEX", "StatusIndex")
        self._endpoint = endpoint or os.environ.get("DYNAMODB_ENDPOINT")

    def _t(self):
        return _table(self._name, self._endpoint)

    def listar_activos(self) -> list[str]:
        from boto3.dynamodb.conditions import Key

        table = self._t()
        chat_ids: list[str] = []
        kwargs = {
            "IndexName": self._index,
            "KeyConditionExpression": Key("status").eq(ACTIVE),
            "ProjectionExpression": "chatId",
        }
        while True:
            resp = table.query(**kwargs)
            chat_ids.extend(item["chatId"] for item in resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return chat_ids

    def registrar(self, chat_id: str, status: str) -> None:
        now = _now_iso()
        self._t().update_item(
            Key={"chatId": chat_id},
            UpdateExpression="SET #s = :s, updatedAt = :now, createdAt = if_not_exists(createdAt, :now)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status, ":now": now},
        )

    def marcar_inactivo(self, chat_id: str) -> None:
        from boto3.dynamodb.conditions import Attr

        self._t().update_item(
            Key={"chatId": chat_id},
            UpdateExpression="SET #s = :inactive, updatedAt = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":inactive": INACTIVE, ":now": _now_iso()},
            ConditionExpression=Attr("chatId").exists(),
        )


class DynamoDbDedupStore(DedupStore):
    def __init__(self, table_name: str | None = None, ttl_seconds: int | None = None, endpoint: str | None = None):
        self._name = table_name or os.environ.get("PROCESSED_UPDATES_TABLE", "ProcessedUpdates")
        self._ttl = ttl_seconds if ttl_seconds is not None else int(os.environ.get("DEDUP_TTL_SECONDS", "86400"))
        self._endpoint = endpoint or os.environ.get("DYNAMODB_ENDPOINT")

    def _t(self):
        return _table(self._name, self._endpoint)

    def marcar(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._t().put_item(
                Item={"updateId": str(key), "expiresAt": int(time.time()) + self._ttl},
                ConditionExpression="attribute_not_exists(updateId)",
            )
            return True
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def borrar(self, key: str) -> None:
        self._t().delete_item(Key={"updateId": str(key)})


class DynamoDbHighWaterMarkStore(HighWaterMarkStore):
    _PREFIX = "__hwm__"

    def __init__(self, table_name: str | None = None, endpoint: str | None = None):
        self._name = table_name or os.environ.get("PROCESSED_UPDATES_TABLE", "ProcessedUpdates")
        self._endpoint = endpoint or os.environ.get("DYNAMODB_ENDPOINT")

    def _t(self):
        return _table(self._name, self._endpoint)

    def obtener(self, channel: str) -> int | None:
        item = self._t().get_item(Key={"updateId": self._PREFIX + channel}).get("Item")
        if not item or "value" not in item:
            return None
        return int(item["value"])

    def guardar(self, channel: str, value: int) -> None:
        self._t().put_item(Item={"updateId": self._PREFIX + channel, "value": int(value)})
