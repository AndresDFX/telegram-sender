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
    # La publicación trae imagen (el texto puede ser solo su caption). El preview t.me no expone
    # las fotos; el lector userbot sí lo detecta, y la captura lo anota para que un caption mínimo
    # ("📌") no parezca una captura vacía/corrupta.
    has_photo: bool = False


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
    errores: list[str] = field(default_factory=list)  # razones legibles de fallo (auditoría)

    def resumen(self) -> dict[str, int]:
        return {"total": self.total, "sent": self.sent, "blocked": self.blocked, "failed": self.failed}
