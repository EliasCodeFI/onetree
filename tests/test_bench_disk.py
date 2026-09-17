from __future__ import annotations

from pathlib import Path

from onetree.bench.systems import tree_bytes


def test_tree_bytes_counts_hardlinks_once_but_preserves_distinct_files(tmp_path: Path) -> None:
    root = tmp_path / "measured"
    root.mkdir()
    original = root / "original"
    original.write_bytes(b"disk accounting\n" * 8192)
    separate = root / "separate"
    separate.write_bytes(original.read_bytes())
    nested = root / "nested"
    nested.mkdir()

    assert not original.samefile(separate)
    expected = sum(path.lstat().st_blocks * 512 for path in (original, separate, nested))
    assert original.lstat().st_blocks > 0
    assert tree_bytes(root) == expected

    same_directory_link = root / "alias"
    nested_link = nested / "alias"
    same_directory_link.hardlink_to(original)
    nested_link.hardlink_to(original)
    assert same_directory_link.samefile(original)
    assert nested_link.samefile(original)

    expected = sum(path.lstat().st_blocks * 512 for path in (original, separate, nested))
    assert tree_bytes(root) == expected
