"""Adapter que envía por WhatsApp a través de **wa-hub** en vez del servicio propio.

    https://github.com/AndresDFX/wa-hub  ·  POST /v1/enviar  ·  HMAC del proyecto

PARA QUÉ. Un número de WhatsApp admite UN socket. Hoy hay tres implementaciones de
Baileys —Doble, Réplica y el puente de la plataforma HV— y el hub existe para dejar una:
mantiene los números vinculados, se vincula desde el navegador y reparte lo que llega.
Este adaptador es la mitad de salida de Réplica hacia ese hub.

⚠️⚠️ LO QUE ESTE ADAPTADOR **NO** PUEDE HACER, Y POR QUÉ NO SE DISIMULA

El servicio propio de Réplica (`whatsapp-service/`) resuelve los destinatarios de la
AGENDA DE WHATSAPP: se suscribe a `contacts.upsert` y `messaging-history.set` de su propio
socket y cachea `jid -> nombre` en DynamoDB. El hub **no expone contactos** —a propósito:
la agenda de alguien no tiene por qué salir del proceso—, así que hay tres cosas que por
acá no se pueden hacer, y en las tres se RECHAZA la difusión con su motivo en vez de
mandarla a menos gente:

  1. `mode="all"` y `mode="except"`. Son «todos mis contactos» y «todos menos éstos»: sin
     la agenda no hay «todos». `mode="only"` sí funciona, porque ahí `list_ids` ya trae
     los ids explícitos de las listas (ver `domain/recipients.py`) y Réplica los conoce
     sin preguntarle a nadie.
  2. `exclude_patterns`. Excluye por PATRÓN DEL NOMBRE del contacto, y sin nombres no se
     puede aplicar. Ignorarlo en silencio sería mandarle el mensaje justo a la gente que
     se pidió excluir: es el peor fallo posible de una herramienta de difusión.
  3. `image_url`. `POST /v1/enviar` del hub manda `{sesion, a, texto}` y nada más. Una
     difusión que pierde la imagen sin decirlo es peor que una que no sale.

⚠️ EL TIEMPO ES EL OTRO LÍMITE, y es el que sorprende. El `forward()` del servicio propio
es fire-and-forget: contesta 202 y envía de fondo. El hub NO: cada `POST /v1/enviar`
aplica su cadencia antiban (2–8 s de espera y después «escribiendo…»), así que la
respuesta llega entre 4 y 15 s DESPUÉS. Cien destinatarios son ~8 minutos de llamadas
sincrónicas, o sea el timeout de la Lambda. Por eso hay `presupuesto_s`: se envía hasta
agotarlo y se DEVUELVE cuántos quedaron pendientes. Réplica ya trocea con `offset`/`limit`
por esta misma razón, así que la vuelta siguiente sigue donde ésta paró.

⚠️ Y NO SE REINTENTA NUNCA. El hub no tiene clave de idempotencia en su petición de envío
(lo dice su propio `api/rutas/enviar.ts`): cortar la conexión no cancela el envío, así que
un reintento le escribe DOS veces a una persona de verdad. Un fallo se reporta y se deja
para que lo decida quien mira, que es lo contrario de lo que uno querría de un webhook
pero exactamente lo correcto cuando al otro lado hay gente.

SIN DEPENDENCIAS: solo stdlib, como el resto de las Lambdas de este proyecto.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from application.ports import WhatsAppForwarder

# ⚠️ La firma y las cabeceras viven en `domain/hub_firma.py`, no acá. Es puro y lo usan los
# DOS lados de Réplica: éste para FIRMAR lo que sale hacia `POST /v1/enviar`, y
# `application/hub_entrada.py` para VERIFICAR lo que entra por el webhook. Tenerlo dos veces
# sería tener dos cadenas canónicas que se pueden separar sin que nada falle, hasta que
# aparece un 401 que no se puede depurar desde el otro lado.
#
# Se re-exportan los nombres para no romper a quien ya los importaba de acá — entre otros la
# prueba del vector de interoperabilidad.
from domain.hub_firma import (  # noqa: F401  (re-export deliberado)
    CAB_KID,
    CAB_SIG,
    CAB_TS,
    MIN_SECRETO,
    cadena_canonica,
    firmar,
    partir_token,
)

logger = logging.getLogger(__name__)

RUTA_ENVIAR = "/v1/enviar"  # la ruta va DENTRO de la firma: cambiarla cambia el HMAC

# Lo que tarda el hub por mensaje en el peor caso (2–8 s de espera + hasta 5 s de
# «escribiendo…» + la red). Se usa para no empezar un envío que no va a caber en el
# presupuesto: mejor dejarlo pendiente y decirlo que cortarlo a la mitad.
COSTE_MSJ_S = 16.0

# Presupuesto por defecto. Una Lambda de este proyecto tiene 15 min; 240 s deja sitio de
# sobra para lo que va después y para que el troceado siguiente arranque.
PRESUPUESTO_DEF_S = 240.0


class HubWhatsAppForwarder(WhatsAppForwarder):
    """Envía por el hub. Se construye con lo que ya está en la configuración de Réplica.

    `sesion` es el id de la sesión del hub (`postula`, `doble`, `replica`): texto elegido a
    mano, no un entero, porque acaba en los logs y ahí un número no dice nada.
    """

    def __init__(
        self,
        base: str,
        token: str,
        sesion: str,
        *,
        timeout: float = 30.0,
        presupuesto_s: float = PRESUPUESTO_DEF_S,
        reloj=time.monotonic,
    ):
        self._base = (base or "").rstrip("/")
        self._kid, self._secreto = partir_token(token)
        self._sesion = (sesion or "").strip()
        # 30 s y no 15: el hub tarda entre 4 y 15 s por su cadencia antiban, así que un
        # timeout corto cortaría la conexión de un envío que SÍ va a salir — y como no hay
        # idempotencia, reintentarlo escribiría dos veces.
        self._timeout = timeout
        self._presupuesto_s = presupuesto_s
        # `monotonic` y no `time()`: un ajuste de reloj a mitad de una difusión no puede
        # convertir el presupuesto en negativo. Inyectable para poder probarlo en seco.
        self._reloj = reloj

    # ------------------------------------------------------------------ #
    def configurado(self) -> tuple[bool, str]:
        """¿Está listo? Devuelve el motivo, no un booleano a secas: «no configurado» y «el
        secreto es demasiado corto» se arreglan de formas distintas."""
        if not self._base:
            return False, "falta la URL del hub"
        if not self._kid or not self._secreto:
            return False, "el token del hub no tiene la forma kid:secreto"
        if len(self._secreto) < MIN_SECRETO:
            return False, (
                "el secreto del hub tiene %d caracteres y el mínimo son %d "
                "(¿se pegó cortado?)" % (len(self._secreto), MIN_SECRETO)
            )
        if not self._sesion:
            return False, "falta el id de la sesión del hub"
        return True, ""

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
        listo, motivo = self.configurado()
        if not listo:
            return {"skipped": "hub no configurado: %s" % motivo}

        # --- lo que por acá NO se puede hacer, y se dice ------------------ #
        # Cada uno de estos tres `return` es una difusión que NO sale. Es a propósito: la
        # alternativa es que salga a menos gente, o sin la imagen, y eso no se ve hasta que
        # alguien pregunta por qué no le llegó.
        if (mode or "all") != "only":
            return {
                "error": "hub_sin_agenda",
                "detalle": (
                    "El hub no expone la agenda de WhatsApp, así que «%s» (todos / todos "
                    "menos éstos) no se puede resolver por acá: no hay «todos». Usa "
                    "mode=only con las listas, o deja el servicio propio para esta "
                    "difusión." % (mode or "all")
                ),
            }
        if exclude_patterns:
            return {
                "error": "hub_sin_nombres",
                "detalle": (
                    "`exclude_patterns` excluye por el NOMBRE del contacto y el hub no da "
                    "nombres. No se aplica en silencio a propósito: sería mandarle el "
                    "mensaje justo a quien se pidió excluir (%d patrón/es)."
                    % len(exclude_patterns)
                ),
            }
        if image_url:
            return {
                "error": "hub_sin_imagen",
                "detalle": (
                    "El hub manda texto (POST /v1/enviar es {sesion, a, texto}). Una "
                    "difusión que pierde la imagen sin decirlo es peor que una que no sale."
                ),
            }
        if not (text or "").strip():
            return {"error": "texto_vacio", "detalle": "WhatsApp no manda mensajes en blanco."}

        # --- los destinatarios ------------------------------------------- #
        # `list_ids` ya es la unión de ids de las listas activas (ver domain/recipients.py),
        # o sea que acá NO se resuelve nada: se filtra y se trocea.
        fuera = {str(x).strip() for x in (exclude or []) if str(x).strip()}
        salvados = {str(x).strip() for x in (pattern_exceptions or []) if str(x).strip()}
        destinos: list[str] = []
        vistos: set[str] = set()
        for crudo in list_ids or []:
            d = str(crudo).strip()
            # Sin duplicados: la misma persona en dos listas es UN mensaje, no dos.
            if not d or d in vistos:
                continue
            if d in fuera and d not in salvados:
                continue
            vistos.add(d)
            destinos.append(d)
        total = len(destinos)
        if offset is not None or limit is not None:
            ini = int(offset or 0)
            fin = ini + int(limit) if limit is not None else None
            destinos = destinos[ini:fin]
        if not destinos:
            return {"enviados": 0, "pendientes": 0, "total": total, "nota": "no quedó nadie"}

        # `delay_min_ms` de Réplica es su cadencia antiban; el hub ya aplica la suya (2–8 s
        # + «escribiendo…»). Solo se espera la DIFERENCIA, si la pedida es mayor: sumar las
        # dos convertiría una difusión de 20 en media hora sin que nadie lo pidiera.
        extra_s = max(0.0, (int(delay_min_ms or 0) / 1000.0) - 8.0)

        arranque = self._reloj()
        enviados = 0
        fallos: list[dict] = []
        parado = ""
        for i, a in enumerate(destinos):
            # ⚠️ Se comprueba ANTES de empezar cada envío, con el coste del peor caso: un
            # envío empezado no se puede cancelar (y no se puede reintentar, porque el hub
            # no tiene idempotencia), así que lo que no cabe se queda pendiente ENTERO.
            if self._reloj() - arranque + COSTE_MSJ_S > self._presupuesto_s:
                parado = "presupuesto de tiempo agotado"
                break
            r = self._enviar_uno(a, text)
            if r.get("ok"):
                enviados += 1
            else:
                fallos.append({"a": a, "error": r.get("error", "")})
                # Un 403 o un 404 no se arreglan en el destinatario siguiente: son del
                # proyecto o de la sesión. Se para para no gastar el presupuesto entero
                # coleccionando el mismo error N veces.
                if r.get("permanente"):
                    parado = "el hub rechazó la petición: %s" % r.get("error", "")
                    break
            if extra_s and i + 1 < len(destinos):
                time.sleep(extra_s)

        pendientes = len(destinos) - enviados - len(fallos)
        fuera_dict = {
            "enviados": enviados,
            "pendientes": pendientes,
            "total": total,
            "en_este_lote": len(destinos),
            "fallos": fallos[:20],
            "sesion": self._sesion,
        }
        if parado:
            # ⚠️ SE DICE. Un lote que se corta en silencio se lee como «ya se mandó a
            # todos», y la diferencia solo se descubre cuando alguien pregunta.
            fuera_dict["parado"] = parado
            logger.warning(
                "difusión por el hub parada: %s (enviados %d de %d)", parado, enviados, len(destinos)
            )
        if fallos:
            logger.warning("difusión por el hub con %d fallo(s)", len(fallos))
        return fuera_dict

    def contar(
        self,
        *,
        mode: str = "all",
        list_ids: list[str] | None = None,
        exclude: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        pattern_exceptions: list[str] | None = None,
    ) -> int:
        """Cuántos resolvería. LANZA en los modos que el hub no puede resolver, igual que
        `forward` los rechaza: si devolviera 0, el plan trocearía en cero lotes y la
        difusión se daría por hecha sin haber salido."""
        if (mode or "all") != "only":
            raise ValueError(
                "el hub no expone la agenda: «%s» no se puede contar por acá" % (mode or "all")
            )
        if exclude_patterns:
            raise ValueError("el hub no da nombres: `exclude_patterns` no se puede aplicar")
        fuera = {str(x).strip() for x in (exclude or []) if str(x).strip()}
        salvados = {str(x).strip() for x in (pattern_exceptions or []) if str(x).strip()}
        vistos: set[str] = set()
        for crudo in list_ids or []:
            d = str(crudo).strip()
            if not d or d in vistos:
                continue
            if d in fuera and d not in salvados:
                continue
            vistos.add(d)
        return len(vistos)

    def ping(self) -> None:
        """`POST /v1/ping` del hub: comprueba la firma, el reloj y el token SIN mandar un
        mensaje a nadie. Best-effort, nunca lanza — igual que el del servicio propio.

        No es un keep-alive cualquiera: el hub en Render Free también duerme, y despertarlo
        con un `ping` firmado es preferible a despertarlo con la primera difusión (que
        pagaría el arranque en frío dentro de su presupuesto de tiempo)."""
        listo, _ = self.configurado()
        if not listo:
            return
        try:
            self._pedir("/v1/ping", {})
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    def _enviar_uno(self, a: str, texto: str) -> dict:
        try:
            self._pedir(RUTA_ENVIAR, {"sesion": self._sesion, "a": a, "texto": texto})
            return {"ok": True}
        except urllib.error.HTTPError as e:
            cuerpo = ""
            try:
                cuerpo = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            # La MISMA tabla de permanentes que usa el hub para dejar de reintentar. 409 no
            # está: es «la sesión no está conectada», que se arregla sola al reconectar.
            permanente = e.code in (400, 401, 403, 404, 410, 422)
            return {"ok": False, "error": "HTTP %d %s" % (e.code, cuerpo), "permanente": permanente}
        except Exception as e:
            # ⚠️ Un timeout NO es un «no se envió»: el hub puede haberlo mandado y perdido
            # la respuesta. Se cuenta como fallo para que se mire, y NO se reintenta.
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "permanente": False}

    def _pedir(self, ruta: str, cuerpo: dict) -> dict:
        # ⚠️ Se firma sobre ESTOS bytes, los mismos que viajan. Serializar dos veces (una
        # para firmar y otra para enviar) puede dar cadenas distintas y el 401 que sale de
        # ahí no se depura desde el otro lado.
        datos = json.dumps(cuerpo, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ts = str(int(time.time()))
        req = urllib.request.Request(
            "%s%s" % (self._base, ruta),
            data=datos,
            method="POST",
            headers={
                "Content-Type": "application/json",
                CAB_KID: self._kid,
                CAB_TS: ts,
                CAB_SIG: firmar(self._secreto, "POST", ruta, ts, datos),
            },
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            crudo = resp.read().decode("utf-8", "replace")
        return json.loads(crudo) if crudo.strip() else {}
