"""Atomic, bounded command acknowledgments independent of delayed entity storage.

Only IDs/status/timestamps, never tokens or health values. One runtime lock owns
each entry/account journal. An IO/validation failure must leave commands pending.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID

MAX_BYTES = 4 * 1024 * 1024
MAX_RECEIPTS = 20_000
RETENTION = timedelta(days=30)


def receipt_path(storage_directory: str, entry_id: str, owner_id: str) -> Path:
    identity = json.dumps([entry_id, owner_id], separators=(",", ":")).encode()
    return Path(storage_directory) / ("halthy_command_receipts_" + hashlib.sha256(identity).hexdigest() + ".json")


def valid_command_id(value: str) -> bool:
    try:
        return isinstance(value, str) and len(value) == 36 and str(UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def load_receipts(path: Path, now: datetime) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Receipt journal exceeds capacity")
    document = json.loads(raw)
    if not isinstance(document, dict) or type(document.get("version")) is not int or document.get("version") != 1:
        raise ValueError("Invalid receipt journal version")
    receipts = document.get("receipts")
    if not isinstance(receipts, dict) or len(receipts) > MAX_RECEIPTS:
        raise ValueError("Invalid receipt journal")
    retained = {}
    for command_id, receipt in receipts.items():
        if (not valid_command_id(command_id) or not isinstance(receipt, dict)
                or set(receipt) != {"status", "acknowledged_at"}
                or not isinstance(receipt.get("status"), str)
                or receipt["status"] not in {"completed", "failed"}
                or not isinstance(receipt.get("acknowledged_at"), str)):
            raise ValueError("Invalid receipt")
        when = datetime.fromisoformat(receipt["acknowledged_at"])
        if when.tzinfo is None or when > now:
            raise ValueError("Invalid receipt timestamp")
        if now - when <= RETENTION:
            retained[command_id] = receipt
    return retained


def save_receipt(path: Path, receipts: dict[str, dict[str, str]], command_id: str, status: str, now: datetime) -> None:
    if not valid_command_id(command_id) or status not in {"completed", "failed"}:
        raise ValueError("Invalid acknowledgment")
    if command_id in receipts and receipts[command_id]["status"] != status:
        raise ValueError("Command result is immutable")
    updated = {**receipts, command_id: {"status": status, "acknowledged_at": now.astimezone(timezone.utc).isoformat()}}
    if len(updated) > MAX_RECEIPTS:
        raise ValueError("Receipt journal is full")
    raw = json.dumps({"version": 1, "receipts": updated}, separators=(",", ":"), sort_keys=True).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("Receipt journal exceeds capacity")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
