"""Behavior tests using the pinned Hermes SessionDB and identity context, not stubs."""

import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from hermes_state import SessionDB
except ImportError:
    SessionDB = None

import plugin_test_support  # noqa: F401 - load the root plugin as a package
from commitments.host import Host
from commitments.ledger import Ledger
from commitments.runtime import Runtime


@unittest.skipIf(
    SessionDB is None,
    "Run with the pinned Hermes environment for host integration tests",
)
class HermesBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cm-bridge-")
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        self.env.start()
        (self.home / "config.yaml").write_text(
            "timezone: UTC\nplugins:\n  enabled: []\n"
        )
        self.source = SessionDB(self.home / "state.db")
        uid = f"uid:{os.getuid()}"
        self.source.create_session(
            "a", source="local", user_id=uid, session_key="route-a", chat_type="dm"
        )
        self.ctx = SimpleNamespace(get_config=lambda k, d=None: d)
        self.host = Host(self.ctx)
        self.ledger = Ledger(sqlite3.connect(":memory:", check_same_thread=False))
        self.runtime = Runtime(self.ctx, self.ledger, self.host)

    def tearDown(self):
        self.runtime.close()
        self.ledger.db.close()
        self.source.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_durable_user_before_turn_and_assistant_after_turn(self):
        self.source.append_message("a", "user", "I will send the outline.")
        with patch("gateway.session_context.get_session_env", return_value=""):
            self.runtime.before("a", "I will send the outline.", platform="local")
        self.source.append_message("a", "assistant", "Happy to help.")
        with patch("gateway.session_context.get_session_env", return_value=""):
            self.runtime.after("a", platform="local")
        events = self.ledger.snapshot("private:" + self.host.local_owner)[
            "observations"
        ]
        self.assertEqual([e["role"] for e in events], ["user", "assistant"])
        self.assertEqual(events[0]["actor"], self.host.local_owner)
        self.runtime.reconcile_sources()
        self.assertEqual(
            len(
                self.ledger.snapshot("private:" + self.host.local_owner)["observations"]
            ),
            2,
        )

    def test_archive_unarchive_and_delete_from_another_connection(self):
        self.source.append_message("a", "user", "A promise")
        with patch("gateway.session_context.get_session_env", return_value=""):
            self.runtime.before("a", "A promise", platform="local")
        self.source.set_session_archived("a", True, notify=False)
        self.runtime.reconcile_sources()
        self.assertEqual(
            self.ledger.rows("SELECT archived FROM sources")[0]["archived"], 1
        )
        self.source.set_session_archived("a", False, notify=False)
        self.runtime.reconcile_sources()
        self.assertEqual(
            self.ledger.rows("SELECT archived FROM sources")[0]["archived"], 0
        )
        self.source.delete_session("a")
        self.runtime.reconcile_sources()
        self.assertEqual(
            self.ledger.rows("SELECT deleted FROM sources")[0]["deleted"], 1
        )
        self.assertEqual(self.ledger.rows("SELECT * FROM observations"), [])

    def test_delete_after_observation_before_cursor_commit_does_not_resurrect(self):
        with patch("gateway.session_context.get_session_env", return_value=""):
            self.runtime.bind("a", platform="local")
        self.ledger.observe(
            "witness", "a", "user", self.host.local_owner, "I will send it."
        )
        self.source.delete_session("a")
        self.runtime.reconcile_sources()
        self.assertEqual(
            self.ledger.rows("SELECT deleted FROM sources")[0]["deleted"], 1
        )
        self.assertEqual(self.ledger.rows("SELECT * FROM observations"), [])

    def test_group_sender_does_not_inherit_session_owner_authority(self):
        env = {
            "HERMES_SESSION_ACTOR_ID": "guest",
            "HERMES_SESSION_CHAT_TYPE": "group",
            "HERMES_SESSION_CHAT_ID": "g",
            "HERMES_SESSION_KEY": "r",
        }
        with patch(
            "gateway.session_context.get_session_env",
            side_effect=lambda k: env.get(k, ""),
        ):
            identity = self.host.identity("", platform="telegram", sender_id="owner")
        self.assertEqual(identity["actor"], "telegram:guest")
        self.assertFalse(self.host.owner(identity["actor"]))

    def test_preceding_context_survives_observation_body_cleanup(self):
        import time

        self.source.append_message(
            "a", "assistant", "Could you send Maya the outline tomorrow?"
        )
        with patch("gateway.session_context.get_session_env", return_value=""):
            identity = self.runtime.bind("a", platform="local")
        self.runtime.scan(identity)
        snapshot = self.ledger.snapshot(identity["scope"])
        generation = self.ledger.acquire("test", time.time())
        self.ledger.apply(snapshot, [], "test", generation, time.time())
        self.source.append_message("a", "user", "Yes, I'll do that.")
        self.runtime.scan(identity)
        context = self.runtime.context(self.ledger.snapshot(identity["scope"]))
        self.assertTrue(
            any("send Maya the outline" in row.get("content", "") for row in context)
        )

    def test_internal_provenance_is_read_from_host_not_message_text(self):
        import time

        now = time.time()
        with patch("gateway.session_context.get_session_env", return_value=""):
            identity = self.runtime.bind("a", platform="local")
        self.ledger.observe(
            "e", "a", "user", self.host.local_owner, "I will send it.", now
        )
        generation = self.ledger.acquire(self.runtime.owner, now)
        data = dict(
            summary="Send it",
            owner=self.host.local_owner,
            state="open",
            deadline=None,
            progress="",
            next_review=now + 60,
            reason="promise",
        )
        self.ledger.apply(
            self.ledger.snapshot(identity["scope"]),
            [dict(id=None, evidence=["e"], data=data)],
            self.runtime.owner,
            generation,
            now,
        )
        entry = self.ledger.items()[0]
        dispatch = self.ledger.review(
            entry,
            self.ledger.snapshot(identity["scope"]),
            dict(action="contact", next_review=now + 120, reason="due"),
            "route-a",
            "ask",
            self.runtime.owner,
            generation,
            now,
        )
        self.ledger.admit_dispatch(dispatch, self.runtime.owner, generation, now)
        text = f"[Commitments dispatch {dispatch}]\ncheck in"
        self.source.append_message(
            "a", "user", text, display_kind="internal_notification"
        )
        with patch("gateway.session_context.get_session_env", return_value=""):
            self.runtime.before("a", text, platform="local", turn_id="internal")
        self.assertEqual(self.runtime.turn_dispatches[("a", "internal")], dispatch)
        self.source.append_message("a", "user", text)
        with patch("gateway.session_context.get_session_env", return_value=""):
            self.runtime.before("a", text, platform="local", turn_id="pasted")
        self.assertNotIn(("a", "pasted"), self.runtime.turn_dispatches)

    def late_persistence_receipt(self, display_kind):
        import time

        now = time.time()
        with patch("gateway.session_context.get_session_env", return_value=""):
            identity = self.runtime.bind("a", platform="local")
        self.ledger.observe(
            "seed", "a", "user", self.host.local_owner, "Delivery fixture", now
        )
        generation = self.ledger.acquire(self.runtime.owner, now)
        data = dict(
            summary="Delivery fixture",
            owner=self.host.local_owner,
            state="open",
            deadline=None,
            progress="",
            next_review=now + 60,
            reason="test",
        )
        self.ledger.apply(
            self.ledger.snapshot(identity["scope"]),
            [dict(id=None, evidence=["seed"], data=data)],
            self.runtime.owner,
            generation,
            now,
        )
        entry = self.ledger.items()[0]
        dispatch = self.ledger.review(
            entry,
            self.ledger.snapshot(identity["scope"]),
            dict(action="contact", next_review=now + 120, reason="test"),
            "route-a",
            "acknowledge",
            self.runtime.owner,
            generation,
            now,
        )
        self.ledger.admit_dispatch(dispatch, self.runtime.owner, generation, now)
        text = f"[Commitments dispatch {dispatch}]\nA controlled delivery fixture."
        with patch("gateway.session_context.get_session_env", return_value=""):
            self.runtime.before("a", text, platform="local", turn_id="late")
        self.assertNotIn(("a", "late"), self.runtime.turn_dispatches)
        self.source.append_message("a", "user", text, display_kind=display_kind)
        self.source.append_message("a", "assistant", "Acknowledged.")
        with patch("gateway.session_context.get_session_env", return_value=""):
            self.runtime.after(
                "a",
                platform="local",
                turn_id="late",
                user_message=text,
                assistant_response="Acknowledged.",
                model="fixture-model",
            )
        return dispatch

    def test_receipt_recovers_when_internal_input_is_persisted_after_pre_hook(self):
        import json

        dispatch = self.late_persistence_receipt("internal_notification")
        self.assertEqual(self.ledger.dispatch(dispatch)["state"], "generated")
        usage = self.ledger.rows("SELECT data FROM usage")
        self.assertEqual(len(usage), 1)
        self.assertIsNone(json.loads(usage[0]["data"])["usage"])

    def test_late_persisted_user_marker_cannot_claim_generated_receipt(self):
        dispatch = self.late_persistence_receipt(None)
        self.assertEqual(self.ledger.dispatch(dispatch)["state"], "unknown")
        self.assertEqual(self.ledger.rows("SELECT * FROM usage"), [])

    def test_hidden_desktop_submission_has_verified_provenance_and_generates_receipt(
        self,
    ):
        import time

        now = time.time()
        self.host.config = lambda: {
            "plugins": {"entries": {"commitments": {"allow_gateway_injection": True}}}
        }
        with patch("gateway.session_context.get_session_env", return_value=""):
            identity = self.runtime.bind("a", platform="desktop")
        self.ledger.observe(
            "seed", "a", "user", self.host.local_owner, "I will send it.", now
        )
        generation = self.ledger.acquire(self.runtime.owner, now)
        self.runtime.lease_generation = generation
        data = dict(
            summary="Send it",
            owner=self.host.local_owner,
            state="open",
            deadline=None,
            progress="",
            next_review=now + 60,
            reason="promise",
        )
        self.ledger.apply(
            self.ledger.snapshot(identity["scope"]),
            [dict(id=None, evidence=["seed"], data=data)],
            self.runtime.owner,
            generation,
            now,
        )
        item = self.ledger.items()[0]
        dispatch = self.ledger.review(
            item,
            self.ledger.snapshot(identity["scope"]),
            dict(action="contact", reason="due", next_review=now + 120),
            "route-a",
            "Ask about the outline.",
            self.runtime.owner,
            generation,
            now,
        )
        self.runtime.send(dispatch, generation)
        bridge = self.runtime.desktop
        params = {
            "id": dispatch,
            "backend_id": bridge.identity,
            "client_id": "desktop-window",
        }
        params["token"] = bridge.claim(params, session_id="a")["token"]
        payload = bridge.begin(params, session_id="a")
        self.source.append_message(
            "a",
            "user",
            payload["text"],
            display_kind="hidden",
            display_metadata={"submission_id": payload["input_id"]},
        )
        with patch("gateway.session_context.get_session_env", return_value=""):
            self.runtime.before(
                "a", payload["text"], platform="desktop", turn_id="hidden"
            )
        self.assertEqual(self.runtime.turn_dispatches[("a", "hidden")], dispatch)
        self.source.append_message("a", "assistant", "Is the outline ready?")
        with patch("gateway.session_context.get_session_env", return_value=""):
            self.runtime.after(
                "a",
                platform="desktop",
                turn_id="hidden",
                user_message=payload["text"],
                assistant_response="Is the outline ready?",
            )
        self.assertEqual(self.ledger.dispatch(dispatch)["state"], "generated")
        self.assertEqual(
            self.ledger.rows("SELECT state FROM desktop_outbox")[0]["state"],
            "generated",
        )

    def test_host_ready_is_this_process_only(self):
        from datetime import datetime, timezone
        from gateway.status import read_runtime_status

        marker = self.home / "gateway_state.json"
        marker.write_text('{"pid":1,"gateway_state":"running"}')
        self.assertFalse(self.host.ready())
        import json

        marker.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "gateway_state": "running",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        self.assertEqual(read_runtime_status(marker)["pid"], os.getpid())
        self.assertTrue(self.host.ready())


if __name__ == "__main__":
    unittest.main()
