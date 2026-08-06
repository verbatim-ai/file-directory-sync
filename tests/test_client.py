"""Tests for the HTTP transport.

The fake backend used elsewhere replaces this layer wholesale, so retry
behaviour, error classification and header handling are only exercised here.
httpx.MockTransport keeps it all offline.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import API_BASE_URL as BASE_URL
from conftest import StubTokens, make_api_client as make_client, responder

from verbatim_sync.api.client import RETRYABLE_STATUSES, ApiClient, _backoff_delay
from verbatim_sync.errors import ApiError, RetryableApiError


class TestRequest:
    def test_returns_the_decoded_body(self):
        api, _ = make_client(responder(httpx.Response(200, json={"id": "doc-1"})))
        assert api.get("/v1/doc/doc-1") == {"id": "doc-1"}

    def test_sends_the_bearer_token(self):
        tokens = StubTokens("abc.def.ghi")
        api, requests = make_client(responder(httpx.Response(200, json={})), tokens=tokens)
        api.get("/v1/auth/whoami")

        assert requests[0].headers["authorization"] == "Bearer abc.def.ghi"
        assert requests[0].headers["accept"] == "application/json"

    def test_signs_every_request(self):
        """The provider caches; the client must not cache on top of it."""
        tokens = StubTokens()
        api, _ = make_client(responder(httpx.Response(200, json={})), tokens=tokens)
        api.get("/v1/doc/")
        api.get("/v1/doc/")
        assert tokens.calls == 2

    def test_normalises_a_path_without_a_leading_slash(self):
        api, requests = make_client(responder(httpx.Response(200, json={})))
        api.get("v1/doc/accept")
        assert requests[0].url.path == "/v1/doc/accept"

    def test_drops_unset_query_parameters(self):
        api, requests = make_client(responder(httpx.Response(200, json={})))
        api.get("/v1/doc/", params={"corpusId": "c1", "status": None, "pageSize": 25})

        query = requests[0].url.params
        assert query["corpusId"] == "c1"
        assert "status" not in query
        assert query["pageSize"] == "25"

    def test_sends_a_json_body(self):
        import json

        api, requests = make_client(responder(httpx.Response(200, json={})))
        api.post("/v1/doc/init", json_body={"corpusId": "c1"})

        assert json.loads(requests[0].content) == {"corpusId": "c1"}

    @pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
    def test_every_verb(self, method):
        api, requests = make_client(responder(httpx.Response(200, json={})))
        getattr(api, method)("/v1/doc/x")
        assert requests[0].method == method.upper()


class TestDecoding:
    def test_204_is_none(self):
        api, _ = make_client(responder(httpx.Response(204)))
        assert api.delete("/v1/doc/x") is None

    def test_empty_body_is_none(self):
        api, _ = make_client(responder(httpx.Response(200, content=b"")))
        assert api.get("/v1/doc/x") is None

    def test_non_json_body_falls_back_to_text(self):
        api, _ = make_client(responder(httpx.Response(200, text="not json")))
        assert api.get("/v1/doc/x") == "not json"

    def test_a_json_list_survives(self):
        api, _ = make_client(responder(httpx.Response(200, json=["application/pdf"])))
        assert api.get("/v1/doc/accept") == ["application/pdf"]


class TestErrorMapping:
    @pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
    def test_transient_statuses_are_retryable(self, status):
        api, _ = make_client(responder(httpx.Response(status)), max_retries=0)
        with pytest.raises(RetryableApiError) as exc:
            api.get("/v1/doc/x")
        assert exc.value.status_code == status

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 415, 422])
    def test_client_errors_are_not_retryable(self, status):
        api, requests = make_client(responder(httpx.Response(status)))
        with pytest.raises(ApiError) as exc:
            api.get("/v1/doc/x")

        assert not isinstance(exc.value, RetryableApiError)
        assert exc.value.status_code == status
        assert len(requests) == 1  # gave up immediately

    def test_surfaces_the_api_error_message(self):
        """Non-2xx bodies follow the Error schema, whose message is safe to show."""
        api, _ = make_client(
            responder(
                httpx.Response(
                    415,
                    json={
                        "status": 415,
                        "error": "Unsupported Media Type",
                        "message": "content type application/zip is not accepted",
                    },
                )
            )
        )
        with pytest.raises(ApiError) as exc:
            api.post("/v1/doc/init")

        assert exc.value.api_message == "content type application/zip is not accepted"
        assert "not accepted" in str(exc.value)
        assert "HTTP 415" in str(exc.value)

    def test_falls_back_to_the_error_field(self):
        api, _ = make_client(responder(httpx.Response(400, json={"error": "Bad Request"})))
        with pytest.raises(ApiError) as exc:
            api.get("/v1/doc/x")
        assert exc.value.api_message == "Bad Request"

    def test_handles_a_plain_string_body(self):
        api, _ = make_client(responder(httpx.Response(409, json="not replaceable")))
        with pytest.raises(ApiError) as exc:
            api.put("/v1/doc/x/init")
        assert exc.value.api_message == "not replaceable"

    def test_handles_a_non_json_error_body(self):
        api, _ = make_client(responder(httpx.Response(502, text="<html>gateway</html>")))
        with pytest.raises(RetryableApiError) as exc:
            api.get("/v1/doc/x")
        assert "gateway" in str(exc.value)

    def test_truncates_a_huge_error_body(self):
        api, _ = make_client(responder(httpx.Response(400, text="x" * 5000)))
        with pytest.raises(ApiError) as exc:
            api.get("/v1/doc/x")
        assert len(exc.value.api_message) <= 200

    def test_an_unexpected_success_status_is_an_error(self):
        api, _ = make_client(responder(httpx.Response(206, json={})))
        with pytest.raises(ApiError):
            api.get("/v1/doc/x", expected=(200,))


class TestRetries:
    def test_recovers_after_transient_failures(self, no_sleeping):
        api, requests = make_client(
            responder(
                httpx.Response(503),
                httpx.Response(429),
                httpx.Response(200, json={"ok": True}),
            )
        )
        assert api.get("/v1/doc/x") == {"ok": True}
        assert len(requests) == 3
        assert len(no_sleeping) == 2

    def test_gives_up_after_max_retries(self, no_sleeping):
        api, requests = make_client(responder(httpx.Response(503)), max_retries=3)
        with pytest.raises(RetryableApiError):
            api.get("/v1/doc/x")

        assert len(requests) == 4  # the first attempt plus three retries
        assert len(no_sleeping) == 3

    def test_max_retries_zero_means_one_attempt(self):
        api, requests = make_client(responder(httpx.Response(500)), max_retries=0)
        with pytest.raises(RetryableApiError):
            api.get("/v1/doc/x")
        assert len(requests) == 1

    def test_retries_timeouts(self, no_sleeping):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(200, json={"ok": True})

        api, _ = make_client(handler)
        assert api.get("/v1/auth/whoami") == {"ok": True}
        assert calls["n"] == 2

    def test_retries_connection_errors(self, no_sleeping):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={})
        api, _ = make_client(handler)
        api.get("/v1/doc/")
        assert calls["n"] == 2

    def test_a_persistent_timeout_surfaces_as_retryable(self, no_sleeping):
        def handler(request):
            raise httpx.ReadTimeout("timed out", request=request)

        api, _ = make_client(handler, max_retries=2)
        with pytest.raises(RetryableApiError, match="timed out"):
            api.get("/v1/auth/whoami")

    def test_backoff_grows_and_is_capped(self):
        # Jittered, so assert the envelope rather than exact values.
        assert all(0 < _backoff_delay(0) <= 1.0 for _ in range(50))
        assert all(0 < _backoff_delay(3) <= 8.0 for _ in range(50))
        assert all(0 < _backoff_delay(20) <= 30.0 for _ in range(50))

    def test_backoff_is_monotonic_on_average(self):
        early = sum(_backoff_delay(0) for _ in range(200))
        later = sum(_backoff_delay(4) for _ in range(200))
        assert later > early


class TestPutFile:
    def test_puts_bytes_to_an_absolute_url(self):
        api, requests = make_client(responder(httpx.Response(200)))
        api.put_file("https://storage.test/bucket/obj?sig=abc", b"data", "application/pdf")

        request = requests[0]
        assert request.method == "PUT"
        # The presigned host is not the API host; httpx must not rewrite it.
        assert str(request.url) == "https://storage.test/bucket/obj?sig=abc"
        assert request.headers["content-type"] == "application/pdf"
        assert request.content == b"data"

    def test_does_not_leak_the_bearer_token_to_storage(self):
        """The signature is in the URL; storage has no business seeing the JWT."""
        api, requests = make_client(responder(httpx.Response(200)))
        api.put_file("https://storage.test/obj?sig=abc", b"data", "application/pdf")
        assert "authorization" not in requests[0].headers

    def test_accepts_a_file_object(self, tmp_path):
        path = tmp_path / "a.pdf"
        path.write_bytes(b"%PDF-1.4 body")

        api, requests = make_client(responder(httpx.Response(200)))
        with path.open("rb") as handle:
            api.put_file("https://storage.test/obj?sig=abc", handle, "application/pdf")

        assert requests[0].read() == b"%PDF-1.4 body"

    def test_201_is_accepted(self):
        api, _ = make_client(responder(httpx.Response(201)))
        api.put_file("https://storage.test/obj", b"d", "application/pdf")

    def test_rejection_is_an_error(self):
        api, _ = make_client(responder(httpx.Response(403)))
        with pytest.raises(ApiError) as exc:
            api.put_file("https://storage.test/obj", b"d", "application/pdf")
        assert exc.value.status_code == 403
        assert not isinstance(exc.value, RetryableApiError)

    def test_transient_storage_failure_is_retryable(self):
        api, _ = make_client(responder(httpx.Response(503)))
        with pytest.raises(RetryableApiError):
            api.put_file("https://storage.test/obj", b"d", "application/pdf")

    def test_is_not_retried_internally(self):
        """A presigned URL is single use, so recovery means going back to init."""
        api, requests = make_client(responder(httpx.Response(503)), max_retries=5)
        with pytest.raises(RetryableApiError):
            api.put_file("https://storage.test/obj", b"d", "application/pdf")
        assert len(requests) == 1

    def test_timeout_is_retryable(self):
        def handler(request):
            raise httpx.WriteTimeout("too slow", request=request)

        api, _ = make_client(handler)
        with pytest.raises(RetryableApiError, match="upload timed out"):
            api.put_file("https://storage.test/obj", b"d", "application/pdf")

    def test_connection_error_is_retryable(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        api, _ = make_client(handler)
        with pytest.raises(RetryableApiError, match="upload failed"):
            api.put_file("https://storage.test/obj", b"d", "application/pdf")

    def test_timeout_override_is_passed_through(self):
        seen = {}

        def handler(request):
            seen["timeout"] = request.extensions.get("timeout")
            return httpx.Response(200)

        api, _ = make_client(handler)
        api.put_file("https://storage.test/obj", b"d", "application/pdf", timeout=300.0)
        assert seen["timeout"]["read"] == 300.0


class TestLifecycle:
    def test_context_manager_closes_the_transport(self):
        api, _ = make_client(responder(httpx.Response(200, json={})))
        with api as entered:
            assert entered is api
            entered.get("/v1/doc/")
        with pytest.raises(RuntimeError):
            api.get("/v1/doc/")

    def test_base_url_trailing_slash_is_trimmed(self):
        api = ApiClient(f"{BASE_URL}/", StubTokens())
        assert api.base_url == BASE_URL
        api.close()
