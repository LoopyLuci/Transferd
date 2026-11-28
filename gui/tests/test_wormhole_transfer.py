"""Folders and files for a wormhole: a folder is zipped and unpacked, and an archive that is not safe is refused whole."""

from __future__ import annotations

import hashlib
import stat
import zipfile
from pathlib import Path

import pytest

from transferd_gui.wormhole.errors import TransferError
from transferd_gui.wormhole.transfer import (
    place_file,
    prepare_file,
    prepare_folder,
    sha256_file,
    unpack_folder,
    zip_folder,
)


def archive(path: Path, members) -> Path:
    """Write an archive with exactly these members: (name, bytes, external_attr or None).

    The name is set after the ZipInfo is made, because ZipInfo rewrites a backslash to a slash on Windows, and a hostile
    archive from another tool can carry one. Members are deflated, as an archive from a real zip tool would be."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data, attr in members:
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            info.compress_type = zipfile.ZIP_DEFLATED
            if attr is not None:
                info.external_attr = attr
            zf.writestr(info, data)
    return path


def setup(tmp_path: Path):
    folder = tmp_path / "downloads"
    folder.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return folder, scratch


def test_a_folder_zips_and_unpacks_to_the_same_tree(tmp_path):
    tree = tmp_path / "trip"
    (tree / "day 1").mkdir(parents=True)
    (tree / "notes.txt").write_text("hi", encoding="utf-8")
    (tree / "day 1" / "a.jpg").write_bytes(b"\xff" * 5000)
    folder, scratch = setup(tmp_path)
    offer, archive_path = prepare_folder(tree, scratch)
    assert offer.kind == "directory" and offer.entries == 3  # 'day 1/', 'day 1/a.jpg', 'notes.txt'
    result = unpack_folder(archive_path, folder, offer.name, expected_entries=offer.entries)
    assert result.name == "trip"
    assert (result / "notes.txt").read_text(encoding="utf-8") == "hi"
    assert (result / "day 1" / "a.jpg").read_bytes() == b"\xff" * 5000


def test_an_existing_folder_with_the_same_name_is_not_overwritten(tmp_path):
    folder, scratch = setup(tmp_path)
    (folder / "trip").mkdir()
    (folder / "trip" / "mine.txt").write_text("keep", encoding="utf-8")
    tree = tmp_path / "trip"
    tree.mkdir()
    (tree / "theirs.txt").write_text("new", encoding="utf-8")
    offer, archive_path = prepare_folder(tree, scratch)
    result = unpack_folder(archive_path, folder, offer.name, expected_entries=offer.entries)
    assert result.name == "trip (2)"
    assert (folder / "trip" / "mine.txt").read_text(encoding="utf-8") == "keep"


def test_a_name_with_dots_keeps_them_for_a_folder(tmp_path):
    folder, scratch = setup(tmp_path)
    tree = tmp_path / "v1.2"
    tree.mkdir()
    (tree / "x.txt").write_text("x", encoding="utf-8")
    offer, archive_path = prepare_folder(tree, scratch)
    result = unpack_folder(archive_path, folder, offer.name, expected_entries=offer.entries)
    assert result.name == "v1.2"


@pytest.mark.parametrize(
    "member",
    ["../evil.txt", "a/../../evil.txt", "/abs.txt", "dir\\evil.txt", "C:evil.txt", "a//b.txt", "./x.txt"],
)
def test_an_archive_with_an_unsafe_name_is_refused_and_leaves_nothing_behind(tmp_path, member):
    folder, scratch = setup(tmp_path)
    bad = archive(scratch / "bad.zip", [("ok.txt", b"x", None), (member, b"evil", None)])
    with pytest.raises(TransferError):
        unpack_folder(bad, folder, "bundle", expected_entries=2)
    assert list(folder.iterdir()) == [], "a refused folder leaves nothing in the download folder"
    assert not (tmp_path / "evil.txt").exists()


def test_a_link_inside_an_archive_is_refused(tmp_path):
    folder, scratch = setup(tmp_path)
    link_attr = (stat.S_IFLNK | 0o777) << 16
    bad = archive(scratch / "link.zip", [("pointer", b"/etc/passwd", link_attr)])
    with pytest.raises(TransferError, match="link"):
        unpack_folder(bad, folder, "bundle", expected_entries=1)
    assert list(folder.iterdir()) == []


def test_a_device_or_fifo_inside_an_archive_is_refused(tmp_path):
    folder, scratch = setup(tmp_path)
    fifo_attr = (stat.S_IFIFO | 0o644) << 16
    bad = archive(scratch / "fifo.zip", [("pipe", b"", fifo_attr)])
    with pytest.raises(TransferError, match="not a file or a folder"):
        unpack_folder(bad, folder, "bundle", expected_entries=1)


def test_an_archive_with_a_different_entry_count_than_offered_is_refused(tmp_path):
    folder, scratch = setup(tmp_path)
    good = archive(scratch / "two.zip", [("a", b"1", None), ("b", b"2", None)])
    with pytest.raises(TransferError, match="number of entries"):
        unpack_folder(good, folder, "bundle", expected_entries=5)
    assert list(folder.iterdir()) == []


def test_an_archive_that_compresses_too_well_is_refused_as_a_bomb(tmp_path):
    folder, scratch = setup(tmp_path)
    bomb = archive(scratch / "bomb.zip", [("zeros.bin", b"\x00" * (20 * 1024 * 1024), None)])
    with pytest.raises(TransferError, match="compresses too well"):
        unpack_folder(bomb, folder, "bundle", expected_entries=1)
    assert list(folder.iterdir()) == []


def test_a_file_that_is_not_a_zip_is_refused_and_leaves_nothing(tmp_path):
    folder, scratch = setup(tmp_path)
    junk = scratch / "junk.zip"
    junk.write_bytes(b"this is not a zip archive")
    with pytest.raises(TransferError, match="could not be unpacked"):
        unpack_folder(junk, folder, "bundle", expected_entries=1)
    assert list(folder.iterdir()) == []


def test_a_file_is_offered_with_its_size_and_hash(tmp_path):
    source = tmp_path / "data.bin"
    source.write_bytes(b"hello world" * 100)
    offer, path = prepare_file(source)
    assert offer.kind == "file" and offer.name == "data.bin"
    assert offer.size == len(b"hello world" * 100)
    assert offer.sha256 == hashlib.sha256(b"hello world" * 100).hexdigest()
    assert path == source


def test_a_missing_file_or_folder_cannot_be_offered(tmp_path):
    with pytest.raises(TransferError):
        prepare_file(tmp_path / "nope.txt")
    with pytest.raises(TransferError):
        prepare_folder(tmp_path / "nope", tmp_path)


def test_placing_a_file_never_overwrites(tmp_path):
    folder, scratch = setup(tmp_path)
    (folder / "x.txt").write_text("old", encoding="utf-8")
    incoming = scratch / "incoming"
    incoming.write_text("new", encoding="utf-8")
    placed = place_file(incoming, folder, "x.txt")
    assert placed.name == "x (2).txt"
    assert (folder / "x.txt").read_text(encoding="utf-8") == "old"
    assert placed.read_text(encoding="utf-8") == "new"


def test_the_sha256_of_a_file_matches_hashlib(tmp_path):
    data = b"\x00\x01\x02" * 300_000
    path = tmp_path / "big.bin"
    path.write_bytes(data)
    assert sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_a_folder_zipped_for_sending_lists_every_entry(tmp_path):
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f.txt").write_text("f", encoding="utf-8")
    (tree / "top.txt").write_text("t", encoding="utf-8")
    dest = tmp_path / "out.zip"
    count = zip_folder(tree, dest)
    with zipfile.ZipFile(dest) as zf:
        names = sorted(zf.namelist())
    assert count == len(names) == 3, "the folder 'sub', its file, and the top file"
    assert "sub/" in names and "sub/f.txt" in names and "top.txt" in names
