"""Composition root: construye los casos de uso cableando adapters y configuración.

Conmuta entre modo **bot** (envía como bot a los suscriptores que dieron /start) y
modo **userbot** (envía como TU cuenta vía Telethon a tus contactos) según SEND_MODE.
"""

from __future__ import annotations

from adapters import config
from adapters.dynamodb import (
    DynamoDbConfigStore,
    DynamoDbDedupStore,
    DynamoDbHighWaterMarkStore,
    DynamoDbSubscriberRepository,
)
from adapters.s3 import S3ImageStore
from adapters.sqs import InlineBroadcastQueue, SqsBroadcastQueue, SqsQueueStats
from adapters.telegram import TelegramSender
from adapters.telethon_user import ContactRecipients, TelethonContacts, TelethonUserSender
from adapters.tme import TmePreviewChannelReader
from application.broadcasting import BroadcastList
from application.deliver_batch import DeliverBatch
from application.onboarding import HandleCommand
from application.poll_channel import PollChannel


def _userbot() -> bool:
    return config.send_mode() == "userbot"


def _recipients():
    """Destinatarios: contactos de la cuenta (userbot) o suscriptores del bot."""
    return ContactRecipients(TelethonContacts()) if _userbot() else DynamoDbSubscriberRepository()


def _sender():
    return TelethonUserSender() if _userbot() else TelegramSender(bot_token=config.bot_token())


def build_config_store() -> DynamoDbConfigStore:
    return DynamoDbConfigStore()


def _broadcast_list() -> BroadcastList:
    recipients = _recipients()
    if config.broadcast_queue_url():
        queue = SqsBroadcastQueue()
    else:
        deliver = DeliverBatch(_sender(), recipients, delay=config.send_delay_seconds())
        queue = InlineBroadcastQueue(lambda text, ids, image_url=None: deliver(text, ids, image_url))
    return BroadcastList(recipients, queue, build_config_store())


def build_dedup() -> DynamoDbDedupStore:
    return DynamoDbDedupStore()


def build_queue_stats() -> SqsQueueStats:
    return SqsQueueStats()


def build_image_store() -> S3ImageStore:
    return S3ImageStore()


def build_subscribers():
    """Lo que el panel muestra como 'destinatarios' (suscriptores o contactos)."""
    return _recipients()


def build_handle_command() -> HandleCommand:
    # El onboarding /start solo aplica al modo bot; en userbot el sender es Telethon.
    return HandleCommand(DynamoDbSubscriberRepository(), TelegramSender(bot_token=config.bot_token()))


def build_broadcast_list() -> BroadcastList:
    return _broadcast_list()


def build_deliver_batch() -> DeliverBatch:
    return DeliverBatch(_sender(), _recipients(), delay=config.send_delay_seconds())


def build_poll_channel() -> PollChannel:
    return PollChannel(
        TmePreviewChannelReader(),
        DynamoDbHighWaterMarkStore(),
        _broadcast_list(),
        build_config_store(),
    )
