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
    def encolar(self, text: str, chat_ids: Sequence[str]) -> int:
        """Encola el broadcast (en lotes). Devuelve cuántos lotes; lanza PartialEnqueueError si falla a medias."""


class MessageSender(ABC):
    @abstractmethod
    def enviar(self, chat_id: str, text: str) -> SendResult:
        """Envía un DM. Devuelve SendResult(ok/blocked); lanza ante errores no recuperables."""


class ChannelReader(ABC):
    @abstractmethod
    def leer_publicaciones(self, channel: str) -> list[Post]:
        """Publicaciones (con texto) del canal público, en orden de aparición."""
