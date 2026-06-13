"""Adapters DynamoDB: suscriptores, dedup y high-water mark.

``boto3``/``botocore`` se importan de forma perezosa para que las pruebas unitarias
puedan importar este módulo sin tenerlos instalados (el runtime Lambda los trae).
"""

from __future__ import annotations

import os
import time
import uuid
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

    def procesado(self, key: str) -> bool:
        """True si la clave ya está marcada (para idempotencia: no reprocesar/reenviar)."""
        try:
            return bool(self._t().get_item(Key={"updateId": str(key)}).get("Item"))
        except Exception:
            return False  # ante duda no bloquear el envío


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
        "bot_token",
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
        # Anti-baneo / colas / ventana de envío (editable desde el panel).
        "sending_enabled",  # interruptor maestro: si es False, NADA se envía (pausa total)
        "batch_size",
        "scheduling_enabled",
        "tg_delay_min",
        "tg_delay_max",
        "wa_delay_min",
        "wa_delay_max",
        "window_enabled",
        "window_start",
        "window_end",
        "window_tz",
    )
    _CONTACTS_ID = "__contacts__"
    _LOGIN_ID = "__telethon_login__"  # sesión temporal del login userbot (entre código y confirmación)

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
            "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),  # editable: usar otro bot sin redeploy
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
            # --- Anti-baneo / colas / ventana ---
            "sending_enabled": True,  # interruptor maestro de envíos (activar/desactivar)
            "batch_size": int(os.environ.get("BROADCAST_BATCH_SIZE", "150")),  # tope 150
            "scheduling_enabled": True,  # fraccionar y enviar 1 lote a la vez (secuencial)
            "tg_delay_min": float(os.environ.get("TG_DELAY_MIN", "1")),  # jitter Telegram (s)
            "tg_delay_max": float(os.environ.get("TG_DELAY_MAX", "4")),
            "wa_delay_min": int(os.environ.get("WA_DELAY_MIN_MS", "3000")),  # jitter WhatsApp (ms)
            "wa_delay_max": int(os.environ.get("WA_DELAY_MAX_MS", "9000")),
            "window_enabled": False,  # ventana horaria de envío (off = 24h)
            "window_start": os.environ.get("WINDOW_START", "08:00"),
            "window_end": os.environ.get("WINDOW_END", "20:00"),
            "window_tz": int(os.environ.get("WINDOW_TZ_OFFSET", "-300")),  # min vs UTC (UTC-5 Colombia)
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
        # Anti-baneo / colas / ventana
        from domain.scheduling import cap_batch_size

        cfg["sending_enabled"] = bool(cfg["sending_enabled"])
        cfg["batch_size"] = cap_batch_size(cfg["batch_size"])
        cfg["scheduling_enabled"] = bool(cfg["scheduling_enabled"])
        cfg["tg_delay_min"] = float(cfg["tg_delay_min"])
        cfg["tg_delay_max"] = float(cfg["tg_delay_max"])
        cfg["wa_delay_min"] = int(float(cfg["wa_delay_min"]))
        cfg["wa_delay_max"] = int(float(cfg["wa_delay_max"]))
        cfg["window_enabled"] = bool(cfg["window_enabled"])
        cfg["window_start"] = str(cfg["window_start"])
        cfg["window_end"] = str(cfg["window_end"])
        cfg["window_tz"] = int(float(cfg["window_tz"]))
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
        self._t().put_item(
            Item={"configId": self._CONTACTS_ID, "items": contactos, "refreshed_at": int(time.time())}
        )

    def contacts_refreshed_at(self) -> int:
        """Epoch del último refresco del caché de contactos (0 si nunca). Para no llamar a
        Telethon GetContacts con demasiada frecuencia (dispara FloodWait)."""
        item = self._t().get_item(Key={"configId": self._CONTACTS_ID}).get("Item") or {}
        try:
            return int(item.get("refreshed_at", 0))
        except (TypeError, ValueError):
            return 0

    # Campos numéricos: DynamoDB no acepta float de Python, se guardan como Decimal.
    _NUMERICOS = (
        "markup_percentage",
        "batch_size",
        "tg_delay_min",
        "tg_delay_max",
        "wa_delay_min",
        "wa_delay_max",
        "window_tz",
    )

    def incr_ban_strikes(self) -> int:
        """Suma 1 al contador de lotes fallidos consecutivos (auto-pausa anti-baneo).
        Devuelve el nuevo valor. Atributo interno (no en _CAMPOS, no se expone)."""
        from decimal import Decimal

        try:
            resp = self._t().update_item(
                Key={"configId": self._id},
                UpdateExpression="ADD ban_strikes :one",
                ExpressionAttributeValues={":one": Decimal(1)},
                ReturnValues="UPDATED_NEW",
            )
            return int(resp.get("Attributes", {}).get("ban_strikes", 0))
        except Exception:
            return 0

    def reset_ban_strikes(self) -> None:
        from decimal import Decimal

        try:
            self._t().update_item(
                Key={"configId": self._id},
                UpdateExpression="SET ban_strikes = :z",
                ExpressionAttributeValues={":z": Decimal(0)},
            )
        except Exception:
            pass

    def set(self, cambios: dict) -> dict:
        from decimal import Decimal

        permitidos = {k: v for k, v in cambios.items() if k in self._CAMPOS}
        if permitidos:
            names = {f"#k{i}": k for i, k in enumerate(permitidos)}
            values = {
                f":v{i}": (Decimal(str(v)) if k in self._NUMERICOS else v)
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

    # --- login userbot desde el panel (sesión temporal entre "enviar código" y "confirmar") ---

    def set_login_temp(self, session: str, phone_code_hash: str, phone: str) -> None:
        self._t().put_item(
            Item={
                "configId": self._LOGIN_ID,
                "session": session,
                "phone_code_hash": phone_code_hash,
                "phone": phone,
                "ttl": int(time.time()) + 900,  # 15 min (si la tabla tiene TTL habilitado)
            }
        )

    def get_login_temp(self) -> dict:
        item = self._t().get_item(Key={"configId": self._LOGIN_ID}).get("Item") or {}
        return {
            "session": item.get("session", ""),
            "phone_code_hash": item.get("phone_code_hash", ""),
            "phone": item.get("phone", ""),
        }

    def update_login_session(self, session: str) -> None:
        self._t().update_item(
            Key={"configId": self._LOGIN_ID},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "session"},
            ExpressionAttributeValues={":s": session},
        )

    def clear_login_temp(self) -> None:
        try:
            self._t().delete_item(Key={"configId": self._LOGIN_ID})
        except Exception:
            pass


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
                "text": (text or "")[:600],  # solo display (el envío usa el texto del plan)
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

        try:
            self._t().update_item(
                Key={"id": broadcast_id},
                UpdateExpression="SET wa_total = :t, wa_started = :b",
                ConditionExpression="attribute_exists(id)",  # no fabricar items fantasma
                ExpressionAttributeValues={":t": Decimal(int(total)), ":b": True},
            )
        except Exception:
            pass

    def marcar_whatsapp_fallido(self, broadcast_id: str) -> None:
        """El reenvío a WhatsApp no llegó al servicio: cierra el canal para que el job no
        quede 'enviando' eterno. Marca wa_started=True con total 0 (no había progreso real)."""
        self.set_whatsapp_total(broadcast_id, 0)

    def incr_whatsapp(self, broadcast_id: str, sent: int = 0, failed: int = 0) -> None:
        self._add(broadcast_id, "ADD wa_sent :s, wa_failed :f", {":s": int(sent), ":f": int(failed)})

    def progreso(self, broadcast_id: str) -> dict:
        """Procesados acumulados por canal (sent+failed). Lo usa el dispatcher para saber
        si el lote en vuelo terminó (los contadores ya los actualizan worker y servicio)."""
        try:
            j = self._t().get_item(Key={"id": broadcast_id}).get("Item") or {}
        except Exception:
            j = {}
        return {
            "tg": int(j.get("tg_sent", 0)) + int(j.get("tg_failed", 0)),
            "wa": int(j.get("wa_sent", 0)) + int(j.get("wa_failed", 0)),
            "tg_sent": int(j.get("tg_sent", 0)),
            "wa_sent": int(j.get("wa_sent", 0)),
        }

    def _add(self, broadcast_id: str, expr: str, values: dict) -> None:
        from decimal import Decimal

        try:
            self._t().update_item(
                Key={"id": broadcast_id},
                UpdateExpression=expr,
                ConditionExpression="attribute_exists(id)",  # solo si el job existe (no upsert fantasma)
                ExpressionAttributeValues={k: Decimal(v) for k, v in values.items()},
            )
        except Exception:
            pass  # el tracking de estado nunca debe romper el envío

    # Tras esta antigüedad (s) un job aún incompleto se considera terminal (incrementos
    # perdidos o un canal que nunca arrancó), para no quedar 'enviando' indefinidamente.
    _EDAD_TERMINAL = 3600

    @staticmethod
    def _estado(j: dict) -> str:
        chans = j.get("channels", []) or []
        tg_sent, tg_failed = int(j.get("tg_sent", 0)), int(j.get("tg_failed", 0))
        wa_sent, wa_failed = int(j.get("wa_sent", 0)), int(j.get("wa_failed", 0))
        total = int(j.get("tg_total", 0)) + int(j.get("wa_total", 0))
        wa_pendiente = ("whatsapp" in chans) and not j.get("wa_started")
        done = tg_sent + tg_failed + wa_sent + wa_failed
        sent = tg_sent + wa_sent
        failed = tg_failed + wa_failed
        if total == 0 and not wa_pendiente:
            return "done"  # sin destinatarios reales / canal ya cerrado: terminal
        if wa_pendiente or done < total:
            base = "queued" if (done == 0 and not wa_pendiente) else "sending"
            created = int(j.get("created_at", 0))
            if created and (int(time.time()) - created) > DynamoDbBroadcastStore._EDAD_TERMINAL:
                return "failed" if sent == 0 else "partial"  # viejo y aún pendiente: cerrar
            return base
        return "failed" if sent == 0 else ("partial" if failed > 0 else "done")

    def _scan_todo(self) -> list[dict]:
        items, start = [], None
        while True:  # paginar para no perder los más recientes si la tabla supera 1MB
            kwargs = {"ExclusiveStartKey": start} if start else {}
            resp = self._t().scan(**kwargs)
            items.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
        return items

    def metricas(self, dias: int = 30) -> dict:
        """Agregado de entrega de los últimos N días: enviados/fallidos totales y por canal,
        tasa de éxito y serie diaria (para el dashboard). Deriva de los contadores de cada job."""
        from collections import defaultdict

        desde = int(time.time()) - int(dias) * 86400
        por_dia: dict[str, dict] = defaultdict(lambda: {"sent": 0, "failed": 0})
        tot = {"tg_s": 0, "tg_f": 0, "wa_s": 0, "wa_f": 0, "jobs": 0}
        for j in self._scan_todo():
            c = int(j.get("created_at", 0))
            if c < desde:
                continue
            ts, tf = int(j.get("tg_sent", 0)), int(j.get("tg_failed", 0))
            ws, wf = int(j.get("wa_sent", 0)), int(j.get("wa_failed", 0))
            tot["tg_s"] += ts; tot["tg_f"] += tf; tot["wa_s"] += ws; tot["wa_f"] += wf; tot["jobs"] += 1
            dia = datetime.fromtimestamp(c, timezone.utc).strftime("%Y-%m-%d")
            por_dia[dia]["sent"] += ts + ws
            por_dia[dia]["failed"] += tf + wf
        env, fail = tot["tg_s"] + tot["wa_s"], tot["tg_f"] + tot["wa_f"]
        return {
            "dias": int(dias),
            "jobs": tot["jobs"],
            "enviados": env,
            "fallidos": fail,
            "tasa_exito": round(env / (env + fail) * 100, 1) if (env + fail) else 100.0,
            "telegram": {"enviados": tot["tg_s"], "fallidos": tot["tg_f"]},
            "whatsapp": {"enviados": tot["wa_s"], "fallidos": tot["wa_f"]},
            "serie": [{"dia": d, **por_dia[d]} for d in sorted(por_dia)],
        }

    def listar(self, limit: int = 30) -> list[dict]:
        items = self._scan_todo()
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


class DynamoDbPlanStore:
    """Planes de envío FRACCIONADO: un plan retiene toda la difusión y el dispatcher libera
    UN lote a la vez (secuencial), dentro de la ventana horaria. Tabla con clave compuesta:

      (pid, "PLAN")       -> metadatos + cursores (tg_next/wa_next), in_flight y bitácora.
      (pid, "TG#000007")  -> ids del lote de Telegram (se leen al despachar).

    WhatsApp no guarda ids: el servicio resuelve y rebana por offset/limit, así el panel no
    maneja jids. El progreso real (enviados) se lee de la tabla Broadcasts por broadcast_id.
    """

    _META = "PLAN"

    def __init__(self, table_name: str | None = None, endpoint: str | None = None):
        self._name = table_name or os.environ.get("PLANS_TABLE", "Plans")
        self._endpoint = endpoint or os.environ.get("DYNAMODB_ENDPOINT")

    def _t(self):
        return _table(self._name, self._endpoint)

    @staticmethod
    def _tg_sk(i: int) -> str:
        return f"TG#{i:06d}"

    def crear(
        self,
        plan_id: str,
        *,
        broadcast_id: str,
        text: str,
        image_url: str | None,
        image_key: str | None,
        batch_size: int,
        tg_lotes: list[list[str]],
        wa_enabled: bool = False,
        wa_total: int = 0,
        wa_resolved: bool = False,
        wa_mode: str = "all",
        wa_list_ids: list[str] | None = None,
        wa_exclude: list[str] | None = None,
        wa_text: str | None = None,
        wa_image_url: str | None = None,
        not_before: int = 0,
        ttl_days: int = 30,
    ) -> None:
        from domain.scheduling import total_lotes

        now = int(time.time())
        wa_batches = total_lotes(int(wa_total), batch_size) if wa_resolved else 0
        meta = {
            "pid": plan_id,
            "sk": self._META,
            "status": "pending",
            "created_at": now,
            "not_before": int(not_before or 0),  # 0 = ya; >0 = programado para esa hora (epoch)
            "broadcast_id": broadcast_id,
            # OJO: este 'text' es el que SE ENVÍA (dispatch lo pasa a encolar_uno). Debe ser el
            # mensaje COMPLETO procesado, no un resumen: por eso el tope es 4096 (límite Telegram),
            # no 280. Truncarlo a 280 cortaba los envíos a la mitad de la lista.
            "text": (text or "")[:4096],
            "batch_size": int(batch_size),
            "tg_total": sum(len(l) for l in tg_lotes),
            "tg_batches": len(tg_lotes),
            "tg_next": 0,
            "tg_dispatched": 0,
            "wa_enabled": bool(wa_enabled),
            "wa_resolved": bool(wa_resolved),
            "wa_total": int(wa_total),
            "wa_batches": wa_batches,
            "wa_next": 0,
            "wa_dispatched": 0,
            "wa_mode": wa_mode or "all",
            "wa_list_ids": [str(x) for x in (wa_list_ids or [])],
            "wa_exclude": [str(x) for x in (wa_exclude or [])],
            "wa_text": wa_text if wa_text is not None else text,
            "wa_image_url": wa_image_url or "",
            "image_url": image_url or "",
            "image_key": image_key or "",
            "in_flight": "",
            "in_flight_at": 0,
            "in_flight_channel": "",
            "in_flight_target": 0,
            "dispatch_log": [],
            "ttl": now + ttl_days * 86400,
        }
        with self._t().batch_writer() as bw:
            bw.put_item(Item=meta)
            for i, ids in enumerate(tg_lotes):
                bw.put_item(Item={"pid": plan_id, "sk": self._tg_sk(i), "ids": list(ids), "n": len(ids)})

    def activos(self) -> list[dict]:
        """Planes PLAN pendientes/en curso, del más antiguo al más nuevo (orden de despacho)."""
        from boto3.dynamodb.conditions import Attr

        items, start = [], None
        while True:
            kwargs = {
                "FilterExpression": Attr("sk").eq(self._META) & Attr("status").is_in(["pending", "running"]),
            }
            if start:
                kwargs["ExclusiveStartKey"] = start
            resp = self._t().scan(**kwargs)
            items.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
        items.sort(key=lambda p: int(p.get("created_at", 0)))
        return items

    def ids_lote_tg(self, plan_id: str, index: int) -> list[str]:
        item = self._t().get_item(Key={"pid": plan_id, "sk": self._tg_sk(index)}).get("Item") or {}
        return [str(x) for x in item.get("ids", [])]

    def registrar_dispatch(
        self, plan_id: str, *, channel: str, index: int, n: int, target: int, now: int
    ) -> bool:
        """Reclama un lote (avanza cursor, fija in_flight, añade bitácora) SI el plan sigue
        despachable. Condicional sobre status != 'canceled' para que un cancel concurrente
        SIEMPRE gane (no se puede 'resucitar' un plan cancelado). Devuelve True si reclamó,
        False si el plan ya estaba cancelado (carrera con cancelar_pendientes)."""
        from decimal import Decimal

        from botocore.exceptions import ClientError

        cursor = "tg_next" if channel == "tg" else "wa_next"
        disp = "tg_dispatched" if channel == "tg" else "wa_dispatched"
        entry = {
            "ch": channel,
            "idx": int(index),
            "at": int(now),
            "n": int(n),
            "target": int(target),
        }
        try:
            self._t().update_item(
                Key={"pid": plan_id, "sk": self._META},
                UpdateExpression=(
                    "SET #st = :running, in_flight = :sk, in_flight_at = :now, "
                    "in_flight_channel = :ch, in_flight_target = :tgt, "
                    f"{cursor} = :nxt, {disp} = :disp, "
                    "dispatch_log = list_append(if_not_exists(dispatch_log, :empty), :entry)"
                ),
                ConditionExpression="attribute_exists(pid) AND #st <> :canceled",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":running": "running",
                    ":canceled": "canceled",
                    ":sk": f"{channel.upper()}#{index:06d}",
                    ":now": Decimal(int(now)),
                    ":ch": channel,
                    ":tgt": Decimal(int(target)),
                    ":nxt": Decimal(int(index) + 1),
                    ":disp": Decimal(int(target)),
                    ":empty": [],
                    ":entry": [entry],
                },
            )
            return True
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False  # cancelado en carrera: el cancel gana
            raise

    def limpiar_inflight(self, plan_id: str) -> None:
        self._t().update_item(
            Key={"pid": plan_id, "sk": self._META},
            UpdateExpression="SET in_flight = :empty, in_flight_channel = :empty",
            ExpressionAttributeValues={":empty": ""},
        )

    def resolver_wa(self, plan_id: str, wa_total: int, batch_size: int) -> None:
        """Fija el total de WhatsApp (resuelto por el servicio) y cuántos lotes implica.
        Permite crear el plan sin depender de que el servicio esté arriba en ese instante."""
        from decimal import Decimal

        from domain.scheduling import total_lotes

        self._t().update_item(
            Key={"pid": plan_id, "sk": self._META},
            UpdateExpression="SET wa_total = :t, wa_batches = :b, wa_resolved = :r",
            ExpressionAttributeValues={
                ":t": Decimal(int(wa_total)),
                ":b": Decimal(total_lotes(int(wa_total), batch_size)),
                ":r": True,
            },
        )

    def finalizar(self, plan_id: str, status: str = "done") -> None:
        from botocore.exceptions import ClientError

        kwargs = {
            "Key": {"pid": plan_id, "sk": self._META},
            "UpdateExpression": "SET #st = :s, in_flight = :empty",
            "ExpressionAttributeNames": {"#st": "status"},
            "ExpressionAttributeValues": {":s": status, ":empty": ""},
        }
        # Cerrar a 'done' NO debe pisar un 'canceled' (cancelar siempre gana). El cancel sí
        # aplica incondicionalmente sobre pending/running.
        if status != "canceled":
            kwargs["ConditionExpression"] = "#st <> :canceled"
            kwargs["ExpressionAttributeValues"][":canceled"] = "canceled"
        try:
            self._t().update_item(**kwargs)
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def descartar(self, plan_id: str) -> bool:
        """¿El worker debe DESCARTAR (no entregar) un lote de este plan? True si el plan ya
        no existe o está cancelado. Honra 'cancelar pendientes' para lotes ya encolados en SQS."""
        try:
            item = self._t().get_item(
                Key={"pid": plan_id, "sk": self._META}, ProjectionExpression="#st",
                ExpressionAttributeNames={"#st": "status"},
            ).get("Item")
        except Exception:
            return False  # ante duda, no bloquear el envío
        if not item:
            return True  # plan inexistente (borrado): descartar
        return str(item.get("status", "")) == "canceled"

    def cancelar(self, plan_id: str) -> bool:
        """Cancela UN envío puntual (para frenar en tiempo real algo que va mal) sin tocar
        los demás. El dispatcher dejará de despacharlo y el worker descartará su lote en vuelo."""
        self.finalizar(plan_id, status="canceled")
        return True

    def cancelar_pendientes(self) -> int:
        """Marca como 'canceled' todos los planes pendientes/en curso para que el dispatcher
        NO los despache (ni al reactivar). Devuelve cuántos se cancelaron."""
        n = 0
        for p in self.activos():
            self.finalizar(p["pid"], status="canceled")
            n += 1
        return n

    def listar(self, limit: int = 20) -> list[dict]:
        """Planes (PLAN) más recientes, con su bitácora, para la vista de programación."""
        from boto3.dynamodb.conditions import Attr

        items, start = [], None
        while True:
            kwargs = {"FilterExpression": Attr("sk").eq(self._META)}
            if start:
                kwargs["ExclusiveStartKey"] = start
            resp = self._t().scan(**kwargs)
            items.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
        items.sort(key=lambda p: int(p.get("created_at", 0)), reverse=True)
        salida = []
        for p in items[:limit]:
            log = []
            for e in p.get("dispatch_log", []) or []:
                log.append(
                    {
                        "ch": e.get("ch"),
                        "idx": int(e.get("idx", 0)),
                        "at": int(e.get("at", 0)),
                        "n": int(e.get("n", 0)),
                        "target": int(e.get("target", 0)),
                    }
                )
            salida.append(
                {
                    "pid": p.get("pid"),
                    "broadcast_id": p.get("broadcast_id"),
                    "status": p.get("status"),
                    "created_at": int(p.get("created_at", 0)),
                    "not_before": int(p.get("not_before", 0)),
                    "text": p.get("text", ""),
                    "batch_size": int(p.get("batch_size", 0)),
                    "tg": {"total": int(p.get("tg_total", 0)), "batches": int(p.get("tg_batches", 0)),
                           "next": int(p.get("tg_next", 0))},
                    "wa": {"total": int(p.get("wa_total", 0)), "batches": int(p.get("wa_batches", 0)),
                           "next": int(p.get("wa_next", 0)), "enabled": bool(p.get("wa_enabled", False)),
                           "resolved": bool(p.get("wa_resolved", False))},
                    "in_flight": p.get("in_flight", ""),
                    "log": log,
                }
            )
        return salida


class DynamoDbAuditStore:
    """Bitácora de acciones del panel (quién hizo qué). Best-effort: nunca rompe la acción."""

    def __init__(self, table_name: str | None = None, endpoint: str | None = None):
        self._name = table_name or os.environ.get("AUDIT_TABLE", "Audit")
        self._endpoint = endpoint or os.environ.get("DYNAMODB_ENDPOINT")

    def _t(self):
        return _table(self._name, self._endpoint)

    def registrar(self, action: str, detail: str = "", user: str = "admin", ttl_days: int = 90) -> None:
        now = int(time.time())
        try:
            self._t().put_item(
                Item={
                    "id": uuid.uuid4().hex,
                    "ts": now,
                    "action": str(action)[:60],
                    "detail": str(detail or "")[:300],
                    "user": str(user or "")[:60],
                    "ttl": now + ttl_days * 86400,
                }
            )
        except Exception:
            logger = __import__("logging").getLogger(__name__)
            logger.exception("No se pudo registrar auditoría (no afecta la acción)")

    def listar(self, limit: int = 50) -> list[dict]:
        items, start = [], None
        while True:
            kwargs = {"ExclusiveStartKey": start} if start else {}
            resp = self._t().scan(**kwargs)
            items.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
        items.sort(key=lambda a: int(a.get("ts", 0)), reverse=True)
        return [
            {"ts": int(a.get("ts", 0)), "action": a.get("action", ""), "detail": a.get("detail", ""),
             "user": a.get("user", "")}
            for a in items[:limit]
        ]


class DynamoDbScheduleStore:
    """Mensajes programados (once/daily/weekly). El dispatcher materializa los vencidos
    creando un envío manual (plan), reutilizando todo el pipeline de entrega.

    NOTA: ``name`` y ``type`` son palabras reservadas en DynamoDB → toda actualización
    usa ``ExpressionAttributeNames`` (``#campo``) para evitar errores de validación."""

    def __init__(self, table_name: str | None = None, endpoint: str | None = None):
        self._name = table_name or os.environ.get("SCHEDULES_TABLE", "Schedules")
        self._endpoint = endpoint or os.environ.get("DYNAMODB_ENDPOINT")

    def _t(self):
        return _table(self._name, self._endpoint)

    @staticmethod
    def _norm(it: dict) -> dict:
        return {
            "sid": it.get("sid", ""),
            "name": it.get("name", ""),
            "text": it.get("text", ""),
            "image_url": it.get("image_url", ""),
            "telegram": bool(it.get("telegram", False)),
            "telegram_list": it.get("telegram_list", ""),
            "whatsapp": bool(it.get("whatsapp", False)),
            "whatsapp_list": it.get("whatsapp_list", ""),
            "type": it.get("type", "once"),
            "at": it.get("at", ""),
            "days": [int(d) for d in (it.get("days") or [])],
            "next_run": int(it.get("next_run", 0) or 0),
            "enabled": bool(it.get("enabled", False)),
            "last_run": int(it.get("last_run", 0) or 0),
            "runs": int(it.get("runs", 0) or 0),
            "created_at": int(it.get("created_at", 0) or 0),
        }

    def crear(self, *, name: str, text: str, image_url: str, telegram: bool, telegram_list: str,
              whatsapp: bool, whatsapp_list: str, type: str, at: str, days, next_run: int,
              enabled: bool = True) -> str:
        sid = "s-" + uuid.uuid4().hex[:16]
        self._t().put_item(
            Item={
                "sid": sid,
                "name": str(name or "")[:80],
                "text": str(text or "")[:4096],
                "image_url": str(image_url or ""),
                "telegram": bool(telegram),
                "telegram_list": str(telegram_list or ""),
                "whatsapp": bool(whatsapp),
                "whatsapp_list": str(whatsapp_list or ""),
                "type": str(type),
                "at": str(at or ""),
                "days": [int(d) for d in (days or [])],
                "next_run": int(next_run or 0),
                "enabled": bool(enabled),
                "last_run": 0,
                "runs": 0,
                "created_at": int(time.time()),
            }
        )
        return sid

    def listar(self) -> list[dict]:
        items, start = [], None
        while True:
            kwargs = {"ExclusiveStartKey": start} if start else {}
            resp = self._t().scan(**kwargs)
            items.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
        salida = [self._norm(i) for i in items]
        # activos primero, luego por próximo disparo ascendente
        salida.sort(key=lambda s: (not s["enabled"], s["next_run"] or 9_999_999_999))
        return salida

    def vencidos(self, now: int) -> list[dict]:
        return [s for s in self.listar() if s["enabled"] and 0 < s["next_run"] <= int(now)]

    def actualizar(self, sid: str, **campos) -> None:
        if not campos:
            return
        names = {f"#{k}": k for k in campos}
        values = {f":{k}": v for k, v in campos.items()}
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in campos)
        self._t().update_item(
            Key={"sid": sid},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def borrar(self, sid: str) -> None:
        self._t().delete_item(Key={"sid": sid})
