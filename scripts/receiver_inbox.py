"""Transactional local storage for received messages and their receipts."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from uuid import uuid4

from message_protocol import ProtocolError


MAX_INBOX = 256


class Inbox:
    def __init__(self, database: sqlite3.Connection, database_lock=None):
        self.database = database
        self.database_lock = database_lock or threading.RLock()
        database.row_factory = sqlite3.Row
        with database:
            database.execute("PRAGMA synchronous = FULL")
            database.execute("PRAGMA secure_delete = ON")
            database.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    device_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    message_type TEXT NOT NULL,
                    body TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    spec_fields TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    receipt_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status = 'received'),
                    PRIMARY KEY (device_id, message_id),
                    UNIQUE (device_id, project_id, sequence)
                )
            """)

    def get(self, device_id: str, message_id: str) -> sqlite3.Row | None:
        with self.database_lock:
            return self.database.execute(
                "SELECT * FROM messages WHERE device_id = ? AND message_id = ?", (device_id, message_id)
            ).fetchone()

    def save(self, request: dict, received_at: str, retry_cutoff: datetime) -> tuple[sqlite3.Row, bool]:
        device_id, message_id = request["deviceId"], request["messageId"]
        content = {
            "project_id": request["projectId"], "sequence": request["sequence"],
            "message_type": request["messageType"], "body": request["body"], "body_sha256": request["bodySha256"],
            "spec_fields": json.dumps(
                {key: request[key] for key in ("specVersion", "confirmation") if key in request}, sort_keys=True
            ),
        }
        with self.database_lock, self.database:
            self.database.execute("BEGIN IMMEDIATE")
            existing = self.get(device_id, message_id)
            if existing is not None:
                if datetime.fromisoformat(existing["created_at"]) < retry_cutoff:
                    raise ProtocolError("RETRY_WINDOW_EXPIRED")
                if any(existing[key] != value for key, value in content.items()):
                    raise ProtocolError("MESSAGE_ID_CONFLICT")
                return existing, False
            if self.database.execute(
                "SELECT 1 FROM messages WHERE device_id = ? AND project_id = ? AND sequence = ?",
                (device_id, content["project_id"], content["sequence"]),
            ).fetchone():
                raise ProtocolError("MESSAGE_SEQUENCE_CONFLICT")
            if self.database.execute("SELECT count(*) FROM messages").fetchone()[0] >= MAX_INBOX:
                raise ProtocolError("INTERNAL_ERROR")
            inserted = self.database.execute("""
                INSERT INTO messages (
                    device_id, message_id, project_id, sequence, message_type, body, body_sha256,
                    spec_fields, created_at, received_at, receipt_id, status
                ) VALUES (:device_id, :message_id, :project_id, :sequence, :message_type, :body, :body_sha256,
                          :spec_fields, :created_at, :received_at, :receipt_id, 'received')
                RETURNING *
            """, {
                **content, "device_id": device_id, "message_id": message_id, "created_at": request["createdAt"],
                "received_at": received_at, "receipt_id": str(uuid4()),
            }).fetchall()
            return inserted[0], True

    def list(self) -> list[dict]:
        with self.database_lock:
            return [dict(row) for row in self.database.execute("""
                SELECT message_id AS messageId, project_id AS projectId, received_at AS receivedAt, status, body
                FROM messages ORDER BY rowid
            """)]

    def dialogue_messages(self) -> list[sqlite3.Row]:
        with self.database_lock:
            return self.database.execute("""
                SELECT device_id, message_id, project_id, message_type, body_sha256
                FROM messages WHERE message_type = 'user.message' ORDER BY rowid
            """).fetchall()

    def clear(self) -> int:
        with self.database_lock, self.database:
            self.database.execute("BEGIN IMMEDIATE")
            return self.database.execute("DELETE FROM messages").rowcount
