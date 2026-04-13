from __future__ import annotations

import base64

from cryptography.fernet import Fernet

from app.infra.config import get_settings


def _normalize_key(raw: str) -> bytes:
    try:
        data = base64.urlsafe_b64decode(raw.encode("utf-8"))
        if len(data) == 32:
            return raw.encode("utf-8")
    except Exception:
        pass
    padded = raw.encode("utf-8").ljust(32, b"0")[:32]
    return base64.urlsafe_b64encode(padded)


class FieldEncryptor:
    def __init__(self) -> None:
        key = _normalize_key(get_settings().master_encryption_key)
        self._fernet = Fernet(key)

    def encrypt(self, value: str | None) -> str | None:
        if value is None:
            return None
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, value: str | None) -> str | None:
        if value is None:
            return None
        return self._fernet.decrypt(value.encode("utf-8")).decode("utf-8")


encryptor = FieldEncryptor()
