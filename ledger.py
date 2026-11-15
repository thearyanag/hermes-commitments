"""Transactional authority. Model calls and delivery never run inside transactions."""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import sqlite3
import threading
import time
import uuid


class Conflict(Exception):
    pass


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def identifier():
    return uuid.uuid4().hex


class Ledger:
    def __init__(self, connection):
        self.db = connection
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
            raise ValueError(
                "Unsupported Commitments database version; no migration was applied"
            )
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, route TEXT NOT NULL,
          actor TEXT NOT NULL, metadata TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
          deleted INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
          cursor INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS scopes (id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS observations (
          id TEXT PRIMARY KEY, source TEXT NOT NULL, scope TEXT NOT NULL, generation INTEGER NOT NULL,
          role TEXT NOT NULL, actor TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          retry_at REAL NOT NULL DEFAULT 0, error TEXT);
        CREATE INDEX IF NOT EXISTS inbox_due ON observations(state,retry_at,created);
        CREATE TABLE IF NOT EXISTS items (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, source TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS changes (
          id TEXT PRIMARY KEY, item TEXT NOT NULL, source TEXT NOT NULL, actor TEXT NOT NULL,
          before_json TEXT, after_json TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS dispatches (
          id TEXT PRIMARY KEY, item TEXT NOT NULL, version INTEGER NOT NULL,
          source TEXT NOT NULL, route TEXT NOT NULL, instruction TEXT NOT NULL,
          state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, error TEXT,
          UNIQUE(item,version));
        CREATE TABLE IF NOT EXISTS preferences (scope TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS lease (name TEXT PRIMARY KEY, owner TEXT NOT NULL,
          generation INTEGER NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, purpose TEXT NOT NULL,
          data TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS admissions (route TEXT NOT NULL, message_id TEXT NOT NULL,
          digest TEXT NOT NULL, actor TEXT NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(route,message_id));
        CREATE TABLE IF NOT EXISTS dispatch_tombstones (id TEXT PRIMARY KEY, route TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS item_links (retired TEXT PRIMARY KEY, target TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contact_log (id TEXT PRIMARY KEY, scope TEXT NOT NULL, occurred REAL NOT NULL);
        INSERT OR IGNORE INTO contact_log SELECT d.id,s.scope,d.created FROM dispatches d
          JOIN sources s ON s.id=d.source WHERE d.state IN ('accepted','generated','unknown');
        PRAGMA user_version=1;
        """)
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def bind(self, source, scope, route, actor, metadata):
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM sources WHERE id=?", (source,)
            ).fetchone()
            if existing:
                # Deleted identities are tombstones. A changed audience cannot inherit history.
                if existing["deleted"] or existing["scope"] != scope:
                    return False
                db.execute(
                    "UPDATE sources SET route=?,metadata=? WHERE id=?",
                    (route, dump(metadata), source),
                )
            else:
                db.execute(
                    "INSERT INTO sources(id,scope,route,actor,metadata) VALUES(?,?,?,?,?)",
                    (source, scope, route, actor, dump(metadata)),
                )
            db.execute("INSERT OR IGNORE INTO scopes(id) VALUES(?)", (scope,))
            return True

    def observe(self, event_id, source, role, actor, content, now=None):
        if not content.strip():
            return False
        with self.transaction() as db:
            src = db.execute("SELECT * FROM sources WHERE id=?", (source,)).fetchone()
            if not src or src["deleted"]:
                return False
            result = db.execute(
                "INSERT OR IGNORE INTO observations(id,source,scope,generation,role,actor,content,created) VALUES(?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    source,
                    src["scope"],
                    src["generation"],
                    role,
                    actor,
                    content,
                    now or time.time(),
                ),
            )
            if result.rowcount:
                db.execute(
                    "UPDATE scopes SET revision=revision+1 WHERE id=?", (src["scope"],)
                )
                return True
            return False

    def snapshot(self, scope):
        with self.lock:
            revision = self.db.execute(
                "SELECT revision FROM scopes WHERE id=?", (scope,)
            ).fetchone()
            events = self.rows(
                "SELECT * FROM observations WHERE scope=? AND state='pending' ORDER BY created,id LIMIT 32",
                (scope,),
            )
            items = self.items(scope)
            return {
                "scope": scope,
                "revision": revision[0] if revision else 0,
                "observations": events,
                "items": items,
            }

    def items(self, scope=None):
        rows = self.rows(
            "SELECT * FROM items"
            + (" WHERE scope=?" if scope else "")
            + " ORDER BY updated DESC",
            (scope,) if scope else (),
        )
        return [{**r, "data": json.loads(r["data"])} for r in rows]

    def pending(self, now):
        return self.rows(
            "SELECT scope FROM observations WHERE state='pending' AND retry_at<=? GROUP BY scope ORDER BY MIN(created) LIMIT 1",
            (now,),
        )

    def fail(self, events, error, now):
        with self.transaction() as db:
            for event in events:
                delay = min(3600, 30 * 2 ** min(event["attempts"], 7))
                db.execute(
                    "UPDATE observations SET attempts=attempts+1,retry_at=?,error=? WHERE id=? AND state='pending'",
                    (now + delay, error[:500], event["id"]),
                )

    def acquire(self, owner, now, ttl=180):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM lease WHERE name='worker'").fetchone()
            if row and row["owner"] != owner and row["expires"] > now:
                return None
            generation = (
                row["generation"]
                if row and row["owner"] == owner
                else (row["generation"] + 1 if row else 1)
            )
            db.execute(
                "INSERT OR REPLACE INTO lease VALUES('worker',?,?,?)",
                (owner, generation, now + ttl),
            )
            return generation

    def check_lease(self, db, owner, generation, now):
        row = db.execute("SELECT * FROM lease WHERE name='worker'").fetchone()
        if (
            not row
            or row["owner"] != owner
            or row["generation"] != generation
            or row["expires"] <= now
        ):
            raise Conflict("worker lease lost")

    def release(self, owner):
        with self.transaction() as db:
            db.execute(
                "UPDATE lease SET expires=0 WHERE name='worker' AND owner=?", (owner,)
            )

    def recover_prepared(self, owner, generation, now):
        """Prepared proves no injection was attempted: re-review rather than replay."""
        with self.transaction() as db:
            self.check_lease(db, owner, generation, now)
            for dispatch in db.execute(
                "SELECT * FROM dispatches WHERE state='prepared'"
            ).fetchall():
                db.execute(
                    "UPDATE dispatches SET state='cancelled',error='worker restarted before injection; re-review',updated=? WHERE id=?",
                    (now, dispatch["id"]),
                )
                row = db.execute(
                    "SELECT * FROM items WHERE id=?", (dispatch["item"],)
                ).fetchone()
                if row and row["version"] == dispatch["version"]:
                    data = json.loads(row["data"])
                    if data["state"] == "open":
                        data["next_review"] = now
                        db.execute(
                            "UPDATE items SET data=?,version=version+1 WHERE id=?",
                            (dump(data), row["id"]),
                        )
                        db.execute(
                            "UPDATE scopes SET revision=revision+1 WHERE id=?",
                            (row["scope"],),
                        )

    def apply(self, snapshot, operations, owner, generation, now):
        evidence = {e["id"]: e for e in snapshot["observations"]}
        with self.transaction() as db:
            self.check_lease(db, owner, generation, now)
            revision = db.execute(
                "SELECT revision FROM scopes WHERE id=?", (snapshot["scope"],)
            ).fetchone()
            if not revision or revision[0] != snapshot["revision"]:
                raise Conflict("new observations arrived during inference")
            for op in operations:
                refs = op.get("evidence", [])
                if not refs or any(ref not in evidence for ref in refs):
                    raise ValueError("operation must cite supplied observations")
                event = evidence[refs[-1]]
                src = db.execute(
                    "SELECT * FROM sources WHERE id=?", (event["source"],)
                ).fetchone()
                if (
                    not src
                    or src["deleted"]
                    or src["generation"] != event["generation"]
                ):
                    raise Conflict("source lifecycle changed")
                quote = op.get("quote", event["content"])
                if (
                    not isinstance(quote, str)
                    or not quote
                    or len(quote) > 1000
                    or not any(quote in evidence[r]["content"] for r in refs)
                ):
                    raise ValueError(
                        "evidence quote must match a supplied observation and be <=1000 characters"
                    )
                retained = [
                    {
                        "id": r,
                        "source": evidence[r]["source"],
                        "actor": evidence[r]["actor"],
                        "role": evidence[r]["role"],
                    }
                    for r in refs
                ]
                data = validate_item(op["data"], now)
                old = None
                if op.get("id"):
                    old = db.execute(
                        "SELECT * FROM items WHERE id=? AND scope=?",
                        (op["id"], snapshot["scope"]),
                    ).fetchone()
                    if not old:
                        raise ValueError("unknown or unauthorized item")
                peers = []
                for peer_id in set(op.get("merge_ids", [])):
                    if not old or peer_id == old["id"]:
                        raise ValueError("invalid merge target")
                    peer = db.execute(
                        "SELECT * FROM items WHERE id=? AND scope=?",
                        (peer_id, snapshot["scope"]),
                    ).fetchone()
                    if not peer:
                        raise ValueError("unknown or unauthorized merge item")
                    peers.append(peer)
                if peers:
                    candidates = [old, *peers]
                    for candidate in candidates:
                        if db.execute(
                            "SELECT 1 FROM dispatches WHERE item=? AND state IN ('prepared','accepted','unknown')",
                            (candidate["id"],),
                        ).fetchone():
                            raise ValueError(
                                "merge deferred until in-flight delivery is resolved"
                            )

                    def created(record):
                        value = db.execute(
                            "SELECT MIN(created) FROM changes WHERE item=?",
                            (record["id"],),
                        ).fetchone()[0]
                        return value if value is not None else record["updated"]

                    old = min(candidates, key=created)
                    for peer in candidates:
                        if peer["id"] == old["id"]:
                            continue
                        db.execute(
                            "UPDATE item_links SET target=? WHERE target=?",
                            (old["id"], peer["id"]),
                        )
                        db.execute(
                            "INSERT OR REPLACE INTO item_links VALUES(?,?)",
                            (peer["id"], old["id"]),
                        )
                        db.execute(
                            "UPDATE changes SET item=? WHERE item=?",
                            (old["id"], peer["id"]),
                        )
                        db.execute("DELETE FROM items WHERE id=?", (peer["id"],))
                        db.execute(
                            "UPDATE dispatches SET state='cancelled',updated=? WHERE item=? AND state='deferred'",
                            (now, peer["id"]),
                        )
                if old:
                    previous = json.loads(old["data"])
                    if data.get("explicit_reminder_at") == previous.get(
                        "explicit_reminder_at"
                    ):
                        data["explicit_reminder_used"] = previous.get(
                            "explicit_reminder_used", False
                        )
                if (
                    data.get("explicit_reminder_at")
                    and data["state"] == "open"
                    and not data.get("explicit_reminder_used")
                ):
                    data["next_review"] = min(
                        data["next_review"],
                        max(now + 0.01, data["explicit_reminder_at"]),
                    )
                item_id = old["id"] if old else identifier()
                source = old["source"] if old else event["source"]
                version = old["version"] + 1 if old else 1
                db.execute(
                    "INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?)",
                    (item_id, snapshot["scope"], source, version, dump(data), now),
                )
                db.execute(
                    "INSERT INTO changes VALUES(?,?,?,?,?,?,?,?)",
                    (
                        identifier(),
                        item_id,
                        event["source"],
                        event["actor"],
                        old["data"] if old else None,
                        dump(data),
                        dump({"references": retained, "quote": quote}),
                        now,
                    ),
                )
                db.execute(
                    "UPDATE dispatches SET state='cancelled',updated=? WHERE item=? AND state IN ('prepared','deferred','awaiting_desktop')",
                    (now, item_id),
                )
            for e in snapshot["observations"]:
                db.execute(
                    "UPDATE observations SET state='applied',content='',error=NULL WHERE id=?",
                    (e["id"],),
                )
            db.execute(
                "UPDATE scopes SET revision=revision+1 WHERE id=?", (snapshot["scope"],)
            )

    def due(self, now):
        out = []
        for item in self.items():
            d = item["data"]
            if (
                d["state"] == "open"
                and d["next_review"] is not None
                and d["next_review"] <= now
            ):
                out.append(item)
        return sorted(out, key=lambda i: i["data"]["next_review"])

    def review(
        self, item, snapshot, decision, route, instruction, owner, generation, now
    ):
        with self.transaction() as db:
            self.check_lease(db, owner, generation, now)
            row = db.execute("SELECT * FROM items WHERE id=?", (item["id"],)).fetchone()
            rev = db.execute(
                "SELECT revision FROM scopes WHERE id=?", (item["scope"],)
            ).fetchone()
            if (
                not row
                or row["version"] != item["version"]
                or not rev
                or rev[0] != snapshot["revision"]
            ):
                raise Conflict("review became stale")
            if db.execute(
                "SELECT 1 FROM observations WHERE scope=? AND state='pending' LIMIT 1",
                (item["scope"],),
            ).fetchone():
                raise Conflict("unprocessed evidence")
            src = db.execute(
                "SELECT * FROM sources WHERE id=?", (item["source"],)
            ).fetchone()
            if not src or src["deleted"] or src["archived"]:
                raise Conflict("source unavailable or archived")
            data = json.loads(row["data"])
            next_review = future_time(decision["next_review"], now)
            exception = bool(
                data.get("explicit_reminder_at")
                and data["explicit_reminder_at"] <= now
                and not data.get("explicit_reminder_used")
            )
            data["next_review"] = next_review
            data["review_reason"] = str(decision["reason"])[:2000]
            version = row["version"] + 1
            db.execute(
                "UPDATE items SET data=?,version=?,updated=? WHERE id=?",
                (dump(data), version, now, item["id"]),
            )
            if decision["action"] != "contact":
                return None
            dispatch = identifier()
            db.execute(
                "INSERT INTO dispatches VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (
                    dispatch,
                    item["id"],
                    version,
                    item["source"],
                    route,
                    dump({"guidance": instruction, "quiet_exception": exception}),
                    "prepared",
                    now,
                    now,
                ),
            )
            return dispatch

    def dispatch(self, dispatch_id):
        rows = self.rows("SELECT * FROM dispatches WHERE id=?", (dispatch_id,))
        return rows[0] if rows else None

    def admit_dispatch(
        self, dispatch_id, owner, generation, now, *, desktop_claim=None
    ):
        """Claim once before crossing the external side-effect boundary. Crash => unknown."""
        with self.transaction() as db:
            self.check_lease(db, owner, generation, now)
            row = db.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)
            ).fetchone()
            expected = "awaiting_desktop" if desktop_claim else "prepared"
            if not row or row["state"] != expected:
                return False
            if desktop_claim:
                token, client, target = desktop_claim
                claim = db.execute(
                    "SELECT * FROM desktop_outbox WHERE dispatch_id=?", (dispatch_id,)
                ).fetchone()
                if (
                    not claim
                    or claim["state"] != "claimed"
                    or claim["token"] != token
                    or claim["client_id"] != client
                    or claim["target"] != target
                    or claim["expires"] < now
                ):
                    return False
            item = db.execute(
                "SELECT * FROM items WHERE id=?", (row["item"],)
            ).fetchone()
            src = db.execute(
                "SELECT * FROM sources WHERE id=?", (row["source"],)
            ).fetchone()
            pending = (
                item
                and db.execute(
                    "SELECT 1 FROM observations WHERE scope=? AND state='pending'",
                    (item["scope"],),
                ).fetchone()
            )
            if (
                not item
                or not src
                or src["deleted"]
                or src["archived"]
                or item["version"] != row["version"]
                or pending
            ):
                db.execute(
                    "UPDATE dispatches SET state='cancelled',updated=? WHERE id=?",
                    (now, dispatch_id),
                )
                return False
            if desktop_claim:
                db.execute(
                    "UPDATE desktop_outbox SET state='submitting',issued=1 WHERE dispatch_id=?",
                    (dispatch_id,),
                )
            if json.loads(row["instruction"]).get("quiet_exception"):
                data = json.loads(item["data"])
                data["explicit_reminder_used"] = True
                db.execute(
                    "UPDATE items SET data=? WHERE id=?", (dump(data), item["id"])
                )
            db.execute(
                "UPDATE dispatches SET state='unknown',updated=? WHERE id=?",
                (now, dispatch_id),
            )
            db.execute(
                "INSERT OR IGNORE INTO contact_log VALUES(?,?,?)",
                (dispatch_id, item["scope"], now),
            )
            return True

    def delivery_state(self, dispatch_id, state, error=None):
        with self.transaction() as db:
            # A fast injected turn may already have generated its reply.
            row = db.execute(
                "SELECT state FROM dispatches WHERE id=?", (dispatch_id,)
            ).fetchone()
            if row and row[0] not in ("generated", "cancelled"):
                if state == "deferred":
                    db.execute("DELETE FROM contact_log WHERE id=?", (dispatch_id,))
                db.execute(
                    "UPDATE dispatches SET state=?,error=?,updated=? WHERE id=?",
                    (state, error, time.time(), dispatch_id),
                )

    def contact_count(self, since, scope=None, exclude=None):
        query = "SELECT COUNT(*) AS count FROM contact_log WHERE occurred>=?"
        args = [since]
        if scope is not None:
            query += " AND scope=?"
            args.append(scope)
        if exclude:
            query += " AND id<>?"
            args.append(exclude)
        return self.rows(query, args)[0]["count"]

    def lifecycle(self, source, reason, now=None):
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source,)).fetchone()
            if not row:
                return
            if reason in ("archive", "unarchive"):
                db.execute(
                    "UPDATE sources SET archived=? WHERE id=?",
                    (int(reason == "archive"), source),
                )
                if reason == "unarchive":
                    for item in db.execute(
                        "SELECT id,data FROM items WHERE source=?", (source,)
                    ).fetchall():
                        data = json.loads(item["data"])
                        if (
                            data.get("explicit_reminder_at")
                            and data["explicit_reminder_at"] <= now
                        ):
                            data["explicit_reminder_used"] = True
                            if data["state"] == "open":
                                data["next_review"] = now
                            db.execute(
                                "UPDATE items SET data=?,version=version+1 WHERE id=?",
                                (dump(data), item["id"]),
                            )
            elif reason == "delete":
                db.execute("DELETE FROM admissions WHERE route=?", (row["route"],))
                db.execute(
                    "INSERT OR IGNORE INTO dispatch_tombstones SELECT id,route FROM dispatches WHERE (source=? OR route=?) AND state IN ('prepared','accepted','unknown','deferred','awaiting_desktop')",
                    (source, row["route"]),
                )
                db.execute(
                    "UPDATE dispatches SET state='cancelled',instruction='',updated=? WHERE route=? AND state IN ('prepared','accepted','unknown','deferred','awaiting_desktop')",
                    (time.time(), row["route"]),
                )
                anchored = [
                    r[0]
                    for r in db.execute(
                        "SELECT id FROM items WHERE source=?", (source,)
                    )
                ]
                for item in anchored:
                    db.execute("DELETE FROM changes WHERE item=?", (item,))
                    db.execute("DELETE FROM dispatches WHERE item=?", (item,))
                    db.execute(
                        "DELETE FROM dispatches WHERE item IN (SELECT retired FROM item_links WHERE target=?)",
                        (item,),
                    )
                    db.execute("DELETE FROM item_links WHERE target=?", (item,))
                db.execute("DELETE FROM items WHERE source=?", (source,))
                db.execute("DELETE FROM observations WHERE source=?", (source,))
                db.execute("DELETE FROM changes WHERE source=?", (source,))
                db.execute(
                    "UPDATE sources SET deleted=1,generation=generation+1,metadata='{}',route='',actor='' WHERE id=?",
                    (source,),
                )
            else:
                raise ValueError("unknown lifecycle transition")
            if reason != "unarchive":
                db.execute(
                    "UPDATE dispatches SET state='cancelled',instruction='',updated=? WHERE source=? AND state IN ('prepared','deferred','awaiting_desktop')",
                    (time.time(), source),
                )
            db.execute(
                "UPDATE scopes SET revision=revision+1 WHERE id=?", (row["scope"],)
            )

    def settings(self, scope):
        rows = self.rows("SELECT data FROM preferences WHERE scope=?", (scope,))
        return json.loads(rows[0]["data"]) if rows else {}

    def set_settings(self, scope, settings):
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO preferences VALUES(?,?)",
                (scope, dump(settings)),
            )
            # Invalidate every in-flight inference/review, including ones for global settings.
            db.execute("UPDATE scopes SET revision=revision+1")

    def usage(self, purpose, data, event_id=None):
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO usage VALUES(?,?,?,?)",
                (event_id or identifier(), purpose, dump(data), time.time()),
            )


def future_time(value, now):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= now
    ):
        raise ValueError("next_review must be a future UTC epoch timestamp")
    return float(value)


def validate_item(data, now):
    if not isinstance(data, dict) or data.get("state") not in (
        "candidate",
        "open",
        "done",
        "dropped",
    ):
        raise ValueError("invalid outcome state")
    allowed = {
        "summary",
        "owner",
        "state",
        "deadline",
        "deadline_text",
        "progress",
        "next_review",
        "reason",
        "review_reason",
        "snooze_until",
        "explicit_reminder_at",
        "explicit_reminder_used",
    }
    if set(data) - allowed or not all(
        isinstance(data.get(k), str) for k in ["summary", "owner", "progress", "reason"]
    ):
        raise ValueError("invalid item fields")
    if not data["summary"].strip() or len(dump(data)) > 12000:
        raise ValueError("empty or oversized item")
    if not isinstance(data.get("deadline_text", ""), str):
        raise ValueError("deadline_text must be text")
    if data["state"] == "open":
        future_time(data.get("next_review"), now)
    elif data.get("next_review") is not None:
        raise ValueError("inactive items cannot schedule nudges")
    for key in ("deadline", "snooze_until", "explicit_reminder_at"):
        v = data.get(key)
        if v is not None and (
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
        ):
            raise ValueError(f"invalid {key}")
    return data
