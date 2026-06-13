"""Composition root: construye los casos de uso cableando adapters y configuración.

Conmuta entre modo **bot** y **userbot** según la config (DynamoDB, con fallback a
env). Las credenciales de Telethon también viven en la config → la cuenta de Telegram
se gestiona desde la plataforma sin redeploy.
"""

from __future__ import annotations

from adapters import config
from adapters.dynamodb import (
    DynamoDbAuditStore,
    DynamoDbBroadcastStore,
    DynamoDbConfigStore,
    DynamoDbDedupStore,
    DynamoDbHighWaterMarkStore,
    DynamoDbPlanStore,
    DynamoDbScheduleStore,
    DynamoDbSubscriberRepository,
)
from adapters.s3 import S3ImageStore
from adapters.sqs import InlineBroadcastQueue, SqsBroadcastQueue, SqsQueueStats
from adapters.telegram import TelegramSender
from adapters.telethon_user import CachedContacts, ContactRecipients, TelethonContacts, TelethonUserSender
from adapters.tme import TmePreviewChannelReader
from adapters.whatsapp import HttpWhatsAppForwarder
from application.broadcasting import BroadcastList
from application.deliver_batch import DeliverBatch
from application.dispatch import DispatchCampaigns
from application.materialize_schedules import MaterializeSchedules
from application.onboarding import HandleCommand
from application.poll_channel import PollChannel


def build_config_store() -> DynamoDbConfigStore:
    return DynamoDbConfigStore()


def _es_userbot(cfg: dict) -> bool:
    return str(cfg.get("send_mode", "bot")).lower() == "userbot"


def _telethon_contacts(cfg: dict) -> TelethonContacts:
    return TelethonContacts(
        api_id=cfg.get("telethon_api_id") or None,
        api_hash=cfg.get("telethon_api_hash") or None,
        session=cfg.get("telethon_session") or None,
    )


def _bot_token(cfg: dict) -> str | None:
    """Token del bot: el de la config (editable desde el panel) o el de entorno."""
    return (cfg.get("bot_token") or "").strip() or config.bot_token()


def _sender(cfg: dict):
    if _es_userbot(cfg):
        return TelethonUserSender(
            api_id=cfg.get("telethon_api_id") or None,
            api_hash=cfg.get("telethon_api_hash") or None,
            session=cfg.get("telethon_session") or None,
        )
    return TelegramSender(bot_token=_bot_token(cfg))


def _recipients(cfg: dict):
    return ContactRecipients(_telethon_contacts(cfg)) if _es_userbot(cfg) else DynamoDbSubscriberRepository()


def _recipients_listado(cfg: dict, store):
    """Destinatarios para LISTAR (difusión/panel). En userbot usa la CACHÉ (DynamoDB),
    NUNCA Telethon en vivo: GetContacts en vivo dispara FloodWait y tumbaba el envío/preview
    del panel (HTTP 500 'internal'). El envío real lo hace el worker, sí en vivo, por mensaje."""
    return CachedContacts(store) if _es_userbot(cfg) else DynamoDbSubscriberRepository()


def _broadcast_list() -> BroadcastList:
    store = build_config_store()
    cfg = store.get()
    # Para LISTAR destinatarios usamos la caché (evita FloodWait de Telethon en el panel/difusión).
    recipients = _recipients_listado(cfg, store)
    if config.broadcast_queue_url():
        queue = SqsBroadcastQueue()
    else:
        deliver = DeliverBatch(_sender(cfg), recipients, delay=config.send_delay_seconds())
        queue = InlineBroadcastQueue(lambda text, ids, image_url=None: deliver(text, ids, image_url))
    whatsapp = HttpWhatsAppForwarder(cfg.get("whatsapp_service_url", ""), cfg.get("whatsapp_token", ""))
    # El scheduler (planes en DynamoDB) solo aplica con SQS real (AWS); en inline (dev) se
    # envía de inmediato. Así el envío fraccionado/secuencial no depende de tablas en local.
    plans = build_plan_store() if config.broadcast_queue_url() else None
    return BroadcastList(
        recipients, queue, store, whatsapp=whatsapp, image_store=S3ImageStore(),
        broadcasts=build_broadcast_store(), plans=plans,
    )


def build_broadcast_store() -> DynamoDbBroadcastStore:
    return DynamoDbBroadcastStore()


def build_plan_store() -> DynamoDbPlanStore:
    return DynamoDbPlanStore()


def build_audit_store() -> DynamoDbAuditStore:
    return DynamoDbAuditStore()


def build_schedule_store() -> DynamoDbScheduleStore:
    return DynamoDbScheduleStore()


def build_materialize_schedules() -> MaterializeSchedules:
    """Materializador de mensajes programados: reutiliza el envío manual (→ plan → dispatcher)."""
    return MaterializeSchedules(
        schedules=build_schedule_store(),
        broadcasting=_broadcast_list(),
        config=build_config_store(),
    )


def build_dedup() -> DynamoDbDedupStore:
    return DynamoDbDedupStore()


def build_queue_stats() -> SqsQueueStats:
    return SqsQueueStats()


def build_image_store() -> S3ImageStore:
    return S3ImageStore()


def build_subscribers():
    """Destinatarios para el PANEL: contactos cacheados (userbot) o suscriptores (bot)."""
    store = build_config_store()
    if _es_userbot(store.get()):
        return CachedContacts(store)
    return DynamoDbSubscriberRepository()


def build_contacts_source() -> TelethonContacts | None:
    """Fuente en vivo de contactos (para refrescar el caché desde el poller). None en modo bot."""
    store = build_config_store()
    cfg = store.get()
    return _telethon_contacts(cfg) if _es_userbot(cfg) else None


def build_handle_command() -> HandleCommand:
    cfg = build_config_store().get()
    return HandleCommand(DynamoDbSubscriberRepository(), TelegramSender(bot_token=_bot_token(cfg)))


def build_deliver_batch() -> DeliverBatch:
    cfg = build_config_store().get()
    fijo = config.send_delay_seconds()
    return DeliverBatch(
        _sender(cfg),
        _recipients(cfg),
        delay_min=cfg.get("tg_delay_min", fijo),
        delay_max=cfg.get("tg_delay_max", fijo),
    )


def build_broadcast_list() -> BroadcastList:
    return _broadcast_list()


def build_dispatch_campaigns() -> DispatchCampaigns:
    store = build_config_store()
    cfg = store.get()
    whatsapp = HttpWhatsAppForwarder(cfg.get("whatsapp_service_url", ""), cfg.get("whatsapp_token", ""))
    return DispatchCampaigns(
        plans=build_plan_store(),
        broadcasts=build_broadcast_store(),
        queue=SqsBroadcastQueue(),
        whatsapp=whatsapp,
        config=store,
    )


def build_poll_channel() -> PollChannel:
    return PollChannel(
        TmePreviewChannelReader(),
        DynamoDbHighWaterMarkStore(),
        _broadcast_list(),
        build_config_store(),
    )
