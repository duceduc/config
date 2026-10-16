"""Durable conditional-map transaction foundation, not an HTTP write endpoint.

The caller must hold the profile runtime gate throughout IO and publication.
Images are self-contained, so archive replacement cannot invalidate recovery.
Receipt pages are bounded without eviction: an old retry must never become a
new write. The manifest and all referenced pages belong in private HA backups.
"""
from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import UUID, uuid4

MAX_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 350 * 1024
MAX_ATTRIBUTES_BYTES = 64 * 1024
MAX_RECEIPTS = 256
MAX_RECEIPT_PAGES = 4096
MAX_GENERATION = 2**63 - 1
_HEX = re.compile(r"[0-9a-f]{64}")


class MapJournalError(Exception):
    """Fixed codes only; no exception details, paths or health values."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MapMutationRejection(MapJournalError):
    """Checked rejection metadata is private; exception text stays a fixed code."""

    def __init__(self, code: str, document: dict[str, Any], operation: str, device: str,
                 expected: str, request_sha256: str) -> None:
        super().__init__(code)
        self.proof = {"ok": False, "version": 1, "error": self.code, "applied": False,
            "account_id": document["scope"]["account_id"], "entry_id": document["scope"]["entry_id"],
            "username": document["scope"]["username"], "device_id": device,
            "operation_id": operation, "request_sha256": request_sha256, "expected_revision": expected,
            "state": {"revision": document["revision"], "generation": document["generation"],
                "current": _projection(document["current"])}}


class MapRevisionConflict(MapMutationRejection):
    def __init__(self, document: dict[str, Any], operation: str, device: str,
                 expected: str, request_sha256: str) -> None:
        super().__init__("map_revision_conflict", document, operation, device, expected, request_sha256)


def _text(value: Any) -> bool:
    return (isinstance(value, str) and value == value.strip()
            and 0 < len(value.encode()) <= 256
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _uuid(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 36 and str(UUID(value)) == value


def _hash(value: Any) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _scope(scope: Any) -> None:
    if (not isinstance(scope, dict) or set(scope) != {"entry_id", "account_id", "username"}
            or not all(_text(value) for value in scope.values())
            or re.fullmatch(r"[a-z0-9_]+", scope["username"]) is None):
        raise ValueError


def validate_json_tree(value: Any) -> None:
    # Bound depth/nodes before JSON encoding, including cyclic Python values.
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 24 or nodes > 20_000:
            raise ValueError
        if item is None or type(item) in (str, bool, int):
            continue
        if type(item) is float and math.isfinite(item):
            continue
        if type(item) is list:
            stack.extend((child, depth + 1) for child in item)
        elif type(item) is dict and all(type(key) is str for key in item):
            stack.extend((child, depth + 1) for child in item.values())
        else:
            raise ValueError


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError
    return result.astimezone(timezone.utc)


def _image(current: Any, scope: dict[str, str]) -> None:
    if current is None:
        return
    if (not isinstance(current, dict) or set(current) != {
            "unique_id", "metric_key", "name", "content_type", "image_base64", "attributes",
            "username", "device_id", "updated_at"}
            or current["metric_key"] != "workout"
            or not all(_text(current[key]) for key in ("unique_id", "name", "username", "device_id"))
            or not isinstance(current["image_base64"], str)
            or len(current["image_base64"]) > (MAX_IMAGE_BYTES + 2) // 3 * 4):
        raise ValueError
    data = base64.b64decode(current["image_base64"].encode("ascii"), validate=True)
    if not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError
    if not ((current["content_type"] == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
            or (current["content_type"] == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))):
        raise ValueError
    attrs = current["attributes"]
    if (not isinstance(attrs, dict) or not _uuid(attrs.get("workout_uuid"))
            or any(key.startswith("archive_") for key in attrs)):
        raise ValueError
    validate_json_tree(attrs)
    if len(_encode(attrs)) > MAX_ATTRIBUTES_BYTES:
        raise ValueError
    _time(current["updated_at"])
    if _time(attrs.get("workout_start")) >= _time(attrs.get("workout_end")):
        raise ValueError


def _projection(current: dict[str, Any] | None) -> dict[str, str] | None:
    if current is None:
        return None
    return {"workout_uuid": current["attributes"]["workout_uuid"],
            "workout_end": _time(current["attributes"]["workout_end"]).isoformat().replace("+00:00", "Z"),
            "source_device_id": current["device_id"]}


def _revision(scope: dict[str, str], epoch: str, slot_id: str, generation: int, current: Any) -> str:
    return hashlib.sha256(_encode({"contract": "halthy_map_journal_v2", "scope": scope,
        "epoch": epoch, "slot_id": slot_id, "generation": generation, "current": current})).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _validate(document: Any, scope: dict[str, str]) -> None:
    _scope(scope)
    keys = {"version", "scope", "epoch", "slot_id", "generation", "revision", "current", "receipts"}
    if (not isinstance(document, dict) or type(document.get("version")) is not int
            or document["version"] not in (2, 3)
            or set(document) != (keys | {"receipt_pages"} if document["version"] == 3 else keys)
            or document["scope"] != scope
            or not _uuid(document["epoch"])
            or not _text(document["slot_id"])
            or type(document["generation"]) is not int
            or not 0 <= document["generation"] <= MAX_GENERATION
            or not _hash(document["revision"])
            or document["revision"] != _revision(scope, document["epoch"], document["slot_id"], document["generation"], document["current"])
            or not isinstance(document["receipts"], dict) or len(document["receipts"]) > MAX_RECEIPTS):
        raise ValueError
    _image(document["current"], scope)
    if document["current"] is not None and document["current"]["unique_id"] != document["slot_id"]:
        raise ValueError
    archived = 0
    hashes = set()
    if document["version"] == 3:
        pages = document["receipt_pages"]
        if not isinstance(pages, list) or not 0 < len(pages) <= MAX_RECEIPT_PAGES:
            raise ValueError
        for page in pages:
            if (not isinstance(page, dict) or set(page) != {"sha256", "count", "start_generation"}
                    or not _hash(page["sha256"]) or page["sha256"] in hashes
                    or type(page["count"]) is not int or not 0 < page["count"] <= MAX_RECEIPTS
                    or type(page["start_generation"]) is not int or page["start_generation"] != archived + 1):
                raise ValueError
            hashes.add(page["sha256"])
            archived += page["count"]
    _validate_receipts(document["receipts"], archived + 1, document["generation"], document)
    if len(_encode(document)) > MAX_BYTES:
        raise ValueError


def _validate_receipts(receipts: Any, first: int, last: int, document: dict[str, Any]) -> None:
    if (not isinstance(receipts, dict) or len(receipts) > MAX_RECEIPTS
            or len(receipts) != last - first + 1):
        raise ValueError
    generations = set()
    for operation_id, receipt in receipts.items():
        if (not _uuid(operation_id) or not isinstance(receipt, dict)
                or set(receipt) != {"request_sha256", "device_id", "generation", "revision", "current"}
                or not _hash(receipt["request_sha256"]) or not _text(receipt["device_id"])
                or type(receipt["generation"]) is not int
                or not first <= receipt["generation"] <= last
                or receipt["generation"] in generations or not _hash(receipt["revision"])):
            raise ValueError
        generations.add(receipt["generation"])
        current = receipt["current"]
        if current is not None:
            if (not isinstance(current, dict) or set(current) != {"workout_uuid", "workout_end", "source_device_id"}
                    or not _uuid(current["workout_uuid"]) or not _text(current["source_device_id"])
                    or current["source_device_id"] != receipt["device_id"]):
                raise ValueError
            _time(current["workout_end"])
        if receipt["generation"] == document["generation"]:
            if receipt["revision"] != document["revision"] or current != _projection(document["current"]):
                raise ValueError


def _page_path(path: Path, digest: str) -> Path:
    return path.with_name(path.name + ".receipts." + digest + ".json")


def _read_page(path: Path, descriptor: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    with _page_path(path, descriptor["sha256"]).open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    return _decode_page(raw, descriptor, document)


def _decode_page(raw: bytes, descriptor: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    if not raw or len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
        raise ValueError
    page = json.loads(raw, object_pairs_hook=_pairs)
    if (not isinstance(page, dict) or set(page) != {"version", "scope", "epoch", "slot_id", "start_generation", "receipts"}
            or type(page["version"]) is not int or page["version"] != 1
            or page["scope"] != document["scope"] or page["epoch"] != document["epoch"]
            or page["slot_id"] != document["slot_id"]
            or type(page["start_generation"]) is not int
            or page["start_generation"] != descriptor["start_generation"]):
        raise ValueError
    first = descriptor["start_generation"]
    _validate_receipts(page["receipts"], first, first + descriptor["count"] - 1, document)
    return page["receipts"]


def _validate_pages(path: Path, document: dict[str, Any]) -> None:
    operations = set(document["receipts"])
    for descriptor in document.get("receipt_pages", []):
        receipts = _read_page(path, descriptor, document)
        if operations.intersection(receipts):
            raise ValueError
        operations.update(receipts)


def _find_receipt(path: Path, document: dict[str, Any], operation: str) -> dict[str, Any] | None:
    if operation in document["receipts"]:
        return document["receipts"][operation]
    for descriptor in document.get("receipt_pages", []):
        receipts = _read_page(path, descriptor, document)
        if operation in receipts:
            return receipts[operation]
    return None


def _confirm_snapshot(path: Path, document: dict[str, Any]) -> None:
    for descriptor in document.get("receipt_pages", []):
        _confirm_file(_page_path(path, descriptor["sha256"]))
    _confirm_file(path)


def _roll_receipts(path: Path, document: dict[str, Any]) -> dict[str, Any]:
    # Page durability precedes the manifest swap. Orphan pages after a crash
    # are harmless; identical retry reuses them, never discards old receipts.
    first = document["generation"] - len(document["receipts"]) + 1
    page = {"version": 1, "scope": document["scope"], "epoch": document["epoch"],
            "slot_id": document["slot_id"], "start_generation": first, "receipts": document["receipts"]}
    raw = _encode(page)
    if len(raw) > MAX_BYTES:
        raise ValueError
    digest = hashlib.sha256(raw).hexdigest()
    target = _page_path(path, digest)
    if target.exists():
        with target.open("rb") as source:
            if source.read(MAX_BYTES + 1) != raw:
                raise ValueError
        _confirm_file(target)
    else:
        _save(target, page)
    result = deepcopy(document)
    result["version"] = 3
    result.setdefault("receipt_pages", []).append({"sha256": digest,
        "count": len(document["receipts"]), "start_generation": first})
    result["receipts"] = {}
    return result


def journal_path(storage_directory: str, scope: dict[str, str]) -> Path:
    try:
        _scope(scope)
        key = hashlib.sha256(_encode(scope)).hexdigest()
        return Path(storage_directory) / ("halthy_map_transactions_" + key + ".json")
    except (TypeError, ValueError, UnicodeError):
        raise MapJournalError("invalid_map_scope") from None


def initial_snapshot(scope: dict[str, str], current: dict[str, Any] | None, slot_id: str | None = None) -> dict[str, Any]:
    try:
        _scope(scope)
        _image(current, scope)
        epoch = str(uuid4())
        slot_id = slot_id or (current["unique_id"] if current is not None else "halthy_" + hashlib.sha256(_encode(scope)).hexdigest() + "_workout")
        result = {"version": 2, "scope": deepcopy(scope), "epoch": epoch, "slot_id": slot_id, "generation": 0,
                  "revision": _revision(scope, epoch, slot_id, 0, current), "current": deepcopy(current), "receipts": {}}
        _validate(result, scope)
        return result
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError, binascii.Error):
        raise MapJournalError("invalid_map_baseline") from None


def load_snapshot(path: Path, scope: dict[str, str]) -> dict[str, Any] | None:
    try:
        _scope(scope)
        try:
            with path.open("rb") as source:
                raw = source.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return None
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError
        result = json.loads(raw, object_pairs_hook=_pairs)
        _validate(result, scope)
        _validate_pages(path, result)
        return result
    except (OSError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError, binascii.Error):
        raise MapJournalError("map_journal_unavailable") from None


def _confirm_file(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_confirmed_snapshot(path: Path, scope: dict[str, str]) -> dict[str, Any] | None:
    result = load_snapshot(path, scope)
    if result is not None:
        try:
            _confirm_snapshot(path, result)
        except OSError:
            raise MapJournalError("map_write_unconfirmed") from None
    return result


def _save(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(_encode(document))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        _confirm_file(path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initialize_journal(path: Path, scope: dict[str, str], baseline: dict[str, Any] | None, slot_id: str | None = None) -> dict[str, Any]:
    """Explicit activation under the gate; never reset a present/corrupt journal."""
    previous = load_snapshot(path, scope)
    if previous is not None:
        try:
            _confirm_snapshot(path, previous)
        except OSError:
            raise MapJournalError("map_write_unconfirmed") from None
        return previous
    result = initial_snapshot(scope, baseline, slot_id)
    try:
        _save(path, result)
    except (OSError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise MapJournalError("map_write_unconfirmed") from None
    return result


def commit_mutation(path: Path, scope: dict[str, str], *,
                    operation_id: str, device_id: str,
                    expected_revision: str, replacement: dict[str, Any] | None,
                    request_body: bytes | None = None) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Returns durable latest state, this operation's receipt, and replay flag.

    All calls for this path must be serialized by the runtime gate. A missing
    journal is not permission to recreate it. A failure after replace is uncertain;
    retry the identical operation rather than creating a new operation ID.
    An HTTP adapter may bind the validated exact body instead of server-derived
    image timestamps/IDs. It must derive `replacement` only from that body and
    the gated current state; never accept a client-supplied digest.
    """
    try:
        _scope(scope)
        if not (_uuid(operation_id) and _text(device_id) and _hash(expected_revision)):
            raise ValueError
        _image(replacement, scope)
        if replacement is not None and replacement["device_id"] != device_id:
            raise ValueError
        identity: Any = replacement
        if request_body is not None:
            if not isinstance(request_body, bytes) or not 0 < len(request_body) <= 512 * 1024:
                raise ValueError
            validate_json_tree(json.loads(request_body, object_pairs_hook=_pairs))
            identity = {"body_sha256": hashlib.sha256(request_body).hexdigest()}
        request_sha256 = hashlib.sha256(_encode({"scope": scope, "operation_id": operation_id,
            "device_id": device_id, "expected_revision": expected_revision, "replacement": identity})).hexdigest()
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError, binascii.Error):
        raise MapJournalError("invalid_map_mutation") from None
    document = load_snapshot(path, scope)
    if document is None:
        raise MapJournalError("map_journal_unavailable")
    if replacement is not None and replacement["unique_id"] != document["slot_id"]:
        raise MapJournalError("invalid_map_mutation")
    try:
        previous = _find_receipt(path, document, operation_id)
    except (OSError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise MapJournalError("map_journal_unavailable") from None
    if previous is not None:
        if previous["device_id"] != device_id or previous["request_sha256"] != request_sha256:
            try:
                _confirm_snapshot(path, document)
            except OSError:
                raise MapJournalError("map_write_unconfirmed") from None
            raise MapMutationRejection("map_operation_conflict", document, operation_id,
                                       device_id, expected_revision, request_sha256)
        try:
            _confirm_snapshot(path, document)
        except OSError:
            raise MapJournalError("map_write_unconfirmed") from None
        return document, deepcopy(previous), True
    if document["revision"] != expected_revision:
        try:
            _confirm_snapshot(path, document)
        except OSError:
            raise MapJournalError("map_write_unconfirmed") from None
        raise MapRevisionConflict(document, operation_id, device_id, expected_revision, request_sha256)
    full = len(document["receipts"]) >= MAX_RECEIPTS
    if (document["generation"] >= MAX_GENERATION
            or (full and len(document.get("receipt_pages", [])) >= MAX_RECEIPT_PAGES)):
        try:
            _confirm_snapshot(path, document)
        except OSError:
            raise MapJournalError("map_write_unconfirmed") from None
        raise MapMutationRejection("map_journal_full", document, operation_id,
                                   device_id, expected_revision, request_sha256)
    try:
        result = _roll_receipts(path, document) if full else deepcopy(document)
    except (OSError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise MapJournalError("map_write_unconfirmed") from None
    result["generation"] += 1
    result["current"] = deepcopy(replacement)
    result["revision"] = _revision(scope, result["epoch"], result["slot_id"], result["generation"], replacement)
    receipt = {"device_id": device_id, "request_sha256": request_sha256,
               "generation": result["generation"], "revision": result["revision"], "current": _projection(replacement)}
    result["receipts"][operation_id] = receipt
    try:
        _validate(result, scope)
        _save(path, result)
        if result["version"] == 3:
            _confirm_snapshot(path, result)
    except (OSError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise MapJournalError("map_write_unconfirmed") from None
    return result, deepcopy(receipt), False
