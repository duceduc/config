"""Read-only map-journal inventory and detached-backup verification.

Capture under the profile's joined runtime gate or with HA stopped. Verification
requires the independently retained capture inventory, not one generated from
the candidate backup. This is one participant in coordinated saved-state backup,
not a complete HA backup, a restore operation, or permission to downgrade code.
No health payloads or credentials are returned, written, logged or reset.
"""
from __future__ import annotations

import binascii
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from . import workout_map_journal as journal


class MapBackupError(Exception):
    """Only a fixed code; never paths, account IDs or underlying IO details."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_file(directory: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= journal.MAX_BYTES:
            raise ValueError
        chunks = []
        remaining = journal.MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (len(raw) != before.st_size or len(raw) > journal.MAX_BYTES
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError
        return raw
    finally:
        os.close(descriptor)


def _file_record(name: str, raw: bytes) -> dict[str, Any]:
    return {"name": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def capture_map_backup_inventory(path: Path, scope: dict[str, str]) -> dict[str, Any]:
    """Validate all retained receipts and capture exact file identity, bounded per file.

    No absent-journal success: the coordinator must separately record a checked
    absence before a profile without a journal can belong to a complete backup.
    The directory descriptor pins reads to one directory throughout capture.
    """
    directory = None
    try:
        journal._scope(scope)
        path = Path(path).absolute()
        if path.name != journal.journal_path(str(path.parent), scope).name:
            raise ValueError
        # Refuse indirection in a backup tree, including its enclosing directories.
        for ancestor in path.parents:
            if not stat.S_ISDIR(ancestor.lstat().st_mode):
                raise ValueError
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        raw = _read_file(directory, path.name)
        document = json.loads(raw, object_pairs_hook=journal._pairs)
        journal._validate(document, scope)
        files = [_file_record(path.name, raw)]
        operations = set(document["receipts"])
        for page in document.get("receipt_pages", []):
            name = journal._page_path(path, page["sha256"]).name
            page_raw = _read_file(directory, name)
            receipts = journal._decode_page(page_raw, page, document)
            if operations.intersection(receipts):
                raise ValueError
            operations.update(receipts)
            files.append(_file_record(name, page_raw))
        # Detect a changed manifest rather than mixing two capture generations.
        if _read_file(directory, path.name) != raw:
            raise ValueError
        return {"version": 1, "kind": "halthy_map_journal_backup", "scope": deepcopy(scope),
                "journal_version": document["version"], "epoch": document["epoch"],
                "slot_id": document["slot_id"], "generation": document["generation"],
                "revision": document["revision"], "files": files}
    except (OSError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError,
            binascii.Error, journal.MapJournalError):
        raise MapBackupError("map_backup_unavailable") from None
    finally:
        if directory is not None:
            os.close(directory)


def _validate_inventory(value: Any, scope: dict[str, str]) -> None:
    keys = {"version", "kind", "scope", "journal_version", "epoch", "slot_id", "generation", "revision", "files"}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value["version"]) is not int or value["version"] != 1
            or value["kind"] != "halthy_map_journal_backup" or value["scope"] != scope
            or type(value["journal_version"]) is not int or value["journal_version"] not in (2, 3)
            or not journal._uuid(value["epoch"]) or not journal._text(value["slot_id"])
            or type(value["generation"]) is not int or not 0 <= value["generation"] <= journal.MAX_GENERATION
            or not journal._hash(value["revision"])
            or not isinstance(value["files"], list) or not 1 <= len(value["files"]) <= journal.MAX_RECEIPT_PAGES + 1):
        raise ValueError
    manifest = journal.journal_path(".", scope).name
    names = set()
    for index, record in enumerate(value["files"]):
        if (not isinstance(record, dict) or set(record) != {"name", "bytes", "sha256"}
                or not journal._hash(record["sha256"])
                or type(record["bytes"]) is not int or not 0 < record["bytes"] <= journal.MAX_BYTES):
            raise ValueError
        expected_name = manifest if index == 0 else manifest + ".receipts." + record["sha256"] + ".json"
        if record["name"] != expected_name or record["name"] in names:
            raise ValueError
        names.add(record["name"])
    if (value["journal_version"] == 2) != (len(value["files"]) == 1):
        raise ValueError


def verify_map_backup_inventory(path: Path, scope: dict[str, str], expected: dict[str, Any]) -> None:
    """Accept only a semantic and byte-exact match to a trusted capture inventory.

    Extra unreferenced orphan files are not adopted, deleted, or replayed. This
    does not certify the app, Keychain, pairing store, archive, or other HA state.
    """
    try:
        journal._scope(scope)
        _validate_inventory(expected, scope)
        actual = capture_map_backup_inventory(path, scope)
        if journal._encode(actual) != journal._encode(expected):
            raise ValueError
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError,
            journal.MapJournalError, MapBackupError):
        raise MapBackupError("map_backup_mismatch") from None
