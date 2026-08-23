"""Adapters: HubWhatsAppForwarder — enviar por wa-hub en vez de por el servicio propio.

Lo que se prueba, y el orden no es casual: primero las tres cosas que el hub NO puede
hacer, porque en las tres el fallo aceptable es «la difusión no sale» y el inaceptable es
«sale a menos gente sin decirlo». Después la firma contra el cliente REAL del hub, que es
lo único que descarta un 401 indepurable.
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

from adapters.whatsapp_hub import (  # noqa: E402
    HubWhatsAppForwarder,
    cadena_canonica,
    firmar,
    partir_token,
)

TOKEN = "kf8905e:" + ("s" * 64)
BASE = "https://wa-hub.example"


class FakeResp:
    def __init__(self, body='{"ok":true,"id":"X"}'):
        self._b = body.encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _hub(**kw):
    return HubWhatsAppForwarder(BASE, TOKEN, "replica", **kw)


class TestLoQueElHubNoPuedeHacer(unittest.TestCase):
    """Las tres negativas. Cada `assertIn` de acá es una difusión que NO sale, a propósito."""

    def setUp(self):
        self.hub = _hub()

    def test_mode_all_se_rechaza_y_no_manda_nada(self):
        with patch("urllib.request.urlopen") as up:
            r = self.hub.forward("hola", None, [], mode="all", list_ids=["57300111@s.whatsapp.net"])
        self.assertEqual(r.get("error"), "hub_sin_agenda")
        # ⚠️ Lo importante no es el código: es que NO se llamó al hub. Un `mode=all`
        # resuelto «con lo que haya a mano» sería una difusión a un subconjunto silencioso.
        up.assert_not_called()

    def test_mode_except_igual(self):
        with patch("urllib.request.urlopen") as up:
            r = self.hub.forward("hola", None, [], mode="except", list_ids=["1"])
        self.assertEqual(r.get("error"), "hub_sin_agenda")
        up.assert_not_called()

    def test_exclude_patterns_se_rechaza_porque_no_hay_nombres(self):
        # ⚠️ La peor de las tres si se ignorara: `exclude_patterns` excluye por NOMBRE, así
        # que tragárselo sería mandarle el mensaje justo a quien se pidió excluir.
        with patch("urllib.request.urlopen") as up:
            r = self.hub.forward("hola", None, [], mode="only", list_ids=["1"],
                                 exclude_patterns=["proveedor"])
        self.assertEqual(r.get("error"), "hub_sin_nombres")
        up.assert_not_called()

    def test_imagen_se_rechaza_en_vez_de_perderse(self):
        with patch("urllib.request.urlopen") as up:
            r = self.hub.forward("hola", "https://x/y.jpg", [], mode="only", list_ids=["1"])
        self.assertEqual(r.get("error"), "hub_sin_imagen")
        up.assert_not_called()

    def test_texto_vacio(self):
        with patch("urllib.request.urlopen") as up:
            r = self.hub.forward("   ", None, [], mode="only", list_ids=["1"])
        self.assertEqual(r.get("error"), "texto_vacio")
        up.assert_not_called()

    def test_contar_LANZA_en_los_modos_que_no_puede(self):
        # ⚠️ Y no devuelve 0: con 0 el plan trocearía en CERO lotes y la difusión se daría
        # por hecha sin haber salido. Un cero es una respuesta; una excepción es un aviso.
        for modo in ("all", "except"):
            with self.assertRaises(ValueError):
                self.hub.contar(mode=modo, list_ids=["1"])
        with self.assertRaises(ValueError):
            self.hub.contar(mode="only", list_ids=["1"], exclude_patterns=["x"])


class TestConfiguracion(unittest.TestCase):
    def test_sin_url_no_hace_nada_y_lo_dice(self):
        r = HubWhatsAppForwarder("", TOKEN, "replica").forward("h", None, [], mode="only",
                                                               list_ids=["1"])
        self.assertIn("skipped", r)

    def test_un_token_sin_dos_puntos_no_se_adivina(self):
        kid, sec = partir_token("solotexto")
        self.assertEqual((kid, sec), ("", ""))
        r = HubWhatsAppForwarder(BASE, "solotexto", "replica").forward("h", None, [], mode="only",
                                                                      list_ids=["1"])
        self.assertIn("skipped", r)

    def test_un_secreto_corto_se_rechaza_diciendo_cuanto_mide(self):
        # Un token pegado por la mitad es la causa más común de un 401, y con este mensaje
        # no hay que ir a buscarla.
        listo, motivo = HubWhatsAppForwarder(BASE, "kid:corto", "replica").configurado()
        self.assertFalse(listo)
        self.assertIn("5", motivo)

    def test_sin_sesion_tampoco(self):
        listo, motivo = HubWhatsAppForwarder(BASE, TOKEN, "").configurado()
        self.assertFalse(listo)
        self.assertIn("sesión", motivo)


class TestEnvio(unittest.TestCase):
    def _capturar(self, hub, **kw):
        """Manda y devuelve (resultado, [cuerpos que viajaron], [cabeceras])."""
        cuerpos, cabeceras = [], []

        def falso(req, timeout=None):
            cuerpos.append(json.loads(req.data.decode()))
            cabeceras.append(dict(req.headers))
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=falso):
            r = hub.forward("hola", None, kw.pop("exclude", []), mode="only", **kw)
        return r, cuerpos, cabeceras

    def test_un_post_por_destinatario_con_la_sesion_dentro(self):
        r, cuerpos, _ = self._capturar(_hub(), list_ids=["57300111", "57300222"])
        self.assertEqual(r["enviados"], 2)
        self.assertEqual([c["a"] for c in cuerpos], ["57300111", "57300222"])
        self.assertTrue(all(c["sesion"] == "replica" for c in cuerpos))
        self.assertTrue(all(c["texto"] == "hola" for c in cuerpos))

    def test_las_tres_cabeceras_firmadas(self):
        _, _, cab = self._capturar(_hub(), list_ids=["1"])
        # Las cabeceras que urllib normaliza a Capitalizado.
        claves = {k.lower() for k in cab[0]}
        for c in ("x-hv-puente-key", "x-hv-puente-ts", "x-hv-puente-sig"):
            self.assertIn(c, claves)

    def test_el_mismo_destinatario_en_dos_listas_es_UN_mensaje(self):
        r, cuerpos, _ = self._capturar(_hub(), list_ids=["57300111", "57300111"])
        self.assertEqual(r["enviados"], 1)
        self.assertEqual(len(cuerpos), 1)

    def test_exclude_saca_y_pattern_exceptions_rescata(self):
        r, cuerpos, _ = self._capturar(_hub(), list_ids=["a", "b", "c"], exclude=["b", "c"],
                                      pattern_exceptions=["c"])
        self.assertEqual([x["a"] for x in cuerpos], ["a", "c"])
        self.assertEqual(r["total"], 2)

    def test_offset_y_limit_trocean_pero_total_es_el_de_verdad(self):
        # ⚠️ `total` tiene que ser el del JOB, no el del trozo: si devolviera el del trozo,
        # el progreso diría «2 de 2» con seis personas sin recibir nada.
        r, cuerpos, _ = self._capturar(_hub(), list_ids=list("abcdefgh"), offset=2, limit=3)
        self.assertEqual([x["a"] for x in cuerpos], ["c", "d", "e"])
        self.assertEqual(r["total"], 8)
        self.assertEqual(r["en_este_lote"], 3)

    def test_un_error_PERMANENTE_para_el_bucle(self):
        import urllib.error

        llamadas = []

        def falso(req, timeout=None):
            llamadas.append(1)
            raise urllib.error.HTTPError(BASE, 403, "no puede enviar", {}, None)

        with patch("urllib.request.urlopen", side_effect=falso):
            r = _hub().forward("hola", None, [], mode="only", list_ids=list("abcdef"))
        # Un 403 es del proyecto, no del destinatario: insistir con los otros cinco sería
        # coleccionar el mismo error seis veces.
        self.assertEqual(len(llamadas), 1)
        self.assertEqual(r["enviados"], 0)
        self.assertIn("parado", r)

    def test_un_fallo_de_red_NO_para_el_bucle_pero_se_cuenta(self):
        llamadas = []

        def falso(req, timeout=None):
            llamadas.append(1)
            if len(llamadas) == 1:
                raise TimeoutError("se cayó la red")
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=falso):
            r = _hub().forward("hola", None, [], mode="only", list_ids=["a", "b", "c"])
        self.assertEqual(len(llamadas), 3)
        self.assertEqual(r["enviados"], 2)
        self.assertEqual(len(r["fallos"]), 1)


class TestPresupuestoDeTiempo(unittest.TestCase):
    """⚠️ El límite que sorprende. El servicio propio contesta 202 y envía de fondo; el hub
    tarda entre 4 y 15 s POR MENSAJE por su cadencia antiban. Cien destinatarios son ocho
    minutos, o sea el timeout de la Lambda."""

    def test_se_para_al_agotar_el_presupuesto_y_DICE_cuantos_quedaron(self):
        # Un reloj de mentira que avanza 16 s por consulta: así el presupuesto se agota de
        # forma determinista, sin dormir de verdad en la prueba.
        t = {"n": 0.0}

        def reloj():
            t["n"] += 16.0
            return t["n"]

        hub = HubWhatsAppForwarder(BASE, TOKEN, "replica", presupuesto_s=50.0, reloj=reloj)
        with patch("urllib.request.urlopen", return_value=FakeResp()):
            r = hub.forward("hola", None, [], mode="only", list_ids=list("abcdefghij"))
        self.assertLess(r["enviados"], 10)
        self.assertGreater(r["pendientes"], 0)
        # Lo que de verdad importa: que se DIGA. Un lote que se corta en silencio se lee
        # como «ya se mandó a todos».
        self.assertIn("presupuesto", r["parado"])
        # Y la cuenta tiene que cuadrar, o el progreso miente.
        self.assertEqual(r["enviados"] + r["pendientes"] + len(r["fallos"]), r["en_este_lote"])


# =========================================================================== #
# EL VECTOR DE INTEROPERABILIDAD
#
# Cada línea es `(método, ruta, ts, cuerpo, hexadecimal)`, y el hexadecimal lo produjo el
# cliente REAL del hub (`wa-hub/clientes/python/hub.py`) con el secreto de abajo. Se
# guarda acá A PROPÓSITO, y no se lee del hub en tiempo de prueba:
#
#   La primera versión importaba el fichero del hub por su ruta absoluta y FALLABA si no
#   estaba. En un portátil con los dos repos al lado funcionaba; en CI el repo del hub no
#   existe, así que la prueba se puso roja sin que nada estuviera mal. Y la alternativa
#   —saltarse la prueba cuando no encuentra el hub— es peor: una prueba de
#   interoperabilidad que se salta sola no prueba nada y deja creer que sí.
#
# Con el vector guardado no hay que elegir: corre SIEMPRE, en cualquier máquina, y sigue
# cazando lo único que puede pasar de verdad —que la cadena canónica de ESTE archivo se
# desvíe—. Lo que el vector no ve es un cambio del lado del HUB, y para eso está la prueba
# de más abajo (que sí compara contra el cliente real cuando está a mano) y el propio
# `check-firma` del hub, que lo compara contra el Python de la plataforma HV.
#
# Los cuatro casos no son decorativos: cuerpo normal, cuerpo VACÍO (el `sha256` de la
# cadena vacía es un sitio clásico donde una implementación pone otra cosa), acentos +
# emoji (si un lado codificara en latin-1, se ve acá) y otra RUTA (que tiene que dar otra
# firma, o una petición capturada se reenvía a otro endpoint).
# =========================================================================== #
SECRETO_VECTOR = "s" * 64
VECTOR = [
    ("POST", "/v1/enviar", 1787000000, b'{"sesion":"replica","a":"57300111","texto":"hola"}',
     "817522ccb09af2a9773a38032147d9dd2ee6894c04feff9be150f5376d6e14ae"),
    ("POST", "/v1/enviar", 1787000001, b"",
     "09c3d73a6f1cd0d99503b4f6d620b5b65475962bb45897db338138e790d8d3b8"),
    ("POST", "/v1/enviar", 1787000002,
     b'{"t": "oferta \xc3\xb1 \xf0\x9f\x92\xbc \xc2\xabcita\xc2\xbb"}',
     "a4dd37408acdcb70991ed8a5842a4521d4e98d775658b787890bfa6c39d522b9"),
    ("POST", "/v1/ping", 1787000003, b"{}",
     "1681790e9385f25697a0c2e32f649639773ad8e3279ab96c6485315000f9a02b"),
]


class TestFirmaContraElHub(unittest.TestCase):
    """⚠️ LA PRUEBA QUE MÁS VALE.

    Si la cadena canónica de acá se desvía un solo byte de la del hub, el único síntoma es
    un **401 en el lado del hub**, sin más pista, con Réplica convencida de haber hecho lo
    correcto. Es el fallo más caro que puede tener un webhook.
    """

    HUB_CLIENTE = r"C:/Projects/Personal/wa-hub/clientes/python/hub.py"

    def test_el_hexadecimal_es_el_del_hub(self):
        """Corre SIEMPRE: el vector va guardado. Ver el comentario de arriba."""
        for metodo, ruta, ts, cuerpo, esperado in VECTOR:
            with self.subTest(ruta=ruta, bytes=len(cuerpo)):
                self.assertEqual(firmar(SECRETO_VECTOR, metodo, ruta, ts, cuerpo), esperado)

    def test_y_contra_el_cliente_real_cuando_esta_a_mano(self):
        """Lo que el vector no puede ver: que el HUB haya cambiado su cadena.

        Acá sí se salta si el hub no está, y se puede: lo que esta prueba añade sobre el
        vector es detectar un cambio del otro lado, y sin el otro lado no hay nada que
        comparar. La firma de Réplica ya quedó comprobada arriba, sin condiciones.
        """
        if not os.path.exists(self.HUB_CLIENTE):
            self.skipTest(
                "el repo del hub no está en %s (normal en CI). El vector guardado ya "
                "comprobó la firma; esto solo detectaría un cambio DEL HUB." % self.HUB_CLIENTE
            )
        import importlib.util

        spec = importlib.util.spec_from_file_location("hub_real", self.HUB_CLIENTE)
        hub_real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hub_real)

        for metodo, ruta, ts, cuerpo, esperado in VECTOR:
            with self.subTest(ruta=ruta, bytes=len(cuerpo)):
                # ⚠️ Contra el VECTOR también: si el hub cambiara su cadena, esto avisa de
                # que el vector guardado quedó viejo — que es el único modo en que el
                # vector podría mentir.
                self.assertEqual(
                    hub_real.firmar(SECRETO_VECTOR, metodo, ruta, ts, cuerpo),
                    esperado,
                    "el hub cambió su cadena canónica: hay que regenerar VECTOR",
                )
                self.assertEqual(
                    hub_real.cadena_canonica(metodo, ruta, ts, cuerpo),
                    cadena_canonica(metodo, ruta, ts, cuerpo),
                    "la cadena canónica no coincide con la del hub",
                )

    def test_el_metodo_en_minusculas_no_produce_una_firma_distinta(self):
        # Extra sobre la referencia, y a favor: un llamador que pase "post" produciría una
        # firma que el hub rechaza con un 401 sin más pista. Normalizar no puede estropear
        # una firma correcta.
        s = "s" * 64
        self.assertEqual(
            firmar(s, "post", "/v1/enviar", 1, b"{}"),
            firmar(s, "POST", "/v1/enviar", 1, b"{}"),
        )

    def test_la_ruta_cambia_la_firma(self):
        # Si no, una petición capturada se puede reenviar a otro endpoint del hub.
        s = "s" * 64
        self.assertNotEqual(firmar(s, "POST", "/v1/enviar", 1, b"{}"),
                            firmar(s, "POST", "/v1/ping", 1, b"{}"))

    def test_se_firma_sobre_LOS_BYTES_QUE_VIAJAN(self):
        # Se serializa UNA vez y se firma eso mismo. Serializar dos veces (una para firmar
        # y otra para enviar) puede dar cadenas distintas, y el 401 que sale de ahí no se
        # depura desde el otro lado.
        capturado = {}

        def falso(req, timeout=None):
            capturado["cuerpo"] = req.data
            capturado["sig"] = req.headers.get("X-hv-puente-sig") or req.headers.get(
                "X-Hv-Puente-Sig"
            )
            capturado["ts"] = req.headers.get("X-hv-puente-ts") or req.headers.get(
                "X-Hv-Puente-Ts"
            )
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=falso):
            _hub().forward("hola", None, [], mode="only", list_ids=["57300111"])
        esperada = firmar("s" * 64, "POST", "/v1/enviar", capturado["ts"], capturado["cuerpo"])
        self.assertEqual(capturado["sig"], esperada)


if __name__ == "__main__":
    unittest.main()
