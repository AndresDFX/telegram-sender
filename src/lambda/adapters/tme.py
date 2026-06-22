"""Adapter de lectura del canal público vía el preview web de Telegram (t.me/s/<canal>)."""

from __future__ import annotations

import html
import logging
import re
import urllib.request

from application.ports import ChannelReader
from domain.models import Post

logger = logging.getLogger(__name__)

_DATA_POST = re.compile(r'data-post="[^"/]+/(\d+)"')
_MSG_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)


class TmePreviewChannelReader(ChannelReader):
    PREVIEW_URL = "https://t.me/s/{channel}"

    def __init__(self, user_agent: str = "Mozilla/5.0 (compatible; TelegramSyncPoller/1.0)", timeout: float = 20):
        self._user_agent = user_agent
        self._timeout = timeout

    def _fetch(self, channel: str) -> str:
        req = urllib.request.Request(
            self.PREVIEW_URL.format(channel=channel), headers={"User-Agent": self._user_agent}
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    @staticmethod
    def _limpiar(fragmento: str) -> str:
        fragmento = re.sub(r"<br\s*/?>", "\n", fragmento)
        fragmento = re.sub(r"</p>", "\n", fragmento)
        fragmento = re.sub(r"<[^>]+>", "", fragmento)
        return html.unescape(fragmento).strip()

    @classmethod
    def parse(cls, html_text: str) -> list[Post]:
        posts: list[Post] = []
        for bloque in html_text.split("tgme_widget_message_wrap")[1:]:
            mid = _DATA_POST.search(bloque)
            if not mid:
                continue
            mtext = _MSG_TEXT.search(bloque)
            texto = cls._limpiar(mtext.group(1)) if mtext else ""
            if texto:
                posts.append(Post(message_id=int(mid.group(1)), text=texto))
        return posts

    def leer_publicaciones(self, channel: str) -> list[Post]:
        # M14: un 404/429/5xx del preview o un fallo de red NO debe reventar el poll;
        # se devuelve vacío y se reintenta en el próximo ciclo del cron.
        try:
            return self.parse(self._fetch(channel))
        except Exception:
            logger.exception("No se pudo leer el preview de %s; sin publicaciones este ciclo", channel)
            return []
