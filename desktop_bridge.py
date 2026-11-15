"""Authenticated, session-scoped handoff to the optional Desktop component.

Claims precede submission; an ambiguous submission is never blindly retried.
No instructions are broadcast or exposed by an unbound/profile-wide RPC.
"""

from __future__ import annotations

import json
import uuid

from .ledger import Conflict, dump

LOCAL = {"local", "desktop", "gui", "cli", "tui"}


class DesktopBridge:
    def __init__(self, runtime):
        self.runtime = runtime
        self.ledger = runtime.ledger
        with self.ledger.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS desktop_outbox (
                dispatch_id TEXT PRIMARY KEY, target TEXT NOT NULL, owner_id TEXT NOT NULL,
                input_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, client_id TEXT,
                token TEXT, expires REAL, admission_id TEXT, issued INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                error TEXT)""")
            db.execute(
                "CREATE TABLE IF NOT EXISTS desktop_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO desktop_meta VALUES(?,?)",
                ("identity", uuid.uuid4().hex),
            )
        self.identity = self.ledger.rows(
            "SELECT value FROM desktop_meta WHERE key='identity'"
        )[0]["value"]

    def register(self, ctx):
        for name in ("poll", "claim", "begin", "ack", "failed", "receipt"):
            ctx.register_rpc("desktop." + name, getattr(self, name))

    def enabled(self):
        config = self.runtime.host.config()
        plugin_id = getattr(self.runtime.ctx, "plugin_id", "commitments")
        return (
            config.get("plugins", {})
            .get("entries", {})
            .get(plugin_id, {})
            .get("allow_gateway_injection")
            is True
        )

    def session(self, session_id):
        db = self.runtime.host.database()
        try:
            row = db.get_session(session_id)
            if (
                not row
                or row.get("archived")
                or row.get("source") not in LOCAL
                or not row.get("user_id")
            ):
                return None
            return row
        finally:
            db.close()

    def enqueue(self, dispatch):
        """True means this local destination is handled, including a safe deferral."""
        source = self.ledger.rows(
            "SELECT * FROM sources WHERE route=? AND deleted=0", (dispatch["route"],)
        )
        if not source:
            self.ledger.delivery_state(
                dispatch["id"], "deferred", "Destination route is no longer available."
            )
            return True
        source = source[0]
        metadata = json.loads(source["metadata"])
        if metadata.get("platform") not in LOCAL:
            return False
        target = metadata.get("physical_id", source["id"])
        row = self.session(target)
        if (
            not self.enabled()
            or not row
            or metadata.get("group")
            or row.get("session_key") != dispatch["route"]
        ):
            self.ledger.delivery_state(
                dispatch["id"],
                "deferred",
                "Local hidden delivery is unavailable or not authorized.",
            )
            return True
        with self.ledger.transaction() as db:
            current = db.execute(
                "SELECT state FROM dispatches WHERE id=?", (dispatch["id"],)
            ).fetchone()
            if not current or current[0] != "prepared":
                return True
            db.execute(
                "INSERT OR IGNORE INTO desktop_outbox(dispatch_id,target,owner_id,input_id,state,created) VALUES(?,?,?,?,?,?)",
                (
                    dispatch["id"],
                    target,
                    str(row["user_id"]),
                    dispatch["id"],
                    "pending",
                    self.runtime.clock(),
                ),
            )
            db.execute(
                "UPDATE dispatches SET state='awaiting_desktop',updated=? WHERE id=?",
                (self.runtime.clock(), dispatch["id"]),
            )
        return True

    def authorized(self, params, session_id, *, identity=True):
        # The host already enforces session:control for this exact session before invoking us.
        if not session_id or not self.enabled():
            raise PermissionError(
                "Desktop bridge requires an authorized session and injection permission."
            )
        row = self.session(session_id)
        if not row:
            raise PermissionError("Unavailable Desktop session.")
        if identity and params.get("backend_id") != self.identity:
            raise PermissionError("Desktop profile changed.")
        return row

    def current(self, outbox):
        dispatch = self.ledger.dispatch(outbox["dispatch_id"])
        if not dispatch:
            return None
        items = [i for i in self.ledger.items() if i["id"] == dispatch["item"]]
        if (
            not items
            or items[0]["version"] != dispatch["version"]
            or items[0]["data"]["state"] != "open"
        ):
            return None
        source = self.ledger.rows(
            "SELECT * FROM sources WHERE id=?", (dispatch["source"],)
        )
        target = self.session(outbox["target"])
        if not source or source[0]["deleted"] or source[0]["archived"] or not target:
            return None
        if (
            str(target["user_id"]) != outbox["owner_id"]
            or target.get("session_key") != dispatch["route"]
        ):
            return None
        if self.ledger.snapshot(items[0]["scope"])["observations"]:
            return None
        return dispatch, items[0]

    def cancel(self, dispatch_id, reason, *, review_again=False):
        with self.ledger.transaction() as db:
            row = db.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)
            ).fetchone()
            db.execute(
                "UPDATE desktop_outbox SET state='cancelled',error=? WHERE dispatch_id=?",
                (reason, dispatch_id),
            )
            if row and row["state"] == "awaiting_desktop":
                db.execute(
                    "UPDATE dispatches SET state='cancelled',error=?,updated=? WHERE id=?",
                    (reason, self.runtime.clock(), dispatch_id),
                )
                if review_again:
                    item = db.execute(
                        "SELECT * FROM items WHERE id=?", (row["item"],)
                    ).fetchone()
                    if item and item["version"] == row["version"]:
                        data = json.loads(item["data"])
                        if data["state"] == "open":
                            data["next_review"] = self.runtime.clock()
                            db.execute(
                                "UPDATE items SET data=?,version=version+1 WHERE id=?",
                                (dump(data), item["id"]),
                            )
                            db.execute(
                                "UPDATE scopes SET revision=revision+1 WHERE id=?",
                                (item["scope"],),
                            )

    def poll(self, params, *, session_id, **kwargs):
        anchor = self.authorized(params, session_id, identity=False)
        jobs = []
        now = self.runtime.clock()
        rows = self.ledger.rows(
            "SELECT * FROM desktop_outbox WHERE owner_id=? AND state IN ('pending','claimed','accepted') ORDER BY created LIMIT 32",
            (str(anchor["user_id"]),),
        )
        for row in rows:
            dispatch = self.ledger.dispatch(row["dispatch_id"])
            if not dispatch or dispatch["state"] == "cancelled":
                self.cancel(row["dispatch_id"], "Commitment changed or was removed.")
                continue
            if dispatch["state"] == "generated":
                self.generated(row["dispatch_id"])
                continue
            if row["state"] in ("pending", "claimed") and now - row["created"] > 300:
                self.cancel(
                    row["dispatch_id"],
                    "Desktop was unavailable; a fresh review is required.",
                    review_again=True,
                )
                continue
            jobs.append(
                {
                    "id": row["dispatch_id"],
                    "session_id": row["target"],
                    "state": row["state"],
                    "admission_id": row["admission_id"],
                }
            )
        return {"backend_id": self.identity, "jobs": jobs}

    def claim(self, params, *, session_id, **kwargs):
        target = self.authorized(params, session_id)
        client = params.get("client_id")
        if not isinstance(client, str) or not 1 <= len(client) <= 100:
            raise ValueError("Invalid Desktop client identity.")
        now = self.runtime.clock()
        with self.ledger.transaction() as db:
            row = db.execute(
                "SELECT * FROM desktop_outbox WHERE dispatch_id=?", (params.get("id"),)
            ).fetchone()
            if (
                not row
                or row["target"] != session_id
                or row["owner_id"] != str(target["user_id"])
            ):
                return {"ok": False, "reason": "unavailable"}
            if row["state"] not in ("pending", "claimed"):
                return {"ok": False, "reason": row["state"]}
            if row["state"] == "claimed" and row["expires"] > now:
                return {"ok": False, "reason": "claimed"}
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE desktop_outbox SET state='claimed',client_id=?,token=?,expires=? WHERE dispatch_id=?",
                (client, token, now + 30, row["dispatch_id"]),
            )
            return {"ok": True, "token": token}

    def claimed(self, params, session_id, *, state=None):
        self.authorized(params, session_id)
        rows = self.ledger.rows(
            "SELECT * FROM desktop_outbox WHERE dispatch_id=?", (params.get("id"),)
        )
        if not rows:
            raise ValueError("Unknown Desktop dispatch.")
        row = rows[0]
        if (
            row["target"] != session_id
            or row["token"] != params.get("token")
            or row["client_id"] != params.get("client_id")
        ):
            raise PermissionError("Desktop claim mismatch.")
        if state and row["state"] != state:
            raise ValueError("Desktop dispatch has already advanced.")
        return row

    def begin(self, params, *, session_id, **kwargs):
        row = self.claimed(params, session_id, state="claimed")
        if row["expires"] < self.runtime.clock():
            return {"ok": False, "reason": "claim_expired"}
        self.runtime.reconcile_sources()
        current = self.current(row)
        if not current:
            self.cancel(
                row["dispatch_id"], "Source or commitment changed before submission."
            )
            return {"ok": False, "reason": "stale"}
        dispatch, item = current
        if self.runtime.clock() - row["created"] > 300:
            self.cancel(row["dispatch_id"], "Fresh review required.", review_again=True)
            return {"ok": False, "reason": "stale"}
        allowed, reason = self.runtime.allowed(
            item["data"],
            item["scope"],
            self.runtime.clock(),
            quiet_exception=json.loads(dispatch["instruction"])["quiet_exception"],
        )
        if not allowed:
            return {"ok": False, "reason": reason}
        try:
            admitted = self.ledger.admit_dispatch(
                row["dispatch_id"],
                self.runtime.owner,
                self.runtime.lease_generation,
                self.runtime.clock(),
                desktop_claim=(row["token"], row["client_id"], row["target"]),
            )
        except Conflict:
            return {"ok": False, "reason": "worker_not_ready"}
        if not admitted:
            self.cancel(row["dispatch_id"], "Admission guard refused.")
            return {"ok": False, "reason": "stale"}
        return {
            "ok": True,
            "input_id": row["input_id"],
            "session_id": row["target"],
            "text": self.runtime.dispatch_text(dispatch),
            "title_preview": "Follow-up: " + item["data"]["summary"][:500],
        }

    def ack(self, params, *, session_id, **kwargs):
        row = self.claimed(params, session_id)
        receipt = params.get("receipt") or {}
        if receipt.get("ref", {}).get("session_id") != session_id or not isinstance(
            receipt.get("admission_id"), str
        ):
            raise ValueError("Hermes admission receipt destination mismatch.")
        with self.ledger.transaction() as db:
            db.execute(
                "UPDATE desktop_outbox SET admission_id=?,state=CASE WHEN state IN ('generated','cancelled') THEN state ELSE 'accepted' END WHERE dispatch_id=?",
                (receipt["admission_id"], row["dispatch_id"]),
            )
        self.ledger.delivery_state(row["dispatch_id"], "accepted")
        current = self.ledger.dispatch(row["dispatch_id"])
        return {"ok": True, "cancel": not current or current["state"] == "cancelled"}

    def failed(self, params, *, session_id, **kwargs):
        row = self.claimed(params, session_id)
        with self.ledger.transaction() as db:
            db.execute(
                "UPDATE desktop_outbox SET state=CASE WHEN state IN ('generated','cancelled') THEN state ELSE 'unknown' END,error='Desktop submission outcome unknown; not automatically retried.' WHERE dispatch_id=?",
                (row["dispatch_id"],),
            )
        self.ledger.delivery_state(
            row["dispatch_id"],
            "unknown",
            "Desktop submission outcome unknown; inspect the conversation before retrying.",
        )
        return {"ok": True}

    def receipt(self, params, *, session_id, **kwargs):
        self.authorized(params, session_id)
        rows = self.ledger.rows(
            "SELECT * FROM desktop_outbox WHERE dispatch_id=?", (params.get("id"),)
        )
        if not rows or rows[0]["target"] != session_id:
            return {"ok": False}
        row = rows[0]
        receipt = params.get("receipt") or {}
        if (
            receipt.get("admission_id") != row["admission_id"]
            or receipt.get("ref", {}).get("session_id") != session_id
        ):
            raise ValueError("Receipt mismatch.")
        dispatch = self.ledger.dispatch(row["dispatch_id"])
        if not dispatch or dispatch["state"] == "cancelled":
            return {"ok": True, "cancel": True}
        if receipt.get("status") == "terminal":
            # Success is recorded by the trusted post-turn observer; a frontend
            # receipt alone is not a transport-delivery assertion.
            if dispatch["state"] == "generated":
                self.generated(row["dispatch_id"])
            else:
                with self.ledger.transaction() as db:
                    db.execute(
                        "UPDATE desktop_outbox SET state='terminal' WHERE dispatch_id=?",
                        (row["dispatch_id"],),
                    )
                self.ledger.delivery_state(
                    row["dispatch_id"],
                    "unknown",
                    "Host turn ended without a correlated generated response.",
                )
        return {"ok": True, "cancel": False}

    def generated(self, dispatch_id):
        with self.ledger.transaction() as db:
            db.execute(
                "UPDATE desktop_outbox SET state='generated' WHERE dispatch_id=?",
                (dispatch_id,),
            )

    def owns_hidden(self, session_id, message, dispatch_id):
        metadata = message.get("display_metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                return False
        if not isinstance(metadata, dict):
            return False
        if (
            message.get("display_kind") != "hidden"
            or metadata.get("submission_id") != dispatch_id
        ):
            return False
        rows = self.ledger.rows(
            "SELECT 1 FROM desktop_outbox WHERE dispatch_id=? AND input_id=? AND target=? AND issued=1 AND state IN ('submitting','accepted','unknown','terminal','generated','cancelled')",
            (dispatch_id, dispatch_id, session_id),
        )
        return bool(rows)
