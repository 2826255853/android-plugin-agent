"""Durable inbox-to-dialogue processing and ordered event delivery."""

import json
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

from dialogue_draft import DRAFT_PROMPT_VERSION, check_draft, draft_input
from dialogue_protocol import WINDOW, check_submission, check_transition, state_of, strict_object, validate
from message_protocol import MAX_REQUEST_BYTES, ProtocolError
from model_boundary import MAX_OUTPUT_TOKENS, MOCK_MODEL, Config, ModelBoundary, ModelError, MockProvider, RATES, Request
from spec_workflow import SPEC_DIRECTORY, SpecError, next_version


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def scope(value):
    return value["deviceId"], value["projectId"], value["sessionId"]


def envelope(key, kind, **fields):
    device, project, session = key
    return {"dialogueVersion": 1, "deviceId": device, "projectId": project, "sessionId": session,
            "type": kind, **fields}


class DialogueCoordinator:
    def __init__(self, inbox, provider=None, *, spec_store=SPEC_DIRECTORY / "projects"):
        self.database = inbox.database
        self.lock = inbox.database_lock
        self.changed = threading.Event()
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self._work, name="dialogue-coordinator")
        self.config = Config("mock", MOCK_MODEL, 25_000_000)
        self.model = ModelBoundary(self.config, provider if provider is not None else MockProvider())
        self.spec_store = spec_store
        self.database.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS dialogue_sessions (
                device_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                body_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                active INTEGER NOT NULL CHECK (active IN (0, 1)),
                desktop_sequence INTEGER NOT NULL DEFAULT 0,
                phone_sequence INTEGER NOT NULL DEFAULT 0,
                acked_sequence INTEGER NOT NULL DEFAULT 0,
                spec_version INTEGER,
                spec_sha256 TEXT,
                pending_question TEXT,
                PRIMARY KEY (device_id, project_id, session_id),
                UNIQUE (device_id, message_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS dialogue_active_project
                ON dialogue_sessions(device_id, project_id) WHERE active = 1;
            CREATE TABLE IF NOT EXISTS dialogue_events (
                device_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('desktop', 'phone')),
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                received_at TEXT NOT NULL,
                PRIMARY KEY (device_id, project_id, session_id, direction, sequence),
                UNIQUE (device_id, project_id, session_id, direction, event_id),
                FOREIGN KEY (device_id, project_id, session_id)
                    REFERENCES dialogue_sessions(device_id, project_id, session_id)
            );
            CREATE TABLE IF NOT EXISTS dialogue_model_calls (
                device_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                status TEXT NOT NULL,
                reserved_nano_usd INTEGER NOT NULL,
                charged_nano_usd INTEGER NOT NULL DEFAULT 0,
                exact_charge INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cached_input_tokens INTEGER,
                elapsed_ms INTEGER,
                content_sha256 TEXT,
                candidate_json TEXT,
                error_code TEXT,
                PRIMARY KEY (device_id, project_id, session_id),
                FOREIGN KEY (device_id, project_id, session_id)
                    REFERENCES dialogue_sessions(device_id, project_id, session_id)
            );
        """)
        self._recover()
        # An A.1 receipt can already be durable when the process stops before ingest.
        for message in inbox.dialogue_messages():
            self.ingest(message)

    @contextmanager
    def transaction(self):
        with self.lock, self.database:
            self.database.execute("BEGIN IMMEDIATE")
            yield

    def start(self):
        self.worker.start()
        self.changed.set()

    def stop(self):
        self.stopped.set()
        self.changed.set()
        if self.worker.ident is not None:
            self.worker.join()

    def ingest(self, message):
        if message["message_type"] != "user.message":
            return
        device, project, message_id = message["device_id"], message["project_id"], message["message_id"]
        key = device, project, message_id
        with self.transaction():
            inserted = self.database.execute("""
                INSERT INTO dialogue_sessions (
                    device_id, project_id, session_id, message_id, body_sha256, state, active
                ) VALUES (?, ?, ?, ?, ?, 'connected', 0)
                ON CONFLICT (device_id, message_id) DO NOTHING
            """, (*key, message_id, message["body_sha256"]))
            if inserted.rowcount:
                self._publish(key, "desktop.status", state="queued")
                self._activate_next(device, project)
        self.changed.set()

    def _recover(self):
        with self.transaction():
            pending = self.database.execute("""
                SELECT device_id, project_id, session_id FROM dialogue_model_calls WHERE status = 'calling'
            """).fetchall()
            for row in pending:
                key = tuple(row)
                self.database.execute("""
                    UPDATE dialogue_model_calls
                    SET status = 'uncertain', charged_nano_usd = reserved_nano_usd,
                        exact_charge = 0, error_code = 'MODEL_USAGE_UNCERTAIN'
                    WHERE device_id = ? AND project_id = ? AND session_id = ?
                """, key)
                session = self._session(key)
                if session["state"] != "cancelled":
                    self._publish(key, "desktop.error", code="MODEL_USAGE_UNCERTAIN")

            legacy_calls = self.database.execute("""
                SELECT c.device_id, c.project_id, c.session_id, c.request_id, c.candidate_json
                FROM dialogue_model_calls c JOIN dialogue_sessions s USING (device_id, project_id, session_id)
                WHERE c.status = 'completed' AND c.candidate_json IS NOT NULL AND s.state = 'running'
            """).fetchall()
            for row in legacy_calls:
                key = tuple(row)[:3]
                try:
                    candidate = strict_object(row["candidate_json"])
                except ProtocolError:
                    candidate = None
                checked, error = self._check_draft(key, candidate)
                self._publish_checked(key, checked, error)
                self.database.execute("""
                    UPDATE dialogue_model_calls SET status = ?, candidate_json = NULL, error_code = ?
                    WHERE request_id = ?
                """, ("failed" if error else "completed", error, row["request_id"]))

    def _work(self):
        while not self.stopped.is_set():
            self.changed.wait()
            self.changed.clear()
            while not self.stopped.is_set():
                try:
                    job = self._claim()
                    if job is None:
                        break
                    key, request = job
                    if request is not None:
                        self._call(key, request)
                except (sqlite3.Error, ProtocolError):
                    # The next service start reconciles any committed claim without resending it.
                    print("dialogue: INTERNAL_ERROR", file=sys.stderr, flush=True)
                    break

    def _claim(self):
        with self.transaction():
            row = self.database.execute("""
                SELECT s.device_id, s.project_id, s.session_id, m.body
                FROM dialogue_sessions s
                LEFT JOIN messages m ON m.device_id = s.device_id AND m.message_id = s.message_id
                    AND m.project_id = s.project_id AND m.body_sha256 = s.body_sha256
                WHERE s.active = 1 AND s.state = 'queued'
                    AND NOT EXISTS (
                        SELECT 1 FROM dialogue_model_calls c
                        WHERE c.device_id = s.device_id AND c.project_id = s.project_id AND c.session_id = s.session_id
                    )
                ORDER BY s.rowid LIMIT 1
            """).fetchone()
            if row is None:
                return None
            key = tuple(row)[:3]
            if row["body"] is None:
                self._publish(key, "desktop.status", state="cancelled")
                return key, None
            version = next_version(self.spec_store, key[1])
            request = Request(uuid4(), key[1], UUID(key[2]), draft_input(row["body"], key[1], version),
                              output_tokens=MAX_OUTPUT_TOKENS, prompt_version=DRAFT_PROMPT_VERSION)
            reserve = RATES[self.config.provider].ceiling(request.output_tokens)
            self.database.execute("""
                INSERT INTO dialogue_model_calls (
                    device_id, project_id, session_id, request_id, provider, model,
                    prompt_version, status, reserved_nano_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'calling', ?)
            """, (*key, str(request.request_id), self.config.provider, self.config.model, request.prompt_version, reserve))
            self._publish(key, "desktop.status", state="running")
            return key, request

    def _call(self, key, request):
        candidate, receipt, error = None, None, None
        try:
            result = self.model.call(request, self.stopped)
            candidate, receipt, error = result.candidate, result.receipt, result.receipt.error
        except ModelError as failure:
            receipt = self.model.receipt(request.request_id)
            error = failure.code
        with self.transaction():
            uncertain = receipt is None and error in {"MODEL_TIMEOUT", "MODEL_CANCELLED"}
            usage = receipt.usage if receipt else None
            session = self._session(key)
            cancelled = session["state"] == "cancelled"
            checked = None
            if not error and not cancelled:
                checked, error = self._check_draft(key, candidate)
            self.database.execute("""
                UPDATE dialogue_model_calls SET status = ?, exact_charge = ?,
                    charged_nano_usd = CASE WHEN ? THEN reserved_nano_usd ELSE ? END,
                    input_tokens = ?, output_tokens = ?, cached_input_tokens = ?, elapsed_ms = ?,
                    content_sha256 = ?, error_code = ?
                WHERE request_id = ? AND status = 'calling'
            """, (
                "uncertain" if uncertain else "failed" if error else "completed",
                int(receipt.exact_charge) if receipt else int(not uncertain), uncertain,
                receipt.charged_nano_usd if receipt else 0, usage.input_tokens if usage else None,
                usage.output_tokens if usage else None, usage.cached_input_tokens if usage else None,
                receipt.elapsed_ms if receipt else None, receipt.content_sha256 if receipt else None,
                error, str(request.request_id),
            ))
            if not cancelled:
                self._publish_checked(key, checked, error)

    def _check_draft(self, key, candidate):
        try:
            version = next_version(self.spec_store, key[1])
            return check_draft(candidate, key[1], version, self.spec_store), None
        except SpecError:
            return None, "MODEL_INVALID_OUTPUT"

    def _publish_checked(self, key, checked, error):
        if error:
            self._publish(key, "desktop.error", code=error)
            return
        fields, decision = checked
        expires_at = (datetime.now(timezone.utc) + WINDOW).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if "question" in decision:
            self._publish(key, "desktop.draft", **fields)
            self._publish(key, "desktop.question", expiresAt=expires_at, **decision,
                          specVersion=fields["specVersion"], specSha256=fields["specSha256"])
        else:
            self._publish(key, "desktop.review", expiresAt=expires_at, **fields, **decision)

    def pull(self, request):
        key, after = scope(request), request["after"]
        with self.lock:
            session = self._session(key)
            latest = session["desktop_sequence"]
            if after > latest:
                raise ProtocolError("INVALID_CURSOR")
            page = envelope(key, "dialogue.page", after=after, next=after, hasMore=after < latest, events=[])
            rows = self.database.execute("""
                SELECT sequence, payload FROM dialogue_events
                WHERE device_id = ? AND project_id = ? AND session_id = ?
                    AND direction = 'desktop' AND sequence > ?
                ORDER BY sequence LIMIT ?
            """, (*key, after, request["limit"]))
            for row in rows:
                if row["sequence"] != page["next"] + 1:
                    raise ProtocolError("CURSOR_EXPIRED")
                event = json.loads(row["payload"])
                trial = dict(page, next=row["sequence"], hasMore=row["sequence"] < latest,
                             events=[*page["events"], event])
                if len(encode(trial).encode("utf-8")) > MAX_REQUEST_BYTES:
                    if not page["events"]:
                        raise ProtocolError("PAYLOAD_TOO_LARGE")
                    break
                page = trial
            if not page["events"] and page["hasMore"]:
                raise ProtocolError("CURSOR_EXPIRED")
            return page

    def acknowledge(self, request):
        key = scope(request)
        with self.transaction():
            session = self._session(key)
            if request["through"] > session["desktop_sequence"]:
                raise ProtocolError("INVALID_CURSOR")
            through = max(request["through"], session["acked_sequence"])
            self.database.execute("""
                UPDATE dialogue_sessions SET acked_sequence = ?
                WHERE device_id = ? AND project_id = ? AND session_id = ?
            """, (through, *key))
            return envelope(key, "dialogue.ack", through=through)

    def submit(self, event):
        key = scope(event)
        with self.transaction():
            session = self._session(key)
            previous = self.database.execute("""
                SELECT payload FROM dialogue_events
                WHERE device_id = ? AND project_id = ? AND session_id = ? AND direction = 'phone' AND event_id = ?
            """, (*key, event["eventId"])).fetchone()
            head = self.database.execute("""
                SELECT payload FROM dialogue_events
                WHERE device_id = ? AND project_id = ? AND session_id = ? AND direction = 'desktop' AND sequence = ?
            """, (*key, session["desktop_sequence"])).fetchone()
            decision = check_submission(
                event, json.loads(head[0]), json.loads(previous[0]) if previous else None,
                session["phone_sequence"] + 1, datetime.now(timezone.utc),
            )
            duplicate = decision == "duplicate"
            if not duplicate:
                # B.5 and B.6 attach handlers for answering, revision, and review decisions.
                if event["type"] != "phone.cancel":
                    raise ProtocolError("INVALID_STATE")
                self._write_event(key, "phone", event)
                self.database.execute("""
                    UPDATE dialogue_sessions SET phone_sequence = ?
                    WHERE device_id = ? AND project_id = ? AND session_id = ?
                """, (event["sequence"], *key))
                self._publish(key, "desktop.status", state="cancelled")
        self.changed.set()
        return envelope(key, "dialogue.accepted", eventId=event["eventId"],
                        sequence=event["sequence"], duplicate=duplicate)

    def _session(self, key):
        row = self.database.execute("""
            SELECT * FROM dialogue_sessions WHERE device_id = ? AND project_id = ? AND session_id = ?
        """, key).fetchone()
        if row is None:
            raise ProtocolError("SESSION_NOT_FOUND")
        return row

    def _write_event(self, key, direction, event):
        self.database.execute("""
            INSERT INTO dialogue_events (
                device_id, project_id, session_id, direction, sequence, event_id, payload, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (*key, direction, event["sequence"], event["eventId"], encode(event), timestamp()))

    def _publish(self, key, kind, **fields):
        session = self._session(key)
        event = validate(envelope(key, kind, eventId=str(uuid4()), createdAt=timestamp(),
                                  sequence=session["desktop_sequence"] + 1, messageId=session["message_id"], **fields))
        state = state_of(event)
        if kind != "desktop.draft" or session["state"] != "running":
            check_transition(session["state"], state)
        if kind == "desktop.question" and (
            session["spec_version"] != event["specVersion"] or session["spec_sha256"] != event["specSha256"]
        ):
            raise ProtocolError("SPEC_MISMATCH")
        terminal = state in {"failed", "cancelled", "confirmed"}
        if len(encode(envelope(key, "dialogue.page", after=event["sequence"] - 1,
                               next=event["sequence"], hasMore=True, events=[event])).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ProtocolError("PAYLOAD_TOO_LARGE")
        self._write_event(key, "desktop", event)
        self.database.execute("""
            UPDATE dialogue_sessions SET state = ?, desktop_sequence = ?, active = ?,
                spec_version = ?, spec_sha256 = ?, pending_question = ?
            WHERE device_id = ? AND project_id = ? AND session_id = ?
        """, (
            state, event["sequence"], 0 if terminal else session["active"],
            event.get("specVersion", session["spec_version"]), event.get("specSha256", session["spec_sha256"]),
            event["eventId"] if kind == "desktop.question" else None, *key,
        ))
        if terminal and session["active"]:
            self._activate_next(*key[:2])
        return event

    def _activate_next(self, device, project):
        self.database.execute("""
            UPDATE dialogue_sessions SET active = 1 WHERE rowid = (
                SELECT rowid FROM dialogue_sessions
                WHERE device_id = ? AND project_id = ? AND state = 'queued' ORDER BY rowid LIMIT 1
            ) AND NOT EXISTS (
                SELECT 1 FROM dialogue_sessions WHERE device_id = ? AND project_id = ? AND active = 1
            )
        """, (device, project, device, project))
