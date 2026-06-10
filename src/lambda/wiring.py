"""Composition root: construye los casos de uso cableando adapters y configuración.

Es la única capa que conoce a la vez los casos de uso y las implementaciones
concretas. Los entrypoints piden aquí sus dependencias ya cableadas.
"""

from __future__ import annotations

from adapters import config
from adapters.dynamodb import (
    DynamoDbConfigStore,
    DynamoDbDedupStore,
    DynamoDbHighWaterMarkStore,
    DynamoDbSubscriberRepository,
)
from adapters.sqs import InlineBroadcastQueue, SqsBroadcastQueue, SqsQueueStats
from adapters.telegram import TelegramSender
from adapters.tme import TmePreviewChannelReader
from application.broadcasting import BroadcastList
from application.deliver_batch import DeliverBatch
from application.onboarding import HandleCommand
from application.poll_channel import PollChannel


def _subscribers() -> DynamoDbSubscriberRepository:
    return DynamoDbSubscriberRepository()


def _sender() -> TelegramSender:
    return TelegramSender(bot_token=config.bot_token())


def build_config_store() -> DynamoDbConfigStore:
    return DynamoDbConfigStore()


def _broadcast_list() -> BroadcastList:
    subs = _subscribers()
    if config.broadcast_queue_url():
        queue = SqsBroadcastQueue()
    else:
        # Dev local sin SQS: entrega inmediata (el envío inline NO lanza, ver DeliverBatch).
        deliver = DeliverBatch(_sender(), subs, delay=config.send_delay_seconds())
        queue = InlineBroadcastQueue(lambda text, ids, image_url=None: deliver(text, ids, image_url))
    return BroadcastList(subs, queue, build_config_store())


def build_dedup() -> DynamoDbDedupStore:
    return DynamoDbDedupStore()


def build_queue_stats() -> SqsQueueStats:
    return SqsQueueStats()


def build_subscribers() -> DynamoDbSubscriberRepository:
    return _subscribers()


def build_handle_command() -> HandleCommand:
    return HandleCommand(_subscribers(), _sender())


def build_broadcast_list() -> BroadcastList:
    return _broadcast_list()


def build_deliver_batch() -> DeliverBatch:
    return DeliverBatch(_sender(), _subscribers(), delay=config.send_delay_seconds())


def build_poll_channel() -> PollChannel:
    return PollChannel(
        TmePreviewChannelReader(),
        DynamoDbHighWaterMarkStore(),
        _broadcast_list(),
        build_config_store(),
    )
