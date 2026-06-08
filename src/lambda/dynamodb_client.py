"""Acceso a suscriptores y deduplicación de updates en DynamoDB.

Los imports de ``boto3``/``botocore`` son perezosos (dentro de cada función) para
que las pruebas unitarias puedan importar este módulo sin tenerlos instalados; el
runtime de Lambda siempre los trae.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Iterator

SUBSCRIBERS_TABLE = os.environ.get("SUBSCRIBERS_TABLE", "SubscriptoresTelegram")
SUBSCRIBERS_STATUS_INDEX = os.environ.get("SUBSCRIBERS_STATUS_INDEX", "StatusIndex")
PROCESSED_UPDATES_TABLE = os.environ.get("PROCESSED_UPDATES_TABLE", "ProcessedUpdates")
DEDUP_TTL_SECONDS = int(os.environ.get("DEDUP_TTL_SECONDS", "86400"))
DYNAMODB_ENDPOINT = os.environ.get("DYNAMODB_ENDPOINT")
ACTIVE_STATUS = "active"
INACTIVE_STATUS = "inactive"


def _resource():
    import boto3

    kwargs = {}
    if DYNAMODB_ENDPOINT:
        kwargs["endpoint_url"] = DYNAMODB_ENDPOINT
    return boto3.resource("dynamodb", **kwargs)


def _table():
    return _resource().Table(SUBSCRIBERS_TABLE)


def _processed_table():
    return _resource().Table(PROCESSED_UPDATES_TABLE)


# --- Suscriptores -----------------------------------------------------------

def obtener_usuarios_activos() -> list[str]:
    """Devuelve chat IDs de suscriptores activos usando el GSI StatusIndex."""
    from boto3.dynamodb.conditions import Key

    table = _table()
    chat_ids: list[str] = []
    query_kwargs = {
        "IndexName": SUBSCRIBERS_STATUS_INDEX,
        "KeyConditionExpression": Key("status").eq(ACTIVE_STATUS),
        "ProjectionExpression": "chatId",
    }

    while True:
        response = table.query(**query_kwargs)
        chat_ids.extend(item["chatId"] for item in response.get("Items", []))

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key

    return chat_ids


def iter_usuarios_activos(page_size: int = 200) -> Iterator[list[str]]:
    """Iterador paginado para escenarios de alto volumen."""
    from boto3.dynamodb.conditions import Key

    table = _table()
    query_kwargs = {
        "IndexName": SUBSCRIBERS_STATUS_INDEX,
        "KeyConditionExpression": Key("status").eq(ACTIVE_STATUS),
        "ProjectionExpression": "chatId",
        "Limit": page_size,
    }

    while True:
        response = table.query(**query_kwargs)
        batch = [item["chatId"] for item in response.get("Items", [])]
        if batch:
            yield batch

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key


def registrar_suscriptor(chat_id: str, status: str = ACTIVE_STATUS) -> None:
    """Alta/baja idempotente de un suscriptor (upsert), preservando createdAt."""
    table = _table()
    now = datetime.now(timezone.utc).isoformat()
    table.update_item(
        Key={"chatId": chat_id},
        UpdateExpression=(
            "SET #status = :status, updatedAt = :now, "
            "createdAt = if_not_exists(createdAt, :now)"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": status, ":now": now},
    )


def marcar_inactivo(chat_id: str) -> None:
    from boto3.dynamodb.conditions import Attr

    table = _table()
    table.update_item(
        Key={"chatId": chat_id},
        UpdateExpression="SET #status = :inactive, updatedAt = :updatedAt",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":inactive": INACTIVE_STATUS,
            ":updatedAt": datetime.now(timezone.utc).isoformat(),
        },
        ConditionExpression=Attr("chatId").exists(),
    )


# --- Deduplicación de updates ------------------------------------------------

def marcar_update_procesado(update_id: str, ttl_seconds: int | None = None) -> bool:
    """Marca un update_id como procesado de forma atómica.

    Devuelve ``True`` si se marcó por primera vez (hay que procesarlo) o
    ``False`` si ya estaba registrado (es un duplicado y debe ignorarse). El item
    expira por TTL (``expiresAt``) para no crecer indefinidamente.
    """
    from botocore.exceptions import ClientError

    ttl = DEDUP_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    table = _processed_table()
    try:
        table.put_item(
            Item={"updateId": str(update_id), "expiresAt": int(time.time()) + ttl},
            ConditionExpression="attribute_not_exists(updateId)",
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def borrar_update_procesado(update_id: str) -> None:
    """Revierte la marca de dedup (compensación si el procesamiento falla)."""
    table = _processed_table()
    table.delete_item(Key={"updateId": str(update_id)})
