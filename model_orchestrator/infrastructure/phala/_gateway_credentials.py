"""
Gateway credentials for spawntee API authentication.

Wraps an RSA private key loaded from a PEM file. Used to sign
X-Gateway-Auth-* headers on every spawntee HTTP request.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.hazmat.primitives.serialization import load_pem_private_key


class GatewayCredentials:
    """Coordinator RSA key used to sign spawntee API requests."""

    def __init__(self, private_key: PrivateKeyTypes):
        self.private_key = private_key

    @classmethod
    def from_pem(cls, key_pem: str | bytes) -> GatewayCredentials:
        """Load from a PEM-encoded private key (str or bytes)."""
        if isinstance(key_pem, str):
            key_pem = key_pem.encode()
        private_key = load_pem_private_key(key_pem, password=None)
        return cls(private_key)
