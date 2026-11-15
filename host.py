"""Public Hermes adapter. Source history is opened read-only; credentials stay host-owned."""

from __future__ import annotations
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path


def text_only(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            p.get("text", "")
            for p in value
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


class Host:
    def __init__(self, ctx):
        from hermes_constants import get_hermes_home

        self.ctx = ctx
        self.home = Path(get_hermes_home())
        self.local_owner = (
            f"local:uid:{os.getuid()}" if hasattr(os, "getuid") else "local:operator"
        )

    def owner(self, actor):
        return actor == self.local_owner or actor in self.ctx.get_config(
            "owner_identities", []
        )

    def ready(self):
        from gateway.status import read_runtime_status, runtime_status_is_stale

        status = read_runtime_status(self.home / "gateway_state.json")
        return bool(
            status
            and status.get("pid") == os.getpid()
            and status.get("gateway_state") == "running"
            and not runtime_status_is_stale(status)
        )

    def config(self):
        from hermes_cli.config import load_config_readonly

        return load_config_readonly() or {}

    def model_ready(self):
        slot = self.config().get("auxiliary", {}).get("commitments", {})
        return bool(slot.get("model"))

    def timezone(self):
        cfg = self.config()
        from agent.secret_scope import is_multiplex_active

        configured = cfg.get("timezone") or ""
        if not is_multiplex_active():
            configured = os.getenv("HERMES_TIMEZONE", "").strip() or configured
        if configured:
            ZoneInfo(configured)
            return configured
        local = datetime.now().astimezone()
        return (
            f"Hermes server-local: {local.tzname()}, UTC offset {local.strftime('%z')}"
        )

    def database(self):
        from hermes_state import SessionDB

        return SessionDB(self.home / "state.db", read_only=True)

    def redact(self, text):
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)

    def identity(self, session_id="", source=None, platform="", sender_id=None):
        from gateway.session_context import get_session_env

        source = source.to_dict() if hasattr(source, "to_dict") else (source or {})
        supplied_source = source.copy()

        def get(field):
            return (
                source.get(field)
                or get_session_env("HERMES_SESSION_" + field.upper())
                or ""
            )

        platform = str(source.get("platform") or platform or get("platform"))
        local = platform in ("local", "desktop", "gui", "tui", "cli")
        user = str(get("actor_id") or sender_id or get("user_id"))
        if local and not user and platform in ("cli", "tui"):
            user = self.local_owner.removeprefix("local:")
        actor = f"{'local' if local else platform}:{user}" if user else ""
        route = str(get("key"))
        sid = session_id or str(get("id"))
        row = None
        if sid and (self.home / "state.db").exists():
            db = self.database()
            try:
                row = db.get_session(sid)
            finally:
                db.close()
        if row:
            origin = json.loads(row.get("origin_json") or "{}")
            route = route or row.get("session_key") or ""
            source = {**origin, **source}
            if not actor and row.get("user_id"):
                actor = f"{'local' if local else platform}:{row['user_id']}"
        kind = (
            source.get("chat_type") or get("chat_type") or ("private" if local else "")
        )
        group = kind in ("group", "supergroup", "channel", "room")
        if group:
            trusted_actor = get("actor_id") or supplied_source.get("user_id") or ""
            actor = f"{platform}:{trusted_actor}" if trusted_actor else ""
            # No ACL API is assumed: isolate topics until cross-topic access is qualified.
            group_key = [
                self.local_owner,
                platform,
                str(source.get("scope_id") or get("scope_id")),
                str(source.get("chat_id") or get("chat_id")),
            ]
            scope = "group:" + json.dumps(
                group_key + [str(source.get("thread_id") or get("thread_id"))],
                separators=(",", ":"),
            )
        elif actor:
            scope = "private:" + (self.local_owner if self.owner(actor) else actor)
        else:
            scope = "isolated:" + sid
        return {
            "source": sid,
            "scope": scope,
            "route": route,
            "actor": actor,
            "metadata": {
                "physical_id": sid,
                "platform": platform,
                "group": group,
                "origin": source,
                "identity_verified": bool(actor),
            },
        }
