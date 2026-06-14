"""Тесты исправлений безопасности и корректности (v1.9.0).

Покрывают: path-traversal containment, выбор флешки, разбор версий,
группировку частей фильма, проверку контрольной суммы обновления,
работоспособность CLI. PyQt5 замокан в conftest.py.

Запуск: python -m pytest tests/test_safety.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestSafeJoin:
    def test_normal_subpath_allowed(self, tmp_path):
        from torflash.helpers import _safe_join
        dst = _safe_join(tmp_path, "sub/film.mkv")
        assert dst is not None
        assert str(dst).startswith(str(tmp_path.resolve()))

    def test_parent_traversal_blocked(self, tmp_path):
        from torflash.helpers import _safe_join
        assert _safe_join(tmp_path, "../../evil") is None

    def test_deep_traversal_blocked(self, tmp_path):
        from torflash.helpers import _safe_join
        assert _safe_join(tmp_path, "a/../../../../etc/passwd") is None

    def test_absolute_escape_blocked(self, tmp_path):
        from torflash.helpers import _safe_join
        # Абсолютный путь в rel не должен утаскивать за пределы root
        assert _safe_join(tmp_path, "/etc/passwd") is None

    def test_plain_name_allowed(self, tmp_path):
        from torflash.helpers import _safe_join
        dst = _safe_join(tmp_path, "movie.mkv")
        assert dst == (tmp_path.resolve() / "movie.mkv")


class TestDetectFlashMount:
    def test_missing_base(self, tmp_path):
        from torflash.helpers import detect_flash_mount
        assert detect_flash_mount(str(tmp_path / "nope")) is None

    def test_empty_base(self, tmp_path):
        from torflash.helpers import detect_flash_mount
        assert detect_flash_mount(str(tmp_path)) is None

    def test_writable_child_found(self, tmp_path):
        from torflash.helpers import detect_flash_mount
        (tmp_path / "FLASH").mkdir()
        assert detect_flash_mount(str(tmp_path)) == str(tmp_path / "FLASH")

    def test_ignores_files(self, tmp_path):
        from torflash.helpers import detect_flash_mount
        (tmp_path / "afile").write_text("x")
        assert detect_flash_mount(str(tmp_path)) is None


class TestVersionTuple:
    def test_with_v_prefix(self):
        from torflash.helpers import _version_tuple
        assert _version_tuple("v1.9.0") == (1, 9, 0)

    def test_plain(self):
        from torflash.helpers import _version_tuple
        assert _version_tuple("1.8") == (1, 8)

    def test_ordering(self):
        from torflash.helpers import _version_tuple
        assert _version_tuple("1.9.0") > _version_tuple("1.8.0")
        assert _version_tuple("1.10.0") > _version_tuple("1.9.0")

    def test_trailing_garbage_stops(self):
        from torflash.helpers import _version_tuple
        # На первом нечисловом компоненте разбор останавливается.
        assert _version_tuple("1.9.0-rc1") == (1, 9)


class TestGroupMovieParts:
    def test_split_parts_regroup(self):
        from torflash.helpers import group_movie_parts, _split_copy_part_name
        # имена частей как их генерирует _split_copy
        files = [
            (Path(_split_copy_part_name("Film", ".mkv", i)), 1000, float(i))
            for i in range(3)
        ]
        groups = group_movie_parts(files)
        assert len(groups) == 1
        assert groups[0]["count"] == 3
        assert groups[0]["title"] == "Film.mkv"

    def test_single_file_keeps_name(self):
        from torflash.helpers import group_movie_parts
        groups = group_movie_parts([(Path("Movie.2024.mkv"), 500, 1.0)])
        assert len(groups) == 1
        assert groups[0]["title"] == "Movie.2024.mkv"
        assert groups[0]["count"] == 1


class TestSizeParserEdges:
    def test_tb(self):
        from torflash.helpers import parse_size_text, SIZE_FACTORS
        if "TB" in SIZE_FACTORS:
            assert parse_size_text("2 TB") == 2 * SIZE_FACTORS["TB"]

    def test_garbage_is_zero(self):
        from torflash.helpers import parse_size_text
        assert parse_size_text("not a size") == 0

    def test_nbsp_separator(self):
        from torflash.helpers import parse_size_text
        assert parse_size_text("1,5\xa0GB") == int(1.5 * 1024**3)


class TestUpdateChecksumParse:
    def test_parses_sha256sum_format(self):
        from torflash.helpers import _sha256_from_sumfile
        digest = "a" * 64
        assert _sha256_from_sumfile(f"{digest}  TorFlash") == digest

    def test_rejects_short(self):
        from torflash.helpers import _sha256_from_sumfile
        assert _sha256_from_sumfile("deadbeef  TorFlash") == ""

    def test_empty(self):
        from torflash.helpers import _sha256_from_sumfile
        assert _sha256_from_sumfile("") == ""

    def test_uppercase_normalized(self):
        from torflash.helpers import _sha256_from_sumfile
        digest = "A" * 64
        assert _sha256_from_sumfile(digest) == "a" * 64


class TestCliImports:
    def test_cli_module_imports(self):
        # Регрессия: CLI импортировал удалённые MIRRORS/parse — ImportError.
        import torflash_cli
        assert hasattr(torflash_cli, "cmd_search")
        assert hasattr(torflash_cli, "main")
