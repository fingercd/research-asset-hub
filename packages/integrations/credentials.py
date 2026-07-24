from __future__ import annotations

import importlib
from typing import Protocol, cast


class CredentialStoreUnavailable(RuntimeError):
    pass


class _KeyringModule(Protocol):
    def set_password(self, service_name: str, account: str, secret: str) -> None: ...

    def get_password(self, service_name: str, account: str) -> str | None: ...

    def delete_password(self, service_name: str, account: str) -> None: ...


class CredentialStore:
    """Store provider secrets in the operating-system credential manager."""

    def __init__(self, service_name: str = "research-asset-hub") -> None:
        self.service_name = service_name

    @staticmethod
    def _keyring() -> _KeyringModule:
        try:
            module = importlib.import_module("keyring")
        except ImportError as exc:  # pragma: no cover - depends on optional runtime package
            raise CredentialStoreUnavailable(
                "Install the 'keyring' package to use Windows Credential Manager."
            ) from exc
        return cast(_KeyringModule, module)

    def set(self, account: str, secret: str) -> None:
        if not account.strip() or not secret:
            raise ValueError("account and secret are required")
        keyring = self._keyring()
        keyring.set_password(self.service_name, account, secret)

    def get(self, account: str) -> str | None:
        keyring = self._keyring()
        return keyring.get_password(self.service_name, account)

    def delete(self, account: str) -> None:
        keyring = self._keyring()
        try:
            keyring.delete_password(self.service_name, account)
        except Exception as exc:  # keyring backends expose different not-found exceptions
            if "not found" not in str(exc).lower():
                raise
