"""Envío de correo transaccional vía Resend (https://resend.com).

Capa gratuita: 100 correos/día, 3.000/mes. API HTTP simple (POST con Bearer),
sin SDK ni dependencias extra — se usa urllib de la stdlib, igual que el proxy
de WhatsApp. El llamador decide el fallback (p. ej. SNS) si esto falla.
"""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"


def enviar_resend(
    api_key: str,
    mail_from: str,
    to: str,
    subject: str,
    text: str,
    html: str = "",
    timeout: float = 12.0,
) -> bool:
    """Envía un correo por Resend. Devuelve True si la API lo aceptó (HTTP 2xx).

    Lanza ValueError si falta api_key o destinatario; deja propagar errores de
    red/HTTP para que el llamador aplique su fallback.
    """
    if not api_key or not to:
        raise ValueError("falta api_key o destinatario")
    payload: dict = {"from": mail_from, "to": [to], "subject": subject, "text": text}
    if html:
        payload["html"] = html
    req = urllib.request.Request(
        _RESEND_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ok = 200 <= int(resp.status) < 300
        if ok:
            logger.info("Resend aceptó el correo para %s", to)
        return ok
