"""Autenticación: hash de contraseñas (PBKDF2-HMAC-SHA256) y códigos de reseteo.

Puro y sin dependencias externas (solo stdlib). El hash se guarda como
``pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>`` y se verifica en tiempo constante.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

_ALGO = "pbkdf2_sha256"
_ITERS = 200_000
_MIN_LEN = 8


def password_valida(password: str) -> bool:
    """Política mínima: al menos 8 caracteres."""
    return isinstance(password, str) and len(password) >= _MIN_LEN


def hash_password(password: str, iterations: int = _ITERS) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = str(stored).split("$")
        if algo != _ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def gen_code(n: int = 6) -> str:
    """Código numérico aleatorio de n dígitos (para reseteo de contraseña)."""
    return "".join(str(b % 10) for b in os.urandom(n))
