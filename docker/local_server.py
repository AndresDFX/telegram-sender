"""Servidor local que emula API Gateway invocando handler.lambda_handler."""

from __future__ import annotations

import json
import logging
import os
import sys

from flask import Flask, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lambda"))
from handler import lambda_handler

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)


class LocalLambdaContext:
    function_name = "local-broadcaster"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:local:0:function:local-broadcaster"
    aws_request_id = "local-request"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook/telegram")
def webhook():
    event = {
        "body": request.get_data(as_text=True) or "{}",
        "headers": dict(request.headers),
        "requestContext": {"http": {"method": "POST", "path": "/webhook/telegram"}},
    }
    result = lambda_handler(event, LocalLambdaContext())
    status_code = result.get("statusCode", 200)
    body = result.get("body", "")
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {"message": body}
    return payload, status_code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
