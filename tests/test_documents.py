"""Tests for the document endpoint wrappers.

These assert the exact wire shape — paths, verbs and camelCase field names —
because a wrong key here fails silently in production and nowhere else: the
fake backend used by the sync tests accepts whatever it is handed.
"""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import make_api_client, responder

from verbatim_sync.api.documents import (
    Document,
    DocumentInit,
    DocumentsApi,
    DocumentStatus,
)
from verbatim_sync.errors import ApiError

CORPUS = "550e8400-e29b-41d4-a716-446655440001"
DOC_ID = "550e8400-e29b-41d4-a716-446655440000"

DOCUMENT_JSON = {
    "id": DOC_ID,
    "corpusId": CORPUS,
    "filename": "annual-report-2025.pdf",
    "contentType": "application/pdf",
    "status": "READY",
    "size": 84213,
    "lang": "fr",
    "provider": "file-directory-sync",
    "metadata": {"sync_fullpath": "/data/documents/annual-report-2025.pdf"},
    "docCreate": "2026-01-15T10:30:00Z",
    "docUpdate": "2026-04-01T08:00:00Z",
    "createdAt": "2026-04-23T04:06:51Z",
    "updatedAt": "2026-04-23T04:07:12Z",
    "tokens": 1200,
    "nbWords": 8000,
}


def make_documents(handler, **kwargs):
    api, requests = make_api_client(handler, **kwargs)
    return DocumentsApi(api), requests


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


class TestInitUpload:
    def test_posts_the_required_fields(self):
        documents, requests = make_documents(
            responder(
                httpx.Response(
                    200,
                    json={
                        "document": DOCUMENT_JSON,
                        "uploadUrl": "https://storage.test/obj?sig=abc",
                        "expiresAt": "2026-04-23T04:21:51Z",
                    },
                )
            )
        )
        result = documents.init_upload(
            corpus_id=CORPUS, filename="a.pdf", content_type="application/pdf"
        )

        request = requests[0]
        assert request.method == "POST"
        assert request.url.path == "/v1/doc/init"
        assert body_of(request) == {
            "corpusId": CORPUS,
            "filename": "a.pdf",
            "contentType": "application/pdf",
        }

        assert isinstance(result, DocumentInit)
        assert result.upload_url == "https://storage.test/obj?sig=abc"
        assert result.expires_at == "2026-04-23T04:21:51Z"
        assert result.document.id == DOC_ID

    def test_includes_every_optional_field(self):
        documents, requests = make_documents(
            responder(
                httpx.Response(
                    200, json={"document": DOCUMENT_JSON, "uploadUrl": "https://s/o"}
                )
            )
        )
        documents.init_upload(
            corpus_id=CORPUS,
            filename="a.pdf",
            content_type="application/pdf",
            lang="fr",
            provider="file-directory-sync",
            doc_create="2026-01-15T10:30:00Z",
            doc_update="2026-04-01T08:00:00Z",
            metadata={"sync_fullpath": "/data/a.pdf"},
        )

        assert body_of(requests[0]) == {
            "corpusId": CORPUS,
            "filename": "a.pdf",
            "contentType": "application/pdf",
            "lang": "fr",
            "provider": "file-directory-sync",
            "docCreate": "2026-01-15T10:30:00Z",
            "docUpdate": "2026-04-01T08:00:00Z",
            "metadata": {"sync_fullpath": "/data/a.pdf"},
        }

    def test_omits_unset_optional_fields(self):
        """Sending nulls would overwrite platform defaults such as lang."""
        documents, requests = make_documents(
            responder(
                httpx.Response(
                    200, json={"document": DOCUMENT_JSON, "uploadUrl": "https://s/o"}
                )
            )
        )
        documents.init_upload(
            corpus_id=CORPUS, filename="a.pdf", content_type="application/pdf", lang=None
        )
        assert set(body_of(requests[0])) == {"corpusId", "filename", "contentType"}

    def test_missing_expires_at_is_tolerated(self):
        documents, _ = make_documents(
            responder(
                httpx.Response(
                    200, json={"document": DOCUMENT_JSON, "uploadUrl": "https://s/o"}
                )
            )
        )
        result = documents.init_upload(
            corpus_id=CORPUS, filename="a.pdf", content_type="application/pdf"
        )
        assert result.expires_at is None

    def test_415_surfaces_the_reason(self):
        documents, _ = make_documents(
            responder(httpx.Response(415, json={"message": "type not accepted"}))
        )
        with pytest.raises(ApiError) as exc:
            documents.init_upload(
                corpus_id=CORPUS, filename="a.zip", content_type="application/zip"
            )
        assert exc.value.status_code == 415
        assert "type not accepted" in str(exc.value)


class TestReinitUpload:
    def test_puts_to_the_document_init_path(self):
        documents, requests = make_documents(
            responder(
                httpx.Response(
                    200,
                    json={"document": DOCUMENT_JSON, "uploadUrl": "https://storage/2"},
                )
            )
        )
        result = documents.reinit_upload(DOC_ID)

        assert requests[0].method == "PUT"
        assert requests[0].url.path == f"/v1/doc/{DOC_ID}/init"
        assert result.upload_url == "https://storage/2"

    def test_409_when_not_replaceable(self):
        documents, _ = make_documents(responder(httpx.Response(409, json="in flight")))
        with pytest.raises(ApiError) as exc:
            documents.reinit_upload(DOC_ID)
        assert exc.value.status_code == 409


class TestCommit:
    def test_posts_to_the_commit_path(self):
        documents, requests = make_documents(
            responder(httpx.Response(202, json={**DOCUMENT_JSON, "status": "PROCESSING"}))
        )
        document = documents.commit(DOC_ID)

        assert requests[0].method == "POST"
        assert requests[0].url.path == f"/v1/doc/{DOC_ID}/commit"
        assert document.status == "PROCESSING"

    def test_accepts_200_for_an_already_ready_document(self):
        """Commit is documented as idempotent once READY."""
        documents, _ = make_documents(responder(httpx.Response(200, json=DOCUMENT_JSON)))
        assert documents.commit(DOC_ID).status == "READY"

    def test_409_on_duplicate_content(self):
        documents, _ = make_documents(
            responder(httpx.Response(409, json={"message": "duplicate"}))
        )
        with pytest.raises(ApiError) as exc:
            documents.commit(DOC_ID)
        assert exc.value.status_code == 409


class TestStatus:
    def test_gets_the_status_path(self):
        documents, requests = make_documents(
            responder(
                httpx.Response(
                    200,
                    json={
                        "id": DOC_ID,
                        "status": "PROCESSING",
                        "statusMsg": None,
                        "updatedAt": "2026-04-23T04:07:12Z",
                    },
                )
            )
        )
        status = documents.status(DOC_ID)

        assert requests[0].url.path == f"/v1/doc/{DOC_ID}/status"
        assert isinstance(status, DocumentStatus)
        assert status.status == "PROCESSING"
        assert status.is_terminal is False
        assert status.updated_at == "2026-04-23T04:07:12Z"

    @pytest.mark.parametrize(
        ("status", "terminal"),
        [
            ("AWAITING_UPLOAD", False),
            ("PENDING", False),
            ("PROCESSING", False),
            ("READY", True),
            ("FAILED", True),
        ],
    )
    def test_terminal_statuses(self, status, terminal):
        documents, _ = make_documents(
            responder(httpx.Response(200, json={"id": DOC_ID, "status": status}))
        )
        assert documents.status(DOC_ID).is_terminal is terminal

    def test_carries_the_failure_reason(self):
        documents, _ = make_documents(
            responder(
                httpx.Response(
                    200,
                    json={"id": DOC_ID, "status": "FAILED", "statusMsg": "bad PDF"},
                )
            )
        )
        assert documents.status(DOC_ID).status_msg == "bad PDF"


class TestGetAndDelete:
    def test_get(self):
        documents, requests = make_documents(
            responder(httpx.Response(200, json=DOCUMENT_JSON))
        )
        document = documents.get(DOC_ID)

        assert requests[0].method == "GET"
        assert requests[0].url.path == f"/v1/doc/{DOC_ID}"
        assert document.filename == "annual-report-2025.pdf"

    @pytest.mark.parametrize("status", [200, 202, 204])
    def test_delete_accepts_each_success_status(self, status):
        documents, requests = make_documents(responder(httpx.Response(status)))
        documents.delete(DOC_ID)

        assert requests[0].method == "DELETE"
        assert requests[0].url.path == f"/v1/doc/{DOC_ID}"

    def test_delete_404_is_an_error_for_the_caller_to_interpret(self):
        documents, _ = make_documents(responder(httpx.Response(404, json={})))
        with pytest.raises(ApiError) as exc:
            documents.delete(DOC_ID)
        assert exc.value.status_code == 404


class TestPatch:
    def test_sends_only_the_fields_given(self):
        documents, requests = make_documents(
            responder(httpx.Response(200, json=DOCUMENT_JSON))
        )
        documents.patch(DOC_ID, filename="renamed.pdf")

        assert requests[0].method == "PATCH"
        assert requests[0].url.path == f"/v1/doc/{DOC_ID}"
        assert body_of(requests[0]) == {"filename": "renamed.pdf"}

    def test_sends_every_editable_field(self):
        documents, requests = make_documents(
            responder(httpx.Response(200, json=DOCUMENT_JSON))
        )
        documents.patch(
            DOC_ID,
            filename="a.pdf",
            doc_create="2026-01-15T10:30:00Z",
            doc_update="2026-04-01T08:00:00Z",
            metadata={"team": "legal"},
        )
        assert body_of(requests[0]) == {
            "filename": "a.pdf",
            "docCreate": "2026-01-15T10:30:00Z",
            "docUpdate": "2026-04-01T08:00:00Z",
            "metadata": {"team": "legal"},
        }

    def test_an_empty_metadata_map_is_still_sent(self):
        """metadata replaces rather than merges, so {} means 'clear it'."""
        documents, requests = make_documents(
            responder(httpx.Response(200, json=DOCUMENT_JSON))
        )
        documents.patch(DOC_ID, metadata={})
        assert body_of(requests[0]) == {"metadata": {}}


class TestList:
    def test_sends_the_pagination_parameters(self):
        documents, requests = make_documents(
            responder(
                httpx.Response(
                    200, json={"corpusId": CORPUS, "pageIndex": 0, "items": [DOCUMENT_JSON]}
                )
            )
        )
        result = documents.list(CORPUS, status="READY", page_size=10, page_index=2)

        query = requests[0].url.params
        assert requests[0].url.path == "/v1/doc/"
        assert query["corpusId"] == CORPUS
        assert query["status"] == "READY"
        assert query["pageSize"] == "10"
        assert query["pageIndex"] == "2"
        assert [d.id for d in result] == [DOC_ID]

    def test_omits_the_status_filter_when_unset(self):
        documents, requests = make_documents(
            responder(httpx.Response(200, json={"items": []}))
        )
        documents.list(CORPUS)
        assert "status" not in requests[0].url.params

    def test_an_empty_page(self):
        documents, _ = make_documents(responder(httpx.Response(200, json={"items": []})))
        assert documents.list(CORPUS) == []

    def test_a_body_without_items(self):
        documents, _ = make_documents(responder(httpx.Response(200, json={})))
        assert documents.list(CORPUS) == []


class TestIterAll:
    def test_pages_until_a_short_page(self):
        def page(count, start):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {**DOCUMENT_JSON, "id": f"doc-{start + i}"} for i in range(count)
                    ]
                },
            )

        documents, requests = make_documents(responder(page(2, 0), page(2, 2), page(1, 4)))
        result = documents.iter_all(CORPUS, page_size=2)

        assert [d.id for d in result] == ["doc-0", "doc-1", "doc-2", "doc-3", "doc-4"]
        assert [r.url.params["pageIndex"] for r in requests] == ["0", "1", "2"]

    def test_a_single_short_page_stops_immediately(self):
        documents, requests = make_documents(
            responder(httpx.Response(200, json={"items": [DOCUMENT_JSON]}))
        )
        assert len(documents.iter_all(CORPUS, page_size=50)) == 1
        assert len(requests) == 1

    def test_an_empty_corpus(self):
        documents, requests = make_documents(
            responder(httpx.Response(200, json={"items": []}))
        )
        assert documents.iter_all(CORPUS) == []
        assert len(requests) == 1

    def test_passes_the_status_filter_through(self):
        documents, requests = make_documents(
            responder(httpx.Response(200, json={"items": []}))
        )
        documents.iter_all(CORPUS, status="FAILED")
        assert requests[0].url.params["status"] == "FAILED"


class TestAcceptAndWhoami:
    def test_accepted_content_types_are_lowercased(self):
        documents, requests = make_documents(
            responder(httpx.Response(200, json=["Application/PDF", "TEXT/HTML"]))
        )
        assert documents.accepted_content_types() == ["application/pdf", "text/html"]
        assert requests[0].url.path == "/v1/doc/accept"

    def test_a_non_list_body_is_not_fatal(self):
        documents, _ = make_documents(responder(httpx.Response(200, json={})))
        assert documents.accepted_content_types() == []

    def test_whoami(self):
        documents, requests = make_documents(
            responder(httpx.Response(200, json={"organizationId": "org-1"}))
        )
        assert documents.whoami() == {"organizationId": "org-1"}
        assert requests[0].url.path == "/v1/auth/whoami"

    def test_whoami_with_an_unexpected_body(self):
        documents, _ = make_documents(responder(httpx.Response(200, json="hello")))
        assert documents.whoami() == {}


class TestDocumentParsing:
    def test_parses_every_documented_field(self):
        document = Document.from_json(DOCUMENT_JSON)

        assert document.id == DOC_ID
        assert document.corpus_id == CORPUS
        assert document.content_type == "application/pdf"
        assert document.status == "READY"
        assert document.size == 84213
        assert document.lang == "fr"
        assert document.metadata["sync_fullpath"].endswith("annual-report-2025.pdf")
        assert document.doc_create == "2026-01-15T10:30:00Z"
        assert document.created_at == "2026-04-23T04:06:51Z"
        # The raw payload is kept so new server fields are not lost.
        assert document.raw["nbWords"] == 8000

    def test_tolerates_a_minimal_payload(self):
        """Only id is truly required by the parser; the rest may be absent."""
        document = Document.from_json({"id": DOC_ID})

        assert document.id == DOC_ID
        assert document.corpus_id == ""
        assert document.size is None
        assert document.metadata is None


class TestUploadContent:
    def test_delegates_to_the_transport(self):
        documents, requests = make_documents(responder(httpx.Response(200)))
        documents.upload_content(
            "https://storage.test/obj?sig=abc", b"bytes", "application/pdf"
        )

        assert requests[0].method == "PUT"
        assert requests[0].headers["content-type"] == "application/pdf"
        assert requests[0].content == b"bytes"

    def test_passes_the_timeout_through(self):
        seen = {}

        def handler(request):
            seen["timeout"] = request.extensions.get("timeout")
            return httpx.Response(200)

        documents, _ = make_documents(handler)
        documents.upload_content("https://s/o", b"d", "application/pdf", timeout=120.0)
        assert seen["timeout"]["read"] == 120.0
