"""Windows CurrentUser DPAPI storage for one provider connection secret."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path

from pydantic import SecretStr

from learnnest.douyin_cookie_store import _protect_windows, _unprotect_windows

_FILE_MAGIC = b"LEARNNEST_PROVIDER_SECRET_DPAPI_V1\0"
_PLAINTEXT_MAGIC = b"LEARNNEST_PROVIDER_SECRET_V1\0"


class SecretStoreError(RuntimeError):
    """A deliberately non-diagnostic provider-secret access failure."""


class ProviderSecretStore:
    """Store independent encrypted secret objects below one output root.

    A connection receives a fresh ``secret_id`` on every configured Key, even
    when the user enters identical text for another connection.  The settings
    model only retains that opaque id; neither it nor task/automation facts can
    recover the Key without this Windows-user-scoped store.
    """

    def __init__(
        self,
        output_root: str | Path,
        *,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
    ) -> None:
        self.directory = (
            Path(output_root).resolve() / ".learnnest" / "providers" / "secrets"
        )
        self._protect = protect or _protect_windows
        self._unprotect = unprotect or _unprotect_windows

    def put(
        self,
        connection_id: str,
        value: str | SecretStr,
        *,
        secret_id: str | None = None,
    ) -> str:
        secret = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not secret.strip() or "\r" in secret or "\n" in secret:
            raise SecretStoreError("provider secret is invalid")
        if not connection_id:
            raise SecretStoreError("provider secret is invalid")
        selected_secret_id = secret_id or uuid.uuid4().hex
        try:
            self.path_for(selected_secret_id)
        except SecretStoreError:
            raise
        try:
            protected = self._protect(_PLAINTEXT_MAGIC + secret.encode("utf-8"))
        except Exception as error:
            raise SecretStoreError("provider secret is unavailable") from error
        if not protected:
            raise SecretStoreError("provider secret is unavailable")
        destination = self.path_for(selected_secret_id)
        if destination.exists():
            raise SecretStoreError("provider secret is unavailable")
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(_FILE_MAGIC + protected)
            os.replace(temporary, destination)
        except OSError as error:
            raise SecretStoreError("provider secret is unavailable") from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return selected_secret_id

    def read(self, secret_id: str) -> SecretStr:
        try:
            payload = self.path_for(secret_id).read_bytes()
            if not payload.startswith(_FILE_MAGIC):
                raise ValueError("bad header")
            plaintext = self._unprotect(payload[len(_FILE_MAGIC) :])
            if not plaintext.startswith(_PLAINTEXT_MAGIC):
                raise ValueError("bad plaintext")
            value = plaintext[len(_PLAINTEXT_MAGIC) :].decode("utf-8")
            if not value.strip() or "\r" in value or "\n" in value:
                raise ValueError("bad secret")
        except Exception as error:
            raise SecretStoreError("provider secret is unavailable") from error
        return SecretStr(value)

    def path_for(self, secret_id: str) -> Path:
        if len(secret_id) != 32 or any(
            character not in "0123456789abcdef" for character in secret_id
        ):
            raise SecretStoreError("provider secret is unavailable")
        return self.directory / f"{secret_id}.dpapi"
