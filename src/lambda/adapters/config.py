"""Configuración de infraestructura: lectura de entorno y validación del secret_token.

Punto único donde se resuelven secretos/entorno. Migrar a SSM/Secrets Manager
(spec 10) solo tocaría aquí, sin afectar dominio ni casos de uso.
"""

from __future__ import annotations

import hmac
import os

_TRUTHY = ("1", "true", "yes", "on")
_SECRET_HEADER = "x-telegram-bot-api-secret-token"


def bot_token() -> str | None:
    return os.environ.get("TELEGRAM_BOT_TOKEN")


def admin_user() -> str:
    return os.environ.get("ADMIN_USER", "admin")


def markup_percentage() -> float:
    return float(os.environ.get("MARKUP_PERCENTAGE", "15"))


def send_delay_seconds() -> float:
    return float(os.environ.get("SEND_DELAY_SECONDS", "0.05"))


def source_channel_username() -> str:
    return os.environ.get("SOURCE_CHANNEL_USERNAME", "iproparts")


def broadcast_queue_url() -> str | None:
    return os.environ.get("BROADCAST_QUEUE_URL")


def webhook_secret_valido(headers: dict | None) -> bool:
    """Fail-closed: sin secreto configurado se rechaza, salvo ALLOW_INSECURE_WEBHOOK
    (solo dev) y NUNCA dentro de Lambda (AWS_LAMBDA_FUNCTION_NAME)."""
    expected = os.environ.get("WEBHOOK_SECRET_TOKEN")
    if not expected:
        if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
            return False
        return os.environ.get("ALLOW_INSECURE_WEBHOOK", "").strip().lower() in _TRUTHY
    lower = {str(k).lower(): v for k, v in (headers or {}).items()}
    return hmac.compare_digest(str(lower.get(_SECRET_HEADER, "")), str(expected))
