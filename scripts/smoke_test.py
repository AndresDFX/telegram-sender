"""Smoke test del receptor local: ejercita /start, broadcast y dedup, y verifica DynamoDB.

Pensado para ejecutarse DENTRO del contenedor webhook-dev (donde el endpoint y
dynamodb-local son alcanzables), evitando el proxy/red del host de Windows:

    Get-Content scripts/smoke_test.py -Raw | docker exec -i telegram-sync-webhook python -
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8080")
sys.path.insert(0, "/app/lambda")


def _post(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}/webhook/telegram",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def _get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as resp:
        return resp.status, resp.read().decode()


print("1) health            :", _get("/health"))

from domain.markup import aplicar_markup  # noqa: E402

print("2) markup (demo live) :", aplicar_markup("A06 4-64GB $325.000 y B $1.150.000"))

print("3) /start alta 7777   :", _post(
    {"update_id": 1001, "message": {"chat": {"id": 7777, "type": "private"}, "text": "/start"}}
))

print("4) channel_post + 15% :", _post(
    {"update_id": 1002, "channel_post": {"chat": {"id": -100123}, "text": "Producto A $100.00 y B 1.250,50"}}
))

print("5) duplicado (1002)   :", _post(
    {"update_id": 1002, "channel_post": {"chat": {"id": -100123}, "text": "otra $1.00"}}
))

print("6) sin secreto + flag : (ALLOW_INSECURE_WEBHOOK=1 en dev → aceptado arriba)")

import boto3  # noqa: E402

ddb = boto3.resource("dynamodb", endpoint_url=os.environ["DYNAMODB_ENDPOINT"])
table = os.environ.get("SUBSCRIBERS_TABLE", "SubscriptoresTelegram")
item = ddb.Table(table).get_item(Key={"chatId": "7777"}).get("Item")
print("7) suscriptor 7777    :", item)
