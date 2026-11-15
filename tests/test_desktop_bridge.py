import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import plugin_test_support  # noqa: F401 - load the root plugin as a package
from commitments.ledger import Ledger
from commitments.runtime import Runtime


class Sessions:
    def __init__(self):
        self.rows = {
            "a": {
                "source": "local",
                "user_id": "owner",
                "archived": False,
                "session_key": "route-a",
            },
            "b": {
                "source": "local",
                "user_id": "owner",
                "archived": False,
                "session_key": "route-b",
            },
            "guest": {
                "source": "local",
                "user_id": "guest",
                "archived": False,
                "session_key": "route-guest",
            },
        }

    def get_session(self, sid):
        return self.rows.get(sid)

    def get_messages(self, *args, **kwargs):
        return []

    def close(self):
        pass


class Host:
    def __init__(self):
        self.db = Sessions()
        self.permission = True

    def database(self):
        return self.db

    def config(self):
        return {
            "plugins": {
                "entries": {"commitments": {"allow_gateway_injection": self.permission}}
            }
        }

    def timezone(self):
        return "UTC"

    def redact(self, text):
        return text

    def owner(self, actor):
        return actor == "owner"


class DesktopBridgeTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.ledger = Ledger(sqlite3.connect(":memory:", check_same_thread=False))
        self.host = Host()
        self.ctx = SimpleNamespace(
            plugin_id="commitments",
            get_config=lambda key, default=None: default,
            inject_message=lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("visible fallback forbidden")
            ),
        )
        self.runtime = Runtime(self.ctx, self.ledger, self.host, lambda: self.now)
        self.bridge = self.runtime.desktop
        self.ledger.bind(
            "a",
            "private:owner",
            "route-a",
            "owner",
            {"platform": "desktop", "physical_id": "a", "group": False},
        )
        self.ledger.observe(
            "event", "a", "user", "owner", "I will send the outline.", self.now
        )
        self.generation = self.ledger.acquire(self.runtime.owner, self.now)
        self.runtime.lease_generation = self.generation
        data = dict(
            summary="Send the outline",
            owner="owner",
            state="open",
            deadline=None,
            progress="",
            next_review=1100,
            reason="promise",
        )
        self.ledger.apply(
            self.ledger.snapshot("private:owner"),
            [dict(id=None, evidence=["event"], data=data)],
            self.runtime.owner,
            self.generation,
            self.now,
        )
        self.item = self.ledger.items()[0]
        self.dispatch = self.ledger.review(
            self.item,
            self.ledger.snapshot("private:owner"),
            dict(action="contact", reason="due", next_review=1300),
            "route-a",
            "Ask whether the outline is ready.",
            self.runtime.owner,
            self.generation,
            self.now,
        )
        self.runtime.send(self.dispatch, self.generation)
        self.base = {
            "id": self.dispatch,
            "backend_id": self.bridge.identity,
            "client_id": "window-one",
        }

    def tearDown(self):
        self.runtime.close()
        self.ledger.db.close()

    def claim(self, **overrides):
        params = {**self.base, **overrides}
        result = self.bridge.claim(params, session_id="a")
        return {**params, "token": result.get("token")}, result

    def test_local_delivery_waits_without_visible_fallback(self):
        self.assertEqual(
            self.ledger.dispatch(self.dispatch)["state"], "awaiting_desktop"
        )
        jobs = self.bridge.poll({}, session_id="a")["jobs"]
        self.assertEqual(jobs[0]["session_id"], "a")
        self.assertNotIn("text", jobs[0])

    def test_route_change_does_not_fall_back_to_visible_injection(self):
        with self.ledger.transaction() as db:
            db.execute(
                "UPDATE dispatches SET state='prepared' WHERE id=?", (self.dispatch,)
            )
            db.execute("UPDATE sources SET route='new-route' WHERE id='a'")
        self.runtime.send(self.dispatch, self.generation)
        self.assertEqual(self.ledger.dispatch(self.dispatch)["state"], "deferred")

    def test_unbound_and_wrong_owner_poll_cannot_read_pending_work(self):
        with self.assertRaises(PermissionError):
            self.bridge.poll({}, session_id=None)
        self.assertEqual(self.bridge.poll({}, session_id="guest")["jobs"], [])
        self.assertFalse(self.bridge.claim(self.base, session_id="guest")["ok"])

    def test_wrong_profile_identity_is_rejected(self):
        with self.assertRaises(PermissionError):
            self.bridge.claim(
                {**self.base, "backend_id": "another-profile"}, session_id="a"
            )

    def test_two_windows_only_one_claims(self):
        def run(client):
            return self.bridge.claim(
                {**self.base, "client_id": client}, session_id="a"
            )["ok"]

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, ["one", "two"]))
        self.assertEqual(sum(results), 1)

    def test_expired_claim_can_be_replaced_but_old_token_cannot_submit(self):
        first, _ = self.claim()
        self.now = 1031
        second, result = self.claim(client_id="window-two")
        self.assertTrue(result["ok"])
        with self.assertRaises(PermissionError):
            self.bridge.begin(first, session_id="a")
        payload = self.bridge.begin(second, session_id="a")
        self.assertEqual(payload["input_id"], self.dispatch)
        self.assertEqual(payload["session_id"], "a")
        self.assertEqual(self.ledger.dispatch(self.dispatch)["state"], "unknown")
        with self.assertRaises(ValueError):
            self.bridge.begin(second, session_id="a")

    def test_completion_before_begin_prevents_submission(self):
        claimed, _ = self.claim()
        self.ledger.observe("done", "a", "user", "owner", "Sent it.", self.now)
        data = {**self.item["data"], "state": "done", "next_review": None}
        self.ledger.apply(
            self.ledger.snapshot("private:owner"),
            [dict(id=self.item["id"], evidence=["done"], data=data)],
            self.runtime.owner,
            self.generation,
            self.now,
        )
        self.assertFalse(self.bridge.begin(claimed, session_id="a")["ok"])

    def test_pause_and_archive_before_begin_prevent_submission(self):
        claimed, _ = self.claim()
        self.ledger.set_settings("private:owner", {"pause_followups": True})
        self.assertFalse(self.bridge.begin(claimed, session_id="a")["ok"])
        self.ledger.set_settings("private:owner", {})
        self.ledger.lifecycle("a", "archive")
        self.host.db.rows["a"]["archived"] = True
        with self.assertRaises(PermissionError):
            self.bridge.begin(claimed, session_id="a")

    def test_permission_is_rechecked(self):
        claimed, _ = self.claim()
        self.host.permission = False
        with self.assertRaises(PermissionError):
            self.bridge.begin(claimed, session_id="a")

    def test_old_waiting_message_requires_new_model_review(self):
        self.now = 1301
        self.assertEqual(self.bridge.poll({}, session_id="a")["jobs"], [])
        self.assertEqual(self.ledger.dispatch(self.dispatch)["state"], "cancelled")
        self.assertEqual(self.ledger.items()[0]["data"]["next_review"], 1301)

    def test_unknown_submission_is_not_reclaimed_or_blindly_retried(self):
        claimed, _ = self.claim()
        self.bridge.begin(claimed, session_id="a")
        self.bridge.failed(claimed, session_id="a")
        self.now = 1100
        self.assertEqual(self.bridge.poll({}, session_id="a")["jobs"], [])
        self.assertFalse(
            self.bridge.claim({**self.base, "client_id": "other"}, session_id="a")["ok"]
        )
        self.assertEqual(self.ledger.dispatch(self.dispatch)["state"], "unknown")

    def test_receipt_must_match_target_and_generation_cannot_regress(self):
        claimed, _ = self.claim()
        self.bridge.begin(claimed, session_id="a")
        with self.assertRaises(ValueError):
            self.bridge.ack(
                {
                    **claimed,
                    "receipt": {
                        "ref": {"session_id": "b"},
                        "admission_id": "admission",
                    },
                },
                session_id="a",
            )
        self.ledger.delivery_state(self.dispatch, "generated")
        self.bridge.generated(self.dispatch)
        self.bridge.ack(
            {
                **claimed,
                "receipt": {"ref": {"session_id": "a"}, "admission_id": "admission"},
            },
            session_id="a",
        )
        self.assertEqual(self.ledger.dispatch(self.dispatch)["state"], "generated")
        self.assertEqual(
            self.ledger.rows("SELECT state FROM desktop_outbox")[0]["state"],
            "generated",
        )

    def test_hidden_typing_alone_is_not_proof_of_our_input(self):
        message = {
            "display_kind": "hidden",
            "display_metadata": json.dumps({"submission_id": self.dispatch}),
        }
        self.assertFalse(self.bridge.owns_hidden("a", message, self.dispatch))
        claimed, _ = self.claim()
        self.bridge.begin(claimed, session_id="a")
        self.assertTrue(self.bridge.owns_hidden("a", message, self.dispatch))
        self.assertFalse(self.bridge.owns_hidden("b", message, self.dispatch))
        message["display_metadata"] = {"submission_id": "different-input"}
        self.assertFalse(self.bridge.owns_hidden("a", message, self.dispatch))


if __name__ == "__main__":
    unittest.main()
