"""Entrypoint del poller (EventBridge cron): delega en el caso de uso PollChannel."""

from __future__ import annotations

import logging
from typing import Any

import wiring

logger = logging.getLogger()
logger.setLevel(logging.INFO)

poll = None  # caso de uso; inyectable en tests


def _ensure() -> None:
    global poll
    if poll is None:
        poll = wiring.build_poll_channel()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ensure()
    return poll()
