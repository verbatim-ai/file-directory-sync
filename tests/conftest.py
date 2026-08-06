from __future__ import annotations

import itertools
import textwrap
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from verbatim_sync.api.documents import Document, DocumentInit, DocumentStatus
from verbatim_sync.errors import ApiError


@pytest.fixture(scope="session")
def rsa_key_pair() -> tuple[str, str]:
    """A throwaway RSA pair. 2048 bits keeps the test suite fast."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


KEY_ID = "11111111-2222-3333-4444-555555555555"
ORG_ID = "66666666-7777-8888-9999-000000000000"
# Deliberately not the key UUID: the filename and the kid are independent, and
# every test that touches the keystore should prove it.
KEY_FILENAME = "staging"


@pytest.fixture
def keys_dir(tmp_path: Path, rsa_key_pair: tuple[str, str]) -> Path:
    """A keystore whose key filename differs from the key ID."""
    private_pem, public_pem = rsa_key_pair
    directory = tmp_path / "keys"
    directory.mkdir()
    private_path = directory / KEY_FILENAME
    private_path.write_text(private_pem)
    private_path.chmod(0o600)
    (directory / f"{KEY_FILENAME}.pub").write_text(public_pem)
    return directory


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    """A tree exercising every filter branch."""
    root = tmp_path / "docs"
    (root / "nested").mkdir(parents=True)
    (root / "reports").mkdir()

    (root / "a.pdf").write_bytes(b"%PDF-1.4 a")
    (root / "nested" / "b.pdf").write_bytes(b"%PDF-1.4 b")
    (root / "reports" / "big.pdf").write_bytes(b"x" * 5000)  # over a small cap
    (root / "notes.txt").write_text("plain text")
    # .zzz maps to nothing in mimetypes, unlike .bin which resolves to
    # application/octet-stream on most hosts.
    (root / "archive.zzz").write_bytes(b"\x00\x01")
    (root / ".hidden.pdf").write_bytes(b"%PDF-1.4 hidden")
    (root / "empty.pdf").write_bytes(b"")
    return root


@pytest.fixture
def api_section(keys_dir: Path) -> str:
    """The minimal valid [api] block, reused by every config fixture."""
    return textwrap.dedent(
        f"""
        [api]
        organization_id = "{ORG_ID}"
        keys_dir = "{keys_dir}"
        key_filename = "{KEY_FILENAME}"
        key_id = "{KEY_ID}"
        """
    )


@pytest.fixture
def write_config(tmp_path: Path, api_section: str, source_tree: Path):
    """Write a config file, overriding fragments of the default body."""

    def _write(body: str | None = None, name: str = "sync.toml") -> Path:
        if body is None:
            body = (
                textwrap.dedent(
                    f"""
                    [source]
                    root_dir = "{source_tree}"
                    exclude = ["**/.*"]

                    [corpus]
                    id = "550e8400-e29b-41d4-a716-446655440001"

                    [filters]
                    content_types = ["application/pdf"]
                    max_file_size = "1KiB"
                    min_file_size = "1B"

                    [database]
                    path = "./state/sync.db"

                    [logging]
                    file = "./logs/sync.log"
                    console = false
                    """
                )
                + api_section
            )
        path = tmp_path / name
        path.write_text(body)
        return path

    return _write


class FakeBackend:
    """In-memory stand-in for a corpus, matching the DocumentsApi surface.

    Models the parts of the real flow the engine depends on: a document is
    only ingestible after its bytes have been PUT, commit is what queues
    ingestion, and content already in the corpus is rejected as a duplicate.
    """

    def __init__(self, corpus_id: str) -> None:
        self.corpus_id = corpus_id
        self.documents: dict[str, dict[str, Any]] = {}
        self.blobs: dict[str, bytes] = {}  # upload_url -> bytes
        self.calls: list[tuple[str, str]] = []
        self._ids = itertools.count(1)
        self._urls = itertools.count(1)
        # The engine drives this from a worker pool, so hand out ids and
        # record calls under a lock rather than relying on CPython atomicity.
        self._lock = threading.Lock()

        # Knobs for the failure paths.
        self.detect_duplicates = False
        self.fail_ingestion_for: set[str] = set()
        self.url_ttl = timedelta(minutes=15)
        self.reinit_error: int | None = None

    # -- helpers ---------------------------------------------------------

    def _doc(self, document_id: str) -> dict[str, Any]:
        if document_id not in self.documents:
            raise ApiError(f"document {document_id} not found", status_code=404)
        return self.documents[document_id]

    def _as_document(self, data: dict[str, Any]) -> Document:
        return Document.from_json(data)

    def _new_upload_url(self, document_id: str) -> tuple[str, str]:
        with self._lock:
            index = next(self._urls)
        url = f"https://storage.example/{document_id}/{index}?sig=secret"
        expires = (datetime.now(UTC) + self.url_ttl).isoformat().replace("+00:00", "Z")
        return url, expires

    def content_of(self, document_id: str) -> bytes | None:
        return self.documents[document_id].get("_content")

    def metadata_of(self, document_id: str) -> dict[str, Any]:
        return self.documents[document_id].get("metadata") or {}

    # -- DocumentsApi surface --------------------------------------------

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
        with self._lock:
            document_id = f"doc-{next(self._ids):04d}"
            self.calls.append(("init", document_id))
        url, expires = self._new_upload_url(document_id)
        self.documents[document_id] = {
            "id": document_id,
            "corpusId": corpus_id,
            "filename": filename,
            "contentType": content_type,
            "status": "AWAITING_UPLOAD",
            "lang": lang,
            "provider": provider,
            "docCreate": doc_create,
            "docUpdate": doc_update,
            "metadata": dict(metadata or {}),
            "_url": url,
        }
        return DocumentInit.from_json(
            {
                "document": {
                    k: v for k, v in self.documents[document_id].items()
                    if not k.startswith("_")
                },
                "uploadUrl": url,
                "expiresAt": expires,
            }
        )

    def reinit_upload(self, document_id: str) -> DocumentInit:
        self.calls.append(("reinit", document_id))
        if self.reinit_error is not None:
            raise ApiError("re-init refused", status_code=self.reinit_error)
        data = self._doc(document_id)
        if data["status"] not in ("READY", "FAILED"):
            raise ApiError("not replaceable", status_code=409)
        url, expires = self._new_upload_url(document_id)
        data.update(status="AWAITING_UPLOAD", _url=url)
        data.pop("size", None)
        return DocumentInit.from_json(
            {
                "document": {
                    k: v for k, v in data.items() if not k.startswith("_")
                },
                "uploadUrl": url,
                "expiresAt": expires,
            }
        )

    def upload_content(
        self,
        upload_url: str,
        data: Any,
        content_type: str,
        timeout: float | None = None,
    ) -> None:
        self.calls.append(("put", upload_url))
        payload = data.read() if hasattr(data, "read") else data
        self.blobs[upload_url] = payload

    def commit(self, document_id: str) -> Document:
        self.calls.append(("commit", document_id))
        data = self._doc(document_id)
        content = self.blobs.get(data["_url"])
        if content is None:
            raise ApiError("nothing was uploaded", status_code=400)

        if self.detect_duplicates:
            for other_id, other in self.documents.items():
                if other_id != document_id and other.get("_content") == content:
                    raise ApiError("duplicate content", status_code=409)

        data["_content"] = content
        data["size"] = len(content)
        data["status"] = (
            "FAILED" if document_id in self.fail_ingestion_for else "READY"
        )
        return self._as_document(
            {k: v for k, v in data.items() if not k.startswith("_")}
        )

    def status(self, document_id: str) -> DocumentStatus:
        self.calls.append(("status", document_id))
        data = self._doc(document_id)
        return DocumentStatus.from_json(
            {
                "id": document_id,
                "status": data["status"],
                "statusMsg": "ingestion failed" if data["status"] == "FAILED" else None,
            }
        )

    def get(self, document_id: str) -> Document:
        return self._as_document(
            {k: v for k, v in self._doc(document_id).items() if not k.startswith("_")}
        )

    def delete(self, document_id: str) -> None:
        self.calls.append(("delete", document_id))
        self._doc(document_id)
        del self.documents[document_id]

    def iter_all(
        self, corpus_id: str, *, status: str | None = None, page_size: int = 50
    ) -> list[Document]:
        return [
            self._as_document({k: v for k, v in d.items() if not k.startswith("_")})
            for d in self.documents.values()
            if d["corpusId"] == corpus_id
        ]

    def accepted_content_types(self) -> list[str]:
        return ["application/pdf", "text/html", "text/plain"]

    def whoami(self) -> dict[str, Any]:
        return {"organizationId": ORG_ID}


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(CORPUS_ID)


CORPUS_ID = "550e8400-e29b-41d4-a716-446655440001"


# --------------------------------------------------------------------------
# HTTP-level helpers: exercise the real ApiClient without leaving the process.
# --------------------------------------------------------------------------


class StubTokens:
    """Stands in for TokenProvider without paying for RSA signing."""

    def __init__(self, token: str = "signed.jwt.value") -> None:
        self._token = token
        self.calls = 0

    def token(self) -> str:
        self.calls += 1
        return self._token


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """No test may actually wait; record the delays instead."""
    import time as _time

    delays: list[float] = []
    monkeypatch.setattr(_time, "sleep", delays.append)
    return delays


def responder(*responses):
    """Return each response in turn, repeating the last one."""
    queue = list(responses)

    def handler(request):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler


API_BASE_URL = "https://api.verbatim-ai.test"


def make_api_client(handler, *, max_retries: int = 5, tokens: StubTokens | None = None):
    """An ApiClient whose transport is a recording MockTransport."""
    import httpx

    from verbatim_sync.api.client import ApiClient

    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    api = ApiClient(
        API_BASE_URL,
        tokens or StubTokens(),
        max_retries=max_retries,
        client=httpx.Client(
            base_url=API_BASE_URL, transport=httpx.MockTransport(recording)
        ),
    )
    return api, requests
