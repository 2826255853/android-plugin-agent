"""Durable inbox-to-dialogue processing and ordered event delivery."""

import hashlib
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
from model_boundary import MAX_CLARIFICATIONS, MAX_OUTPUT_TOKENS, MOCK_MODEL, Config, ModelBoundary, ModelError, MockProvider, RATES, Request
from spec_workflow import SPEC_DIRECTORY, SpecError, load_json, next_version, save_confirmed, spec_json


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
        self.call_cancellations = {}
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
            CREATE TABLE IF NOT EXISTS dialogue_confirmations (
                recovery_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                finished INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (device_id, project_id, session_id)
                    REFERENCES dialogue_sessions(device_id, project_id, session_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS dialogue_pending_confirmation
                ON dialogue_confirmations(device_id, project_id, session_id) WHERE finished = 0;
            CREATE TABLE IF NOT EXISTS dialogue_model_calls (
                request_id TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
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
                FOREIGN KEY (device_id, project_id, session_id)
                    REFERENCES dialogue_sessions(device_id, project_id, session_id)
            );
        """)
        self._migrate_model_calls()
        self._recover()
        self._recover_confirmations()
        # An A.1 receipt can already be durable when the process stops before ingest.
        for message in inbox.dialogue_messages():
            self.ingest(message)

    def _migrate_model_calls(self):
        primary_key = self.database.execute("PRAGMA table_info(dialogue_model_calls)").fetchall()
        if not any(row[1] == "session_id" and row[5] for row in primary_key):
            return
        with self.transaction():
            self.database.execute("ALTER TABLE dialogue_model_calls RENAME TO dialogue_model_calls_legacy")
            self.database.execute("""
                CREATE TABLE dialogue_model_calls (
                    request_id TEXT NOT NULL UNIQUE,
                    device_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
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
                    FOREIGN KEY (device_id, project_id, session_id)
                        REFERENCES dialogue_sessions(device_id, project_id, session_id)
                )
            """)
            columns = "request_id, device_id, project_id, session_id, provider, model, prompt_version, status, " \
                      "reserved_nano_usd, charged_nano_usd, exact_charge, input_tokens, output_tokens, " \
                      "cached_input_tokens, elapsed_ms, content_sha256, candidate_json, error_code"
            self.database.execute(
                f"INSERT INTO dialogue_model_calls ({columns}) SELECT {columns} FROM dialogue_model_calls_legacy"
            )
            self.database.execute("DROP TABLE dialogue_model_calls_legacy")
            self.database.execute("CREATE INDEX dialogue_calls_session ON dialogue_model_calls "
                                  "(device_id, project_id, session_id)")

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
        with self.lock:
            for cancellation in self.call_cancellations.values():
                cancellation.set()
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
                    WHERE device_id = ? AND project_id = ? AND session_id = ? AND status = 'calling'
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
                        WHERE c.device_id = s.device_id AND c.project_id = s.project_id
                            AND c.session_id = s.session_id AND c.status = 'calling'
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
            latest_phone = self.database.execute("""
                SELECT payload FROM dialogue_events WHERE device_id = ? AND project_id = ? AND session_id = ?
                    AND direction = 'phone' ORDER BY sequence DESC LIMIT 1
            """, key).fetchone()
            request_input = draft_input(row["body"], key[1], version)
            if latest_phone is not None:
                phone_event = json.loads(latest_phone["payload"])
                linked = self.database.execute("""
                    SELECT sequence, payload FROM dialogue_events
                    WHERE device_id = ? AND project_id = ? AND session_id = ? AND direction = 'desktop' AND event_id = ?
                """, (*key, phone_event["inReplyTo"])).fetchone()
                if linked is None:
                    self._publish(key, "desktop.error", code="MODEL_REQUEST_CONFLICT")
                    return key, None
                prior = linked if phone_event["type"] == "phone.revise" else self.database.execute("""
                    SELECT payload FROM dialogue_events
                    WHERE device_id = ? AND project_id = ? AND session_id = ? AND direction = 'desktop' AND sequence = ?
                """, (*key, linked["sequence"] - 1)).fetchone()
                draft = json.loads(prior["payload"]) if prior else {}
                if "specJson" not in draft or any(draft[name] != phone_event[name] for name in ("specVersion", "specSha256")):
                    self._publish(key, "desktop.error", code="MODEL_REQUEST_CONFLICT")
                    return key, None
                request_input = draft_input(
                    row["body"], key[1], version,
                    previous_spec=json.loads(draft["specJson"]),
                    user_input={name: phone_event[name] for name in
                                ("type", "text", "inReplyTo", "specVersion", "specSha256")},
                )
            calls = self.database.execute("""
                SELECT COUNT(*) FROM dialogue_model_calls WHERE device_id = ? AND project_id = ? AND session_id = ?
            """, key).fetchone()[0]
            if calls >= MAX_CLARIFICATIONS + 1:
                self._publish(key, "desktop.error", code="MODEL_ROUND_LIMIT")
                return key, None
            request = Request(uuid4(), key[1], UUID(key[2]), request_input,
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
        cancellation = threading.Event()
        with self.lock:
            self.call_cancellations[key] = cancellation
            if self.stopped.is_set() or self._session(key)["state"] == "cancelled":
                cancellation.set()
        try:
            result = self.model.call(request, cancellation)
            candidate, receipt, error = result.candidate, result.receipt, result.receipt.error
        except ModelError as failure:
            receipt = self.model.receipt(request.request_id)
            error = failure.code
        with self.transaction():
            uncertain = (receipt is None and error in {"MODEL_TIMEOUT", "MODEL_CANCELLED"}
                         and self.model.has_request(request.request_id))
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
            self.call_cancellations.pop(key, None)

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
            if session["state"] == "result_ready" and after <= session["desktop_sequence"]:
                pending = self._pending_confirmation(key)
                if pending is not None:
                    self._finish_confirmation(pending)
                else:
                    head = self._head(key, session)
                    if datetime.now(timezone.utc) >= datetime.fromisoformat(head["expiresAt"]):
                        with self.transaction():
                            self._refresh_review(key, head)
                        self.changed.set()
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
        kind = event["type"]
        with self.lock:
            pending = self._pending_confirmation(key)
            if pending is not None:
                error = self._finish_confirmation(pending)
                if error and event == json.loads(pending["payload"]):
                    raise ProtocolError(error)
            with self.transaction():
                session = self._session(key)
                previous = self.database.execute("""
                    SELECT payload FROM dialogue_events
                    WHERE device_id = ? AND project_id = ? AND session_id = ? AND direction = 'phone' AND event_id = ?
                """, (*key, event["eventId"])).fetchone()
                head = self._head(key, session)
                decision = check_submission(
                    event, head, json.loads(previous[0]) if previous else None,
                    session["phone_sequence"] + 1, datetime.now(timezone.utc),
                )
                if decision == "duplicate":
                    return self._submission_receipt(key, event, duplicate=True)
                if kind == "phone.confirm":
                    confirmation = self.database.execute("""
                        INSERT INTO dialogue_confirmations (recovery_id, device_id, project_id, session_id, payload)
                        VALUES (?, ?, ?, ?, ?) RETURNING *
                    """, (str(uuid4()), *key, encode(event))).fetchall()[0]
                else:
                    self._record_submission(key, event)
                    state = "cancelled" if kind in {"phone.cancel", "phone.reject"} else "queued"
                    self._publish(key, "desktop.status", state=state)
            if kind == "phone.confirm":
                error = self._finish_confirmation(confirmation)
                if error:
                    raise ProtocolError(error)
            if kind == "phone.cancel":
                cancellation = self.call_cancellations.get(key)
                if cancellation is not None:
                    cancellation.set()
        self.changed.set()
        return self._submission_receipt(key, event, duplicate=False)

    def _pending_confirmation(self, key):
        return self.database.execute("""
            SELECT * FROM dialogue_confirmations
            WHERE device_id = ? AND project_id = ? AND session_id = ? AND finished = 0
        """, key).fetchone()

    @staticmethod
    def _submission_receipt(key, event, *, duplicate):
        return envelope(key, "dialogue.accepted", eventId=event["eventId"],
                        sequence=event["sequence"], duplicate=duplicate)

    def _record_submission(self, key, event):
        self._write_event(key, "phone", event)
        self.database.execute("""
            UPDATE dialogue_sessions SET phone_sequence = ?
            WHERE device_id = ? AND project_id = ? AND session_id = ?
        """, (event["sequence"], *key))

    def _head(self, key, session):
        row = self.database.execute("""
            SELECT payload FROM dialogue_events
            WHERE device_id = ? AND project_id = ? AND session_id = ? AND direction = 'desktop' AND sequence = ?
        """, (*key, session["desktop_sequence"])).fetchone()
        return json.loads(row["payload"])

    def _recover_confirmations(self):
        for row in self.database.execute("SELECT * FROM dialogue_confirmations ORDER BY rowid").fetchall():
            try:
                if row["finished"]:
                    self._clean_confirmation(row, self._recovery_link(row))
                else:
                    self._finish_confirmation(row)
            except OSError:
                print("dialogue: INTERNAL_ERROR", file=sys.stderr, flush=True)

    def _recovery_link(self, row):
        return self.spec_store / row["project_id"] / f".confirm-{row['recovery_id']}.tmp"

    def _finish_confirmation(self, row):
        event = json.loads(row["payload"])
        key = scope(event)
        recovery_link = self._recovery_link(row)
        with self.transaction():
            head = self._head(key, self._session(key))
            error = self._save_review(key, event, head, recovery_link)
            self.database.execute("UPDATE dialogue_confirmations SET finished = 1 WHERE recovery_id = ?",
                                  (row["recovery_id"],))
        self._clean_confirmation(row, recovery_link)
        self.changed.set()
        return error

    def _clean_confirmation(self, row, recovery_link):
        try:
            recovery_link.unlink(missing_ok=True)
            with self.transaction():
                self.database.execute("DELETE FROM dialogue_confirmations WHERE recovery_id = ?",
                                      (row["recovery_id"],))
        except (OSError, sqlite3.Error):
            # The receipt is already durable. Startup can retry cleanup.
            print("dialogue: INTERNAL_ERROR", file=sys.stderr, flush=True)

    def _save_review(self, key, event, head, recovery_link):
        version = event["specVersion"]
        try:
            validate(head)
            draft = strict_object(head["specJson"])
            fields, decision = check_draft(draft, key[1], version, self.spec_store)
            if (head["type"] != "desktop.review" or "summary" not in decision
                    or fields["specSha256"] != event["specSha256"]
                    or fields["specJson"] != head["specJson"] or head["eventId"] != event["inReplyTo"]):
                raise SpecError("review binding changed")
        except (ProtocolError, SpecError):
            self._publish(key, "desktop.error", code="SPEC_MISMATCH")
            return "SPEC_MISMATCH"

        destination = recovery_link.parent / f"v{version}.json"
        # The extra link identifies this intent's file across a crash before the SQLite commit.
        try:
            already_saved = recovery_link.samefile(destination)
        except FileNotFoundError:
            already_saved = False
        if not already_saved:
            try:
                save_confirmed(draft, self.spec_store, recovery_link=recovery_link)
            except SpecError:
                self._refresh_review(key, head)
                return "SPEC_MISMATCH"
        try:
            saved_json = spec_json(load_json(destination))
        except ValueError as failure:
            raise OSError("could not verify saved specification") from failure
        if saved_json != spec_json(dict(draft, status="confirmed")):
            self._publish(key, "desktop.error", code="SPEC_MISMATCH")
            return "SPEC_MISMATCH"
        saved_digest = hashlib.sha256(saved_json.encode("utf-8")).hexdigest()
        self._record_submission(key, event)
        self._publish(
            key, "desktop.status", state="confirmed", specVersion=version,
            specSha256=event["specSha256"], inReplyTo=event["eventId"], savedSpecSha256=saved_digest,
        )
        return None

    def _refresh_review(self, key, review):
        checked, error = self._check_draft(key, strict_object(review["specJson"]))
        if error or "summary" not in checked[1]:
            self._publish(key, "desktop.error", code="SPEC_MISMATCH")
        else:
            self._publish_checked(key, checked, None)

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
