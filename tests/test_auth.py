from __future__ import annotations

import time
from pathlib import Path

import jwt
import pytest

from verbatim_sync.api.auth import ALGORITHM, ISSUER, Key, TokenProvider
from verbatim_sync.errors import AuthError

from conftest import KEY_FILENAME, KEY_ID, ORG_ID


def load_key(keys_dir: Path) -> Key:
    return Key.from_keystore(keys_dir, KEY_FILENAME, KEY_ID, ORG_ID)


class TestKey:
    def test_loads_from_the_keystore(self, keys_dir: Path):
        key = load_key(keys_dir)
        assert key.key_id == KEY_ID
        assert key.key_filename == KEY_FILENAME
        assert key.organization_id == ORG_ID
        assert "PRIVATE KEY" in key.private_key
        assert key.public_key is not None

    def test_key_id_is_independent_of_the_filename(self, keys_dir: Path):
        """The kid is what the platform issued, not what the file is called."""
        key = load_key(keys_dir)
        assert key.key_filename == "staging"
        assert key.key_id == KEY_ID
        assert key.key_id != key.key_filename

    def test_finds_the_public_key_beside_a_suffixed_private_key(
        self, keys_dir: Path, rsa_key_pair
    ):
        """prod.pem should find prod.pub, not just prod.pem.pub."""
        private_pem, public_pem = rsa_key_pair
        (keys_dir / "prod.pem").write_text(private_pem)
        (keys_dir / "prod.pub").write_text(public_pem)

        key = Key.from_keystore(keys_dir, "prod.pem", KEY_ID, ORG_ID)
        assert key.public_key is not None
        assert key.key_id == KEY_ID

    def test_missing_private_key(self, keys_dir: Path):
        with pytest.raises(AuthError, match="cannot read private key"):
            Key.from_keystore(keys_dir, "absent", KEY_ID, ORG_ID)

    def test_rejects_the_public_key_by_mistake(self, keys_dir: Path):
        with pytest.raises(AuthError, match="not the .pub"):
            Key.from_keystore(keys_dir, f"{KEY_FILENAME}.pub", KEY_ID, ORG_ID)

    def test_public_key_is_optional(self, keys_dir: Path):
        (keys_dir / f"{KEY_FILENAME}.pub").unlink()
        key = load_key(keys_dir)
        assert key.public_key is None

    def test_warns_when_world_readable(self, keys_dir: Path, caplog):
        (keys_dir / KEY_FILENAME).chmod(0o644)
        with caplog.at_level("WARNING"):
            load_key(keys_dir)
        assert "readable beyond its owner" in caplog.text


class TestTokenProvider:
    def test_signs_the_documented_claims(self, keys_dir: Path, rsa_key_pair):
        _, public_pem = rsa_key_pair
        token = TokenProvider(load_key(keys_dir), ttl_minutes=30).token()

        header = jwt.get_unverified_header(token)
        assert header["alg"] == ALGORITHM == "RS512"
        assert header["typ"] == "JWT"
        assert header["kid"] == KEY_ID

        claims = jwt.decode(token, public_pem, algorithms=["RS512"], issuer=ISSUER)
        assert claims["iss"] == "verbatim-ai.com"
        assert claims["oid"] == ORG_ID
        assert claims["exp"] - claims["iat"] == 30 * 60

    def test_does_not_carry_a_sub_claim(self, keys_dir: Path, rsa_key_pair):
        """The Java client puts the org in `sub`; the docs and the reference
        Python implementation use `oid` only."""
        _, public_pem = rsa_key_pair
        claims = jwt.decode(
            TokenProvider(load_key(keys_dir)).token(), public_pem, algorithms=["RS512"]
        )
        assert "sub" not in claims

    def test_optional_user_claim(self, keys_dir: Path, rsa_key_pair):
        _, public_pem = rsa_key_pair
        provider = TokenProvider(load_key(keys_dir), ttl_minutes=5, user_id="user_42")
        claims = jwt.decode(provider.token(), public_pem, algorithms=["RS512"])
        assert claims["uid"] == "user_42"

    def test_omits_the_user_claim_when_unset(self, keys_dir: Path, rsa_key_pair):
        _, public_pem = rsa_key_pair
        claims = jwt.decode(
            TokenProvider(load_key(keys_dir)).token(), public_pem, algorithms=["RS512"]
        )
        assert "uid" not in claims

    def test_reuses_a_live_token(self, keys_dir: Path):
        provider = TokenProvider(load_key(keys_dir), ttl_minutes=30)
        assert provider.token() == provider.token()

    def test_resigns_near_expiry(self, keys_dir: Path, monkeypatch):
        provider = TokenProvider(load_key(keys_dir), ttl_minutes=1)
        first = provider.token()

        later = time.time() + 10_000
        monkeypatch.setattr(time, "time", lambda: later)
        assert provider.token() != first

    def test_does_not_resign_mid_life(self, keys_dir: Path, monkeypatch):
        provider = TokenProvider(load_key(keys_dir), ttl_minutes=60)
        first = provider.token()

        halfway = time.time() + 30 * 60
        monkeypatch.setattr(time, "time", lambda: halfway)
        assert provider.token() == first

    def test_rejects_ttl_over_24h(self, keys_dir: Path):
        with pytest.raises(AuthError, match="24 hours"):
            TokenProvider(load_key(keys_dir), ttl_minutes=1441)

    def test_rejects_non_positive_ttl(self, keys_dir: Path):
        with pytest.raises(AuthError):
            TokenProvider(load_key(keys_dir), ttl_minutes=0)

    def test_rejects_a_broken_private_key(self, keys_dir: Path):
        (keys_dir / KEY_FILENAME).write_text(
            "-----BEGIN PRIVATE KEY-----\nnot a key\n-----END PRIVATE KEY-----\n"
        )
        with pytest.raises(AuthError, match="cannot sign token"):
            TokenProvider(load_key(keys_dir)).token()


class TestVerifyLocally:
    def test_accepts_a_matching_pair(self, keys_dir: Path):
        claims = TokenProvider(load_key(keys_dir)).verify_locally()
        assert claims is not None
        assert claims["oid"] == ORG_ID

    def test_returns_none_without_a_public_key(self, keys_dir: Path):
        (keys_dir / f"{KEY_FILENAME}.pub").unlink()
        assert TokenProvider(load_key(keys_dir)).verify_locally() is None

    def test_rejects_a_mismatched_pair(self, keys_dir: Path):
        """A stale .pub would otherwise surface as an opaque 403."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        (keys_dir / f"{KEY_FILENAME}.pub").write_bytes(
            other.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

        with pytest.raises(AuthError, match="does not verify"):
            TokenProvider(load_key(keys_dir)).verify_locally()


class TestTokenProviderConcurrency:
    def test_workers_share_one_consistent_token(self, keys_dir: Path):
        """Without a lock, two threads can pair a fresh token with a stale
        expiry, or sign needlessly."""
        from concurrent.futures import ThreadPoolExecutor

        provider = TokenProvider(load_key(keys_dir), ttl_minutes=30)
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = set(pool.map(lambda _: provider.token(), range(200)))

        assert len(tokens) == 1
