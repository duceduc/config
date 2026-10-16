"""Real temporary-file journal tests; no HA server, user health data or network."""
import base64
from copy import deepcopy
import importlib
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_statistics import BRIDGE

MODULE = importlib.import_module(BRIDGE.__name__ + ".workout_map_journal")


class WorkoutMapJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.scope = {"entry_id": "entry", "account_id": "owner", "username": "tester"}
        self.path = MODULE.journal_path(self.directory.name, self.scope)
        self.image = {"unique_id": "halthy_tester_phone_workout", "metric_key": "workout",
            "name": "Workout", "content_type": "image/jpeg", "image_base64": base64.b64encode(b"\xff\xd8\xff\x00").decode(),
            "username": "Tester", "device_id": "phone", "updated_at": "2026-10-09T10:00:00Z",
            "attributes": {"workout_uuid": "74d459fe-20aa-4180-b19c-e198b3a712ac",
                "workout_start": "2026-10-09T09:00:00Z", "workout_end": "2026-10-09T10:00:00Z",
                "workout_duration_s": 3600, "route_points": [{"latitude": 1, "longitude": 2}]}}

    def initialize(self, current=None):
        return MODULE.initialize_journal(self.path, self.scope, current, self.image["unique_id"])

    def load(self):
        return MODULE.load_snapshot(self.path, self.scope)

    def commit(self, snapshot, replacement, operation_id=None, device_id="phone"):
        return MODULE.commit_mutation(self.path, self.scope, operation_id=operation_id or str(uuid4()),
            device_id=device_id, expected_revision=snapshot["revision"], replacement=replacement)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(MODULE.MapJournalError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("route_points", str(raised.exception))

    def test_explicit_initialization_is_private_durable_idempotent_and_scope_bound(self):
        before = deepcopy(self.image)
        first = self.initialize(self.image)
        self.assertEqual(first, self.load())
        self.assertEqual(self.initialize(None), first)
        self.assertEqual(self.image, before)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(first["generation"], 0)
        self.assertEqual(first["receipts"], {})
        self.assertNotIn("owner", self.path.name)
        for key in self.scope:
            scope = {**self.scope, key: self.scope[key] + "_other"}
            self.assertNotEqual(MODULE.journal_path(self.directory.name, scope), self.path)
            self.assert_code("map_journal_unavailable", MODULE.load_snapshot, self.path, scope)

    def test_success_commits_self_contained_map_and_receipt_in_one_document(self):
        snapshot = self.initialize()
        document, receipt, replay = self.commit(snapshot, self.image)
        self.assertFalse(replay)
        self.assertEqual(document, self.load())
        self.assertEqual(receipt["generation"], 1)
        self.assertEqual(receipt["revision"], document["revision"])
        self.assertEqual(document["current"], self.image)
        self.assertEqual(set(receipt), {"device_id", "request_sha256", "generation", "revision", "current"})
        self.assertNotIn("route_points", json.dumps(receipt))
        self.assertNotIn("image_base64", json.dumps(receipt))

    def test_revision_rejection_confirms_disk_and_preserves_exception_redaction(self):
        initial = self.initialize()
        written, _, _ = self.commit(initial, self.image)
        operation = str(uuid4())
        before = self.path.read_bytes()
        with patch.object(MODULE, "_confirm_file", wraps=MODULE._confirm_file) as confirmed:
            with self.assertRaises(MODULE.MapRevisionConflict) as raised:
                self.commit(initial, None, operation)
        confirmed.assert_called_once_with(self.path)
        error = raised.exception
        self.assertEqual(str(error), "map_revision_conflict")
        self.assertNotIn(operation, repr(error))
        self.assertNotIn("workout_uuid", repr(error))
        self.assertEqual(error.proof["state"]["generation"], written["generation"])
        self.assertEqual(error.proof["operation_id"], operation)
        self.assertIs(error.proof["applied"], False)
        self.assertEqual(self.path.read_bytes(), before)

    def test_revision_rejection_never_returns_evidence_when_disk_confirmation_fails(self):
        initial = self.initialize()
        self.commit(initial, self.image)
        with patch.object(MODULE, "_confirm_file", side_effect=OSError("synthetic private failure")):
            with self.assertRaises(MODULE.MapJournalError) as raised:
                self.commit(initial, None)
        self.assertEqual(raised.exception.code, "map_write_unconfirmed")
        self.assertFalse(hasattr(raised.exception, "proof"))

    def test_exact_lost_ack_replay_is_unchanged_and_does_not_write_or_advance_generation(self):
        before = self.initialize()
        operation = str(uuid4())
        first, receipt, _ = self.commit(before, self.image, operation)
        raw = self.path.read_bytes()
        with patch.object(MODULE, "_save", side_effect=AssertionError("replay must not rewrite")):
            replayed, replay_receipt, replay = self.commit(before, self.image, operation)
        self.assertTrue(replay)
        self.assertEqual(first, replayed)
        self.assertEqual(receipt, replay_receipt)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_old_replay_after_newer_write_confirms_old_receipt_without_overwriting_latest(self):
        initial = self.initialize()
        old_operation = str(uuid4())
        first, old_receipt, _ = self.commit(initial, self.image, old_operation)
        changed = deepcopy(self.image); changed["attributes"]["workout_duration_s"] = 3700
        second, _, _ = self.commit(first, changed)
        latest, receipt, replay = self.commit(initial, self.image, old_operation)
        self.assertTrue(replay)
        self.assertEqual(latest, second)
        self.assertEqual(receipt, old_receipt)
        self.assertNotEqual(receipt["revision"], latest["revision"])
        self.assertEqual(self.load(), second)

    def test_same_operation_cannot_change_bytes_metadata_precondition_or_device(self):
        initial = self.initialize()
        operation = str(uuid4())
        committed, _, _ = self.commit(initial, self.image, operation)
        for kind in ("bytes", "metadata", "precondition", "device", "clear"):
            replacement = deepcopy(self.image); scope = initial
            device = "phone"
            if kind == "bytes": replacement["image_base64"] = base64.b64encode(b"\xff\xd8\xff\x01").decode()
            elif kind == "metadata": replacement["attributes"]["workout_duration_s"] = 3700
            elif kind == "precondition": scope = committed
            elif kind == "device": device = "other_phone"; replacement["device_id"] = device
            else: replacement = None
            self.assert_code("map_operation_conflict", self.commit, scope, replacement, operation, device)
            self.assertEqual(self.load(), committed)

    def test_stale_new_operation_fails_without_disk_or_receipt_mutation(self):
        initial = self.initialize()
        latest, _, _ = self.commit(initial, self.image)
        raw = self.path.read_bytes()
        self.assert_code("map_revision_conflict", self.commit, initial, None)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.load(), latest)

    def test_clear_tombstone_and_older_fallback_require_exact_revision(self):
        initial = self.initialize(self.image)
        cleared, receipt, _ = self.commit(initial, None)
        self.assertIsNone(cleared["current"])
        self.assertIsNone(receipt["current"])
        self.assertNotEqual(initial["revision"], cleared["revision"])
        self.assert_code("map_revision_conflict", self.commit, initial, self.image)
        fallback = deepcopy(self.image)
        fallback["attributes"]["workout_uuid"] = str(uuid4())
        fallback["attributes"]["workout_start"] = "2026-10-08T09:00:00Z"
        fallback["attributes"]["workout_end"] = "2026-10-08T10:00:00Z"
        current, _, _ = self.commit(cleared, fallback)
        self.assertEqual(current["current"], fallback)

    def test_empty_replace_clear_empty_cycle_never_reuses_absence_revision(self):
        first = self.initialize()
        with_image, _, _ = self.commit(first, self.image)
        empty, _, _ = self.commit(with_image, None)
        self.assertIsNone(empty["current"])
        self.assertNotEqual(empty["revision"], first["revision"])
        self.assert_code("map_revision_conflict", self.commit, first, self.image)

    def test_missing_journal_never_implicitly_reinitializes_and_new_epoch_rejects_old_request(self):
        initial = self.initialize()
        self.path.unlink()
        self.assert_code("map_journal_unavailable", self.commit, initial, self.image)
        fresh = self.initialize()
        self.assertNotEqual(fresh["epoch"], initial["epoch"])
        self.assertNotEqual(fresh["revision"], initial["revision"])
        self.assert_code("map_revision_conflict", self.commit, initial, self.image)

    def test_write_and_replace_failures_leave_old_complete_document_and_no_temp_files(self):
        initial = self.initialize()
        raw = self.path.read_bytes()
        for target in ("fsync", "replace"):
            with patch.object(MODULE.os, target, side_effect=OSError("private failure details")):
                self.assert_code("map_write_unconfirmed", self.commit, initial, self.image)
            self.assertEqual(self.path.read_bytes(), raw)
            self.assertEqual(list(Path(self.directory.name).iterdir()), [self.path])

    def test_failure_after_replace_is_uncertain_and_exact_retry_recovers_saved_receipt(self):
        initial = self.initialize()
        operation = str(uuid4())
        with patch.object(MODULE, "_confirm_file", side_effect=OSError("directory sync failed")):
            self.assert_code("map_write_unconfirmed", self.commit, initial, self.image, operation)
        saved = self.load()
        self.assertEqual(saved["current"], self.image)
        self.assertIn(operation, saved["receipts"])
        latest, receipt, replay = self.commit(initial, self.image, operation)
        self.assertTrue(replay)
        self.assertEqual(latest, saved)
        self.assertEqual(receipt, saved["receipts"][operation])

    def test_replay_and_initialization_do_not_acknowledge_failed_durability_confirmation(self):
        initial = self.initialize()
        operation = str(uuid4())
        self.commit(initial, self.image, operation)
        with patch.object(MODULE, "_confirm_file", side_effect=OSError("sync failed")):
            self.assert_code("map_write_unconfirmed", self.commit, initial, self.image, operation)
            self.assert_code("map_write_unconfirmed", self.initialize, None)

    def test_corrupt_empty_oversized_duplicate_or_non_finite_storage_never_resets(self):
        initial = self.initialize()
        for raw in (b"", b"{", b"x" * (MODULE.MAX_BYTES + 1),
                    b'{"version":1,"version":1}', b'{"version":NaN}', b"[" * 1500 + b"]" * 1500):
            self.path.write_bytes(raw)
            self.assert_code("map_journal_unavailable", self.load)
            self.assert_code("map_journal_unavailable", self.initialize, None)
            self.assert_code("map_journal_unavailable", self.commit, initial, self.image)
            self.assertEqual(self.path.read_bytes(), raw)

    def test_invalid_stored_schema_revision_receipts_and_images_fail_closed(self):
        initial = self.initialize()
        valid, _, _ = self.commit(initial, self.image)
        for kind in ("scope", "version", "epoch", "revision", "generation", "receipt-hash", "receipt-generation", "receipt-projection", "receipt-device", "extra"):
            doc = deepcopy(valid)
            if kind == "scope": doc["scope"]["account_id"] = "foreign"
            elif kind == "version": doc["version"] = True
            elif kind == "epoch": doc["epoch"] = "invalid"
            elif kind == "revision": doc["revision"] = "0" * 64
            elif kind == "generation": doc["generation"] = True
            elif kind == "extra": doc["extra"] = 1
            else:
                receipt = next(iter(doc["receipts"].values()))
                if kind == "receipt-hash": receipt["request_sha256"] = "invalid"
                elif kind == "receipt-generation": receipt["generation"] = 0
                elif kind == "receipt-device": receipt["device_id"] = "other_phone"
                else: receipt["current"] = None
            self.path.write_text(json.dumps(doc))
            self.assert_code("map_journal_unavailable", self.load)

    def test_invalid_mutations_and_images_never_touch_disk(self):
        initial = self.initialize()
        raw = self.path.read_bytes()
        for kind in ("uuid", "bytes", "mime", "oversized", "attrs", "nan", "deep", "cycle", "archive", "end", "date", "device", "extra"):
            image = deepcopy(self.image)
            if kind == "uuid": image["attributes"]["workout_uuid"] = "invalid"
            elif kind == "bytes": image["image_base64"] = "%%%%"
            elif kind == "mime": image["content_type"] = "image/png"
            elif kind == "oversized": image["image_base64"] = base64.b64encode(b"\xff\xd8\xff" + b"x" * MODULE.MAX_IMAGE_BYTES).decode()
            elif kind == "attrs": image["attributes"]["huge"] = "x" * MODULE.MAX_ATTRIBUTES_BYTES
            elif kind == "nan": image["attributes"]["invalid"] = float("nan")
            elif kind == "deep":
                value = None
                for _ in range(25): value = [value]
                image["attributes"]["invalid"] = value
            elif kind == "cycle": image["attributes"]["cycle"] = image["attributes"]
            elif kind == "archive": image["attributes"]["archive_relative_path"] = "unconfirmed.jpg"
            elif kind == "end": image["attributes"]["workout_end"] = image["attributes"]["workout_start"]
            elif kind == "date": image["updated_at"] = "2026-10-09T10:00:00"
            elif kind == "device": image["device_id"] = "other_phone"
            else: image["unexpected"] = 1
            self.assert_code("invalid_map_mutation", self.commit, initial, image)
            self.assertEqual(self.path.read_bytes(), raw)
        for operation in ("not-a-uuid", "74D459FE-20AA-4180-B19C-E198B3A712AC", None):
            self.assert_code("invalid_map_mutation", MODULE.commit_mutation, self.path, self.scope,
                operation_id=operation, device_id="phone", expected_revision=initial["revision"], replacement=self.image)

    def test_capacity_does_not_evict_old_receipts_and_old_retry_still_succeeds(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2), patch.object(MODULE, "MAX_RECEIPT_PAGES", 0):
            initial = self.initialize()
            operation = str(uuid4())
            first, _, _ = self.commit(initial, self.image, operation)
            latest, _, _ = self.commit(first, None)
            self.assert_code("map_journal_full", self.commit, latest, self.image)
            replayed, _, replay = self.commit(initial, self.image, operation)
            self.assertTrue(replay)
            self.assertEqual(replayed, latest)

    def test_byte_capacity_failure_does_not_partial_commit(self):
        initial = self.initialize()
        raw = self.path.read_bytes()
        with patch.object(MODULE, "MAX_BYTES", len(raw) + 10):
            self.assert_code("map_write_unconfirmed", self.commit, initial, self.image)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_caller_and_returned_mutations_cannot_change_saved_state(self):
        initial = self.initialize()
        saved, receipt, _ = self.commit(initial, self.image)
        raw = self.path.read_bytes()
        self.image["attributes"]["workout_duration_s"] = 1
        saved["current"]["attributes"]["workout_duration_s"] = 2
        receipt["current"]["workout_uuid"] = str(uuid4())
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.load()["current"]["attributes"]["workout_duration_s"], 3600)

    def test_dictionary_order_does_not_change_retry_identity(self):
        initial = self.initialize()
        operation = str(uuid4())
        first, _, _ = self.commit(initial, self.image, operation)
        reordered = dict(reversed(list(self.image.items())))
        reordered["attributes"] = dict(reversed(list(reordered["attributes"].items())))
        result, _, replay = self.commit(initial, reordered, operation)
        self.assertTrue(replay)
        self.assertEqual(result, first)

    def test_invalid_scope_and_io_failure_never_become_empty_state(self):
        self.assert_code("invalid_map_scope", MODULE.journal_path, self.directory.name, {**self.scope, "username": "Tester"})
        self.path.mkdir()
        self.assert_code("map_journal_unavailable", self.load)
        self.assert_code("map_journal_unavailable", self.initialize, None)

    def test_http_retry_binds_exact_validated_body_not_server_derived_timestamp(self):
        initial = self.initialize()
        operation = str(uuid4())
        body = b'{"synthetic":"exact frozen request"}'
        arguments = {"operation_id": operation, "device_id": "phone", "expected_revision": initial["revision"], "request_body": body}
        first, receipt, _ = MODULE.commit_mutation(self.path, self.scope, replacement=self.image, **arguments)
        derived = deepcopy(self.image); derived["updated_at"] = "2026-10-09T11:00:00Z"
        latest, replay_receipt, replay = MODULE.commit_mutation(self.path, self.scope, replacement=derived, **arguments)
        self.assertTrue(replay)
        self.assertEqual(latest, first)
        self.assertEqual(replay_receipt, receipt)
        self.assert_code("map_operation_conflict", MODULE.commit_mutation, self.path, self.scope,
            replacement=self.image, **{**arguments, "request_body": b'{"synthetic":"changed request"}'})

    def test_stable_image_slot_survives_clear_and_cannot_be_changed_by_a_later_write(self):
        initial = self.initialize(self.image)
        cleared, _, _ = self.commit(initial, None)
        self.assertEqual(cleared["slot_id"], self.image["unique_id"])
        altered = deepcopy(self.image); altered["unique_id"] = "different_slot"
        self.assert_code("invalid_map_mutation", self.commit, cleared, altered)
        self.assertIsNone(self.load()["current"])

    def test_unconfirmed_snapshot_read_does_not_clear_durability_error(self):
        self.initialize()
        with patch.object(MODULE, "_confirm_file", side_effect=OSError("sync failed")):
            self.assert_code("map_write_unconfirmed", MODULE.load_confirmed_snapshot, self.path, self.scope)

    def test_old_foundation_format_is_rejected_not_silently_reinterpreted(self):
        initial = self.initialize()
        initial["version"] = 1
        del initial["slot_id"]
        self.path.write_text(json.dumps(initial))
        self.assert_code("map_journal_unavailable", self.load)

    def fill_page(self, count=2):
        initial = self.initialize()
        state = initial
        requests = []
        for index in range(count):
            operation = str(uuid4())
            image = self.image if index % 2 == 0 else None
            previous = state
            state, receipt, _ = self.commit(previous, image, operation)
            requests.append((previous, image, operation, receipt))
        return initial, state, requests

    def page_files(self):
        return list(Path(self.directory.name).glob(self.path.name + ".receipts.*.json"))

    def test_real_256_boundary_rolls_without_resetting_epoch_slot_revision_or_receipts(self):
        initial, full, requests = self.fill_page(MODULE.MAX_RECEIPTS)
        self.assertEqual(full["version"], 2)
        state, _, replay = self.commit(full, self.image)
        self.assertFalse(replay)
        self.assertEqual(state["version"], 3)
        self.assertEqual(state["generation"], 257)
        self.assertEqual(state["epoch"], initial["epoch"])
        self.assertEqual(state["slot_id"], initial["slot_id"])
        self.assertEqual(len(state["receipts"]), 1)
        self.assertEqual(state["receipt_pages"][0]["count"], 256)
        page = self.page_files()[0]
        self.assertEqual(stat.S_IMODE(page.stat().st_mode), 0o600)
        self.assertNotIn("image_base64", page.read_text())
        self.assertNotIn("route_points", page.read_text())
        frozen, image, operation, receipt = requests[0]
        latest, old, replay = self.commit(frozen, image, operation)
        self.assertTrue(replay)
        self.assertEqual(old, receipt)
        self.assertEqual(latest, state)

    def test_multiple_page_restart_replays_every_old_operation_without_new_writes(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2):
            _, state, requests = self.fill_page(9)
            self.assertEqual(len(state["receipt_pages"]), 4)
            raw = {path: path.read_bytes() for path in Path(self.directory.name).iterdir()}
            with patch.object(MODULE, "_save", side_effect=AssertionError("replay cannot write")):
                self.assertEqual(self.initialize(), state)
                for frozen, image, operation, receipt in requests:
                    latest, old, replay = self.commit(frozen, image, operation)
                    self.assertTrue(replay)
                    self.assertEqual(old, receipt)
                    self.assertEqual(latest, state)
            self.assertEqual({path: path.read_bytes() for path in raw}, raw)

    def test_archived_collision_precedes_stale_revision_and_never_mutates_state(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2):
            initial, state, requests = self.fill_page(5)
            operation = requests[0][2]
            before = self.path.read_bytes()
            with self.assertRaises(MODULE.MapMutationRejection) as raised:
                self.commit(initial, None, operation)
            self.assertEqual(raised.exception.code, "map_operation_conflict")
            self.assertEqual(raised.exception.proof["state"]["generation"], 5)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.load(), state)

    def test_missing_corrupt_or_foreign_page_blocks_reads_replays_new_writes_and_activation(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2):
            initial, state, requests = self.fill_page(3)
            manifest = self.path.read_bytes()
            page = self.page_files()[0]
            raw = page.read_bytes()
            for damage in ("missing", "bytes", "foreign", "range", "duplicate"):
                with self.subTest(damage=damage):
                    self.path.write_bytes(manifest); page.write_bytes(raw)
                    if damage == "missing": page.unlink()
                    elif damage == "bytes": page.write_bytes(b"corrupt")
                    else:
                        content = json.loads(raw)
                        if damage == "foreign": content["scope"]["account_id"] = "foreign"
                        elif damage == "range": content["start_generation"] = 2
                        else:
                            old_key = next(iter(content["receipts"]))
                            content["receipts"][requests[-1][2]] = content["receipts"].pop(old_key)
                        replacement = MODULE._encode(content)
                        digest = hashlib.sha256(replacement).hexdigest()
                        MODULE._page_path(self.path, digest).write_bytes(replacement)
                        doc = json.loads(manifest); doc["receipt_pages"][0]["sha256"] = digest
                        self.path.write_text(json.dumps(doc))
                    before = self.path.read_bytes()
                    self.assert_code("map_journal_unavailable", self.load)
                    self.assert_code("map_journal_unavailable", self.initialize)
                    self.assert_code("map_journal_unavailable", self.commit, initial, self.image, requests[0][2])
                    self.assert_code("map_journal_unavailable", self.commit, state, None)
                    self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_manifest_page_descriptors_fail_closed(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2):
            _, valid, _ = self.fill_page(3)
            for kind in ("extra", "hash", "count", "start", "duplicate", "empty", "gap"):
                doc = deepcopy(valid)
                item = doc["receipt_pages"][0]
                if kind == "extra": item["path"] = "../foreign"
                elif kind == "hash": item["sha256"] = "../foreign"
                elif kind == "count": item["count"] = True
                elif kind == "start": item["start_generation"] = True
                elif kind == "duplicate": doc["receipt_pages"].append(deepcopy(item))
                elif kind == "empty": doc["receipt_pages"] = []
                else: item["count"] = 1
                self.path.write_text(json.dumps(doc))
                self.assert_code("map_journal_unavailable", self.load)

    def test_page_confirmation_failure_leaves_old_manifest_and_identical_retry_reuses_orphan(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2):
            _, full, _ = self.fill_page()
            before = self.path.read_bytes()
            operation = str(uuid4())
            confirm = MODULE._confirm_file
            def fail_page(path):
                if path != self.path: raise OSError("synthetic page confirmation failure")
                confirm(path)
            with patch.object(MODULE, "_confirm_file", side_effect=fail_page):
                self.assert_code("map_write_unconfirmed", self.commit, full, self.image, operation)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(len(self.page_files()), 1)
            page = self.page_files()[0]; old = page.read_bytes()
            state, receipt, replay = self.commit(full, self.image, operation)
            self.assertFalse(replay)
            self.assertEqual(receipt["generation"], 3)
            self.assertEqual(page.read_bytes(), old)
            self.assertEqual(self.load(), state)

    def test_manifest_save_failure_never_loses_old_receipts_or_consumes_attempt(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2):
            _, full, requests = self.fill_page()
            before = self.path.read_bytes()
            save = MODULE._save
            def fail_manifest(path, document):
                if path == self.path: raise OSError("synthetic manifest failure")
                save(path, document)
            operation = str(uuid4())
            with patch.object(MODULE, "_save", side_effect=fail_manifest):
                self.assert_code("map_write_unconfirmed", self.commit, full, self.image, operation)
            self.assertEqual(self.path.read_bytes(), before)
            frozen, image, old_operation, receipt = requests[0]
            self.assertEqual(self.commit(frozen, image, old_operation)[1], receipt)
            state, _, replay = self.commit(full, self.image, operation)
            self.assertFalse(replay)
            self.assertEqual(state["generation"], 3)

    def test_manifest_confirmation_failure_after_replace_recovers_exact_original_receipt(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2):
            _, full, _ = self.fill_page()
            operation = str(uuid4())
            confirm = MODULE._confirm_file
            def fail_manifest(path):
                if path == self.path: raise OSError("synthetic manifest confirmation failure")
                confirm(path)
            with patch.object(MODULE, "_confirm_file", side_effect=fail_manifest):
                self.assert_code("map_write_unconfirmed", self.commit, full, self.image, operation)
            saved = self.load()
            self.assertEqual(saved["generation"], 3)
            latest, receipt, replay = self.commit(full, self.image, operation)
            self.assertTrue(replay)
            self.assertEqual(latest, saved)
            self.assertEqual(receipt, saved["receipts"][operation])

    def test_page_confirmation_failure_cannot_ack_replay_or_prove_non_delivery(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2):
            initial, state, requests = self.fill_page(3)
            confirm = MODULE._confirm_file
            def fail_page(path):
                if path != self.path: raise OSError("synthetic page confirmation failure")
                confirm(path)
            with patch.object(MODULE, "_confirm_file", side_effect=fail_page):
                for frozen, image, operation, _ in requests:
                    self.assert_code("map_write_unconfirmed", self.commit, frozen, image, operation)
                self.assert_code("map_write_unconfirmed", MODULE.load_confirmed_snapshot, self.path, self.scope)
                self.assert_code("map_write_unconfirmed", self.initialize)
                with self.assertRaises(MODULE.MapJournalError) as raised:
                    self.commit(initial, None)
                self.assertFalse(hasattr(raised.exception, "proof"))
            self.assertEqual(self.load(), state)

    def test_finite_page_limit_still_replays_old_receipts_without_eviction(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2), patch.object(MODULE, "MAX_RECEIPT_PAGES", 1):
            initial, state, requests = self.fill_page(4)
            before = self.path.read_bytes()
            self.assert_code("map_journal_full", self.commit, state, self.image)
            for frozen, image, operation, receipt in requests:
                latest, old, replay = self.commit(frozen, image, operation)
                self.assertTrue(replay); self.assertEqual(old, receipt); self.assertEqual(latest, state)
            self.assertEqual(self.path.read_bytes(), before)

    def test_archived_confirmation_is_required_for_every_kind_of_non_delivery_proof(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2), patch.object(MODULE, "MAX_RECEIPT_PAGES", 1):
            initial, state, requests = self.fill_page(4)
            confirm = MODULE._confirm_file
            def fail_page(path):
                if path != self.path: raise OSError("synthetic archived confirmation failure")
                confirm(path)
            attempts = [(initial, None, str(uuid4())), (initial, None, requests[0][2]),
                        (state, self.image, str(uuid4()))]
            with patch.object(MODULE, "_confirm_file", side_effect=fail_page):
                for frozen, image, operation in attempts:
                    with self.assertRaises(MODULE.MapJournalError) as raised:
                        self.commit(frozen, image, operation)
                    self.assertEqual(raised.exception.code, "map_write_unconfirmed")
                    self.assertFalse(hasattr(raised.exception, "proof"))

    def test_corrupt_orphan_page_is_not_overwritten_or_adopted(self):
        with patch.object(MODULE, "MAX_RECEIPTS", 2):
            _, full, _ = self.fill_page()
            original = MODULE._save
            def fail_manifest(path, doc):
                if path == self.path: raise OSError("synthetic crash before manifest")
                original(path, doc)
            with patch.object(MODULE, "_save", side_effect=fail_manifest):
                self.assert_code("map_write_unconfirmed", self.commit, full, self.image)
            orphan = self.page_files()[0]; orphan.write_bytes(b"corrupt orphan")
            self.assert_code("map_write_unconfirmed", self.commit, full, self.image)
            self.assertEqual(orphan.read_bytes(), b"corrupt orphan")
            self.assertEqual(self.load(), full)
