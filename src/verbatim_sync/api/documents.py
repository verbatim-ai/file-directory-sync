"""Typed wrappers over the corpus/document endpoints this job needs.

Upload is a three-step flow:

    1. POST /v1/doc/init          -> document (AWAITING_UPLOAD) + presigned URL
    2. PUT  <uploadUrl>           -> bytes go straight to storage
    3. POST /v1/doc/{id}/commit   -> ingestion queued, document -> PROCESSING

then poll GET /v1/doc/{id}/status until READY or FAILED.

Replacing the content of an existing document uses PUT /v1/doc/{id}/init in
place of step 1, keeping the same document id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from verbatim_sync.api.client import ApiClient


@dataclass(frozen=True)
class Document:
    id: str
    corpus_id: str
    filename: str
    content_type: str
    status: str
    size: int | None = None
    lang: str | None = None
    provider: str | None = None
    metadata: dict[str, Any] | None = None
    doc_create: str | None = None
    doc_update: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    raw: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Document:
        return cls(
            id=data["id"],
            corpus_id=data.get("corpusId", ""),
            filename=data.get("filename", ""),
            content_type=data.get("contentType", ""),
            status=data.get("status", ""),
            size=data.get("size"),
            lang=data.get("lang"),
            provider=data.get("provider"),
            metadata=data.get("metadata"),
            doc_create=data.get("docCreate"),
            doc_update=data.get("docUpdate"),
            created_at=data.get("createdAt"),
            updated_at=data.get("updatedAt"),
            raw=data,
        )


@dataclass(frozen=True)
class DocumentInit:
    """Result of an init call: where to PUT the bytes, and until when."""

    document: Document
    upload_url: str
    expires_at: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DocumentInit:
        return cls(
            document=Document.from_json(data["document"]),
            upload_url=data["uploadUrl"],
            expires_at=data.get("expiresAt"),
        )


@dataclass(frozen=True)
class DocumentStatus:
    id: str
    status: str
    status_msg: str | None = None
    updated_at: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in ("READY", "FAILED")

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DocumentStatus:
        return cls(
            id=data.get("id", ""),
            status=data.get("status", ""),
            status_msg=data.get("statusMsg"),
            updated_at=data.get("updatedAt"),
        )


class DocumentsApi:
    def __init__(self, client: ApiClient) -> None:
        self._client = client

    def accepted_content_types(self) -> list[str]:
        """MIME types the platform will ingest (``GET /v1/doc/accept``)."""
        body = self._client.get("/v1/doc/accept", expected=(200,))
        return [str(item).lower() for item in body] if isinstance(body, list) else []

    def whoami(self) -> dict[str, Any]:
        body = self._client.get("/v1/auth/whoami", expected=(200,))
        return body if isinstance(body, dict) else {}

    def init_upload(
        self,
        *,
        corpus_id: str,
        filename: str,
        content_type: str,
        lang: str | None = None,
        provider: str | None = None,
        doc_create: str | None = None,
        doc_update: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentInit:
        payload: dict[str, Any] = {
            "corpusId": corpus_id,
            "filename": filename,
            "contentType": content_type,
        }
        if lang:
            payload["lang"] = lang
        if provider:
            payload["provider"] = provider
        if doc_create:
            payload["docCreate"] = doc_create
        if doc_update:
            payload["docUpdate"] = doc_update
        if metadata:
            payload["metadata"] = metadata

        body = self._client.post("/v1/doc/init", json_body=payload, expected=(200, 201))
        return DocumentInit.from_json(body)

    def reinit_upload(self, document_id: str) -> DocumentInit:
        """Replace an existing document's content, keeping its id.

        The document must be READY or FAILED; anything else is a 409. Its
        embeddings, summary and derived counters are dropped, and it returns to
        AWAITING_UPLOAD with a fresh presigned URL.
        """
        body = self._client.put(f"/v1/doc/{document_id}/init", expected=(200, 201))
        return DocumentInit.from_json(body)

    def upload_content(
        self,
        upload_url: str,
        data: Any,
        content_type: str,
        timeout: float | None = None,
    ) -> None:
        """Push the bytes. ``data`` may be bytes or a readable file object."""
        self._client.put_file(upload_url, data, content_type, timeout=timeout)

    def commit(self, document_id: str) -> Document:
        """Queue ingestion for an uploaded document.

        The server validates size, content type, and rejects a duplicate of
        content already present in the corpus (409). Idempotent once READY.
        """
        body = self._client.post(f"/v1/doc/{document_id}/commit", expected=(200, 202))
        return Document.from_json(body)

    def status(self, document_id: str) -> DocumentStatus:
        body = self._client.get(f"/v1/doc/{document_id}/status", expected=(200,))
        return DocumentStatus.from_json(body)

    def get(self, document_id: str) -> Document:
        body = self._client.get(f"/v1/doc/{document_id}", expected=(200,))
        return Document.from_json(body)

    def delete(self, document_id: str) -> None:
        self._client.delete(f"/v1/doc/{document_id}", expected=(200, 202, 204))

    def patch(
        self,
        document_id: str,
        *,
        filename: str | None = None,
        doc_create: str | None = None,
        doc_update: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        """Update descriptive attributes without re-triggering ingestion.

        ``metadata`` *replaces* the stored map — merge client-side first if
        existing keys must survive.
        """
        payload: dict[str, Any] = {}
        if filename is not None:
            payload["filename"] = filename
        if doc_create is not None:
            payload["docCreate"] = doc_create
        if doc_update is not None:
            payload["docUpdate"] = doc_update
        if metadata is not None:
            payload["metadata"] = metadata

        body = self._client.patch(
            f"/v1/doc/{document_id}", json_body=payload, expected=(200,)
        )
        return Document.from_json(body)

    def list(
        self,
        corpus_id: str,
        *,
        status: str | None = None,
        page_size: int = 25,
        page_index: int = 0,
    ) -> list[Document]:
        body = self._client.get(
            "/v1/doc/",
            params={
                "corpusId": corpus_id,
                "status": status,
                "pageSize": page_size,
                "pageIndex": page_index,
            },
            expected=(200,),
        )
        items = body.get("items", []) if isinstance(body, dict) else []
        return [Document.from_json(item) for item in items]

    def iter_all(
        self, corpus_id: str, *, status: str | None = None, page_size: int = 50
    ) -> list[Document]:
        """Every document in the corpus, paging until a short page comes back."""
        documents: list[Document] = []
        page_index = 0
        while True:
            page = self.list(
                corpus_id, status=status, page_size=page_size, page_index=page_index
            )
            documents.extend(page)
            if len(page) < page_size:
                return documents
            page_index += 1
