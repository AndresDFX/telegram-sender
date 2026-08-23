"""La ENTRADA desde wa-hub: un grupo de WhatsApp como segunda fuente de Réplica.

El orden de las clases no es casual. Las dos primeras son las que impiden un desastre:

  1. LO QUE ENTRA NO SE DIFUNDE. `BroadcastList` difunde cuando `sending_enabled` está en
     `true`. Si lo que llega de un grupo cayera por ahí, cada mensaje del grupo se
     reenviaría automáticamente a los contactos del dueño: contenido de terceros, con su
     identidad, sin que nadie lo haya leído. Se comprueba con `sending_enabled=True`
     puesto, que es el caso peligroso.
  2. LOS MENSAJES PROPIOS SE SALTAN. Si Réplica difunde a un grupo y además lo lee por el
     hub, capturaría su propia salida — y difundir la captura vuelve a pasar por el grupo.
     Es un bucle que se alimenta solo.

Después: la firma (con su vector), la reentrega y la puerta del entrypoint.
"""

import json
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from application.hub_entrada import (  # noqa: E402
    CapturarDesdeHub,
    a_captura,
    revisar_cuerpo,
)
from domain.hub_firma import CAB_KID, CAB_SIG, CAB_TS, firmar, verificar  # noqa: E402

TOKEN = "kf8905e:" + ("s" * 64)
SECRETO = "s" * 64
RUTA = "/hub/entrada"


def msj(**kw):
    base = {
        "id": "3EB0C767D0F1A2B3C4",
        "sesion": "replica",
        "cuenta": "573243198985",
        "chat": "120363044332649288@g.us",
        "de_mi": False,
        "autor": "Ana Ruiz",
        "ts": "2026-08-23T14:03:12.000Z",
        "tipo": "texto",
        "texto": "iPhone 13 128GB $2.350.000",
        "media": None,
        "media_omitida": 0,
    }
    base.update(kw)
    return base


def cuerpo(*mensajes, v=1, proyecto="replica"):
    return {"v": v, "evento": "mensaje", "proyecto": proyecto, "mensajes": list(mensajes)}


class DedupFalso:
    def __init__(self, revienta=False):
        self.vistas = set()
        self.revienta = revienta

    def marcar_estricto(self, key):
        if self.revienta:
            raise RuntimeError("DynamoDB estranguló la escritura")
        if key in self.vistas:
            return False
        self.vistas.add(key)
        return True


class BroadcastFalso:
    """Registra CÓMO se le llamó. `solo_capturar` es lo que de verdad se vigila."""

    def __init__(self):
        self.llamadas = []

    def __call__(self, text, dedup_key=None, tiene_imagen=False, solo_capturar=False):
        self.llamadas.append(
            {"text": text, "dedup_key": dedup_key, "tiene_imagen": tiene_imagen,
             "solo_capturar": solo_capturar}
        )
        return {"captured": True, "broadcast_id": "b-x"}


# =========================================================================== #
class TestNoSeDifundeNunca(unittest.TestCase):
    """⚠️ LA COMPROBACIÓN QUE IMPIDE EL DESASTRE."""

    def test_siempre_solo_capturar(self):
        b = BroadcastFalso()
        r = CapturarDesdeHub(DedupFalso(), b)(cuerpo(msj()))
        self.assertEqual(r["capturadas"], 1)
        self.assertTrue(b.llamadas[0]["solo_capturar"],
                        "sin solo_capturar, un mensaje de un grupo se reenvía a los contactos")

    def test_y_BroadcastList_de_verdad_captura_con_sending_enabled_puesto(self):
        """El caso peligroso, contra el `BroadcastList` REAL y no un doble.

        Un doble que registra `solo_capturar` demuestra que se le pasa el argumento, no que
        el argumento haga algo. Acá se ejecuta la clase de verdad con `sending_enabled=True`
        y con lista elegida en los dos canales —el ajuste con el que la difusión SÍ sale— y
        se comprueba que la cola se queda VACÍA.

        Los dobles se reutilizan de `test_application_broadcasting`: son los que ya
        representan la configuración real de esta clase, y una copia local se desincronizaría
        el día que la clase pida una clave nueva.
        """
        from application.broadcasting import BroadcastList
        from test_application_broadcasting import FakeConfig, FakeQueue, FakeSubs

        registradas = []

        class Store:
            def crear(self, broadcast_id, text, source, channels, tg_total=0,
                      price_diff=None, original_text=None):
                registradas.append((broadcast_id, source))

            def registrar_error(self, bid, msg):
                pass

        cola = FakeQueue()
        cfg = FakeConfig(
            # ENCENDIDO, y con lista elegida en los dos canales: si `solo_capturar` no
            # cortara, esto difundiría de verdad.
            sending_enabled=True,
            auto_telegram_list="todos",
            auto_whatsapp_list="todos",
            telegram_lists=[{"name": "todos", "ids": ["1"]}],
            whatsapp_lists=[{"name": "todos", "ids": ["57300111"]}],
        )
        bl = BroadcastList(FakeSubs(["1"]), cola, cfg, broadcasts=Store())

        r = bl("iPhone 13 $2.350.000", dedup_key="hub:replica:ABC", solo_capturar=True)
        self.assertTrue(r.get("captured"))
        self.assertEqual([s for _, s in registradas], ["capture"])
        # ⚠️ LO QUE DE VERDAD IMPORTA: nada se encoló, o sea que nada salió hacia nadie.
        # `calls` es donde `FakeQueue` apunta cada `encolar`. La primera versión de esta línea
        # miraba `batches`/`enqueued`, que NO existen, así que comparaba [] con [] y pasaba
        # siempre — vigilaba nada.
        self.assertEqual(cola.calls, [], "¡se encoló algo: lo del grupo salió hacia alguien!")

        # ⚠️ Y el id es DETERMINISTA: una reentrega sobrescribe la captura en vez de crear
        # otra. La rama de captura usaba `_nuevo_id()`, que dejaba una repetida por reintento.
        bl("iPhone 13 $2.350.000", dedup_key="hub:replica:ABC", solo_capturar=True)
        self.assertEqual(registradas[0][0], registradas[1][0])

        # Y la contraprueba: SIN `solo_capturar`, con esta misma configuración, sí difunde.
        # Si no, la comprobación de arriba pasaría por cualquier motivo.
        bl("iPhone 13 $2.350.000", dedup_key="otro")
        self.assertNotEqual([s for _, s in registradas][-1], "capture")


class TestElBucleSeCorta(unittest.TestCase):
    def test_un_mensaje_propio_no_se_captura(self):
        # ⚠️ Réplica difunde al grupo Y lo lee: sin esto capturaría su propia salida, y
        # difundir la captura vuelve a pasar por el grupo. Bucle.
        self.assertIsNone(a_captura(msj(de_mi=True)))
        b = BroadcastFalso()
        r = CapturarDesdeHub(DedupFalso(), b)(cuerpo(msj(de_mi=True)))
        self.assertEqual(r["capturadas"], 0)
        self.assertEqual(b.llamadas, [])
        # Y se DICE cuál se saltó, con su id: un mensaje que no entra deja rastro.
        self.assertEqual(r["saltados"], ["3EB0C767D0F1A2B3C4"])


class TestMapeo(unittest.TestCase):
    def test_los_tres_argumentos_que_BroadcastList_ya_consume(self):
        p = a_captura(msj())
        self.assertEqual(p["texto"], "iPhone 13 128GB $2.350.000")
        self.assertEqual(p["tiene_imagen"], False)

    def test_el_dedup_key_lleva_prefijo_y_sesion(self):
        # Namespaced: el canal de Telegram usa `<canal>:<message_id>` y compartir espacio de
        # claves entre dos fuentes es una captura que desaparece sin motivo aparente.
        p = a_captura(msj())
        self.assertEqual(p["dedup_key"], "hub:replica:3EB0C767D0F1A2B3C4")

    def test_dos_sesiones_con_el_mismo_id_son_dos_capturas(self):
        a = a_captura(msj(sesion="replica"))["dedup_key"]
        b = a_captura(msj(sesion="otra"))["dedup_key"]
        self.assertNotEqual(a, b)

    def test_una_imagen_se_anota(self):
        self.assertTrue(a_captura(msj(tipo="imagen"))["tiene_imagen"])
        self.assertTrue(a_captura(msj(media={"mime": "image/jpeg", "b64": "AAA"}))["tiene_imagen"])

    def test_sin_id_no_se_procesa(self):
        # Sin id no hay con qué deduplicar, y sin dedup una reentrega es una captura repetida.
        self.assertIsNone(a_captura(msj(id="")))

    def test_sin_texto_no_hay_lista_que_capturar(self):
        self.assertIsNone(a_captura(msj(texto="")))
        self.assertIsNone(a_captura(msj(texto="   ")))
        self.assertIsNone(a_captura(msj(texto=None)))

    def test_un_mensaje_que_no_es_objeto_no_revienta(self):
        self.assertIsNone(a_captura("hola"))
        self.assertIsNone(a_captura(None))

    def test_la_version_del_contrato_se_comprueba(self):
        self.assertTrue(revisar_cuerpo(cuerpo(msj()))[0])
        ok, motivo = revisar_cuerpo(cuerpo(msj(), v=2))
        self.assertFalse(ok)
        self.assertIn("2", motivo)
        self.assertFalse(revisar_cuerpo({"v": 1})[0])          # sin `mensajes`
        self.assertFalse(revisar_cuerpo({"mensajes": []})[0])   # sin `v`


class TestReentrega(unittest.TestCase):
    """La cola del hub es at-least-once: reentrega el mismo mensaje si no ve el 2xx."""

    def test_la_segunda_vez_es_repetida_y_no_se_vuelve_a_capturar(self):
        d, b = DedupFalso(), BroadcastFalso()
        c = CapturarDesdeHub(d, b)
        self.assertEqual(c(cuerpo(msj()))["capturadas"], 1)
        r = c(cuerpo(msj()))
        self.assertEqual((r["capturadas"], r["repetidas"]), (0, 1))
        self.assertEqual(len(b.llamadas), 1)

    def test_si_el_dedup_no_puede_confirmar_se_captura_IGUAL(self):
        # ⚠️ La misma disciplina que el webhook de Telegram (A8): NO asumir «duplicado» y
        # descartar en silencio un mensaje legítimo que el hub ya daría por entregado. A lo
        # sumo se reprocesa uno, y el `dedup_key` determinista lo sobrescribe.
        b = BroadcastFalso()
        r = CapturarDesdeHub(DedupFalso(revienta=True), b)(cuerpo(msj()))
        self.assertEqual(r["capturadas"], 1)

    def test_varios_mensajes_en_un_lote_se_cuentan_por_separado(self):
        b = BroadcastFalso()
        r = CapturarDesdeHub(DedupFalso(), b)(
            cuerpo(msj(id="A"), msj(id="B"), msj(id="C", de_mi=True), msj(id=""))
        )
        self.assertEqual((r["capturadas"], len(r["saltados"])), (2, 2))


class TestFirma(unittest.TestCase):
    def _cab(self, ts=None, cuerpo_bytes=b"{}", secreto=SECRETO, kid="kf8905e"):
        ts = str(int(time.time())) if ts is None else str(ts)
        return {
            CAB_KID: kid,
            CAB_TS: ts,
            CAB_SIG: firmar(secreto, "POST", RUTA, ts, cuerpo_bytes),
        }

    def test_una_firma_buena_pasa(self):
        c = json.dumps(cuerpo(msj())).encode()
        ok, motivo = verificar(self._cab(cuerpo_bytes=c), c, TOKEN, RUTA)
        self.assertTrue(ok, motivo)

    def test_las_cabeceras_valen_en_mayusculas(self):
        # API Gateway las da en minúsculas, pero un curl o un proxy pueden capitalizarlas:
        # buscar solo la forma exacta daría «falta la cabecera» con la cabecera puesta.
        c = b"{}"
        cab = {k.title(): v for k, v in self._cab(cuerpo_bytes=c).items()}
        self.assertTrue(verificar(cab, c, TOKEN, RUTA)[0])

    def test_sin_token_es_apagado_y_lo_dice(self):
        # Fail-closed, pero distinguible: «apagado» es una variable sin poner, no una
        # credencial mala. El entrypoint lo traduce a 503 y no a 403.
        ok, motivo = verificar(self._cab(), b"{}", "", RUTA)
        self.assertFalse(ok)
        self.assertEqual(motivo, "apagado")

    def test_un_secreto_corto_se_rechaza_diciendo_cuanto_mide(self):
        ok, motivo = verificar(self._cab(), b"{}", "kid:corto", RUTA)
        self.assertFalse(ok)
        self.assertIn("5", motivo)

    def test_kid_reloj_y_firma_se_distinguen(self):
        c = b"{}"
        self.assertEqual(verificar(self._cab(cuerpo_bytes=c, kid="otro"), c, TOKEN, RUTA)[1], "kid")
        viejo = self._cab(ts=1, cuerpo_bytes=c)
        self.assertIn("reloj", verificar(viejo, c, TOKEN, RUTA)[1])
        mala = self._cab(cuerpo_bytes=c)
        mala[CAB_SIG] = "0" * 64
        self.assertEqual(verificar(mala, c, TOKEN, RUTA)[1], "firma")

    def test_un_cuerpo_cambiado_invalida_la_firma(self):
        c = json.dumps(cuerpo(msj())).encode()
        cab = self._cab(cuerpo_bytes=c)
        # Un byte distinto: es lo único que protege de que alguien reescriba el mensaje.
        self.assertFalse(verificar(cab, c + b" ", TOKEN, RUTA)[0])

    def test_OTRA_RUTA_invalida_la_firma(self):
        # La ruta va dentro: sin eso, una petición capturada contra `/hub/entrada` se
        # reenvía contra otro endpoint del mismo API.
        c = b"{}"
        self.assertFalse(verificar(self._cab(cuerpo_bytes=c), c, TOKEN, "/webhook/telegram")[0])

    def test_el_ts_que_se_firma_es_EL_DE_LA_CABECERA(self):
        # Si se firmara con `int(time.time())` recalculado, la firma cambiaría entre firmar
        # y verificar y saldría un 403 intermitente imposible de reproducir.
        c = b"{}"
        ts = str(int(time.time()) - 120)   # dentro de la ventana de 300 s
        cab = {CAB_KID: "kf8905e", CAB_TS: ts, CAB_SIG: firmar(SECRETO, "POST", RUTA, ts, c)}
        self.assertTrue(verificar(cab, c, TOKEN, RUTA)[0])


class TestLaPuertaDelEntrypoint(unittest.TestCase):
    """La ruta del hub se atiende ANTES del `secret_token` de Telegram."""

    def _evento(self, ruta=RUTA, cuerpo_bytes=b"{}", cabeceras=None):
        return {
            "rawPath": ruta,
            "requestContext": {"http": {"method": "POST", "path": ruta}},
            "headers": cabeceras or {},
            "body": cuerpo_bytes.decode(),
            "isBase64Encoded": False,
        }

    def test_no_pasa_por_el_secret_token_de_telegram(self):
        from entrypoints import receiver

        c = json.dumps(cuerpo(msj())).encode()
        ts = str(int(time.time()))
        cab = {CAB_KID: "kf8905e", CAB_TS: ts, CAB_SIG: firmar(SECRETO, "POST", RUTA, ts, c)}
        d, b = DedupFalso(), BroadcastFalso()
        receiver.dedup, receiver.broadcast, receiver.handle_command = d, b, lambda *a: None

        class Store:
            def get(self):
                return {"hub_token": TOKEN}

        with patch.object(receiver.wiring, "build_config_store", return_value=Store()), \
             patch.object(receiver.config, "webhook_secret_valido", return_value=False):
            # `webhook_secret_valido` en False a propósito: si la ruta del hub pasara por esa
            # puerta, el hub se llevaría un 403 y no habría forma de saber desde allá que el
            # problema era la cabecera equivocada.
            r = receiver.lambda_handler(self._evento(cuerpo_bytes=c, cabeceras=cab), None)
        self.assertEqual(r["statusCode"], 200, r["body"])
        self.assertEqual(json.loads(r["body"])["capturadas"], 1)
        self.assertTrue(b.llamadas[0]["solo_capturar"])

    def test_con_la_ETAPA_delante_la_firma_sigue_cuadrando(self):
        """⚠️ El API de este proyecto tiene etapa `dev`, no `$default`.

        O sea que la URL pública es `.../dev/hub/entrada` y eso es lo que el hub mete en la
        cadena canónica: firma el `pathname` de la URL que se le configuró. Si acá se
        verificara contra la constante `/hub/entrada`, el resultado seria un 403 SIEMPRE y el
        único síntoma al otro lado un `fallido` sin motivo.
        """
        from entrypoints import receiver

        c = json.dumps(cuerpo(msj())).encode()
        ts = str(int(time.time()))
        con_etapa = "/dev" + RUTA
        # El hub firma la ruta CON la etapa, porque es la que está en la URL del proyecto.
        cab = {CAB_KID: "kf8905e", CAB_TS: ts, CAB_SIG: firmar(SECRETO, "POST", con_etapa, ts, c)}
        d, b = DedupFalso(), BroadcastFalso()
        receiver.dedup, receiver.broadcast, receiver.handle_command = d, b, lambda *a: None

        class Store:
            def get(self):
                return {"hub_token": TOKEN}

        with patch.object(receiver.wiring, "build_config_store", return_value=Store()):
            r = receiver.lambda_handler(
                self._evento(ruta=con_etapa, cuerpo_bytes=c, cabeceras=cab), None
            )
        self.assertEqual(r["statusCode"], 200, r["body"])
        self.assertEqual(json.loads(r["body"])["capturadas"], 1)

    def test_y_una_firma_de_OTRA_ruta_no_vale(self):
        # La contraprueba de la de arriba: aceptar cualquier ruta seria aceptar una firma
        # capturada contra otro endpoint del mismo API (`/admin`, por ejemplo).
        from entrypoints import receiver

        c = b"{}"
        ts = str(int(time.time()))
        cab = {CAB_KID: "kf8905e", CAB_TS: ts,
               CAB_SIG: firmar(SECRETO, "POST", "/dev/admin", ts, c)}
        receiver.dedup, receiver.broadcast, receiver.handle_command = DedupFalso(), BroadcastFalso(), (lambda *a: None)

        class Store:
            def get(self):
                return {"hub_token": TOKEN}

        with patch.object(receiver.wiring, "build_config_store", return_value=Store()):
            r = receiver.lambda_handler(
                self._evento(ruta="/dev" + RUTA, cuerpo_bytes=c, cabeceras=cab), None
            )
        self.assertEqual(r["statusCode"], 403)

    def test_sin_token_configurado_contesta_503_no_403(self):
        from entrypoints import receiver

        receiver.dedup, receiver.broadcast, receiver.handle_command = DedupFalso(), BroadcastFalso(), (lambda *a: None)

        class Store:
            def get(self):
                return {"hub_token": ""}

        with patch.object(receiver.wiring, "build_config_store", return_value=Store()):
            r = receiver.lambda_handler(self._evento(), None)
        # 503 y no 403: es una variable sin poner, y encima reintentable — el hub no pierde
        # el mensaje mientras se configura.
        self.assertEqual(r["statusCode"], 503)
        self.assertEqual(json.loads(r["body"])["status"], "apagado")

    def test_una_firma_mala_da_403_SIN_decir_por_que(self):
        from entrypoints import receiver

        receiver.dedup, receiver.broadcast, receiver.handle_command = DedupFalso(), BroadcastFalso(), (lambda *a: None)

        class Store:
            def get(self):
                return {"hub_token": TOKEN}

        cab = {CAB_KID: "kf8905e", CAB_TS: str(int(time.time())), CAB_SIG: "0" * 64}
        with patch.object(receiver.wiring, "build_config_store", return_value=Store()):
            r = receiver.lambda_handler(self._evento(cabeceras=cab), None)
        self.assertEqual(r["statusCode"], 403)
        # ⚠️ El motivo va al log, NO al cuerpo: decir si falló el kid o la firma le confirma
        # a quien lo intenta qué mitad del token acertó.
        self.assertNotIn("firma", r["body"])
        self.assertNotIn("kid", r["body"])

    def test_el_cuerpo_en_base64_se_descodifica_ANTES_de_firmar(self):
        # API Gateway puede entregar el cuerpo en base64. Si se hasheara el base64, la firma
        # no cuadraría nunca y el 403 no diría por qué.
        import base64 as b64

        from entrypoints import receiver

        c = json.dumps(cuerpo(msj())).encode()
        ts = str(int(time.time()))
        cab = {CAB_KID: "kf8905e", CAB_TS: ts, CAB_SIG: firmar(SECRETO, "POST", RUTA, ts, c)}
        d, b = DedupFalso(), BroadcastFalso()
        receiver.dedup, receiver.broadcast, receiver.handle_command = d, b, lambda *a: None
        ev = self._evento(cuerpo_bytes=c, cabeceras=cab)
        ev["body"] = b64.b64encode(c).decode()
        ev["isBase64Encoded"] = True

        class Store:
            def get(self):
                return {"hub_token": TOKEN}

        with patch.object(receiver.wiring, "build_config_store", return_value=Store()):
            r = receiver.lambda_handler(ev, None)
        self.assertEqual(r["statusCode"], 200, r["body"])

    def test_la_ruta_de_telegram_sigue_pasando_por_su_puerta(self):
        # Lo que no se puede haber roto: el webhook de Telegram sigue exigiendo su secreto.
        from entrypoints import receiver

        receiver.dedup, receiver.broadcast, receiver.handle_command = DedupFalso(), BroadcastFalso(), (lambda *a: None)
        with patch.object(receiver.config, "webhook_secret_valido", return_value=False):
            r = receiver.lambda_handler(self._evento(ruta="/webhook/telegram"), None)
        self.assertEqual(r["statusCode"], 403)


class TestLaRutaCoincideConLaInfra(unittest.TestCase):
    """⚠️ La ruta va DENTRO de la firma, así que tres sitios tienen que decir lo mismo.

    Si `receiver.RUTA_HUB` y el `RouteKey` de CloudFormation se separan, el síntoma es un
    403 acá y un `fallido` en el hub, sin nada que señale la ruta.
    """

    def test_la_ruta_del_codigo_esta_en_la_plantilla(self):
        from entrypoints import receiver

        plantilla = os.path.join(
            os.path.dirname(__file__), "..", "infra", "cloudformation", "template.yaml"
        )
        with open(plantilla, encoding="utf-8") as f:
            texto = f.read()
        # Sin comentarios: la plantilla EXPLICA la ruta en su comentario, así que buscarla
        # en el fichero crudo casaría con la prosa y no con el `RouteKey`.
        sin_comentarios = "\n".join(
            l for l in texto.splitlines() if not l.lstrip().startswith("#")
        )
        self.assertIn('RouteKey: "POST %s"' % receiver.RUTA_HUB, sin_comentarios)


if __name__ == "__main__":
    unittest.main()
