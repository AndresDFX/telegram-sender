"""Entrypoint del worker (consumidor SQS): entrega cada lote y reporta fallos parciales.

Respuesta parcial de lotes (``batchItemFailures``): solo los mensajes con fallo
sistémico (todo el lote falló) vuelven a la cola y, agotados los reintentos, caen a
la DLQ. Los fallos por usuario individual se registran pero NO reencolan el lote.

Si el lote trae ``image_key`` (imagen subida al panel), se resuelve a una URL
temporal (presigned) justo antes de enviar; ``image_url`` (URL externa) se usa tal cual.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import wiring

logger = logging.getLogger()
logger.setLevel(logging.INFO)

deliver = None  # caso de uso DeliverBatch; inyectable en tests
image_store = None  # S3ImageStore; inyectable en tests
broadcasts = None  # DynamoDbBroadcastStore; inyectable en tests
config_store = None  # DynamoDbConfigStore; inyectable en tests
plans = None  # DynamoDbPlanStore; inyectable en tests
dedup = None  # DynamoDbDedupStore; idempotencia de lotes (no reenviar)

# Tras N lotes TOTALMENTE fallidos seguidos asumimos baneo/rate-limit sistémico y AUTO-PAUSAMOS
# los envíos (sending_enabled=False) para proteger la cuenta/número. Se reinicia al primer éxito.
_BAN_STRIKES_UMBRAL = 2


def _ensure() -> None:
    global deliver, image_store, broadcasts, config_store, plans, dedup
    if deliver is None:
        deliver = wiring.build_deliver_batch()
    if image_store is None:
        image_store = wiring.build_image_store()
    if broadcasts is None:
        broadcasts = wiring.build_broadcast_store()
    if config_store is None:
        config_store = wiring.build_config_store()
    if plans is None:
        plans = wiring.build_plan_store()
    if dedup is None:
        dedup = wiring.build_dedup()


def _detectar_baneo() -> None:
    """Un lote falló por completo: cuenta el strike y auto-pausa si se alcanza el umbral."""
    try:
        strikes = config_store.incr_ban_strikes()
        if strikes >= _BAN_STRIKES_UMBRAL:
            config_store.set({"sending_enabled": False})
            logger.warning("AUTO-PAUSA anti-baneo: %d lotes fallaron consecutivamente; envíos desactivados.", strikes)
    except Exception:
        logger.exception("No se pudo registrar strike / auto-pausar")


def _resolver_imagen(body: dict) -> str | None:
    key = body.get("image_key")
    if key:
        try:
            return image_store.url_temporal(key)
        except Exception:
            logger.exception("No se pudo generar URL temporal para %s", key)
    return body.get("image_url")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()

    # Interruptor maestro: si los envíos están desactivados, NO enviar. Confirmamos (ack)
    # los lotes en vuelo sin entregarlos para no reintentarlos ni mandarlos al reactivar.
    try:
        if not config_store.get().get("sending_enabled", True):
            logger.info("Envíos PAUSADOS; se descartan %d registro(s) sin enviar.", len(event.get("Records", [])))
            return {"batchItemFailures": []}
    except Exception:
        logger.exception("No se pudo leer sending_enabled; continúo (fail-open al envío)")

    batch_item_failures: list[dict[str, str]] = []

    for record in event.get("Records", []):
        message_id = record.get("messageId")
        try:
            body = json.loads(record["body"])
            # IDEMPOTENCIA: si este lote ya se entregó (reentrega SQS espuria), no reenviar.
            batch_id = body.get("batch_id")
            if batch_id and dedup.procesado(batch_id):
                logger.info("Lote %s ya entregado (dedup); se omite para no duplicar", batch_id)
                continue
            # Si el plan fue CANCELADO tras encolar este lote, se descarta sin enviar
            # (honra 'cancelar pendientes' para lotes ya en vuelo en SQS). Ack, no reencola.
            pid = body.get("pid")
            if pid:
                try:
                    if plans.descartar(pid):
                        logger.info("Plan %s cancelado/inexistente; lote %s descartado sin enviar", pid, message_id)
                        continue
                except Exception:
                    logger.exception("No se pudo verificar el estado del plan %s; continúo (fail-open)", pid)
            stats = deliver(body["text"], body.get("chat_ids", []), _resolver_imagen(body))
            logger.info("Lote %s procesado: %s", body.get("batch_index"), stats.resumen())
            bid = body.get("broadcast_id")
            # Auditoría: guarda la razón legible del fallo (idempotente: set + last_error, no infla
            # contadores aunque el lote se reintente). Responde "¿por qué falló este envío?".
            if bid and stats.errores:
                try:
                    broadcasts.registrar_error(bid, "Telegram — " + stats.errores[0])
                except Exception:
                    logger.exception("No se pudo registrar la razón de fallo del envío %s", bid)
            fallo_total = stats.total > 0 and stats.failed == stats.total
            # Solo contamos si el lote NO se va a reencolar: si reencola (fallo total), un ADD
            # aquí se reaplicaría en cada reintento SQS e inflaría los contadores.
            if bid and not fallo_total:
                broadcasts.incr_telegram(bid, sent=stats.sent, failed=stats.failed + stats.blocked)
            if not fallo_total:
                if batch_id:
                    dedup.marcar(batch_id)  # marca entregado (idempotencia ante reentrega)
                if stats.sent > 0:
                    config_store.reset_ban_strikes()  # hubo entregas: reinicia el contador anti-baneo
            else:
                _detectar_baneo()  # fallo total: cuenta strike y auto-pausa si procede
                raise RuntimeError(f"Todos los envíos del lote {message_id} fallaron")
        except Exception:
            logger.exception("Fallo procesando el mensaje SQS %s", message_id)
            if message_id:
                batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
