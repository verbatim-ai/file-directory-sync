from __future__ import annotations

import os
from pathlib import Path

import pytest

from verbatim_sync.config import FiltersConfig, SourceConfig
from verbatim_sync.scan import (
    SkipReason,
    apply_filters,
    hash_file,
    matches_any,
    resolve_content_type,
    walk,
)


class TestMatchesAny:
    @pytest.mark.parametrize(
        ("rel_path", "patterns", "expected"),
        [
            ("a.pdf", ("**/*.pdf",), True),
            ("nested/b.pdf", ("**/*.pdf",), True),
            ("deep/deeper/c.pdf", ("**/*.pdf",), True),
            ("a.txt", ("**/*.pdf",), False),
            (".hidden", ("**/.*",), True),
            ("nested/.hidden", ("**/.*",), True),
            ("archive/x.pdf", ("archive",), True),
            ("archive", ("archive",), True),
            ("archives/x.pdf", ("archive",), False),
            ("~$draft.docx", ("**/~$*",), True),
        ],
    )
    def test_patterns(self, rel_path, patterns, expected):
        assert matches_any(rel_path, patterns) is expected


class TestResolveContentType:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("a.pdf", "application/pdf"),
            ("A.PDF", "application/pdf"),
            ("a.html", "text/html"),
            ("a.txt", "text/plain"),
            (
                "a.docx",
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document",
            ),
        ],
    )
    def test_known_extensions(self, name, expected):
        assert resolve_content_type(name) == expected

    def test_unknown_extension(self):
        assert resolve_content_type("a.zzz") is None
        assert resolve_content_type("noextension") is None


class TestApplyFilters:
    config = FiltersConfig(
        content_types=("application/pdf",), max_file_size=1000, min_file_size=1
    )

    def test_accepts_a_matching_file(self):
        result = apply_filters("a.pdf", 500, self.config)
        assert result.accepted
        assert result.content_type == "application/pdf"
        assert result.reason is None

    def test_rejects_oversized(self):
        result = apply_filters("a.pdf", 1001, self.config)
        assert not result.accepted
        assert result.reason is SkipReason.TOO_LARGE

    def test_rejects_empty(self):
        result = apply_filters("a.pdf", 0, self.config)
        assert not result.accepted
        assert result.reason is SkipReason.TOO_SMALL

    def test_rejects_disallowed_type(self):
        result = apply_filters("a.txt", 10, self.config)
        assert not result.accepted
        assert result.reason is SkipReason.CONTENT_TYPE_NOT_ALLOWED
        assert result.content_type == "text/plain"

    def test_rejects_unknown_type(self):
        result = apply_filters("a.zzz", 10, self.config)
        assert not result.accepted
        assert result.reason is SkipReason.UNKNOWN_CONTENT_TYPE

    def test_size_is_reported_before_type(self):
        """An oversized .txt reports the size, the actionable problem."""
        result = apply_filters("a.txt", 5000, self.config)
        assert result.reason is SkipReason.TOO_LARGE

    def test_empty_content_types_accepts_any_known_type(self):
        config = FiltersConfig(content_types=(), max_file_size=1000, min_file_size=1)
        assert apply_filters("a.txt", 10, config).accepted


class TestWalk:
    def test_walks_recursively_and_honours_exclude(self, source_tree: Path):
        config = SourceConfig(root_dir=source_tree, exclude=("**/.*",))
        found = {file.rel_path for file in walk(config)}

        assert "a.pdf" in found
        assert "nested/b.pdf" in found
        assert "reports/big.pdf" in found
        assert ".hidden.pdf" not in found

    def test_include_allowlist(self, source_tree: Path):
        config = SourceConfig(root_dir=source_tree, include=("**/*.pdf",))
        found = {file.rel_path for file in walk(config)}

        assert "notes.txt" not in found
        assert "archive.zzz" not in found
        assert "a.pdf" in found

    def test_reports_size_and_mtime(self, source_tree: Path):
        config = SourceConfig(root_dir=source_tree, include=("a.pdf",))
        files = list(walk(config))

        assert len(files) == 1
        assert files[0].size == (source_tree / "a.pdf").stat().st_size
        assert files[0].mtime_ns > 0
        assert files[0].filename == "a.pdf"

    def test_excluded_directory_is_not_descended(self, source_tree: Path):
        config = SourceConfig(root_dir=source_tree, exclude=("nested",))
        found = {file.rel_path for file in walk(config)}
        assert not any(path.startswith("nested/") for path in found)

    def test_rel_paths_use_posix_separators(self, source_tree: Path):
        config = SourceConfig(root_dir=source_tree)
        assert all("\\" not in file.rel_path for file in walk(config))


class TestHashFile:
    def test_matches_known_digest(self, tmp_path: Path):
        path = tmp_path / "x.bin"
        path.write_bytes(b"hello")
        # sha256("hello")
        assert hash_file(path) == (
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        )

    def test_streams_across_chunk_boundaries(self, tmp_path: Path):
        path = tmp_path / "big.bin"
        path.write_bytes(b"ab" * 5000)
        assert hash_file(path, chunk_size=7) == hash_file(path, chunk_size=1 << 20)


class TestWalkResilience:
    """A cron job meets unreadable directories and odd file types. It must
    log them and carry on, not abort the whole run."""

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
    def test_unreadable_directory_is_skipped(self, source_tree: Path, caplog):
        locked = source_tree / "locked"
        locked.mkdir()
        (locked / "secret.pdf").write_bytes(b"%PDF-1.4 x")
        locked.chmod(0o000)
        try:
            config = SourceConfig(root_dir=source_tree)
            with caplog.at_level("WARNING"):
                found = {file.rel_path for file in walk(config)}
        finally:
            locked.chmod(0o755)

        assert "a.pdf" in found  # the rest of the tree still came through
        assert not any(p.startswith("locked/") for p in found)
        assert "Cannot read directory" in caplog.text

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
    def test_unreadable_root_yields_nothing_without_raising(self, tmp_path: Path, caplog):
        root = tmp_path / "sealed"
        root.mkdir()
        root.chmod(0o000)
        try:
            with caplog.at_level("WARNING"):
                assert list(walk(SourceConfig(root_dir=root))) == []
        finally:
            root.chmod(0o755)
        assert "Cannot read directory" in caplog.text

    def test_a_fifo_is_not_a_regular_file(self, source_tree: Path):
        os.mkfifo(source_tree / "pipe.pdf")
        found = {file.rel_path for file in walk(SourceConfig(root_dir=source_tree))}
        assert "pipe.pdf" not in found
        assert "a.pdf" in found

    def test_a_broken_symlink_is_skipped(self, source_tree: Path):
        (source_tree / "dangling.pdf").symlink_to(source_tree / "nowhere.pdf")
        config = SourceConfig(root_dir=source_tree, follow_symlinks=True)
        found = {file.rel_path for file in walk(config)}
        assert "dangling.pdf" not in found
        assert "a.pdf" in found

    def test_symlinks_are_ignored_by_default(self, source_tree: Path, tmp_path: Path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leaked.pdf").write_bytes(b"%PDF-1.4 leak")
        (source_tree / "link").symlink_to(outside)

        found = {file.rel_path for file in walk(SourceConfig(root_dir=source_tree))}
        assert not any(p.startswith("link/") for p in found)

    def test_symlinked_directories_are_followed_when_enabled(
        self, source_tree: Path, tmp_path: Path
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "linked.pdf").write_bytes(b"%PDF-1.4 linked")
        (source_tree / "link").symlink_to(outside)

        config = SourceConfig(root_dir=source_tree, follow_symlinks=True)
        found = {file.rel_path for file in walk(config)}
        assert "link/linked.pdf" in found

    def test_a_symlink_loop_is_broken(self, source_tree: Path, caplog):
        """Following links can otherwise recurse until the stack gives out."""
        (source_tree / "loop").symlink_to(source_tree)

        config = SourceConfig(root_dir=source_tree, follow_symlinks=True)
        with caplog.at_level("WARNING"):
            found = list(walk(config))

        assert "symlink loop" in caplog.text
        assert len(found) < 100  # terminated rather than recursing forever

    def test_a_file_removed_mid_walk_is_skipped(self, source_tree: Path, monkeypatch, caplog):
        """The tree can change under a long scan."""
        import os as os_module

        real_scandir = os_module.scandir

        def vanishing(path):
            for entry in real_scandir(path):
                if entry.name == "a.pdf":
                    Path(entry.path).unlink()
                yield entry

        monkeypatch.setattr("verbatim_sync.scan.walker.os.scandir", vanishing)
        with caplog.at_level("WARNING"):
            found = {file.rel_path for file in walk(SourceConfig(root_dir=source_tree))}

        assert "a.pdf" not in found
        assert "Cannot stat" in caplog.text


class TestScannedFileTimestamps:
    def test_exposes_iso_utc_timestamps(self, source_tree: Path):
        config = SourceConfig(root_dir=source_tree, include=("a.pdf",))
        scanned = next(iter(walk(config)))

        assert scanned.modified_at.endswith("Z")
        assert scanned.created_at.endswith("Z")
        assert scanned.modified_at.startswith("20")
        assert "T" in scanned.modified_at

    def test_created_at_falls_back_to_modified(self):
        from verbatim_sync.scan.walker import ScannedFile

        scanned = ScannedFile(
            abs_path=Path("/docs/a.pdf"),
            rel_path="a.pdf",
            size=1,
            mtime_ns=1_700_000_000_000_000_000,
            ctime_ns=0,
        )
        assert scanned.created_at == scanned.modified_at
