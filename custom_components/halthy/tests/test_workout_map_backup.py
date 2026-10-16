"""Detached synthetic backup tests; never capture real HA or app data."""
import base64
from copy import deepcopy
import importlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_statistics import BRIDGE

MODULE = importlib.import_module(BRIDGE.__name__ + ".workout_map_backup")
JOURNAL = importlib.import_module(BRIDGE.__name__ + ".workout_map_journal")


class WorkoutMapBackupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.live = self.root / "original"
        self.backup = self.root / "detached"
        self.live.mkdir()
        self.backup.mkdir()
        self.scope = {"entry_id": "entry", "account_id": "owner", "username": "tester"}
        self.path = JOURNAL.journal_path(str(self.live), self.scope)
        self.target = self.backup / self.path.name
        self.image = {"unique_id": "halthy_tester_phone_workout", "metric_key": "workout",
            "name": "Workout", "content_type": "image/jpeg", "image_base64": base64.b64encode(b"\xff\xd8\xff\x00").decode(),
            "username": "Tester", "device_id": "phone", "updated_at": "2026-10-09T10:00:00Z",
            "attributes": {"workout_uuid": "74d459fe-20aa-4180-b19c-e198b3a712ac",
                "workout_start": "2026-10-09T09:00:00Z", "workout_end": "2026-10-09T10:00:00Z",
                "route_points": [{"latitude": 1, "longitude": 2}]}}

    def create(self, count=0):
        state = JOURNAL.initialize_journal(self.path, self.scope, None, self.image["unique_id"])
        requests = []
        for index in range(count):
            previous = state
            image = deepcopy(self.image) if index % 2 == 0 else None
            operation = str(uuid4())
            state, receipt, replay = JOURNAL.commit_mutation(self.path, self.scope,
                operation_id=operation, device_id="phone", expected_revision=previous["revision"], replacement=image)
            self.assertFalse(replay)
            requests.append((previous, image, operation, receipt))
        return state, requests

    def copy(self):
        for path in self.live.iterdir():
            shutil.copyfile(path, self.backup / path.name)

    def capture(self, path=None):
        return MODULE.capture_map_backup_inventory(path or self.path, self.scope)

    def verify(self, inventory):
        MODULE.verify_map_backup_inventory(self.target, self.scope, inventory)

    def assert_code(self, code, operation, *args):
        with self.assertRaises(MODULE.MapBackupError) as raised:
            operation(*args)
        self.assertEqual(str(raised.exception), code)
        self.assertNotIn("tester", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_v2_empty_and_mutated_snapshots_are_exact_read_only_and_payload_free(self):
        self.create()
        for mutation in (False, True):
            if mutation:
                state = JOURNAL.load_snapshot(self.path, self.scope)
                JOURNAL.commit_mutation(self.path, self.scope, operation_id=str(uuid4()), device_id="phone",
                    expected_revision=state["revision"], replacement=self.image)
            before = {path.name: path.read_bytes() for path in self.live.iterdir()}
            with patch.object(JOURNAL, "_save", side_effect=AssertionError("cannot write")), \
                    patch.object(JOURNAL, "_confirm_file", side_effect=AssertionError("cannot fsync")):
                inventory = self.capture()
                self.copy()
                self.verify(inventory)
            self.assertEqual(inventory["generation"], int(mutation))
            self.assertEqual(inventory["journal_version"], 2)
            self.assertEqual(len(inventory["files"]), 1)
            self.assertEqual(before, {path.name: path.read_bytes() for path in self.live.iterdir()})
            for private in ("route_points", "image_base64", "latitude", "request_sha256"):
                self.assertNotIn(private, json.dumps(inventory))

    def test_real_256_boundary_copy_replays_old_receipt_without_advancing_restored_map(self):
        state, requests = self.create(257)
        inventory = self.capture()
        self.assertEqual(inventory["journal_version"], 3)
        self.assertEqual(len(inventory["files"]), 2)
        self.copy()
        self.verify(inventory)
        before = {path.name: path.read_bytes() for path in self.backup.iterdir()}
        frozen, image, operation, receipt = requests[0]
        latest, delivered, replay = JOURNAL.commit_mutation(self.target, self.scope,
            operation_id=operation, device_id="phone", expected_revision=frozen["revision"], replacement=image)
        self.assertTrue(replay)
        self.assertEqual(delivered, receipt)
        self.assertEqual(latest, state)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.backup.iterdir()})
        self.verify(inventory)

    def test_every_page_in_multiple_page_backup_is_required(self):
        with patch.object(JOURNAL, "MAX_RECEIPTS", 2):
            self.create(9)
            inventory = self.capture()
            self.assertEqual(len(inventory["files"]), 5)
            self.copy()
            for record in inventory["files"]:
                path = self.backup / record["name"]
                raw = path.read_bytes()
                path.unlink()
                self.assert_code("map_backup_mismatch", self.verify, inventory)
                path.write_bytes(raw)
            self.verify(inventory)

    def test_missing_manifest_never_becomes_empty_backup_success(self):
        self.assert_code("map_backup_unavailable", self.capture)
        self.create()
        inventory = self.capture()
        self.assert_code("map_backup_mismatch", self.verify, inventory)

    def test_valid_but_older_or_newer_copy_cannot_match_trusted_inventory(self):
        self.create()
        initial = self.capture()
        self.copy()
        old_bytes = self.target.read_bytes()
        state = JOURNAL.load_snapshot(self.path, self.scope)
        JOURNAL.commit_mutation(self.path, self.scope, operation_id=str(uuid4()), device_id="phone",
            expected_revision=state["revision"], replacement=self.image)
        newest = self.capture()
        self.assert_code("map_backup_mismatch", self.verify, newest)
        self.copy()
        self.assert_code("map_backup_mismatch", self.verify, initial)
        self.verify(newest)
        self.target.write_bytes(old_bytes)
        self.verify(initial)

    def test_manifest_byte_changes_fail_even_when_semantic_state_is_identical(self):
        self.create()
        inventory = self.capture()
        self.copy()
        self.target.write_bytes(self.target.read_bytes() + b"\n")
        self.assertIsNotNone(JOURNAL.load_snapshot(self.target, self.scope))
        self.assert_code("map_backup_mismatch", self.verify, inventory)

    def test_corrupt_page_failures_do_not_mutate_original(self):
        with patch.object(JOURNAL, "MAX_RECEIPTS", 2):
            self.create(3)
            inventory = self.capture()
            original = {path.name: path.read_bytes() for path in self.live.iterdir()}
            self.copy()
            page = self.backup / inventory["files"][1]["name"]
            raw = page.read_bytes()
            for replacement in (b"", raw[:-1], raw + b"\n", b"x" * (JOURNAL.MAX_BYTES + 1)):
                page.write_bytes(replacement)
                self.assert_code("map_backup_mismatch", self.verify, inventory)
            page.write_bytes(raw)
            self.verify(inventory)
            self.assertEqual(original, {path.name: path.read_bytes() for path in self.live.iterdir()})

    def test_rehashed_foreign_or_invalid_page_is_not_a_valid_capture(self):
        with patch.object(JOURNAL, "MAX_RECEIPTS", 2):
            self.create(3)
            self.copy()
            manifest_raw = self.target.read_bytes()
            manifest = json.loads(manifest_raw)
            descriptor = manifest["receipt_pages"][0]
            page = json.loads((self.backup / JOURNAL._page_path(self.target, descriptor["sha256"]).name).read_bytes())
            variants = [
                {**deepcopy(page), "scope": {**self.scope, "account_id": "foreign"}},
                {**deepcopy(page), "epoch": str(uuid4())},
                {**deepcopy(page), "slot_id": "foreign"},
                {**deepcopy(page), "start_generation": True},
                {**deepcopy(page), "receipts": {}},
            ]
            for variant in variants:
                raw = JOURNAL._encode(variant)
                digest = MODULE.hashlib.sha256(raw).hexdigest()
                (self.backup / JOURNAL._page_path(self.target, digest).name).write_bytes(raw)
                changed = deepcopy(manifest)
                changed["receipt_pages"][0]["sha256"] = digest
                self.target.write_bytes(JOURNAL._encode(changed))
                self.assert_code("map_backup_unavailable", self.capture, self.target)
            self.target.write_bytes(manifest_raw)
            self.assertEqual(self.capture(self.target), self.capture())

    def test_rehashed_duplicate_operation_across_pages_is_not_a_valid_capture(self):
        with patch.object(JOURNAL, "MAX_RECEIPTS", 2):
            self.create(5)
            self.copy()
            manifest = json.loads(self.target.read_bytes())
            first = manifest["receipt_pages"][0]
            second = manifest["receipt_pages"][1]
            first_page = json.loads(JOURNAL._page_path(self.target, first["sha256"]).read_bytes())
            second_page = json.loads(JOURNAL._page_path(self.target, second["sha256"]).read_bytes())
            operation, receipt = next(iter(second_page["receipts"].items()))
            del second_page["receipts"][operation]
            second_page["receipts"][next(iter(first_page["receipts"]))] = receipt
            raw = JOURNAL._encode(second_page)
            digest = MODULE.hashlib.sha256(raw).hexdigest()
            JOURNAL._page_path(self.target, digest).write_bytes(raw)
            manifest["receipt_pages"][1]["sha256"] = digest
            self.target.write_bytes(JOURNAL._encode(manifest))
            self.assert_code("map_backup_unavailable", self.capture, self.target)

    def test_in_place_file_change_is_rejected_before_decoding_or_returning_inventory(self):
        self.create()
        reader = MODULE.os.read
        changed = False
        def change(descriptor, size):
            nonlocal changed
            raw = reader(descriptor, size)
            if not changed:
                changed = True
                self.path.write_bytes(self.path.read_bytes() + b"\n")
            return raw
        with patch.object(MODULE.os, "read", side_effect=change):
            self.assert_code("map_backup_unavailable", self.capture)
        self.assertTrue(changed)

    def test_different_scope_and_renamed_manifest_are_rejected(self):
        self.create()
        inventory = self.capture()
        self.copy()
        for key in self.scope:
            scope = {**self.scope, key: "foreign"}
            self.assert_code("map_backup_mismatch", MODULE.verify_map_backup_inventory,
                self.target, scope, inventory)
        renamed = self.backup / "different.json"
        shutil.copyfile(self.target, renamed)
        self.assert_code("map_backup_unavailable", self.capture, renamed)

    def test_invalid_inventory_types_paths_duplicates_and_metadata_are_rejected(self):
        with patch.object(JOURNAL, "MAX_RECEIPTS", 2):
            self.create(3)
            inventory = self.capture()
            self.copy()
            variants = []
            for key, value in (("version", True), ("generation", True), ("journal_version", True),
                    ("epoch", str(uuid4())), ("slot_id", "foreign"), ("revision", "a" * 64),
                    ("kind", "complete_ha_backup"), ("extra", "unexpected")):
                variants.append({**deepcopy(inventory), key: value})
            for key, value in (("bytes", True), ("name", "../secret"), ("sha256", "z" * 64)):
                changed = deepcopy(inventory)
                changed["files"][0][key] = value
                variants.append(changed)
            variants.append({**deepcopy(inventory), "files": []})
            changed = deepcopy(inventory)
            changed["files"].append(changed["files"][1])
            variants.append(changed)
            variants.append({**deepcopy(inventory), "files": list(reversed(inventory["files"]))})
            for changed in variants:
                self.assert_code("map_backup_mismatch", self.verify, changed)
            self.verify(inventory)

    def test_manifest_and_page_symlinks_and_directory_indirection_are_rejected(self):
        with patch.object(JOURNAL, "MAX_RECEIPTS", 2):
            self.create(3)
            inventory = self.capture()
            self.copy()
            for record in inventory["files"]:
                target = self.backup / record["name"]
                raw = target.read_bytes()
                target.unlink()
                target.symlink_to(self.live / record["name"])
                self.assert_code("map_backup_mismatch", self.verify, inventory)
                target.unlink()
                target.write_bytes(raw)
            alias = self.root / "alias"
            alias.symlink_to(self.backup, target_is_directory=True)
            self.assert_code("map_backup_unavailable", self.capture, alias / self.path.name)

    def test_non_regular_manifest_never_blocks_on_fifo_or_accepts_directory(self):
        self.target.mkdir()
        self.assert_code("map_backup_unavailable", self.capture, self.target)
        self.target.rmdir()
        MODULE.os.mkfifo(self.target)
        self.assert_code("map_backup_unavailable", self.capture, self.target)

    def test_changing_manifest_during_capture_cannot_mix_generations(self):
        with patch.object(JOURNAL, "MAX_RECEIPTS", 2):
            self.create(3)
            reader = MODULE._read_file
            reads = 0
            def change(directory, name):
                nonlocal reads
                raw = reader(directory, name)
                reads += 1
                if reads == 2:
                    state = JOURNAL.load_snapshot(self.path, self.scope)
                    JOURNAL.commit_mutation(self.path, self.scope, operation_id=str(uuid4()), device_id="phone",
                        expected_revision=state["revision"], replacement=None)
                return raw
            with patch.object(MODULE, "_read_file", side_effect=change):
                self.assert_code("map_backup_unavailable", self.capture)
            self.assertEqual(JOURNAL.load_snapshot(self.path, self.scope)["generation"], 4)

    def test_unreferenced_orphan_pages_are_not_adopted_deleted_or_required(self):
        self.create()
        inventory = self.capture()
        self.copy()
        orphan = self.backup / (self.path.name + ".receipts." + "a" * 64 + ".json")
        orphan.write_bytes(b"not a referenced page")
        self.verify(inventory)
        self.assertEqual(self.capture(self.target), inventory)
        self.assertEqual(orphan.read_bytes(), b"not a referenced page")
