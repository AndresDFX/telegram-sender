"""Caso de uso: sondear el canal público (de la config) y difundir solo lo nuevo.

Usa un high-water mark por message_id para no re-difundir el backlog del preview.
La primera ejecución (por canal) siembra el HWM sin difundir nada. El canal se lee
de la config en cada corrida, así que cambiarlo desde la GUI surte efecto al instante.
"""

from __future__ import annotations

import logging

from application.broadcasting import BroadcastList
from application.ports import ChannelReader, ConfigStore, HighWaterMarkStore

logger = logging.getLogger(__name__)


class PollChannel:
    def __init__(
        self,
        reader: ChannelReader,
        hwm: HighWaterMarkStore,
        broadcast: BroadcastList,
        config: ConfigStore,
    ) -> None:
        self._reader = reader
        self._hwm = hwm
        self._broadcast = broadcast
        self._config = config

    def __call__(self) -> dict[str, int]:
        channel = self._config.get()["source_channel"]
        posts = self._reader.leer_publicaciones(channel)
        if not posts:
            logger.info("Preview de %s sin publicaciones con texto", channel)
            return {"checked": 0, "new": 0}

        max_id = max(p.message_id for p in posts)
        mark = self._hwm.obtener(channel)

        if mark is None:
            self._hwm.guardar(channel, max_id)
            logger.info("Sembrado HWM de %s en %d (sin difundir backlog)", channel, max_id)
            return {"checked": len(posts), "new": 0, "seeded": max_id}

        nuevos = sorted((p for p in posts if p.message_id > mark), key=lambda p: p.message_id)
        # B15: reutiliza UNA sola conexión Telethon del preview en todos los posts de esta corrida
        # (en captura, cada post se previsualiza a Mensajes Guardados); se cierra una vez al final.
        self._broadcast._diferir_cierre_preview = True
        try:
            for post in nuevos:
                # tiene_imagen: la captura anota los posts cuyo contenido real está en la foto
                # (caption mínimo tipo "📌"), para que no parezcan capturas vacías en el panel.
                self._broadcast(post.text, tiene_imagen=bool(getattr(post, "has_photo", False)))
                self._hwm.guardar(channel, post.message_id)
        finally:
            self._broadcast._diferir_cierre_preview = False
            try:
                self._broadcast.cerrar_preview()
            except Exception:
                logger.exception("No se pudo cerrar el cliente de preview tras la corrida")

        logger.info("Canal %s: %d revisados, %d nuevos difundidos", channel, len(posts), len(nuevos))
        return {"checked": len(posts), "new": len(nuevos)}
