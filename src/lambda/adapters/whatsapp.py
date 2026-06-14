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

    def forward(
        self,
        text: str,
        image_url: str | None,
        exclude: list[str],
        *,
        mode: str = "all",
        list_ids: list[str] | None = None,
        broadcast_id: str | None = None,
        broadcasts_table: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
        bc_total: int | None = None,
        delay_min_ms: int | None = None,
        delay_max_ms: int | None = None,
        exclude_patterns: list[str] | None = None,
        pattern_exceptions: list[str] | None = None,
    ) -> dict:
        if not self._url or not self._token:
            return {"skipped": "whatsapp no configurado"}
        payload = {
            "text": text,
            "image_url": image_url,
            "exclude": list(exclude or []),
            "exclude_patterns": list(exclude_patterns or []),  # auto-excluir por patrón de nombre
            "pattern_exceptions": list(pattern_exceptions or []),  # ids incluidos pese al patrón
            "mode": mode or "all",
            "list_ids": list(list_ids or []),
            "broadcast_id": broadcast_id,
            "broadcasts_table": broadcasts_table,
        }
        # Fraccionado: el servicio resuelve el set completo y rebana [offset, offset+limit).
        if offset is not None:
            payload["offset"] = int(offset)
        if limit is not None:
            payload["limit"] = int(limit)
        if bc_total is not None:
            payload["bc_total"] = int(bc_total)  # total real del job (no el del slice)
        if delay_min_ms is not None:
            payload["delay_min_ms"] = int(delay_min_ms)
        if delay_max_ms is not None:
            payload["delay_max_ms"] = int(delay_max_ms)
        return self._post("/send", payload)

    def contar(self, *, mode: str = "all", list_ids: list[str] | None = None, exclude: list[str] | None = None,
               exclude_patterns: list[str] | None = None, pattern_exceptions: list[str] | None = None) -> int:
        """Cuántos contactos resolvería el servicio para (mode, list_ids, exclude). Lo usa el
        plan para saber en cuántos lotes fraccionar WhatsApp. Lanza si el servicio no responde."""
        if not self._url or not self._token:
            return 0
        resp = self._post(
            "/send",
            {"mode": mode or "all", "list_ids": list(list_ids or []), "exclude": list(exclude or []),
             "exclude_patterns": list(exclude_patterns or []), "pattern_exceptions": list(pattern_exceptions or []),
             "count_only": True},
        )
        return int(resp.get("count", 0))

    def ping(self) -> None:
        """Keep-alive: GET /health con timeout corto para mantener despierto el servicio
        (Render Free duerme a los 15 min). Best-effort: nunca lanza."""
        if not self._url:
            return
        try:
            req = urllib.request.Request(f"{self._url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception:
            pass

    def _post(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._url}{path}",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode())


class NullWhatsAppForwarder(WhatsAppForwarder):
    def forward(
        self,
        text: str,
        image_url: str | None,
        exclude: list[str],
        *,
        mode: str = "all",
        list_ids: list[str] | None = None,
        broadcast_id: str | None = None,
        broadcasts_table: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
        bc_total: int | None = None,
        delay_min_ms: int | None = None,
        delay_max_ms: int | None = None,
        exclude_patterns: list[str] | None = None,
        pattern_exceptions: list[str] | None = None,
    ) -> dict:
        return {"skipped": "disabled"}

    def contar(self, *, mode: str = "all", list_ids: list[str] | None = None, exclude: list[str] | None = None,
               exclude_patterns: list[str] | None = None, pattern_exceptions: list[str] | None = None) -> int:
        return 0
