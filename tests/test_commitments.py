import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import plugin_test_support  # noqa: F401 - load the root plugin as a package
from commitments.ledger import Ledger, Conflict
from commitments.policy import (
    delivery_allowed,
    preferences,
    validate_preferences,
    Model,
)
from commitments.runtime import Runtime


def item(state="open", review=1100):
    return dict(
        summary="Send the outline",
        owner="user",
        state=state,
        deadline=None,
        progress="",
        next_review=review if state == "open" else None,
        snooze_until=None,
        reason="source promise",
    )


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.l = Ledger(sqlite3.connect(":memory:", check_same_thread=False))
        self.l.bind("a", "p", "route", "owner", {})
        self.g = self.l.acquire("worker", 1000)

    def tearDown(self):
        self.l.db.close()

    def capture(self, data=None):
        self.l.observe("e1", "a", "user", "owner", "I will send it", 1000)
        s = self.l.snapshot("p")
        self.l.apply(
            s,
            [dict(id=None, evidence=["e1"], data=data or item())],
            "worker",
            self.g,
            1000,
        )
        return self.l.items()[0]

    def test_duplicate_observation_and_inactive_candidate(self):
        self.capture(item("candidate"))
        self.assertFalse(
            self.l.observe("e1", "a", "user", "owner", "I will send it", 1001)
        )
        self.assertEqual(self.l.due(9999), [])

    def test_newer_evidence_invalidates_model_result(self):
        self.l.observe("e1", "a", "user", "owner", "I will send it", 1000)
        s = self.l.snapshot("p")
        self.l.observe("e2", "a", "user", "owner", "cancelled", 1001)
        with self.assertRaises(Conflict):
            self.l.apply(s, [], "worker", self.g, 1001)
        self.assertEqual(len(self.l.snapshot("p")["observations"]), 2)

    def test_atomic_rejection_rolls_back_all_operations(self):
        self.l.observe("e1", "a", "user", "owner", "promise", 1000)
        s = self.l.snapshot("p")
        with self.assertRaises(ValueError):
            self.l.apply(
                s,
                [
                    dict(id=None, evidence=["e1"], data=item()),
                    dict(id=None, evidence=["forged"], data=item()),
                ],
                "worker",
                self.g,
                1000,
            )
        self.assertEqual(self.l.items(), [])
        self.assertEqual(len(self.l.snapshot("p")["observations"]), 1)

    def test_cross_chat_completion_preserves_source(self):
        old = self.capture()
        self.l.bind("b", "p", "route-b", "owner", {})
        self.l.observe("e2", "b", "user", "owner", "sent", 1001)
        self.l.apply(
            self.l.snapshot("p"),
            [dict(id=old["id"], evidence=["e2"], data=item("done"))],
            "worker",
            self.g,
            1001,
        )
        new = self.l.items()[0]
        self.assertEqual(new["source"], "a")
        self.assertEqual(new["data"]["state"], "done")
        self.assertEqual(self.l.due(9999), [])

    def test_scope_boundary_rejects_foreign_item(self):
        old = self.capture()
        self.l.bind("b", "other", "rb", "other", {})
        self.l.observe("e2", "b", "user", "other", "sent", 1001)
        with self.assertRaises(ValueError):
            self.l.apply(
                self.l.snapshot("other"),
                [dict(id=old["id"], evidence=["e2"], data=item("done"))],
                "worker",
                self.g,
                1001,
            )

    def test_lease_expiry_fences_old_worker(self):
        self.assertIsNone(self.l.acquire("other", 1001))
        g2 = self.l.acquire("other", 1181)
        self.assertGreater(g2, self.g)
        with self.l.transaction() as db:
            with self.assertRaises(Conflict):
                self.l.check_lease(db, "worker", self.g, 1181)

    def test_completion_between_prepare_and_send_cancels_dispatch(self):
        old = self.capture()
        d = self.l.review(
            old,
            self.l.snapshot("p"),
            dict(action="contact", next_review=1300, reason="due"),
            "route",
            "ask",
            "worker",
            self.g,
            1100,
        )
        self.l.observe("e2", "a", "user", "owner", "sent", 1101)
        self.assertFalse(self.l.admit_dispatch(d, "worker", self.g, 1101))
        self.assertEqual(self.l.dispatch(d)["state"], "cancelled")

    def test_crash_boundary_is_unknown_and_not_reclaimable(self):
        old = self.capture()
        d = self.l.review(
            old,
            self.l.snapshot("p"),
            dict(action="contact", next_review=1300, reason="due"),
            "route",
            "ask",
            "worker",
            self.g,
            1100,
        )
        self.assertTrue(self.l.admit_dispatch(d, "worker", self.g, 1100))
        self.assertEqual(self.l.dispatch(d)["state"], "unknown")
        self.assertFalse(self.l.admit_dispatch(d, "worker", self.g, 1101))

    def test_fast_response_does_not_regress_to_accepted(self):
        old = self.capture()
        d = self.l.review(
            old,
            self.l.snapshot("p"),
            dict(action="contact", next_review=1300, reason="due"),
            "route",
            "ask",
            "worker",
            self.g,
            1100,
        )
        self.l.delivery_state(d, "generated")
        self.l.delivery_state(d, "accepted")
        self.assertEqual(self.l.dispatch(d)["state"], "generated")

    def test_archive_pause_unarchive_and_pending_update(self):
        old = self.capture()
        self.l.lifecycle("a", "archive")
        with self.assertRaises(Conflict):
            self.l.review(
                old,
                self.l.snapshot("p"),
                dict(action="contact", next_review=1300, reason="due"),
                "route",
                "ask",
                "worker",
                self.g,
                1100,
            )
        self.l.observe("e2", "a", "user", "owner", "sent", 1101)
        self.l.lifecycle("a", "unarchive")
        self.l.apply(
            self.l.snapshot("p"),
            [dict(id=old["id"], evidence=["e2"], data=item("done"))],
            "worker",
            self.g,
            1101,
        )
        self.assertEqual(self.l.items()[0]["data"]["state"], "done")

    def test_delete_anchor_erases_content_and_prevents_resurrection(self):
        self.capture()
        self.l.lifecycle("a", "delete")
        self.assertEqual(self.l.items(), [])
        self.assertEqual(self.l.rows("SELECT * FROM changes"), [])
        self.assertEqual(self.l.rows("SELECT * FROM observations"), [])
        self.assertFalse(
            self.l.observe("late", "a", "user", "owner", "late promise", 1001)
        )
        self.assertFalse(self.l.bind("a", "p", "route", "owner", {}))

    def test_secondary_chat_delete_keeps_original_commitment(self):
        old = self.capture()
        self.l.bind("b", "p", "rb", "owner", {})
        self.l.observe("e2", "b", "user", "owner", "draft ready", 1001)
        data = item()
        data["progress"] = "draft ready"
        self.l.apply(
            self.l.snapshot("p"),
            [dict(id=old["id"], evidence=["e2"], data=data)],
            "worker",
            self.g,
            1001,
        )
        self.l.lifecycle("b", "delete")
        self.assertEqual(len(self.l.items()), 1)
        self.assertEqual(self.l.rows("SELECT * FROM changes WHERE source='b'"), [])

    def test_merge_keeps_earliest_source_as_deletion_anchor(self):
        first = self.capture()
        self.l.bind("b", "p", "rb", "owner", {})
        self.l.observe("e2", "b", "user", "owner", "same promise", 1001)
        self.l.apply(
            self.l.snapshot("p"),
            [dict(id=None, evidence=["e2"], data=item())],
            "worker",
            self.g,
            1001,
        )
        second = next(i for i in self.l.items() if i["id"] != first["id"])
        self.l.observe("e3", "b", "user", "owner", "Those are the same outline", 1002)
        self.l.apply(
            self.l.snapshot("p"),
            [
                dict(
                    id=second["id"],
                    merge_ids=[first["id"]],
                    evidence=["e3"],
                    data=item(),
                )
            ],
            "worker",
            self.g,
            1002,
        )
        remaining = self.l.items()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["source"], "a")
        self.l.lifecycle("a", "delete")
        self.assertEqual(self.l.items(), [])

    def test_one_off_quiet_exception_is_consumed_on_contact(self):
        data = item()
        data["explicit_reminder_at"] = 1090
        old = self.capture(data)
        d = self.l.review(
            old,
            self.l.snapshot("p"),
            dict(action="contact", next_review=1300, reason="requested"),
            "route",
            "reminder",
            "worker",
            self.g,
            1100,
        )
        self.assertTrue(
            json.loads(self.l.dispatch(d)["instruction"])["quiet_exception"]
        )
        self.assertFalse(self.l.items()[0]["data"].get("explicit_reminder_used", False))
        self.l.admit_dispatch(d, "worker", self.g, 1100)
        self.l.delivery_state(d, "generated")
        self.assertTrue(self.l.items()[0]["data"]["explicit_reminder_used"])
        current = self.l.items()[0]
        later = self.l.review(
            current,
            self.l.snapshot("p"),
            dict(action="contact", next_review=1400, reason="later"),
            "route",
            "check",
            "worker",
            self.g,
            1101,
        )
        self.assertFalse(
            json.loads(self.l.dispatch(later)["instruction"])["quiet_exception"]
        )

    def test_applied_full_text_is_cleared_but_evidence_remains(self):
        self.capture()
        self.assertEqual(
            self.l.rows("SELECT content FROM observations")[0]["content"], ""
        )
        evidence = json.loads(
            self.l.rows("SELECT evidence FROM changes")[0]["evidence"]
        )
        self.assertEqual(evidence["quote"], "I will send it")

    def test_prepared_crash_restarts_review_instead_of_replaying_message(self):
        old = self.capture()
        dispatch = self.l.review(
            old,
            self.l.snapshot("p"),
            dict(action="contact", next_review=1300, reason="due"),
            "route",
            "ask",
            "worker",
            self.g,
            1100,
        )
        self.l.release("worker")
        generation = self.l.acquire("new", 1101)
        self.l.recover_prepared("new", generation, 1101)
        self.assertEqual(self.l.dispatch(dispatch)["state"], "cancelled")
        self.assertEqual(len(self.l.due(1101)), 1)

    def test_unarchive_does_not_replay_missed_one_off(self):
        data = item()
        data["explicit_reminder_at"] = 1090
        self.capture(data)
        self.l.lifecycle("a", "archive", now=1050)
        self.l.lifecycle("a", "unarchive", now=1150)
        self.assertTrue(self.l.items()[0]["data"]["explicit_reminder_used"])
        self.assertEqual(self.l.items()[0]["data"]["state"], "open")

    def test_deleting_source_does_not_reset_recent_attempt_count(self):
        old = self.capture()
        dispatch = self.l.review(
            old,
            self.l.snapshot("p"),
            dict(action="contact", next_review=1300, reason="due"),
            "route",
            "ask",
            "worker",
            self.g,
            1100,
        )
        self.l.admit_dispatch(dispatch, "worker", self.g, 1100)
        self.l.lifecycle("a", "delete")
        self.assertEqual(self.l.contact_count(1000), 1)
        self.assertEqual(self.l.rows("SELECT * FROM items"), [])

    def test_known_host_refusal_does_not_consume_frequency_budget(self):
        old = self.capture()
        dispatch = self.l.review(
            old,
            self.l.snapshot("p"),
            dict(action="contact", next_review=1300, reason="due"),
            "route",
            "ask",
            "worker",
            self.g,
            1100,
        )
        self.l.admit_dispatch(dispatch, "worker", self.g, 1100)
        self.l.delivery_state(dispatch, "deferred", "host refused")
        self.assertEqual(self.l.contact_count(1000), 0)

    def test_future_database_version_is_not_silently_downgraded(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA user_version=99")
        with self.assertRaises(ValueError):
            Ledger(connection)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)
        connection.close()

    def test_persistence_after_connection_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.db"
            stored = Ledger(sqlite3.connect(path))
            stored.bind("x", "p", "r", "u", {})
            stored.observe("e", "x", "user", "u", "promise", 1000)
            stored.db.close()
            reopened = Ledger(sqlite3.connect(path))
            self.assertEqual(reopened.pending(1001), [{"scope": "p"}])
            reopened.db.close()


class PolicyTests(unittest.TestCase):
    def test_no_default_quiet_hours_or_frequency_cap(self):
        self.assertTrue(
            delivery_allowed(
                item(), {}, [{"created": 1000, "state": "generated"}] * 30, 1001
            )[0]
        )

    def test_explicit_limits_and_group_override(self):
        quiet = {"start": "00:00", "end": "23:59", "timezone": "UTC"}
        settings = preferences({"quiet_hours": quiet}, {"quiet_hours": None})
        self.assertTrue(delivery_allowed(item(), settings, [], 1001)[0])
        self.assertFalse(delivery_allowed(item(), {"quiet_hours": quiet}, [], 1001)[0])
        self.assertFalse(
            delivery_allowed(
                item(),
                {"frequency_limit": {"count": 1, "window_seconds": 60}},
                [{"created": 1000, "state": "accepted"}],
                1001,
            )[0]
        )

    def test_quiet_exception_does_not_bypass_other_constraints(self):
        quiet = {"start": "00:00", "end": "23:59", "timezone": "UTC"}
        self.assertTrue(
            delivery_allowed(
                item(), {"quiet_hours": quiet}, [], 1001, quiet_exception=True
            )[0]
        )
        self.assertFalse(
            delivery_allowed(
                item(),
                {"quiet_hours": quiet, "pause_followups": True},
                [],
                1001,
                quiet_exception=True,
            )[0]
        )
        data = item()
        data["snooze_until"] = 2000
        self.assertFalse(
            delivery_allowed(
                data, {"quiet_hours": quiet}, [], 1001, quiet_exception=True
            )[0]
        )

    def test_invalid_config_is_rejected(self):
        for config in [
            {"quiet_hours": {"start": "25:00", "end": "10:00", "timezone": "UTC"}},
            {"frequency_limit": {"count": 0, "window_seconds": 60}},
            {"owner": "someone"},
        ]:
            with self.assertRaises((ValueError, KeyError)):
                validate_preferences(config)

    def test_actual_host_model_and_unknown_usage_recorded(self):
        ledger = Ledger(sqlite3.connect(":memory:"))

        class LLM:
            def complete_structured(self, **kw):
                assert kw["task"] == "commitments"
                return SimpleNamespace(
                    parsed={"operations": []},
                    provider="host-fallback",
                    model="main-model",
                    usage=None,
                )

        model = Model(SimpleNamespace(llm=LLM()), ledger, "UTC")
        model.call("reconcile", {}, {}, "test", 1000)
        usage = json.loads(ledger.rows("SELECT data FROM usage")[0]["data"])
        self.assertEqual(usage["model"], "main-model")
        self.assertIsNone(usage["usage"])
        ledger.db.close()


class FakeDB:
    def get_session(self, sid):
        return {"archived": False}

    def get_messages(self, *args, **kwargs):
        return []

    def close(self):
        pass


class FakeHost:
    ready_value = True
    actor = "local:owner"

    def ready(self):
        return self.ready_value

    def model_ready(self):
        return True

    def timezone(self):
        return "UTC"

    def database(self):
        return FakeDB()

    def redact(self, text):
        return text

    def owner(self, actor):
        return actor == "local:owner"

    def identity(self, session_id="", **kwargs):
        return {
            "source": session_id or "a",
            "scope": "p",
            "route": "route",
            "actor": self.actor,
            "metadata": {"physical_id": session_id or "a", "group": False},
        }


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.sent = []
        self.ctx = SimpleNamespace(
            get_config=lambda k, d=None: d,
            inject_message=lambda text, **kw: self.sent.append((text, kw)) or True,
        )
        self.l = Ledger(sqlite3.connect(":memory:", check_same_thread=False))
        self.h = FakeHost()
        self.r = Runtime(self.ctx, self.l, self.h, lambda: self.clock[0])
        self.r.bind("a")

    def tearDown(self):
        self.r.close()
        self.l.db.close()

    def test_end_to_end_fixture_capture_review_and_cross_chat_close(self):
        r = self.r

        class Decisions:
            def call(_, purpose, payload, *args):
                if purpose == "review":
                    return dict(
                        action="contact",
                        next_review=1500,
                        reason="useful check",
                        guidance="Ask whether the outline was sent.",
                    )
                events = payload["snapshot"]["observations"]
                existing = payload["snapshot"]["items"]
                return {
                    "operations": [
                        dict(
                            id=existing[0]["id"] if existing else None,
                            evidence=[events[0]["id"]],
                            data=item("done" if existing else "open"),
                        )
                    ]
                }

        r.model = Decisions()
        self.l.observe("e1", "a", "user", "owner", "promise", 1000)
        r.tick()
        self.clock[0] = 1101
        r.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(
            self.l.rows("SELECT state FROM dispatches")[0]["state"], "accepted"
        )
        self.r.bind("b")
        self.l.observe("e2", "b", "user", "owner", "sent", 1102)
        self.clock[0] = 1102
        r.tick()
        self.assertEqual(self.l.items()[0]["data"]["state"], "done")
        self.clock[0] = 2000
        r.tick()
        self.assertEqual(len(self.sent), 1)
        context = r.before("a", "What is open?", platform="local")
        self.assertIn('"state":"done"', context["context"])

    def test_no_work_in_doctor_or_short_lived_cli_process(self):
        self.h.ready_value = False
        self.l.observe("e", "a", "user", "owner", "promise", 1000)
        self.r.tick()
        self.assertEqual(self.r.status, "waiting_for_gateway")
        self.assertEqual(len(self.l.snapshot("p")["observations"]), 1)

    def test_guest_settings_refused(self):
        self.h.actor = "telegram:guest"
        out = self.r.command('settings {"pause_followups":true}', session_id="a")
        self.assertIn("Only", out)
        self.assertEqual(self.l.settings("p"), {})

    def test_owner_cannot_expose_private_items_in_group(self):
        self.l.observe("e", "a", "user", "owner", "promise", 1000)
        g = self.l.acquire(self.r.owner, 1000)
        self.l.apply(
            self.l.snapshot("p"),
            [dict(id=None, evidence=["e"], data=item())],
            self.r.owner,
            g,
            1000,
        )
        private = self.l.items()[0]
        self.h.identity = lambda *a, **kw: dict(
            source="group",
            scope="group:x",
            route="g",
            actor="local:owner",
            metadata={"group": True},
        )
        self.assertIn("No commitments", self.r.command("list"))
        self.assertIn("unambiguous", self.r.command("show " + private["id"]))
        self.assertIn("private", self.r.command("sources"))

    def test_queued_dispatch_is_cancelled_after_source_deletion(self):
        self.l.observe("e", "a", "user", "owner", "promise", 1000)
        g = self.l.acquire(self.r.owner, 1000)
        self.l.apply(
            self.l.snapshot("p"),
            [dict(id=None, evidence=["e"], data=item())],
            self.r.owner,
            g,
            1000,
        )
        old = self.l.items()[0]
        dispatch = self.l.review(
            old,
            self.l.snapshot("p"),
            dict(action="contact", next_review=1300, reason="due"),
            "route",
            "ask",
            self.r.owner,
            g,
            1100,
        )
        self.l.admit_dispatch(dispatch, self.r.owner, g, 1100)
        self.l.lifecycle("a", "delete")
        text = f"[Commitments dispatch {dispatch}]\ncheck in"
        db = FakeDB()
        db.get_messages = lambda *a, **kw: [
            {
                "id": 77,
                "role": "user",
                "content": text,
                "display_kind": "internal_notification",
            }
        ]
        self.h.database = lambda: db
        result = self.r.before("a", text, platform="local")
        self.assertEqual(result["action"], "block")
        self.assertEqual(self.l.items(), [])

    def test_pasted_dispatch_marker_is_not_trusted_internal_input(self):
        token = "a" * 32
        with self.l.transaction() as db:
            db.execute("INSERT INTO dispatch_tombstones VALUES(?,?)", (token, "route"))
        text = f"[Commitments dispatch {token}]\nI will send the outline."
        database = FakeDB()
        database.get_messages = lambda *a, **kw: [
            {"id": 78, "role": "user", "content": text, "display_kind": None}
        ]
        self.h.database = lambda: database
        result = self.r.before("a", text, platform="local")
        self.assertNotEqual(result.get("action"), "block")
        self.assertEqual(self.l.snapshot("p")["observations"][0]["content"], text)

    def test_frequency_budget_is_not_limited_to_model_history_window(self):
        with self.l.transaction() as db:
            db.executemany(
                "INSERT INTO contact_log VALUES(?,?,?)",
                [(str(i), "other-scope", 990) for i in range(150)],
            )
        self.l.set_settings(
            "*", {"frequency_limit": {"count": 150, "window_seconds": 60}}
        )
        self.assertFalse(self.r.allowed(item(), "p", 1000)[0])
        self.l.set_settings(
            "p", {"frequency_limit": {"count": 1, "window_seconds": 60}}
        )
        self.assertTrue(self.r.allowed(item(), "p", 1000)[0])

    def test_main_model_usage_is_separate_and_idempotent(self):
        self.r.turn_dispatches[("a", "turn")] = "dispatch"
        for _ in range(2):
            self.r.api_usage(
                "a",
                "turn",
                "request",
                "provider",
                "main-model",
                {"input_tokens": 100, "output_tokens": 10},
            )
        rows = self.l.rows("SELECT * FROM usage")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purpose"], "wake")

    def test_reset_preserves_anchor_and_durable_mapping(self):
        self.r.reset(old_session_id="a", new_session_id="new")
        self.assertEqual(self.r.bind("new")["source"], "a")
        second = Runtime(self.ctx, self.l, self.h, lambda: 1000)
        self.assertEqual(second.aliases["new"], "a")

    def test_unknown_delivery_not_retried(self):
        self.l.observe("e", "a", "user", "owner", "promise", 1000)
        g = self.l.acquire(self.r.owner, 1000)
        self.l.apply(
            self.l.snapshot("p"),
            [dict(id=None, evidence=["e"], data=item())],
            self.r.owner,
            g,
            1000,
        )
        old = self.l.items()[0]
        d = self.l.review(
            old,
            self.l.snapshot("p"),
            dict(action="contact", next_review=1200, reason="due"),
            "route",
            "ask",
            self.r.owner,
            g,
            1100,
        )
        self.l.admit_dispatch(d, self.r.owner, g, 1100)
        self.clock[0] = 1300
        self.r.model = SimpleNamespace(
            call=lambda *a: (_ for _ in ()).throw(AssertionError("must not call"))
        )
        self.r.tick()
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
