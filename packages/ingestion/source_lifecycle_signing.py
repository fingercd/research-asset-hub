"""Ed25519 signing boundary for source lifecycle events.

Only public keys are read from project configuration. Private keys are accepted
at the command boundary from an explicitly named environment variable, an
explicit file, or the OS keyring and are never serialized by this module.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import keyring
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .source_retirement import canonical_payload_bytes, canonical_payload_digest


class SourceLifecycleKeyError(ValueError):
    """A lifecycle signing key or public registry is invalid."""


@dataclass(frozen=True, slots=True)
class RegisteredPublicKey:
    key_id: str
    public_key: Ed25519PublicKey
    enabled: bool = True


def _decode_raw_key(value: str, *, expected_size: int) -> bytes:
    cleaned = value.strip()
    if cleaned.startswith("base64:"):
        cleaned = cleaned.removeprefix("base64:").strip()
    if cleaned.startswith("hex:"):
        try:
            decoded = bytes.fromhex(cleaned.removeprefix("hex:").strip())
        except ValueError as error:
            raise SourceLifecycleKeyError("key contains invalid hexadecimal data") from error
    else:
        try:
            decoded = base64.b64decode(cleaned, validate=True)
        except (binascii.Error, ValueError) as error:
            raise SourceLifecycleKeyError("key is neither PEM nor valid base64") from error
    if len(decoded) != expected_size:
        raise SourceLifecycleKeyError(f"Ed25519 raw key must contain exactly {expected_size} bytes")
    return decoded


def parse_public_key(value: str) -> Ed25519PublicKey:
    """Parse an Ed25519 public key from PEM, base64, or ``hex:`` form."""

    if "BEGIN PUBLIC KEY" in value:
        try:
            key = serialization.load_pem_public_key(value.encode("utf-8"))
        except ValueError as error:
            raise SourceLifecycleKeyError("public key PEM is invalid") from error
        if not isinstance(key, Ed25519PublicKey):
            raise SourceLifecycleKeyError("public registry key is not Ed25519")
        return key
    return Ed25519PublicKey.from_public_bytes(_decode_raw_key(value, expected_size=32))


def parse_private_key(value: str) -> Ed25519PrivateKey:
    """Parse an unencrypted Ed25519 private key without retaining its text."""

    if "BEGIN PRIVATE KEY" in value:
        try:
            key = serialization.load_pem_private_key(
                value.encode("utf-8"),
                password=None,
            )
        except (TypeError, ValueError) as error:
            raise SourceLifecycleKeyError("private key PEM is invalid or encrypted") from error
        if not isinstance(key, Ed25519PrivateKey):
            raise SourceLifecycleKeyError("private signing key is not Ed25519")
        return key
    return Ed25519PrivateKey.from_private_bytes(_decode_raw_key(value, expected_size=32))


def load_private_key(
    *,
    private_key_file: Path | None = None,
    private_key_env: str | None = None,
    keyring_service: str | None = None,
    keyring_account: str | None = None,
) -> Ed25519PrivateKey:
    """Load exactly one explicitly selected private-key source.

    The environment option names the environment variable; it does not accept
    key material as a CLI value. Keyring service and account must be supplied
    together.
    """

    has_keyring = keyring_service is not None or keyring_account is not None
    if has_keyring and (not keyring_service or not keyring_account):
        raise SourceLifecycleKeyError(
            "keyring_service and keyring_account must be supplied together"
        )
    selected = sum(
        (
            private_key_file is not None,
            private_key_env is not None,
            has_keyring,
        )
    )
    if selected != 1:
        raise SourceLifecycleKeyError(
            "select exactly one private-key source: file, environment, or keyring"
        )
    material: str | None
    if private_key_file is not None:
        try:
            material = private_key_file.read_text(encoding="utf-8")
        except OSError as error:
            raise SourceLifecycleKeyError("private key file could not be read") from error
    elif private_key_env is not None:
        material = os.environ.get(private_key_env)
        if not material:
            raise SourceLifecycleKeyError(
                f"private key environment variable {private_key_env!r} is empty or unset"
            )
    else:
        try:
            material = keyring.get_password(str(keyring_service), str(keyring_account))
        except Exception as error:
            raise SourceLifecycleKeyError("OS keyring lookup failed") from error
        if not material:
            raise SourceLifecycleKeyError("OS keyring contains no private key for that account")
    assert material is not None
    return parse_private_key(material)


class PublicKeyRegistry:
    """Callable verifier backed by a versioned YAML public-key registry."""

    def __init__(self, keys: dict[str, RegisteredPublicKey]) -> None:
        self._keys = dict(keys)

    @classmethod
    def load(cls, path: Path) -> PublicKeyRegistry:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise SourceLifecycleKeyError("public signer registry could not be read") from error
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise SourceLifecycleKeyError("public signer registry schema_version must be 1")
        rows = document.get("keys")
        if not isinstance(rows, list):
            raise SourceLifecycleKeyError("public signer registry keys must be a list")
        keys: dict[str, RegisteredPublicKey] = {}
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise SourceLifecycleKeyError(f"public signer registry keys[{index}] is invalid")
            key_id = str(row.get("id", "")).strip()
            algorithm = str(row.get("algorithm", "ed25519")).casefold()
            public_key = row.get("public_key")
            if not key_id or not isinstance(public_key, str):
                raise SourceLifecycleKeyError(
                    f"public signer registry keys[{index}] requires id and public_key"
                )
            if algorithm != "ed25519":
                raise SourceLifecycleKeyError(
                    f"public signer registry key {key_id!r} is not Ed25519"
                )
            if key_id in keys:
                raise SourceLifecycleKeyError(f"duplicate public signer key id {key_id!r}")
            enabled = row.get("enabled", True)
            if not isinstance(enabled, bool):
                raise SourceLifecycleKeyError(
                    f"public signer registry key {key_id!r} enabled must be boolean"
                )
            keys[key_id] = RegisteredPublicKey(
                key_id=key_id,
                public_key=parse_public_key(public_key),
                enabled=enabled,
            )
        return cls(keys)

    def __call__(
        self,
        *,
        payload: bytes,
        signature: bytes,
        key_id: str,
        algorithm: str,
    ) -> bool:
        if algorithm.casefold() != "ed25519":
            return False
        registered = self._keys.get(key_id)
        if registered is None or not registered.enabled:
            return False
        try:
            registered.public_key.verify(signature, payload)
        except InvalidSignature:
            return False
        return True


def sign_payload(private_key: Ed25519PrivateKey, payload: dict[str, Any]) -> bytes:
    """Sign the canonical payload bytes; no key or signature is logged."""

    return private_key.sign(canonical_payload_bytes(payload))


__all__ = [
    "PublicKeyRegistry",
    "RegisteredPublicKey",
    "SourceLifecycleKeyError",
    "canonical_payload_digest",
    "load_private_key",
    "parse_private_key",
    "parse_public_key",
    "sign_payload",
]
