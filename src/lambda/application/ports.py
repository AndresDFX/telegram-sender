"""Puertos: interfaces que la capa de aplicación necesita y la de adapters implementa.

La regla de dependencia apunta hacia adentro: los casos de uso dependen de estas
abstracciones, nunca de boto3/requests ni de detalles de infraestructura.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from domain.models import Post, SendResult


class PartialEnqueueError(RuntimeError):
    """El encolado falló tras haber enviado ``enqueued`` de ``total`` lotes."""

    def __init__(self, enqueued: int, total: int) -> None:
        super().__init__(f"Encolado parcial: {enqueued}/{total} lotes")
        self.enqueued = enqueued
        self.total = total


class SubscriberRepository(ABC):
    @abstractmethod
    def listar_activos(self) -> list[str]:
        """Chat IDs de suscriptores activos."""

    @abstractmethod
    def registrar(self, chat_id: str, status: str) -> None:
        """Alta/baja idempotente (upsert) con el status dado."""

    @abstractmethod
    def marcar_inactivo(self, chat_id: str) -> None:
        """Marca inactivo (p.ej. tras un 403). No falla si no existe el flujo de negocio."""

    @abstractmethod
    def listar_todos(self) -> list[dict]:
        """Todos los suscriptores con su estado: [{'chatId': ..., 'status': ...}] (para el admin)."""


class QueueStats(ABC):
    @abstractmethod
    def profundidades(self) -> dict:
        """Mensajes aproximados en la cola de broadcast y en la DLQ: {'broadcast': n, 'dlq': n}."""


class DedupStore(ABC):
    @abstractmethod
    def marcar(self, key: str) -> bool:
        """Marca atómicamente; True si es la primera vez (procesar), False si ya existía."""

    @abstractmethod
    def borrar(self, key: str) -> None:
        """Revierte la marca (compensación)."""


class HighWaterMarkStore(ABC):
    @abstractmethod
    def obtener(self, channel: str) -> int | None:
        """Último message_id difundido del canal, o None si no se ha sembrado."""

    @abstractmethod
    def guardar(self, channel: str, value: int) -> None: ...


class BroadcastQueue(ABC):
    @abstractmethod
    def encolar(
        self,
        text: str,
        chat_ids: Sequence[str],
        image_url: str | None = None,
        image_key: str | None = None,
        broadcast_id: str | None = None,
    ) -> int:
        """Encola el broadcast (en lotes). Devuelve cuántos lotes; lanza PartialEnqueueError si falla a medias.

        ``broadcast_id`` se propaga en cada lote para que el worker reporte progreso del job.
        """


class MessageSender(ABC):
    @abstractmethod
    def enviar(self, chat_id: str, text: str) -> SendResult:
        """Envía un DM de texto. Devuelve SendResult(ok/blocked); lanza ante errores no recuperables."""

    @abstractmethod
    def enviar_foto(self, chat_id: str, image_url: str, caption: str = "") -> SendResult:
        """Envía una foto (por URL) con caption opcional."""


class ChannelReader(ABC):
    @abstractmethod
    def leer_publicaciones(self, channel: str) -> list[Post]:
        """Publicaciones (con texto) del canal público, en orden de aparición."""


class WhatsAppForwarder(ABC):
    @abstractmethod
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
    ) -> dict:
        """Reenvía la lista al servicio de WhatsApp (fire-and-forget). No-op si no está configurado.

        mode: "all" | "only" (whitelist sobre list_ids) | "except" (blacklist sobre list_ids).
        list_ids: unión de ids de las listas activas (para whitelist/blacklist).
        broadcast_id/broadcasts_table: para que el servicio reporte progreso del job en DynamoDB.
        offset/limit: envío FRACCIONADO; el servicio resuelve el set y rebana [offset, offset+limit).
        bc_total: total real del job (para no sobreescribir el total con el del slice).
        delay_min_ms/delay_max_ms: rango de delay ALEATORIO entre mensajes (anti-patrón).
        """

    def contar(self, *, mode: str = "all", list_ids: list[str] | None = None, exclude: list[str] | None = None) -> int:
        """Cuántos contactos resolvería el servicio (para fraccionar). Default 0 (no-op)."""
        return 0

    def ping(self) -> None:
        """Keep-alive best-effort al servicio (mantiene Render despierto). Default no-op."""


class ImageStore(ABC):
    @abstractmethod
    def guardar(self, data: bytes, content_type: str = "image/jpeg") -> str:
        """Guarda la imagen y devuelve su key."""

    @abstractmethod
    def url_temporal(self, key: str, expira: int = 3600) -> str:
        """URL temporal (presigned) para que Telegram descargue la imagen al enviarla."""


class ConfigStore(ABC):
    @abstractmethod
    def get(self) -> dict:
        """Configuración efectiva (defaults de entorno mezclados con overrides guardados):
        source_channel, markup_percentage, currency_symbols, strip_patterns,
        whatsapp_footer, image_url."""

    @abstractmethod
    def set(self, cambios: dict) -> dict:
        """Persiste un cambio parcial y devuelve la config resultante."""
