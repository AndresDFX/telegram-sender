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
        "image_key",
        "excluded_ids",
        "send_mode",
        "telethon_api_id",
        "telethon_api_hash",
        "telethon_session",
        "whatsapp_enabled",
        "whatsapp_service_url",
        "whatsapp_token",
        "whatsapp_excluded",
        "telegram_lists",
        "telegram_target",
        "whatsapp_lists",
        "whatsapp_target",
    )
    _CONTACTS_ID = "__contacts__"

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
            "image_key": "",  # objeto subido en S3 (tiene prioridad sobre image_url)
            "excluded_ids": [],  # chat IDs a excluir del envío
            "send_mode": os.environ.get("SEND_MODE", "bot"),
            "telethon_api_id": os.environ.get("TELETHON_API_ID", ""),
            "telethon_api_hash": os.environ.get("TELETHON_API_HASH", ""),
            "telethon_session": os.environ.get("TELETHON_SESSION", ""),
            "whatsapp_enabled": False,
            "whatsapp_service_url": os.environ.get("WHATSAPP_SERVICE_URL", ""),
            "whatsapp_token": os.environ.get("WHATSAPP_TOKEN", ""),
            "whatsapp_excluded": [],  # ids de WhatsApp a excluir
            # Listas de distribución con nombre + modo de targeting por canal.
            # lists: [{"name": str, "ids": [str]}]; target: {"mode": all|only|except, "lists": [name]}
            "telegram_lists": [],
            "telegram_target": {"mode": "all", "lists": []},
            "whatsapp_lists": [],
            "whatsapp_target": {"mode": "all", "lists": []},
        }

    def get(self) -> dict:
        item = self._t().get_item(Key={"configId": self._id}).get("Item") or {}
        cfg = self._defaults()
        for k in self._CAMPOS:
            if item.get(k) is not None:
                cfg[k] = item[k]
        cfg["markup_percentage"] = float(cfg["markup_percentage"])
        cfg["strip_patterns"] = list(cfg["strip_patterns"])
        cfg["excluded_ids"] = [str(x) for x in cfg["excluded_ids"]]
        cfg["whatsapp_enabled"] = bool(cfg["whatsapp_enabled"])
        cfg["whatsapp_excluded"] = [str(x) for x in cfg["whatsapp_excluded"]]
        cfg["telegram_lists"] = self._norm_lists(cfg["telegram_lists"])
        cfg["telegram_target"] = self._norm_target(cfg["telegram_target"])
        cfg["whatsapp_lists"] = self._norm_lists(cfg["whatsapp_lists"])
        cfg["whatsapp_target"] = self._norm_target(cfg["whatsapp_target"])
        return cfg

    @staticmethod
    def _norm_lists(raw) -> list[dict]:
        out = []
        for l in raw or []:
            nombre = str((l or {}).get("name", "")).strip()
            if not nombre:
                continue
            ids = [str(x) for x in (l or {}).get("ids", []) if str(x).strip()]
            out.append({"name": nombre, "ids": ids})
        return out

    @staticmethod
    def _norm_target(raw) -> dict:
        raw = raw or {}
        mode = str(raw.get("mode", "all"))
        if mode not in ("all", "only", "except"):
            mode = "all"
        listas = [str(x) for x in raw.get("lists", []) if str(x).strip()]
        return {"mode": mode, "lists": listas}

    # --- caché de contactos (para que el panel no dependa de Telethon en vivo) ---

    def get_contacts(self) -> list[dict]:
        item = self._t().get_item(Key={"configId": self._CONTACTS_ID}).get("Item") or {}
        return list(item.get("items", []))

    def set_contacts(self, contactos: list[dict]) -> None:
        self._t().put_item(Item={"configId": self._CONTACTS_ID, "items": contactos})

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


class DynamoDbBroadcastStore:
    """Estados de los envíos (jobs): encolado→enviando→enviado/parcial, con progreso por canal.

    El estado se DERIVA de los contadores al listar, así el worker (Telegram) y el servicio
    (WhatsApp) solo necesitan incrementar contadores de forma atómica (ADD), sin coordinarse.
    """

    def __init__(self, table_name: str | None = None, endpoint: str | None = None):
        self._name = table_name or os.environ.get("BROADCASTS_TABLE", "Broadcasts")
        self._endpoint = endpoint or os.environ.get("DYNAMODB_ENDPOINT")

    def _t(self):
        return _table(self._name, self._endpoint)

    def crear(self, broadcast_id: str, text: str, source: str, channels, tg_total: int = 0, ttl_days: int = 30) -> None:
        now = int(time.time())
        self._t().put_item(
            Item={
                "id": broadcast_id,
                "created_at": now,
                "text": (text or "")[:280],
                "source": source,
                "channels": list(channels),
                "tg_total": int(tg_total),
                "tg_sent": 0,
                "tg_failed": 0,
                "wa_total": 0,
                "wa_sent": 0,
                "wa_failed": 0,
                "wa_started": False,
                "ttl": now + ttl_days * 86400,
            }
        )

    def incr_telegram(self, broadcast_id: str, sent: int = 0, failed: int = 0) -> None:
        self._add(broadcast_id, "ADD tg_sent :s, tg_failed :f", {":s": int(sent), ":f": int(failed)})

    def set_whatsapp_total(self, broadcast_id: str, total: int) -> None:
        from decimal import Decimal

        self._t().update_item(
            Key={"id": broadcast_id},
            UpdateExpression="SET wa_total = :t, wa_started = :b",
            ExpressionAttributeValues={":t": Decimal(int(total)), ":b": True},
        )

    def incr_whatsapp(self, broadcast_id: str, sent: int = 0, failed: int = 0) -> None:
        self._add(broadcast_id, "ADD wa_sent :s, wa_failed :f", {":s": int(sent), ":f": int(failed)})

    def _add(self, broadcast_id: str, expr: str, values: dict) -> None:
        from decimal import Decimal

        try:
            self._t().update_item(
                Key={"id": broadcast_id},
                UpdateExpression=expr,
                ExpressionAttributeValues={k: Decimal(v) for k, v in values.items()},
            )
        except Exception:
            pass  # el tracking de estado nunca debe romper el envío

    @staticmethod
    def _estado(j: dict) -> str:
        chans = j.get("channels", []) or []
        tg_total, tg_done = int(j.get("tg_total", 0)), int(j.get("tg_sent", 0)) + int(j.get("tg_failed", 0))
        wa_total, wa_done = int(j.get("wa_total", 0)), int(j.get("wa_sent", 0)) + int(j.get("wa_failed", 0))
        wa_pendiente = ("whatsapp" in chans) and not j.get("wa_started")
        total, done = tg_total + wa_total, tg_done + wa_done
        failed = int(j.get("tg_failed", 0)) + int(j.get("wa_failed", 0))
        if done == 0 and not wa_pendiente:
            return "queued"
        if wa_pendiente or total == 0 or done < total:
            return "sending"
        return "partial" if failed > 0 else "done"

    def listar(self, limit: int = 30) -> list[dict]:
        items = self._t().scan().get("Items", [])
        items.sort(key=lambda j: int(j.get("created_at", 0)), reverse=True)
        salida = []
        for j in items[:limit]:
            salida.append(
                {
                    "id": j.get("id"),
                    "created_at": int(j.get("created_at", 0)),
                    "text": j.get("text", ""),
                    "source": j.get("source", ""),
                    "channels": list(j.get("channels", [])),
                    "status": self._estado(j),
                    "telegram": {
                        "total": int(j.get("tg_total", 0)),
                        "sent": int(j.get("tg_sent", 0)),
                        "failed": int(j.get("tg_failed", 0)),
                    },
                    "whatsapp": {
                        "total": int(j.get("wa_total", 0)),
                        "sent": int(j.get("wa_sent", 0)),
                        "failed": int(j.get("wa_failed", 0)),
                    },
                }
            )
        return salida
