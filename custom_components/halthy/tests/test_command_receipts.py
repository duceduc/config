"""Atomic receipt persistence and real endpoint replay with local HA stubs."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from test_statistics import BRIDGE


class CommandReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.command_id = str(uuid4())
        self.command = {"id": self.command_id, "type": "force_upload", "requested_at": "2026-10-06T12:00:00Z"}
        self.runtime = BRIDGE.IntegrationRuntime(configured_username="tester", app_username="tester",
            display_name="Tester", owner_user_id="owner", pending_force_upload_command=self.command.copy())

        async def execute(function, *args):
            return function(*args)

        self.hass = types.SimpleNamespace(data={BRIDGE.DOMAIN: {"entries": {"entry": self.runtime}}},
            config=types.SimpleNamespace(path=lambda sub: self.directory.name + "/" + sub), async_add_executor_job=execute)

    def request(self, command_id=None, status="completed", user="owner"):
        return types.SimpleNamespace(app={BRIDGE.KEY_HASS: self.hass}, query={"username": "tester"},
            json=AsyncMock(return_value={"username": "tester", "command_id": command_id or self.command_id, "status": status}),
            get=lambda key: types.SimpleNamespace(id=user) if user is not None else None)

    async def ack(self, **kwargs):
        with patch.object(BRIDGE, "_schedule_store_save"):
            return await BRIDGE.HalthyCommandAckView().post(self.request(**kwargs))

    def journal(self):
        return BRIDGE.receipt_path(self.hass.config.path(".storage"), "entry", "owner")

    async def test_lost_ack_can_retry_after_command_is_forgotten(self):
        first = await self.ack()
        self.assertTrue(first["args"][0]["acknowledged"])
        self.assertIsNone(self.runtime.pending_force_upload_command)
        second = await self.ack()
        self.assertEqual(second["args"][0], {"ok": True, "acknowledged": True, "pending_command_id": self.command_id})

    async def test_receipt_survives_runtime_reload_and_does_not_clear_new_command(self):
        await self.ack()
        newer = {**self.command, "id": str(uuid4())}
        self.runtime.pending_force_upload_command = newer
        reply = await self.ack()
        self.assertTrue(reply["args"][0]["acknowledged"])
        self.assertEqual(self.runtime.pending_force_upload_command, newer)

    async def test_replayed_ack_result_is_immutable(self):
        await self.ack()
        reply = await self.ack(status="failed")
        self.assertEqual(reply["kwargs"]["status"], 409)

    async def test_failed_result_replays_without_clearing_newer_command(self):
        first = await self.ack(status="failed")
        self.assertTrue(first["args"][0]["acknowledged"])
        self.assertIsNone(self.runtime.pending_force_upload_command)
        self.assertEqual(self.runtime.last_force_upload_ack_status, "failed")
        raw = self.journal().read_bytes()
        receipt = json.loads(raw)["receipts"][self.command_id]
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(set(receipt), {"status", "acknowledged_at"})
        newer = {**self.command, "id": str(uuid4())}
        self.runtime.pending_force_upload_command = newer
        reply = await self.ack(status="failed")
        self.assertEqual(reply["args"][0], {"ok": True, "acknowledged": True, "pending_command_id": self.command_id})
        self.assertEqual(self.runtime.pending_force_upload_command, newer)
        self.assertEqual(self.journal().read_bytes(), raw)

    async def test_failed_result_cannot_later_be_reported_as_completed(self):
        await self.ack(status="failed")
        raw = self.journal().read_bytes()
        newer = {**self.command, "id": str(uuid4())}
        self.runtime.pending_force_upload_command = newer
        reply = await self.ack(status="completed")
        self.assertEqual(reply["kwargs"]["status"], 409)
        self.assertEqual(self.runtime.pending_force_upload_command, newer)
        self.assertEqual(self.journal().read_bytes(), raw)
        self.assertEqual(self.runtime.last_force_upload_ack_status, "failed")

    async def test_failed_write_never_forgets_or_acknowledges_command(self):
        with patch.object(BRIDGE, "save_receipt", side_effect=OSError("simulated")):
            reply = await self.ack()
        self.assertEqual(reply["kwargs"]["status"], 503)
        self.assertEqual(self.runtime.pending_force_upload_command, self.command)

    async def test_corrupt_journal_fails_closed_without_replacing_bytes(self):
        path = self.journal()
        path.parent.mkdir(parents=True)
        path.write_text("broken")
        reply = await self.ack()
        self.assertEqual(reply["kwargs"]["status"], 503)
        self.assertEqual(path.read_text(), "broken")
        self.assertEqual(self.runtime.pending_force_upload_command, self.command)

    async def test_malformed_receipts_are_not_accepted_or_silently_reset(self):
        path = self.journal()
        path.parent.mkdir(parents=True)
        base = {"status": "completed", "acknowledged_at": datetime.now(timezone.utc).isoformat()}
        for receipt in [{**base, "health_value": 90}, {**base, "acknowledged_at": 123},
                        {**base, "status": []}, {**base, "acknowledged_at": "2026-10-06T12:00:00"},
                        {**base, "acknowledged_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}]:
            with self.subTest(receipt=receipt):
                raw = json.dumps({"version": 1, "receipts": {self.command_id: receipt}})
                path.write_text(raw)
                reply = await self.ack()
                self.assertEqual(reply["kwargs"]["status"], 503)
                self.assertEqual(path.read_text(), raw)
                self.assertEqual(self.runtime.pending_force_upload_command, self.command)

    async def test_unknown_ack_does_not_claim_ownerless_entry(self):
        self.runtime.owner_user_id = None
        reply = await self.ack(command_id=str(uuid4()))
        self.assertFalse(reply["args"][0]["acknowledged"])
        self.assertIsNone(self.runtime.owner_user_id)

    async def test_other_account_cannot_read_receipt_or_acknowledge(self):
        await self.ack()
        reply = await self.ack(user="other")
        self.assertEqual(reply["kwargs"]["status"], 403)

    async def test_restored_pending_command_is_not_polled_again_after_durable_ack(self):
        await self.ack()
        self.runtime.pending_force_upload_command = self.command.copy()
        with patch.object(BRIDGE, "_schedule_store_save"):
            reply = await BRIDGE.HalthyCommandView().get(self.request())
        self.assertIsNone(reply["args"][0]["command"])
        self.assertIsNone(self.runtime.pending_force_upload_command)

    async def test_cancellation_joins_receipt_job_before_releasing_runtime_lock(self):
        started, released = asyncio.Event(), asyncio.Event()

        async def execute(function, *args):
            if function == BRIDGE.save_receipt:
                started.set()
                await released.wait()
            return function(*args)

        self.hass.async_add_executor_job = execute
        task = asyncio.create_task(self.ack())
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(self.runtime.lock.locked())
        released.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.runtime.pending_force_upload_command, self.command)
        reply = await self.ack()
        self.assertTrue(reply["args"][0]["acknowledged"])

    async def test_expired_receipts_do_not_manufacture_ack(self):
        old = datetime.now(timezone.utc) - timedelta(days=31)
        BRIDGE.save_receipt(self.journal(), {}, self.command_id, "completed", old)
        self.runtime.pending_force_upload_command = None
        reply = await self.ack()
        self.assertFalse(reply["args"][0]["acknowledged"])

    def test_capacity_failure_does_not_evict_existing_receipts(self):
        now = datetime.now(timezone.utc)
        receipt = {"status": "completed", "acknowledged_at": now.isoformat()}
        receipts = {str(uuid4()): receipt for _ in range(20_000)}
        with self.assertRaises(ValueError):
            BRIDGE.save_receipt(self.journal(), receipts, self.command_id, "completed", now)
        self.assertFalse(self.journal().exists())

    def test_path_is_account_scoped_and_cannot_escape_storage(self):
        base = self.directory.name
        one = BRIDGE.receipt_path(base, "../../entry", "owner")
        two = BRIDGE.receipt_path(base, "../../entry", "other")
        self.assertEqual(one.parent, Path(base))
        self.assertNotEqual(one, two)

    def test_private_journal_contains_no_health_values_or_tokens(self):
        BRIDGE.save_receipt(self.journal(), {}, self.command_id, "completed", datetime.now(timezone.utc))
        raw = self.journal().read_text()
        self.assertEqual(set(json.loads(raw)["receipts"][self.command_id]), {"status", "acknowledged_at"})
        self.assertEqual(self.journal().stat().st_mode & 0o777, 0o600)
