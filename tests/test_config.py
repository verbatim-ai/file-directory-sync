from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from verbatim_sync.config import load_config, parse_size
from verbatim_sync.errors import ConfigError

from conftest import KEY_FILENAME, KEY_ID, ORG_ID


class TestParseSize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1024, 1024),
            ("512", 512),
            ("1B", 1),
            ("50MB", 50_000_000),
            ("50MiB", 52_428_800),
            ("1GB", 1_000_000_000),
            ("1GiB", 1_073_741_824),
            ("10 MiB", 10 * 2**20),
            ("1.5MB", 1_500_000),
            ("2kb", 2000),
        ],
    )
    def test_accepts_si_and_iec_units(self, value, expected):
        assert parse_size(value, "filters.max_file_size") == expected

    @pytest.mark.parametrize("value", ["50 potatoes", "MB", "", "-", True, None, 1.5])
    def test_rejects_garbage(self, value):
        with pytest.raises(ConfigError):
            parse_size(value, "filters.max_file_size")

    def test_rejects_negative(self):
        with pytest.raises(ConfigError):
            parse_size(-1, "filters.max_file_size")


class TestLoadConfig:
    def test_loads_a_valid_file(self, write_config, source_tree, keys_dir):
        config = load_config(write_config())

        assert config.source.root_dir == source_tree
        assert config.corpus.id == "550e8400-e29b-41d4-a716-446655440001"
        assert config.filters.content_types == ("application/pdf",)
        assert config.filters.max_file_size == 1024
        assert config.api.base_url == "https://api.verbatim-ai.com"
        assert config.api.organization_id == ORG_ID
        assert config.api.keys_dir == keys_dir
        assert config.api.key_filename == KEY_FILENAME
        assert config.api.key_id == KEY_ID

    def test_resolves_paths_against_the_config_file_not_the_cwd(
        self, write_config, tmp_path, monkeypatch
    ):
        config_path = write_config()
        # cron gives the job an arbitrary working directory.
        monkeypatch.chdir(tmp_path.parent)
        config = load_config(config_path)

        assert config.database.path == (tmp_path / "state" / "sync.db")
        assert config.logging.file == (tmp_path / "logs" / "sync.log")

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nope.toml")

    def test_invalid_toml(self, tmp_path):
        path = tmp_path / "bad.toml"
        path.write_text("this is = = not toml")
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(path)

    def test_missing_required_section(self, write_config):
        with pytest.raises(ConfigError) as exc:
            load_config(write_config('[corpus]\nid = "x"\n'))
        assert exc.value.key == "source"

    def test_api_section_is_required(self, write_config, source_tree):
        body = textwrap.dedent(
            f"""
            [source]
            root_dir = "{source_tree}"
            [corpus]
            id = "550e8400-e29b-41d4-a716-446655440001"
            """
        )
        with pytest.raises(ConfigError) as exc:
            load_config(write_config(body))
        assert exc.value.key == "api"

    def test_rejects_non_uuid_corpus_id(self, write_config, source_tree, api_section):
        body = textwrap.dedent(
            f"""
            [source]
            root_dir = "{source_tree}"
            [corpus]
            id = "not-a-uuid"
            """
        ) + api_section
        with pytest.raises(ConfigError) as exc:
            load_config(write_config(body))
        assert exc.value.key == "corpus.id"

    def test_rejects_missing_root_dir(self, write_config, tmp_path, api_section):
        body = textwrap.dedent(
            f"""
            [source]
            root_dir = "{tmp_path / "absent"}"
            [corpus]
            id = "550e8400-e29b-41d4-a716-446655440001"
            """
        ) + api_section
        with pytest.raises(ConfigError) as exc:
            load_config(write_config(body))
        assert exc.value.key == "source.root_dir"

    def test_rejects_unknown_keys(self, write_config, source_tree, api_section):
        """A typo silently disabling a filter is the worst failure mode here."""
        body = textwrap.dedent(
            f"""
            [source]
            root_dir = "{source_tree}"
            [corpus]
            id = "550e8400-e29b-41d4-a716-446655440001"
            [filters]
            content_type = ["application/pdf"]
            """
        ) + api_section
        with pytest.raises(ConfigError, match="unknown key"):
            load_config(write_config(body))

    def test_rejects_max_below_min(self, write_config, source_tree, api_section):
        body = textwrap.dedent(
            f"""
            [source]
            root_dir = "{source_tree}"
            [corpus]
            id = "550e8400-e29b-41d4-a716-446655440001"
            [filters]
            max_file_size = "1B"
            min_file_size = "1MB"
            """
        ) + api_section
        with pytest.raises(ConfigError) as exc:
            load_config(write_config(body))
        assert exc.value.key == "filters.max_file_size"

    def test_rejects_logging_fully_disabled(self, write_config, source_tree, api_section):
        body = textwrap.dedent(
            f"""
            [source]
            root_dir = "{source_tree}"
            [corpus]
            id = "550e8400-e29b-41d4-a716-446655440001"
            [logging]
            console = false
            """
        ) + api_section
        with pytest.raises(ConfigError, match="fully disabled"):
            load_config(write_config(body))

    def test_example_config_is_structurally_valid(
        self, tmp_path, source_tree, keys_dir
    ):
        """Guard against the shipped example drifting from the loader."""
        example = Path(__file__).parents[1] / "config.example.toml"
        body = example.read_text()
        body = body.replace('root_dir = "/data/documents"', f'root_dir = "{source_tree}"')
        body = body.replace('keys_dir = "/etc/verbatim/keys"', f'keys_dir = "{keys_dir}"')
        body = body.replace(
            f'key_filename = "{KEY_ID}"', f'key_filename = "{KEY_FILENAME}"'
        )
        path = tmp_path / "example.toml"
        path.write_text(body)

        config = load_config(path)
        assert config.filters.max_file_size == 50_000_000
        assert config.logging.rotate_max_bytes == 10 * 2**20
        assert config.sync.delete_remote_when_missing is True
        # The example ships the same placeholder UUIDs as the test keystore.
        assert config.api.organization_id == ORG_ID
        assert config.api.key_id == KEY_ID


class TestApiSection:
    def _config(self, write_config, source_tree, api_body: str) -> Path:
        return write_config(
            textwrap.dedent(
                f"""
                [source]
                root_dir = "{source_tree}"
                [corpus]
                id = "550e8400-e29b-41d4-a716-446655440001"
                """
            )
            + textwrap.dedent(api_body)
        )

    def test_timeout_defaults_to_5000ms(self, write_config):
        config = load_config(write_config())
        assert config.api.timeout_ms == 5000
        assert config.api.timeout_seconds == 5.0

    def test_timeout_is_converted_to_seconds(self, write_config, source_tree, keys_dir):
        config = load_config(
            self._config(
                write_config,
                source_tree,
                f"""
                [api]
                organization_id = "{ORG_ID}"
                keys_dir = "{keys_dir}"
                key_filename = "{KEY_FILENAME}"
                key_id = "{KEY_ID}"
                timeout_ms = 250
                """,
            )
        )
        assert config.api.timeout_ms == 250
        assert config.api.timeout_seconds == 0.25

    def test_derives_key_paths(self, write_config, keys_dir):
        config = load_config(write_config())
        assert config.api.key_id == KEY_ID
        assert config.api.key_filename == KEY_FILENAME
        # The kid is configured, never inferred from the filename.
        assert config.api.key_id != config.api.key_filename
        assert config.api.private_key_path == keys_dir / KEY_FILENAME
        assert config.api.public_key_path == keys_dir / f"{KEY_FILENAME}.pub"

    def test_public_key_path_accepts_a_stemmed_name(
        self, write_config, source_tree, keys_dir, rsa_key_pair
    ):
        """prod.pem should resolve to prod.pub, not prod.pem.pub."""
        private_pem, public_pem = rsa_key_pair
        (keys_dir / "prod.pem").write_text(private_pem)
        (keys_dir / "prod.pub").write_text(public_pem)

        config = load_config(
            self._config(
                write_config,
                source_tree,
                f"""
                [api]
                organization_id = "{ORG_ID}"
                keys_dir = "{keys_dir}"
                key_filename = "prod.pem"
                key_id = "{KEY_ID}"
                """,
            )
        )
        assert config.api.public_key_path == keys_dir / "prod.pub"

    def test_rejects_non_uuid_organization_id(
        self, write_config, source_tree, keys_dir
    ):
        with pytest.raises(ConfigError) as exc:
            load_config(
                self._config(
                    write_config,
                    source_tree,
                    f"""
                    [api]
                    organization_id = "nope"
                    keys_dir = "{keys_dir}"
                    key_filename = "{KEY_FILENAME}"
                    key_id = "{KEY_ID}"
                    """,
                )
            )
        assert exc.value.key == "api.organization_id"

    def test_rejects_missing_keys_dir(self, write_config, source_tree, tmp_path):
        with pytest.raises(ConfigError) as exc:
            load_config(
                self._config(
                    write_config,
                    source_tree,
                    f"""
                    [api]
                    organization_id = "{ORG_ID}"
                    keys_dir = "{tmp_path / "absent"}"
                    key_filename = "{KEY_FILENAME}"
                    key_id = "{KEY_ID}"
                    """,
                )
            )
        assert exc.value.key == "api.keys_dir"

    def test_rejects_missing_private_key(self, write_config, source_tree, keys_dir):
        with pytest.raises(ConfigError) as exc:
            load_config(
                self._config(
                    write_config,
                    source_tree,
                    f"""
                    [api]
                    organization_id = "{ORG_ID}"
                    keys_dir = "{keys_dir}"
                    key_filename = "absent"
                    key_id = "{KEY_ID}"
                    """,
                )
            )
        assert exc.value.key == "api.key_filename"

    def test_rejects_a_path_as_key_filename(self, write_config, source_tree, keys_dir):
        with pytest.raises(ConfigError, match="bare filename") as exc:
            load_config(
                self._config(
                    write_config,
                    source_tree,
                    f"""
                    [api]
                    organization_id = "{ORG_ID}"
                    keys_dir = "{keys_dir}"
                    key_filename = "subdir/{KEY_FILENAME}"
                    key_id = "{KEY_ID}"
                    """,
                )
            )
        assert exc.value.key == "api.key_filename"

    def test_any_filename_is_accepted(self, write_config, source_tree, keys_dir):
        """The filename is the operator's choice; only the kid must be a UUID."""
        config = load_config(write_config())
        assert config.api.key_filename == "staging"

    def test_key_id_is_required(self, write_config, source_tree, keys_dir):
        with pytest.raises(ConfigError) as exc:
            load_config(
                self._config(
                    write_config,
                    source_tree,
                    f"""
                    [api]
                    organization_id = "{ORG_ID}"
                    keys_dir = "{keys_dir}"
                    key_filename = "{KEY_FILENAME}"
                    """,
                )
            )
        assert exc.value.key == "api.key_id"

    def test_rejects_a_non_uuid_key_id(self, write_config, source_tree, keys_dir):
        """The server only accepts a UUID in the `kid` header."""
        with pytest.raises(ConfigError, match="not a valid UUID") as exc:
            load_config(
                self._config(
                    write_config,
                    source_tree,
                    f"""
                    [api]
                    organization_id = "{ORG_ID}"
                    keys_dir = "{keys_dir}"
                    key_filename = "{KEY_FILENAME}"
                    key_id = "staging"
                    """,
                )
            )
        assert exc.value.key == "api.key_id"

    def test_rejects_token_ttl_over_24h(self, write_config, source_tree, keys_dir):
        with pytest.raises(ConfigError) as exc:
            load_config(
                self._config(
                    write_config,
                    source_tree,
                    f"""
                    [api]
                    organization_id = "{ORG_ID}"
                    keys_dir = "{keys_dir}"
                    key_filename = "{KEY_FILENAME}"
                    key_id = "{KEY_ID}"
                    token_ttl_minutes = 1441
                    """,
                )
            )
        assert exc.value.key == "api.token_ttl_minutes"


class TestTypeValidation:
    """Every message must name the offending key, since the file sits next to
    credentials and cannot simply be dumped into the error."""

    def _load(self, write_config, source_tree, api_section, extra: str):
        body = textwrap.dedent(
            f"""
            [source]
            root_dir = "{source_tree}"
            [corpus]
            id = "550e8400-e29b-41d4-a716-446655440001"
            """
        ) + textwrap.dedent(extra) + api_section
        return load_config(write_config(body))

    @pytest.mark.parametrize(
        ("extra", "key"),
        [
            ('[source]\nroot_dir = 42\n', "source.root_dir"),
            ('[source]\nroot_dir = "x"\nfollow_symlinks = "yes"\n', "source.follow_symlinks"),
            ('[source]\nroot_dir = "x"\ninclude = "*.pdf"\n', "source.include"),
            ('[source]\nroot_dir = "x"\ninclude = [1, 2]\n', "source.include"),
            ('[logging]\nbackup_count = "seven"\n', "logging.backup_count"),
            ('[logging]\nbackup_count = -1\n', "logging.backup_count"),
            ('[logging]\nlevel = "CHATTY"\n', "logging.level"),
            ('[logging]\nformat = "xml"\n', "logging.format"),
            ('[sync]\npoll_timeout_seconds = 0\n', "sync.poll_timeout_seconds"),
            ('[sync]\ndry_run = 1\n', "sync.dry_run"),
        ],
    )
    def test_reports_the_offending_key(
        self, write_config, source_tree, api_section, extra, key
    ):
        # A second [source] table would be a duplicate; drop ours when overriding it.
        if extra.startswith("[source]"):
            body = textwrap.dedent(extra).replace('root_dir = "x"', f'root_dir = "{source_tree}"')
            body = (
                body
                + '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
                + api_section
            )
            with pytest.raises(ConfigError) as exc:
                load_config(write_config(body))
        else:
            with pytest.raises(ConfigError) as exc:
                self._load(write_config, source_tree, api_section, extra)
        assert exc.value.key == key

    def test_a_section_that_is_not_a_table(self, write_config, source_tree, api_section):
        # Must precede every table header, or TOML scopes it into the last one.
        body = (
            'filters = "nope"\n'
            f'[source]\nroot_dir = "{source_tree}"\n'
            '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
        ) + api_section
        with pytest.raises(ConfigError, match="expected a table") as exc:
            load_config(write_config(body))
        assert exc.value.key == "filters"

    def test_a_directory_given_as_the_config_path(self, tmp_path):
        with pytest.raises(ConfigError, match="is a directory"):
            load_config(tmp_path)

    def test_root_dir_pointing_at_a_file(self, write_config, tmp_path, api_section):
        target = tmp_path / "afile"
        target.write_text("x")
        body = (
            f'[source]\nroot_dir = "{target}"\n'
            '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
        ) + api_section
        with pytest.raises(ConfigError, match="not a directory") as exc:
            load_config(write_config(body))
        assert exc.value.key == "source.root_dir"

    def test_a_non_http_base_url(self, write_config, source_tree, keys_dir):
        body = (
            f'[source]\nroot_dir = "{source_tree}"\n'
            '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
            f'[api]\norganization_id = "{ORG_ID}"\nkeys_dir = "{keys_dir}"\n'
            f'key_filename = "{KEY_FILENAME}"\nkey_id = "{KEY_ID}"\n'
            'base_url = "ftp://files.example"\n'
        )
        with pytest.raises(ConfigError, match="http") as exc:
            load_config(write_config(body))
        assert exc.value.key == "api.base_url"

    def test_keys_dir_pointing_at_a_file(self, write_config, source_tree, tmp_path):
        target = tmp_path / "notadir"
        target.write_text("x")
        body = (
            f'[source]\nroot_dir = "{source_tree}"\n'
            '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
            f'[api]\norganization_id = "{ORG_ID}"\nkeys_dir = "{target}"\n'
            f'key_filename = "{KEY_FILENAME}"\nkey_id = "{KEY_ID}"\n'
        )
        with pytest.raises(ConfigError, match="not a directory") as exc:
            load_config(write_config(body))
        assert exc.value.key == "api.keys_dir"


class TestThreadsSetting:
    def test_defaults_to_five(self, write_config):
        assert load_config(write_config()).sync.threads == 5

    @pytest.mark.parametrize("threads", [1, 2, 16, 32])
    def test_accepts_a_sensible_range(
        self, write_config, source_tree, api_section, threads
    ):
        body = (
            f'[source]\nroot_dir = "{source_tree}"\n'
            '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
            f"[sync]\nthreads = {threads}\n"
        ) + api_section
        assert load_config(write_config(body)).sync.threads == threads

    @pytest.mark.parametrize("threads", [0, -1])
    def test_rejects_non_positive(self, write_config, source_tree, api_section, threads):
        body = (
            f'[source]\nroot_dir = "{source_tree}"\n'
            '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
            f"[sync]\nthreads = {threads}\n"
        ) + api_section
        with pytest.raises(ConfigError) as exc:
            load_config(write_config(body))
        assert exc.value.key == "sync.threads"

    def test_rejects_an_absurd_count(self, write_config, source_tree, api_section):
        """5000 is a typo, not an intention, and would hammer the platform."""
        body = (
            f'[source]\nroot_dir = "{source_tree}"\n'
            '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
            "[sync]\nthreads = 5000\n"
        ) + api_section
        with pytest.raises(ConfigError, match="<= 32") as exc:
            load_config(write_config(body))
        assert exc.value.key == "sync.threads"

    def test_rejects_a_non_integer(self, write_config, source_tree, api_section):
        body = (
            f'[source]\nroot_dir = "{source_tree}"\n'
            '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
            '[sync]\nthreads = "five"\n'
        ) + api_section
        with pytest.raises(ConfigError) as exc:
            load_config(write_config(body))
        assert exc.value.key == "sync.threads"
