"""Thin HTTP client for the Verbatim API.

Deliberately hand-written rather than generated: the job touches roughly six
endpoints, and the generated package in ``clients/python/verbatim-python-client``
targets an older API version (host ``api.verbatim.cloud``, a
``POST /v1/doc/{corpusId}`` upload returning a Google resumable session) that no
longer matches production.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx

from verbatim_sync.api.auth import TokenProvider
from verbatim_sync.errors import ApiError, RetryableApiError
from verbatim_sync.logging_setup import get_logger

logger = get_logger("api")

#: Statuses worth another attempt: rate limiting and transient server faults.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class ApiClient:
    """Authenticated JSON transport with bounded exponential backoff."""

    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider,
        *,
        timeout: float = 30.0,
        max_retries: int = 5,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._tokens = token_provider
        self._max_retries = max_retries
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=False,
        )

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.token()}",
            "Accept": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Any:
        """Call an API endpoint, retrying transient failures.

        Returns the decoded JSON body, or ``None`` for an empty response.
        """
        url = path if path.startswith("/") else f"/{path}"
        # Drop unset query parameters rather than sending "None".
        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.request(
                    method,
                    url,
                    params=clean_params,
                    json=json_body,
                    headers=self._auth_headers(),
                )
            except httpx.TimeoutException as exc:
                last_error = RetryableApiError(f"{method} {url} timed out: {exc}")
            except httpx.TransportError as exc:
                last_error = RetryableApiError(f"{method} {url} failed: {exc}")
            else:
                if response.status_code in expected:
                    return _decode(response)
                error = _error_for(method, url, response)
                if not isinstance(error, RetryableApiError):
                    raise error
                last_error = error

            if attempt < self._max_retries:
                delay = _backoff_delay(attempt)
                logger.warning(
                    "%s %s failed (attempt %d/%d), retrying in %.1fs: %s",
                    method,
                    url,
                    attempt + 1,
                    self._max_retries + 1,
                    delay,
                    last_error,
                )
                time.sleep(delay)

        assert last_error is not None
        raise last_error

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def put_file(
        self,
        upload_url: str,
        data: bytes | Any,
        content_type: str,
        timeout: float | None = None,
    ) -> None:
        """PUT file bytes to a presigned storage URL.

        Unauthenticated by design — the signature is in the URL, which httpx
        passes through untouched because it is absolute — and the
        ``Content-Type`` must match the type declared at init or storage
        rejects the request. Never log ``upload_url`` unredacted.

        ``timeout`` overrides the client default, which is sized for JSON API
        calls: pushing tens of megabytes needs far longer than a control-plane
        request does.

        Not retried here: a presigned URL is single use, so recovering means
        going back to init. That decision belongs to the sync engine.
        """
        try:
            response = self._client.request(
                "PUT",
                upload_url,
                content=data,
                headers={"Content-Type": content_type},
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TimeoutException as exc:
            raise RetryableApiError(f"upload timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise RetryableApiError(f"upload failed: {exc}") from exc

        if response.status_code not in (200, 201):
            message = (
                f"upload rejected by storage with HTTP {response.status_code}"
            )
            if response.status_code in RETRYABLE_STATUSES:
                raise RetryableApiError(message, status_code=response.status_code)
            raise ApiError(message, status_code=response.status_code)


def _decode(response: httpx.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _error_for(method: str, url: str, response: httpx.Response) -> ApiError:
    """Map a non-success response onto the exception hierarchy.

    Non-2xx bodies follow the ``Error`` schema, whose ``message`` is documented
    as safe to surface.
    """
    api_message = None
    try:
        body = response.json()
        if isinstance(body, dict):
            api_message = body.get("message") or body.get("error")
        elif isinstance(body, str):
            api_message = body
    except ValueError:
        api_message = response.text.strip()[:200] or None

    detail = f": {api_message}" if api_message else ""
    summary = f"{method} {url} returned HTTP {response.status_code}{detail}"

    if response.status_code in RETRYABLE_STATUSES:
        return RetryableApiError(
            summary, status_code=response.status_code, api_message=api_message
        )
    return ApiError(
        summary, status_code=response.status_code, api_message=api_message
    )


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at 30s."""
    return min(2.0**attempt, 30.0) * (0.5 + random.random() / 2)
