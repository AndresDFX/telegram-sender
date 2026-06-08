"""Entidades y objetos de valor del dominio (inmutables, sin dependencias externas)."""

from __future__ import annotations

from dataclasses import dataclass, field

ACTIVE = "active"
INACTIVE = "inactive"


@dataclass(frozen=True)
class Post:
    """Una publicación del canal fuente."""

    message_id: int
    text: str


@dataclass(frozen=True)
class SendResult:
    """Resultado de intentar enviar un mensaje a un chat."""

    ok: bool
    blocked: bool = False  # el usuario bloqueó el bot (403)


@dataclass
class BroadcastStats:
    """Acumula el resultado de difundir un lote."""

    total: int = 0
    sent: int = 0
    blocked: int = 0
    failed: int = 0
    failed_ids: list[str] = field(default_factory=list)

    def resumen(self) -> dict[str, int]:
        return {"total": self.total, "sent": self.sent, "blocked": self.blocked, "failed": self.failed}
