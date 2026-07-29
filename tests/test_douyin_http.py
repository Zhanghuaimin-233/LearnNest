from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from learnnest.adapters.douyin_http import (
    DouyinAuthenticationError,
    DouyinHttpTransport,
    DouyinSignedRequest,
)
from learnnest.adapters.douyin import DouyinAdapterError


class FakeResponse:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class RecordingSigner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def sign(
        self,
        *,
        method: str,
        path: str,
        params: Mapping[str, str],
        body: Mapping[str, str] | None,
        cookie: SecretStr,
    ) -> DouyinSignedRequest:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": dict(params),
                "body": None if body is None else dict(body),
                "cookie": cookie,
            }
        )
        return DouyinSignedRequest(
            params={"a_bogus": "runtime-signature"},
            headers={"X-Runtime-Signer": "test"},
        )


class RecordingOpener:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.requests = []

    def __call__(self, request: Any, *, timeout: float) -> FakeResponse:
        self.requests.append((request, timeout))
        return FakeResponse(self.payload)


def test_http_transport_signs_and_sends_all_favorite_endpoints() -> None:
    signer = RecordingSigner()
    opener = RecordingOpener(b'{"aweme_list": [], "has_more": false}')
    transport = DouyinHttpTransport(
        SecretStr("session-cookie"),
        signer=signer,
        base_url="https://douyin.test",
        opener=opener,
    )

    transport.list_video_favorites(cursor=0, count=20)
    transport.list_folders(cursor=20, count=10)
    transport.list_folder_items("folder-1", cursor=30, count=5)

    assert len(opener.requests) == 3
    video_request, video_timeout = opener.requests[0]
    assert video_request.full_url == (
        "https://douyin.test/aweme/v1/web/aweme/listcollection/"
        "?a_bogus=runtime-signature"
    )
    assert video_request.data == b"count=20&cursor=0"
    assert video_request.get_header("Cookie") == "session-cookie"
    assert video_request.get_header("X-runtime-signer") == "test"
    assert video_timeout == 20.0
    assert signer.calls[0]["body"] == {"count": "20", "cursor": "0"}
    assert signer.calls[0]["params"] == {"aid": "6383"}
    assert signer.calls[1]["params"] == {"count": "10", "cursor": "20"}
    assert signer.calls[2]["params"] == {
        "collects_id": "folder-1",
        "count": "5",
        "cursor": "30",
    }
    assert all(call["cookie"] == SecretStr("session-cookie") for call in signer.calls)


def test_http_transport_fails_closed_on_html_response() -> None:
    signer = RecordingSigner()
    opener = RecordingOpener(b"<html>login</html>")
    transport = DouyinHttpTransport(
        SecretStr("secret-cookie"), signer=signer, opener=opener
    )

    with pytest.raises(DouyinAdapterError, match="non-JSON") as error:
        transport.list_video_favorites(cursor=0, count=20)

    assert not isinstance(error.value, DouyinAuthenticationError)
    assert "secret-cookie" not in str(error.value)


def test_http_transport_uses_verified_unsigned_video_baseline_without_signer() -> None:
    opener = RecordingOpener(b'{"aweme_list": [], "has_more": false}')
    transport = DouyinHttpTransport(
        SecretStr("session-cookie"),
        base_url="https://douyin.test",
        opener=opener,
    )

    transport.list_video_favorites(cursor=0, count=20)

    request, _ = opener.requests[0]
    assert request.full_url == (
        "https://douyin.test/aweme/v1/web/aweme/listcollection/?aid=6383"
    )
    assert request.data == b"count=20&cursor=0"


def test_http_transport_uses_verified_unsigned_detail_baseline_without_signer() -> None:
    opener = RecordingOpener(b'{"aweme_detail": {"aweme_id": "101", "images": []}}')
    transport = DouyinHttpTransport(
        SecretStr("session-cookie"),
        base_url="https://douyin.test",
        opener=opener,
    )

    detail = transport.get_aweme_detail("101")

    assert detail["aweme_id"] == "101"
    request, _ = opener.requests[0]
    assert request.full_url == (
        "https://douyin.test/aweme/v1/web/aweme/detail/?aid=6383&aweme_id=101"
    )
    assert request.get_header("Cookie") == "session-cookie"


def test_http_transport_keeps_unverified_endpoints_fail_closed_without_signer() -> None:
    transport = DouyinHttpTransport(SecretStr("session-cookie"))

    with pytest.raises(DouyinAdapterError, match="runtime request signer"):
        transport.list_folders(cursor=0, count=20)


def test_http_transport_rejects_invalid_runtime_configuration() -> None:
    signer = RecordingSigner()
    with pytest.raises(ValueError, match="cookie"):
        DouyinHttpTransport(SecretStr("  "), signer=signer)
    with pytest.raises(ValueError, match="HTTPS"):
        DouyinHttpTransport(
            SecretStr("cookie"), signer=signer, base_url="http://douyin.test"
        )
