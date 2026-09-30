"""Encrypt credentials saved explicitly for tenant inventory checks."""

import base64
import hashlib

from cryptography.fernet import Fernet

from app.core.config import get_settings


def _cipher() -> Fernet:
    settings = get_settings()
    key_material = settings.encryption_key or settings.secret_key
    if not key_material:
        raise RuntimeError("An application secret is required to save tenant credentials")
    key = base64.urlsafe_b64encode(hashlib.sha256(key_material.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_inventory_value(value: str) -> str:
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_inventory_value(value: str) -> str:
    return _cipher().decrypt(value.encode("ascii")).decode("utf-8")
