"""Adapters DynamoDB: suscriptores, dedup y high-water mark.

``boto3``/``botocore`` se importan de forma perezosa para que las pruebas unitarias
puedan importar este módulo sin tenerlos instalados (el runtime Lambda los trae).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from application.ports import ConfigStore, DedupStore, HighWaterMarkStore, SubscriberRepository
from domain.markup import DEFAULT_CURRENCY_SYMBOLS
from domain.message import DEFAULT_LOCATION_PATTERNS
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

    def listar_todos(self) -> list[dict]:
        table = self._t()
        items: list[dict] = []
        kwargs = {"ProjectionExpression": "chatId, #s", "ExpressionAttributeNames": {"#s": "status"}}
        while True:
            resp = table.scan(**kwargs)
            items.extend(
                {"chatId": i["chatId"], "status": i.get("status", "")} for i in resp.get("Items", [])
            )
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return items


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


class DynamoDbConfigStore(ConfigStore):
    """Config editable en runtime: un único item; los defaults vienen del entorno."""

    _CAMPOS = (
        "source_channel",
        "markup_percentage",
        "currency_symbols",
        "strip_patterns",
        "whatsapp_footer",
        "image_url",
    )

    def __init__(self, table_name: str | None = None, endpoint: str | None = None, config_id: str = "default"):
        self._name = table_name or os.environ.get("CONFIG_TABLE", "Config")
        self._endpoint = endpoint or os.environ.get("DYNAMODB_ENDPOINT")
        self._id = config_id

    def _t(self):
        return _table(self._name, self._endpoint)

    def _defaults(self) -> dict:
        return {
            "source_channel": os.environ.get("SOURCE_CHANNEL_USERNAME", "iproparts"),
            "markup_percentage": float(os.environ.get("MARKUP_PERCENTAGE", "15")),
            "currency_symbols": os.environ.get("CURRENCY_SYMBOLS", DEFAULT_CURRENCY_SYMBOLS),
            "strip_patterns": list(DEFAULT_LOCATION_PATTERNS),
            "whatsapp_footer": os.environ.get("WHATSAPP_FOOTER", ""),
            "image_url": os.environ.get("BROADCAST_IMAGE_URL", ""),
        }

    def get(self) -> dict:
        item = self._t().get_item(Key={"configId": self._id}).get("Item") or {}
        cfg = self._defaults()
        for k in self._CAMPOS:
            if item.get(k) is not None:
                cfg[k] = item[k]
        cfg["markup_percentage"] = float(cfg["markup_percentage"])
        cfg["strip_patterns"] = list(cfg["strip_patterns"])
        return cfg

    def set(self, cambios: dict) -> dict:
        from decimal import Decimal

        permitidos = {k: v for k, v in cambios.items() if k in self._CAMPOS}
        if permitidos:
            names = {f"#k{i}": k for i, k in enumerate(permitidos)}
            values = {
                f":v{i}": (Decimal(str(v)) if k == "markup_percentage" else v)
                for i, (k, v) in enumerate(permitidos.items())
            }
            set_expr = "SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(permitidos)))
            self._t().update_item(
                Key={"configId": self._id},
                UpdateExpression=set_expr,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        return self.get()
