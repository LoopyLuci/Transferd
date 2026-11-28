"""Files and folders for a wormhole: hashing, zipping a folder to send, and unpacking a received folder safely.

A folder is sent as one zip archive. The receiver checks the archive's hash against the offer, and only then unpacks it,
and it unpacks it under the rules in `gui\\docs\\WORMHOLE.md`: no absolute or `..` names, no links or devices, a limit on the
number of entries and on the total size, and a limit on how well the archive compresses.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import stat
import zipfile
from pathlib import Path
from typing import Tuple

from transferd_gui.backend.files import unique_path
from transferd_gui.wormhole import protocol as P
from transferd_gui.wormhole.errors import TransferError

CHUNK = 256 * 1024
UNPACKED_MAX = 10 * 1024 ** 3
RATIO_MAX = 100
RATIO_FREE_BYTES = 1024 * 1024
_BAD_PART = re.compile(r'[\x00-\x1f<>:"\\|?*]')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def remove_quietly(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
    except OSError:
        pass


def unique_folder(parent: Path, name: str) -> Path:
    """`parent/name`, or `name (2)`, `name (3)` and so on, whichever does not exist yet. Created by the caller."""
    candidate = parent / name
    counter = 2
    while candidate.exists():
        candidate = parent / f"{name} ({counter})"
        counter += 1
    return candidate


def place_file(source: Path, folder: Path, name: str) -> Path:
    """Move a checked file into `folder` under its offered name, or the next free name. Returns where it went."""
    target = unique_path(folder, P.safe_offered_name(name))
    shutil.move(str(source), str(target))
    return target


# ----- sending ----------------------------------------------------------------------------------------


def prepare_file(path: Path) -> Tuple[P.Offer, Path]:
    """The offer for one file, with its size and hash, and the file itself."""
    if not path.is_file():
        raise TransferError(f"{path.name} is not a file")
    return P.Offer.for_file(path.name, path.stat().st_size, sha256_file(path)), path


def zip_folder(folder: Path, archive: Path) -> int:
    """Zip a folder into `archive`. Links and anything that is not a file or a folder are refused. Returns the entry count."""
    entries = 0
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                raise TransferError(f"{path.name} is a link; links are not sent")
            relative = path.relative_to(folder).as_posix()
            if path.is_dir():
                zf.writestr(relative + "/", b"")
            elif path.is_file():
                zf.write(path, relative)
            else:
                raise TransferError(f"{path.name} is neither a file nor a folder")
            entries += 1
            if entries > P.ENTRIES_MAX:
                raise TransferError(f"a folder can have at most {P.ENTRIES_MAX} entries")
    return entries


def prepare_folder(folder: Path, scratch: Path) -> Tuple[P.Offer, Path]:
    """Zip a folder into `scratch` and return its offer and the archive."""
    if not folder.is_dir():
        raise TransferError(f"{folder.name} is not a folder")
    archive = scratch / (re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", folder.name).strip(" .") + ".zip")
    entries = zip_folder(folder, archive)
    return P.Offer.for_directory(folder.name, archive.stat().st_size, sha256_file(archive), entries), archive


# ----- receiving --------------------------------------------------------------------------------------


def _member_refused(info: zipfile.ZipInfo) -> str:
    """Why an archive member must not be unpacked, or the empty string if it may be."""
    name = info.filename
    # Windows rewrites backslashes while reading, so the name as written is checked too.
    raw = getattr(info, "orig_filename", name)
    if "\\" in raw or raw.startswith("/") or name.startswith("/"):
        return "an entry has an absolute or backslash path"
    parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "an entry has an empty, '.' or '..' part"
    if any(_BAD_PART.search(part) for part in parts):
        return "an entry name has a character a file name cannot have"
    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        return "an entry is a link"
    if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
        return "an entry is not a file or a folder"
    return ""


def unpack_folder(archive: Path, parent: Path, name: str, *, expected_entries: int) -> Path:
    """Unpack a checked archive into a new folder under `parent`. Refuses an unsafe archive and leaves nothing behind."""
    target = unique_folder(parent, P.safe_offered_name(name))
    target.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if len(infos) != expected_entries:
                raise TransferError("the folder does not hold the number of entries that was offered")
            compressed = max(1, sum(info.compress_size for info in infos))
            total = 0
            root = target.resolve()
            for info in infos:
                reason = _member_refused(info)
                if reason:
                    raise TransferError(f"the folder was refused: {reason}")
                destination = target.joinpath(*info.filename.rstrip("/").split("/")).resolve()
                if root not in destination.parents and destination != root:
                    raise TransferError("the folder was refused: an entry leaves it")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, open(destination, "xb") as out:
                    for block in iter(lambda: source.read(CHUNK), b""):
                        total += len(block)
                        if total > UNPACKED_MAX:
                            raise TransferError("the folder is larger than the limit")
                        if total > RATIO_FREE_BYTES and total > RATIO_MAX * compressed:
                            raise TransferError("the folder compresses too well to be a folder; it was refused")
                        out.write(block)
    except (zipfile.BadZipFile, OSError) as exc:
        remove_quietly(target)
        raise TransferError("the folder could not be unpacked") from exc
    except TransferError:
        remove_quietly(target)
        raise
    return target
