"""Fernet wrapper for YouTube refresh-token encryption.

Plaintext refresh tokens at rest are unacceptable: anyone with a copy
of the SQLite file could impersonate every guild's YouTube channel.
We encrypt with Fernet (AES-128-CBC + HMAC) using a single master
key from `FERNET_MASTER_KEY` env. Rotation = re-encrypt every row
with a new key; the dataclass exposes a `migrate_key()` hook for
when we wire that up in v2.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class TokenCrypto:
    def __init__(self, master_key: str) -> None:
        # Fernet accepts urlsafe-base64 keys directly.
        self._fernet = Fernet(master_key.encode("ascii"))

    def encrypt(self, plain: str) -> bytes:
        """Encrypt a UTF-8 string. Returns the Fernet token as bytes
        (suitable for sqlite BLOB columns)."""
        return self._fernet.encrypt(plain.encode("utf-8"))

    def decrypt(self, cipher: bytes) -> str:
        """Decrypt a previously-encrypted blob. Raises InvalidToken
        on tampered or wrong-key payload."""
        return self._fernet.decrypt(cipher).decode("utf-8")


def generate_master_key() -> str:
    """Convenience for the bootstrap script. Print + paste into .env."""
    return Fernet.generate_key().decode("ascii")


__all__ = ["TokenCrypto", "InvalidToken", "generate_master_key"]
