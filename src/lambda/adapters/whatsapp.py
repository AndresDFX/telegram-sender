"""Adapter que reenvía la lista al servicio de WhatsApp (Baileys) por HTTP.

Fire-and-forget: el servicio responde 202 y envía en segundo plano, así que el
timeout corto solo cubre la entrega del pedido, no el envío completo.
"""

from __future__ import annotations

import json
import logging
import urllib.request

from application.ports import WhatsAppForwarder

logger = logging.getLogger(__name__)


class HttpWhatsAppForwarder(WhatsAppForwarder):
    def __init__(self, service_url: str, token: str, timeout: float = 15.0):
        self._url = (service_url or "").rstrip("/")
        self._token = token or ""
        self._timeout = timeout

    def forward(self, text: str, image_url: str | None, exclude: list[str]) -> dict:
        if not self._url or not self._token:
            return {"skipped": "whatsapp no configurado"}
        data = json.dumps({"text": text, "image_url": image_url, "exclude": list(exclude or [])}).encode()
        req = urllib.request.Request(
            f"{self._url}/send",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode())


class NullWhatsAppForwarder(WhatsAppForwarder):
    def forward(self, text: str, image_url: str | None, exclude: list[str]) -> dict:
        return {"skipped": "disabled"}
