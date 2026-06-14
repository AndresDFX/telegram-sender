"""Adapter de almacenamiento de imágenes en S3 (subida desde el panel + URL temporal para envío)."""

from __future__ import annotations

import os
import uuid

from application.ports import ImageStore

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp"}


class S3ImageStore(ImageStore):
    def __init__(self, bucket: str | None = None, prefix: str = "images/", endpoint: str | None = None):
        self._bucket = bucket or os.environ.get("IMAGES_BUCKET")
        self._prefix = prefix
        self._endpoint = endpoint or os.environ.get("S3_ENDPOINT")

    def _client(self):
        import boto3

        kwargs = {"endpoint_url": self._endpoint} if self._endpoint else {}
        return boto3.client("s3", **kwargs)

    def guardar(self, data: bytes, content_type: str = "image/jpeg") -> str:
        if not self._bucket:
            raise RuntimeError("IMAGES_BUCKET no configurado")
        # Clave ÚNICA por subida: evita que una imagen nueva (compositor o Configuración) pise el
        # objeto de un envío aún en curso/diferido. La URL se re-firma al despachar (no se congela).
        key = f"{self._prefix}{uuid.uuid4().hex}.{_EXT.get(content_type, 'jpg')}"
        self._client().put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
        return key

    def url_temporal(self, key: str, expira: int = 3600) -> str:
        return self._client().generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=expira
        )
