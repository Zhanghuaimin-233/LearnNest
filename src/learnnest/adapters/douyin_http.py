"""Pure HTTP transport for the Douyin favorites API adapter.

The transport deliberately does not implement Douyin's rotating request
signature.  A signer is injected at runtime for endpoints that need one.  The
verified default-video favorites endpoint also has an explicit unsigned
baseline; other endpoints remain fail-closed when no signer is supplied.
Browser automation is not required by this module.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from pydantic import SecretStr

from learnnest.adapters.douyin import DouyinAdapterError

_VIDEO_FAVORITES_PATH = "/aweme/v1/web/aweme/listcollection/"
_DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
_FOLDERS_PATH = "/aweme/v1/web/collects/list/"
_FOLDER_ITEMS_PATH = "/aweme/v1/web/collects/video/list/"
_DEFAULT_WEB_AID = "6383"
_DEFAULT_WEB_CHANNEL = "channel_pc_web"
_UNSIGNED_BASELINE_PATHS = frozenset({_VIDEO_FAVORITES_PATH, _DETAIL_PATH})
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class DouyinAuthenticationError(DouyinAdapterError):
    """The runtime CookieJar is no longer accepted by Douyin."""

    def __init__(
        self,
        message: str,
        *,
        reason: Literal[
            "credentials_rejected", "request_rejected", "business_rejected"
        ] = "credentials_rejected",
        status_code: int | str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


def douyin_authentication_message(error: DouyinAuthenticationError) -> str:
    """Project one safe, actionable authentication/request rejection reason."""
    status = error.status_code
    if error.reason == "request_rejected" and status is not None:
        return (
            f"登录凭据已取得，但收藏请求被抖音拦截（HTTP {status}）；"
            "当前网页接口校验已变化，不是扫码或验证码失败。"
        )
    if error.reason == "business_rejected" and status is not None:
        return f"登录凭据已取得，但收藏接口返回登录失效（状态码 {status}）。"
    if error.reason == "credentials_rejected" and status is not None:
        return f"登录凭据已取得，但收藏接口拒绝了该登录（HTTP {status}）。"
    return "已取得登录凭据，但抖音未接受本次登录。"


@dataclass(frozen=True)
class DouyinSignedRequest:
    """Signer output that is still safe to combine with the request body."""

    params: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)


class DouyinRequestSigner(Protocol):
    """Runtime signer boundary for Douyin's changing web request parameters."""

    def sign(
        self,
        *,
        method: str,
        path: str,
        params: Mapping[str, str],
        body: Mapping[str, str] | None,
        cookie: SecretStr,
    ) -> DouyinSignedRequest: ...


class DouyinUnsignedRequestSigner:
    """Preserve verified base params without adding browser signature fields.

    This is intentionally a narrow transport primitive, not a claim that all
    Douyin endpoints accept unsigned requests.  ``DouyinHttpTransport`` only
    uses it automatically for the verified default-video favorites endpoint.
    """

    def sign(
        self,
        *,
        method: str,
        path: str,
        params: Mapping[str, str],
        body: Mapping[str, str] | None,
        cookie: SecretStr,
    ) -> DouyinSignedRequest:
        del method, path, body, cookie
        return DouyinSignedRequest(params=dict(params))


class DouyinHttpTransport:
    """Call verified favorites/detail endpoints without starting a browser.

    ``cookie`` and the signer are retained only by this runtime object.  They
    are never returned in the JSON response, discovery facts, cursor, or
    exception text.  The injected signer may add the current signed query
    parameters and request headers required by the web API.
    """

    def __init__(
        self,
        cookie: SecretStr,
        *,
        signer: DouyinRequestSigner | None = None,
        base_url: str = "https://www.douyin.com",
        timeout: float = 20.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not cookie.get_secret_value().strip():
            raise ValueError("Douyin cookie must not be empty")
        if timeout <= 0:
            raise ValueError("Douyin HTTP timeout must be positive")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Douyin HTTP base URL must be an HTTPS origin")
        self.cookie = cookie
        self.signer = signer
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener

    def list_video_favorites(
        self,
        *,
        cursor: int,
        count: int,
    ) -> Mapping[str, Any]:
        _validate_page(cursor=cursor, count=count)
        return self._request(
            "POST",
            _VIDEO_FAVORITES_PATH,
            params={
                "device_platform": "webapp",
                "aid": _DEFAULT_WEB_AID,
                "channel": _DEFAULT_WEB_CHANNEL,
                "publish_video_strategy_type": "2",
            },
            body={"count": str(count), "cursor": str(cursor)},
        )

    def list_folders(
        self,
        *,
        cursor: int,
        count: int,
    ) -> Mapping[str, Any]:
        _validate_page(cursor=cursor, count=count)
        return self._request(
            "GET",
            _FOLDERS_PATH,
            params={"count": str(count), "cursor": str(cursor)},
        )

    def get_aweme_detail(self, aweme_id: str) -> Mapping[str, Any]:
        """Fetch one detail payload through the verified unsigned baseline."""
        selected_id = aweme_id.strip()
        if not selected_id or not selected_id.isdigit():
            raise ValueError("Douyin aweme ID must contain only digits")
        payload = self._request(
            "GET",
            _DETAIL_PATH,
            params={"aid": _DEFAULT_WEB_AID, "aweme_id": selected_id},
        )
        detail = payload.get("aweme_detail")
        if not isinstance(detail, Mapping):
            raise DouyinAdapterError("Douyin detail response has no aweme_detail")
        return detail

    def list_folder_items(
        self,
        folder_id: str,
        *,
        cursor: int,
        count: int,
    ) -> Mapping[str, Any]:
        _validate_page(cursor=cursor, count=count)
        selected_folder = folder_id.strip()
        if not selected_folder:
            raise ValueError("Douyin folder ID must not be empty")
        return self._request(
            "GET",
            _FOLDER_ITEMS_PATH,
            params={
                "collects_id": selected_folder,
                "count": str(count),
                "cursor": str(cursor),
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        base_params = dict(params or {})
        signer = self.signer
        if signer is None:
            if path not in _UNSIGNED_BASELINE_PATHS:
                raise DouyinAdapterError(
                    "Douyin endpoint requires a runtime request signer"
                )
            signer = DouyinUnsignedRequestSigner()
        signed = signer.sign(
            method=method,
            path=path,
            params=base_params,
            body=body,
            cookie=self.cookie,
        )
        if not isinstance(signed, DouyinSignedRequest):
            raise DouyinAdapterError("Douyin signer returned an invalid request")
        request_params = dict(signed.params)
        query = urlencode(request_params)
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{self.base_url}/",
            "User-Agent": _DEFAULT_USER_AGENT,
        }
        headers.update(dict(signed.headers))
        headers["Cookie"] = self.cookie.get_secret_value()
        encoded_body = None
        if body is not None:
            encoded_body = urlencode(dict(body)).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        request = Request(url, data=encoded_body, headers=headers, method=method)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None)
                if status is not None and not 200 <= int(status) < 300:
                    if int(status) in {401, 403}:
                        raise _http_authentication_error(int(status))
                    raise DouyinAdapterError(
                        f"Douyin API request returned HTTP {int(status)}"
                    )
                raw = response.read()
        except DouyinAdapterError:
            raise
        except HTTPError as error:
            if error.code in {401, 403}:
                raise _http_authentication_error(error.code) from error
            raise DouyinAdapterError(
                f"Douyin API request returned HTTP {error.code}"
            ) from error
        except (OSError, URLError) as error:
            raise DouyinAdapterError("Douyin API request failed") from error
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, JSONDecodeError) as error:
            # A 200 HTML/challenge page is ambiguous: it may be an expired
            # session, but it may also be a transient edge/WAF response. Only
            # explicit authentication signals are allowed to destroy a
            # persisted login.
            raise DouyinAdapterError(
                "Douyin API returned a non-JSON response"
            ) from error
        if not isinstance(payload, Mapping):
            raise DouyinAdapterError("Douyin API returned a non-object response")
        return payload


def _validate_page(*, cursor: int, count: int) -> None:
    if cursor < 0:
        raise ValueError("Douyin cursor must not be negative")
    if count < 1:
        raise ValueError("Douyin page count must be positive")


def _http_authentication_error(status_code: int) -> DouyinAuthenticationError:
    reason = "request_rejected" if status_code == 403 else "credentials_rejected"
    return DouyinAuthenticationError(
        f"Douyin API request returned HTTP {status_code}",
        reason=reason,
        status_code=status_code,
    )
