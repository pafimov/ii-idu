import copy
import json
import secrets
import time
import unittest
from unittest.mock import patch

from app import connect, session_hash
from engine import ValidationError
from sandbox import SandboxError
from sandbox_store import SandboxConflict, encode
import test_sandbox_service as service_tests


class QueueTests(unittest.TestCase):
    setUp = service_tests.SandboxServiceTests.setUp
    tearDown = service_tests.SandboxServiceTests.tearDown
    add_actor = service_tests.SandboxServiceTests.add_actor
    action = service_tests.SandboxServiceTests.action
    public = service_tests.SandboxServiceTests.public
    demo_ready = service_tests.SandboxServiceTests.demo_ready
    demo_preview = service_tests.SandboxServiceTests.demo_preview
    confirm = service_tests.SandboxServiceTests.confirm
    dispatch = service_tests.SandboxServiceTests.dispatch
    stored_proposal = service_tests.SandboxServiceTests.stored_proposal

    def market(self, closed):
        fake = self.actor["fake"]
        original = fake.preview_order
        def preview(account_id, instrument_uid, *args, **kwargs):
            if instrument_uid in closed and not kwargs.get("allow_waiting"):
                raise SandboxError("TRADING_CLOSED")
            plan = original(account_id, instrument_uid, *args, **kwargs)
            return {**plan, "waiting_market": instrument_uid in closed,
                    "waiting_reason": "Торги закрыты" if instrument_uid in closed else ""}
        return patch.object(fake, "preview_order", side_effect=preview)

    def due(self):
        with connect(self.database) as conn:
            rows = conn.execute("SELECT id,payload FROM sandbox_orders WHERE user_id=? AND status='waiting_market'", (self.actor["owner"],)).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                payload["next_check_at"] = 0
                conn.execute("UPDATE sandbox_orders SET payload=? WHERE id=?", (encode(payload), row["id"]))

    def test_waiting_first_does_not_starve_open_orders_and_reopens_with_same_uuid(self):
        closed = {"tgld-uid"}
        fake = self.actor["fake"]
        with self.market(closed):
            proposal = self.demo_ready()
            waiting = next(order for order in proposal["orders"] if order["instrument_uid"] in closed)
            fingerprint = proposal["fingerprint"]
            self.assertTrue(proposal["waiting_until"])
            self.assertEqual(fake.posts, [])
            self.confirm(proposal)
            for _ in range(5):
                self.dispatch()
                for request_id in fake.posts:
                    fake.fill(request_id)
                self.action("refresh")
            self.assertEqual(len(fake.posts), 4)
            self.assertNotIn(waiting["id"], fake.posts)
            self.assertEqual(next(row for row in self.public()["orders"] if row["id"] == waiting["id"])["status"], "waiting_market")
            closed.clear()
            self.due()
            self.dispatch()
            self.assertEqual(fake.posts[-1], waiting["id"])
            self.assertEqual(len(fake.posts), 5)
            self.assertEqual(fake.remote_orders[waiting["id"]]["plan"]["limit_price"], waiting["limit_price"])
            self.dispatch()
            self.assertEqual(len(fake.posts), 5)
            fake.fill(waiting["id"])
            self.action("refresh")
            self.assertTrue(self.public()["snapshot_reconciled"])
            self.assertEqual(self.stored_proposal(proposal["id"])["fingerprint"], fingerprint)

    def test_all_closed_stays_local_and_poll_is_throttled(self):
        closed = {row["instrument_uid"] for row in self.actor["fake"].catalog()}
        with self.market(closed) as preview:
            proposal = self.demo_ready()
            self.confirm(proposal)
            self.assertTrue(all(order["waiting_market"] for order in proposal["orders"]))
            self.dispatch()
            checks = preview.call_count
            self.dispatch()
            self.assertEqual(preview.call_count, checks)
            self.assertEqual({row["status"] for row in self.public()["orders"]}, {"waiting_market"})
            self.assertEqual(self.actor["fake"].posts, [])

    def test_deadline_is_bounded_by_auth_and_at_most_seven_days(self):
        with self.market({"tgld-uid"}):
            proposal = self.demo_ready()
        with connect(self.database) as conn:
            session = conn.execute("SELECT expires_at FROM sessions WHERE token_hash=?", (self.actor["key"],)).fetchone()
            row = conn.execute("SELECT expires_at FROM sandbox_proposals WHERE id=?", (proposal["id"],)).fetchone()
        self.assertLessEqual(row["expires_at"], session["expires_at"])
        self.assertLessEqual(row["expires_at"], time.time() + 7 * 86400)
        self.assertEqual(proposal["waiting_until"], proposal["expires_at"])

    def test_stop_cancels_waiting_and_reopen_does_not_send(self):
        closed = {"tgld-uid"}
        with self.market(closed):
            self.confirm(self.demo_ready())
            self.action("stop")
            closed.clear()
            self.due()
            self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [])
        self.assertEqual({row["status"] for row in self.public()["orders"]}, {"cancelled"})

    def test_logout_or_expiry_cancel_pending_without_sending(self):
        with self.market({"tgld-uid"}):
            self.confirm(self.demo_ready())
        with connect(self.database) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (self.actor["key"],))
        self.service.expire_queues()
        self.assertEqual({row["status"] for row in self.public()["orders"]}, {"cancelled"})
        self.assertEqual(self.actor["fake"].posts, [])

    def test_waiting_blocks_reset_and_new_preview_until_explicit_stop(self):
        with self.market({"tgld-uid"}):
            self.confirm(self.demo_ready())
            with self.assertRaises(SandboxConflict):
                self.demo_preview()
            with self.assertRaises(SandboxConflict):
                self.action("reset-demo", {"key": "b29ce9e9-9880-402e-9ce6-a854fe8d8e14"})
        self.assertEqual(self.actor["fake"].open_calls, 1)

    def test_other_session_cannot_take_or_revoke_original_queue(self):
        with self.market({"tgld-uid"}):
            proposal = self.demo_ready()
            self.confirm(proposal)
        second_key = session_hash(secrets.token_urlsafe(32))
        with connect(self.database) as conn:
            conn.execute("INSERT INTO sessions VALUES(?,?,?)", (second_key, self.actor["owner"], time.time() + 3600))
        second = {**self.actor, "key": second_key}
        self.action("connect", {"token": self.actor["secret"]}, second)
        self.action("select", {"account_id": proposal["account_id"]}, second)
        before = copy.deepcopy(self.public()["orders"])
        with self.assertRaises(SandboxConflict):
            self.service.dispatch(second["owner"], second_key)
        self.assertEqual(self.public()["orders"], before)
        self.assertEqual(self.actor["fake"].posts, [])

    def test_unknown_post_blocks_every_other_waiting_intent(self):
        with self.market({"tgld-uid"}):
            self.confirm(self.demo_ready())
            self.actor["fake"].post_unknown = True
            with self.assertRaises(SandboxError):
                self.dispatch()
            sent = list(self.actor["fake"].posts)
            self.dispatch()
        self.assertEqual(self.actor["fake"].posts, sent)
        self.assertEqual(len(sent), 1)
        self.assertIn("unknown", {row["status"] for row in self.public()["orders"]})

    def test_queue_reserves_all_remaining_costs_and_never_resizes(self):
        with self.market({"tgld-uid"}):
            proposal = self.demo_ready()
            self.confirm(proposal)
            self.actor["fake"].cash = 1_000
            with self.assertRaises(ValidationError):
                self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [])
        self.assertEqual(self.stored_proposal(proposal["id"])["fingerprint"], proposal["fingerprint"])

    def test_mixed_basket_does_not_extend_waiting_consent_to_other_orders(self):
        with self.market({"tgld-uid"}):
            proposal = self.demo_ready()
            self.confirm(proposal)
        fake = self.actor["fake"]
        original = fake.preview_order
        def unavailable(account_id, uid, *args, **kwargs):
            if uid == "tmon-uid":
                raise SandboxError("NETWORK")
            return original(account_id, uid, *args, **kwargs)
        with patch.object(fake, "preview_order", side_effect=unavailable):
            with self.assertRaises(SandboxError):
                self.dispatch()
        self.due()
        self.dispatch()
        self.assertEqual(fake.posts, [])
        self.assertFalse(any(row["status"] == "waiting_market" for row in self.public()["orders"]))

    def test_closed_during_last_client_precheck_requires_new_waiting_consent(self):
        proposal = self.demo_ready()
        self.confirm(proposal)
        self.actor["fake"].client_precheck_hook = lambda: (_ for _ in ()).throw(SandboxError("TRADING_CLOSED"))
        with self.assertRaises(SandboxError):
            self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [])
        self.assertEqual({row["status"] for row in self.public()["orders"]}, {"blocked", "cancelled"})


if __name__ == "__main__":
    unittest.main()
