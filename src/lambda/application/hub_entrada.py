"""Caso de uso: lo que llega de wa-hub se CAPTURA como lista, nunca se difunde.

Réplica ya ingiere de un canal de Telegram (`PollChannel`). Esto añade una segunda fuente:
un GRUPO DE WHATSAPP, leído por el hub y entregado acá por webhook firmado. La razón es la
misma por la que existe el hub: un número de WhatsApp admite UN socket, así que un número
vinculado allá no lo puede abrir además el `whatsapp-service` de Réplica; lo RECIBE.

⚠️⚠️ SE CAPTURA. NUNCA SE DIFUNDE, y esto es lo más importante de este archivo.

`BroadcastList.__call__` difunde cuando `sending_enabled` está en `true`. Si lo que entra
de un grupo de WhatsApp cayera por ese camino, **cada mensaje de un grupo se reenviaría
automáticamente a tus contactos**: contenido de terceros, sin que nadie lo haya leído, con
tu identidad y con el riesgo de baneo que eso trae. Así que acá se pasa
`solo_capturar=True` SIEMPRE, y no hay interruptor para lo contrario. Un grupo es una
FUENTE, no una orden de enviar. Lo que entra queda «capturado» en el panel y la persona
decide.

⚠️ LOS MENSAJES PROPIOS SE SALTAN (`de_mi`). Si Réplica difunde a un grupo y además lo lee
por el hub, capturaría su propia salida — y como la difusión de una captura vuelve a pasar
por el grupo, sería un bucle que se alimenta solo. Un mensaje con `de_mi: true` no es una
fuente.

⚠️ LA ENTREGA ES AT-LEAST-ONCE. El hub marca la entrega cuando VE un 2xx; si esa respuesta
se pierde, reentrega el MISMO mensaje. Se deduplica por `id` de WhatsApp, que es estable
entre reintentos, y además el `dedup_key` que se le pasa a `BroadcastList` es determinista,
así que una reentrega que se colara sobrescribe la captura en vez de crear otra.

Este módulo es PURO: no hace red, ni DynamoDB, ni firma nada. Decide qué hacer con un
mensaje; ejecutar es del entrypoint.
"""

from __future__ import annotations

# El prefijo del `dedup_key`. Va NAMESPACED a propósito: el canal de Telegram usa
# `<canal>:<message_id>` y un id de WhatsApp podría, en teoría, coincidir con uno de
# aquéllos. Dos fuentes que comparten espacio de claves es una captura que desaparece sin
# motivo aparente.
PREFIJO_DEDUP = "hub"

# Tipos del hub que traen imagen. `documento` no entra: un PDF no es una foto de una lista,
# y el caption de un documento suele ser el nombre del archivo.
TIPOS_CON_IMAGEN = ("imagen",)


def a_captura(mensaje: dict) -> dict | None:
    """Un `MensajeHub` convertido a lo que `BroadcastList` ya consume, o `None` si se salta.

    Devuelve `{texto, dedup_key, tiene_imagen}` — los mismos tres argumentos con los que
    `PollChannel` llama a `BroadcastList`, para que la fuente nueva recorra EXACTAMENTE el
    mismo camino que la vieja (markup, quitar ubicación, footer) y no un carril paralelo
    que se desvíe con el tiempo.

    Se salta, y en cada caso por su razón:
      · sin `id` — no hay con qué deduplicar, y sin dedup una reentrega es una captura
        repetida;
      · `de_mi` — es la propia salida de Réplica: capturarla es un bucle;
      · sin texto — no hay lista que capturar. Una foto sin caption no se puede leer acá
        (el hub manda los bytes, pero Réplica no hace visión); se cuenta como saltada para
        que no desaparezca en silencio.
    """
    if not isinstance(mensaje, dict):
        return None
    ident = str(mensaje.get("id") or "").strip()
    if not ident:
        return None
    if mensaje.get("de_mi") is True:
        return None
    texto = mensaje.get("texto")
    texto = texto.strip() if isinstance(texto, str) else ""
    if not texto:
        return None
    sesion = str(mensaje.get("sesion") or "").strip() or "?"
    tiene_imagen = bool(mensaje.get("media")) or str(mensaje.get("tipo") or "") in TIPOS_CON_IMAGEN
    return {
        "texto": texto,
        # La sesión va dentro: con dos números vinculados, el mismo id de WhatsApp puede
        # llegar por los dos y son dos capturas distintas.
        "dedup_key": "%s:%s:%s" % (PREFIJO_DEDUP, sesion, ident),
        "tiene_imagen": tiene_imagen,
        "id": ident,
    }


def revisar_cuerpo(cuerpo: dict) -> tuple[bool, str]:
    """¿Es un webhook del hub bien formado? `(ok, motivo)`.

    Se comprueba la versión porque el contrato la lleva (`v: 1`): el día que el hub emita
    una `v: 2` con otra forma, esto tiene que decir «no la entiendo» en vez de leer campos
    que ya no significan lo mismo.
    """
    if not isinstance(cuerpo, dict):
        return False, "el cuerpo no es un objeto"
    try:
        version = int(cuerpo.get("v"))
    except (TypeError, ValueError):
        return False, "falta `v` o no es un número"
    if version != 1:
        return False, "versión de contrato %d: este código solo entiende la 1" % version
    if not isinstance(cuerpo.get("mensajes"), list):
        return False, "`mensajes` no es una lista"
    return True, ""


class CapturarDesdeHub:
    """Captura lo que entra del hub. Ejecuta; decidir es de las funciones de arriba.

    `dedup` y `broadcast` entran inyectados para poder ejecutar esto en seco, sin DynamoDB
    y sin Telethon.
    """

    def __init__(self, dedup, broadcast, log=None) -> None:
        self._dedup = dedup
        self._broadcast = broadcast
        self._log = log

    def __call__(self, cuerpo: dict) -> dict:
        ok, motivo = revisar_cuerpo(cuerpo)
        if not ok:
            return {"error": "cuerpo", "detalle": motivo}

        capturadas, repetidas = 0, 0
        saltados: list[str] = []
        for crudo in cuerpo.get("mensajes") or []:
            plan = a_captura(crudo)
            if plan is None:
                # Se DICE cuál y por qué no se pudo, con el id si lo había: un mensaje que
                # no entra tiene que dejar rastro en vez de desaparecer.
                saltados.append(str((crudo or {}).get("id") or "")[:64] if isinstance(crudo, dict) else "")
                continue
            try:
                nuevo = self._dedup.marcar_estricto(plan["dedup_key"])
            except Exception:
                # La misma disciplina que el webhook de Telegram (A8): si el dedup NO pudo
                # confirmar, NO se asume «duplicado» y se descarta un mensaje legítimo que
                # el hub ya daría por entregado. Se procesa; a lo sumo se reprocesa uno, y
                # el `dedup_key` determinista hace que la captura se sobrescriba.
                if self._log:
                    self._log.exception(
                        "dedup no pudo confirmar %s; lo capturo igual (no asumir duplicado)",
                        plan["dedup_key"],
                    )
                nuevo = True
            if not nuevo:
                repetidas += 1
                continue
            # ⚠️ `solo_capturar=True` SIEMPRE. Ver la cabecera del módulo: sin esto, un
            # mensaje de un grupo se reenviaría a tus contactos.
            self._broadcast(
                plan["texto"],
                dedup_key=plan["dedup_key"],
                tiene_imagen=plan["tiene_imagen"],
                solo_capturar=True,
            )
            capturadas += 1

        return {
            "ok": True,
            "capturadas": capturadas,
            "repetidas": repetidas,
            "saltados": saltados,
            "proyecto": str(cuerpo.get("proyecto") or ""),
        }
