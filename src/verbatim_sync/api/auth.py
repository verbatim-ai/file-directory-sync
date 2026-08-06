"""RS512 JWT credentials for the Verbatim API.

The credential is an RSA private key held in a keystore directory outside the
project, as produced by the platform's ``build_keys.py``::

    python build_keys.py --gen-keys --key-id <uuid>
    keys/<uuid>       private key, chmod 600 — never leaves the server
    keys/<uuid>.pub   public key, registered in the backoffice

``build_keys.py`` names the private key after the key's UUID, but that is only
its convention: the filename on disk and the key ID the platform issued are
configured separately (``api.key_filename`` and ``api.key_id``), because a
keystore built by hand is free to call the file ``staging`` or ``prod.pem``.
The ``kid`` header always carries the configured key ID, never the filename.

Claim set, per https://verbatim-ai.gitbook.io/docs/integration/rsa-keys::

    header  {"alg": "RS512", "typ": "JWT", "kid": "<key uuid>"}
    payload {"iss": "verbatim-ai.com", "iat": ..., "exp": ...,
             "oid": "<organization uuid>", "uid": "<optional user id>"}

Note this differs from ``clients/java/verbatim-java-client-auth``, which issues
``iss=verbatim_client`` and carries the organization in ``sub``. The Python
reference implementation and the integration docs agree on ``iss`` and ``oid``,
so those win here.
"""

from __future__ import annotations

import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import jwt

from verbatim_sync.errors import AuthError
from verbatim_sync.logging_setup import get_logger

logger = get_logger("api.auth")

ISSUER = "verbatim-ai.com"
ALGORITHM = "RS512"
MAX_TTL_SECONDS = 24 * 60 * 60

#: Re-sign this many seconds before expiry so a long run never presents a
#: token that lapses mid-request.
_REFRESH_MARGIN_SECONDS = 60


@dataclass(frozen=True)
class Key:
    """An RSA key pair loaded from the keystore.

    ``key_id`` is the platform-issued UUID sent as the JWT ``kid``;
    ``key_filename`` is what the private key is called on disk. They are
    unrelated by design.
    """

    key_id: str
    organization_id: str
    private_key: str
    key_filename: str
    public_key: str | None = None

    @classmethod
    def from_keystore(
        cls,
        keys_dir: str | Path,
        key_filename: str,
        key_id: str,
        organization_id: str,
    ) -> Key:
        """Load the key pair named ``key_filename`` from ``keys_dir``.

        ``key_id`` is the UUID the platform issued, carried in the JWT ``kid``
        header. It is passed in rather than derived, because the filename on
        disk is the operator's choice and need not match.
        """
        directory = Path(keys_dir)
        private_path = directory / key_filename

        try:
            private_key = private_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AuthError(
                f"cannot read private key {private_path}: {exc.strerror}"
            ) from exc

        if "PRIVATE KEY" not in private_key:
            # Pointing at the .pub by mistake is the easy error to make here.
            raise AuthError(
                f"{private_path} does not look like a PEM private key; "
                "api.key_filename must name the private key, not the .pub"
            )

        _warn_if_world_readable(private_path)

        public_key: str | None = None
        public_path = _find_public_key(directory, key_filename)
        if public_path is not None:
            try:
                public_key = public_path.read_text(encoding="utf-8")
            except OSError as exc:
                # The public half is optional; it only powers a local
                # self-check, so a read failure is not fatal.
                logger.debug("Cannot read public key %s: %s", public_path, exc)

        return cls(
            key_id=key_id,
            organization_id=organization_id,
            private_key=private_key,
            key_filename=key_filename,
            public_key=public_key,
        )


def _find_public_key(directory: Path, key_filename: str) -> Path | None:
    """Locate the public half beside the private key, if it is there.

    Accepts both ``<name>.pub`` and ``<stem>.pub`` so a key called ``prod.pem``
    finds ``prod.pub`` as readily as ``prod.pem.pub``.
    """
    candidates = [
        directory / f"{key_filename}.pub",
        directory / f"{Path(key_filename).stem}.pub",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _warn_if_world_readable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:  # pragma: no cover - already read the file successfully
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "Private key %s is readable beyond its owner (mode %o); "
            "run: chmod 600 %s",
            path,
            stat.S_IMODE(mode),
            path,
        )


class TokenProvider:
    """Signs bearer tokens on demand and reuses one until it nears expiry."""

    def __init__(
        self,
        key: Key,
        ttl_minutes: int = 30,
        user_id: str | None = None,
        issuer: str = ISSUER,
    ) -> None:
        ttl_seconds = ttl_minutes * 60
        if ttl_seconds <= 0:
            raise AuthError("token TTL must be positive")
        if ttl_seconds > MAX_TTL_SECONDS:
            raise AuthError("token TTL cannot exceed 24 hours")

        self._key = key
        self._ttl_seconds = ttl_seconds
        self._user_id = user_id
        self._issuer = issuer
        self._token: str | None = None
        self._expires_at: float = 0.0
        # Worker threads share one provider; without this, two of them could
        # interleave and pair a fresh token with a stale expiry.
        self._lock = threading.Lock()

    @property
    def key_id(self) -> str:
        return self._key.key_id

    @property
    def organization_id(self) -> str:
        return self._key.organization_id

    def token(self) -> str:
        with self._lock:
            now = time.time()
            if self._token is None or now >= self._expires_at - _REFRESH_MARGIN_SECONDS:
                self._token = self._sign(now)
                self._expires_at = now + self._ttl_seconds
            return self._token

    def _sign(self, now: float) -> str:
        issued_at = int(now)
        claims: dict[str, object] = {
            "iss": self._issuer,
            "iat": issued_at,
            "exp": issued_at + self._ttl_seconds,
            "oid": self._key.organization_id,
        }
        if self._user_id:
            claims["uid"] = self._user_id

        try:
            return jwt.encode(
                claims,
                self._key.private_key,
                algorithm=ALGORITHM,
                headers={"kid": self._key.key_id},
            )
        except Exception as exc:  # pyjwt wraps a broad range of key errors
            raise AuthError(f"cannot sign token: {exc}") from exc

    def verify_locally(self) -> dict[str, object] | None:
        """Verify a freshly signed token against the local public key.

        Catches a mismatched pair or a corrupt ``.pub`` before any request goes
        out, turning what would be an opaque 403 into a precise error.

        Returns ``None`` when no public key sits beside the private one — the
        check is simply unavailable, which is not a failure. A key pair that
        does not match raises :class:`AuthError`.
        """
        if not self._key.public_key:
            return None
        try:
            return jwt.decode(
                self.token(),
                self._key.public_key,
                algorithms=[ALGORITHM],
                issuer=self._issuer,
            )
        except jwt.PyJWTError as exc:
            raise AuthError(
                f"token does not verify against the public key beside "
                f"{self._key.key_filename}: {exc}"
            ) from exc
