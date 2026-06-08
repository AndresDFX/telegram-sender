"""Poller del canal fuente PÚBLICO (ingesta por opción C).

El bot no es (ni puede ser) admin del canal de precios, así que en vez de recibir
`channel_post` por webhook, este Lambda —disparado por EventBridge cada pocos
minutos— lee el **preview público** `https://t.me/s/<canal>`, detecta las
publicaciones nuevas (por message_id, con un high-water mark), les aplica markup y
las **encola en SQS** para que el worker existente las distribuya.

Reutiliza markup, suscriptores, SQS y worker. El onboarding (/start·/stop) sigue
por el webhook del receptor (los DMs al bot llegan igual, no dependen del canal).
"""

from __future__ import annotations

import html
import logging
import os
import re
import urllib.request
from typing import Any

from broadcaster import procesar_lote
from dynamodb_client import guardar_hwm, obtener_hwm, obtener_usuarios_activos
from markup import aplicar_markup
from sqs_client import encolar_lotes

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CHANNEL = os.environ.get("SOURCE_CHANNEL_USERNAME", "iproparts")
PREVIEW_URL = "https://t.me/s/{channel}"
USER_AGENT = "Mozilla/5.0 (compatible; TelegramSyncPoller/1.0)"

_DATA_POST = re.compile(r'data-post="[^"/]+/(\d+)"')
_MSG_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)


def _fetch(channel: str) -> str:
    req = urllib.request.Request(PREVIEW_URL.format(channel=channel), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


def _limpiar(fragmento: str) -> str:
    fragmento = re.sub(r"<br\s*/?>", "\n", fragmento)
    fragmento = re.sub(r"</p>", "\n", fragmento)
    fragmento = re.sub(r"<[^>]+>", "", fragmento)
    return html.unescape(fragmento).strip()


def _parse_posts(html_text: str) -> list[tuple[int, str]]:
    """Devuelve (message_id, texto) de cada publicación con texto, en orden de aparición."""
    posts: list[tuple[int, str]] = []
    # Cada publicación va en un contenedor 'tgme_widget_message_wrap'.
    for bloque in html_text.split("tgme_widget_message_wrap")[1:]:
        mid = _DATA_POST.search(bloque)
        if not mid:
            continue
        mtext = _MSG_TEXT.search(bloque)
        texto = _limpiar(mtext.group(1)) if mtext else ""
        if texto:
            posts.append((int(mid.group(1)), texto))
    return posts


def _distribuir(texto: str) -> None:
    mensaje = aplicar_markup(texto)
    clientes = obtener_usuarios_activos()
    queue_url = os.environ.get("BROADCAST_QUEUE_URL")
    if queue_url:
        lotes = encolar_lotes(mensaje, clientes, queue_url=queue_url)
        logger.info("Encolados %d lotes para %d clientes", lotes, len(clientes))
    else:
        procesar_lote(mensaje, clientes)  # modo inline (dev local)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    posts = _parse_posts(_fetch(CHANNEL))
    if not posts:
        logger.info("Preview de %s sin publicaciones con texto", CHANNEL)
        return {"checked": 0, "new": 0}

    max_id = max(mid for mid, _ in posts)
    hwm = obtener_hwm(CHANNEL)

    # Primera ejecución: sembrar el HWM sin difundir el backlog del preview.
    if hwm is None:
        guardar_hwm(CHANNEL, max_id)
        logger.info("Sembrado HWM de %s en %d (sin difundir backlog)", CHANNEL, max_id)
        return {"checked": len(posts), "new": 0, "seeded": max_id}

    nuevos = sorted((mid, txt) for mid, txt in posts if mid > hwm)
    for mid, texto in nuevos:
        _distribuir(texto)
        guardar_hwm(CHANNEL, mid)  # avanzar tras cada uno (idempotente ante reintentos)

    logger.info("Canal %s: %d revisados, %d nuevos difundidos", CHANNEL, len(posts), len(nuevos))
    return {"checked": len(posts), "new": len(nuevos)}
