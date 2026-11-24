package com.example.pluginhost

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONObject
import java.io.File
import java.util.UUID

internal data class DesktopProject(val id: String, val name: String, val fingerprint: String)

internal enum class Delivery { QUEUED, RETRYABLE, RECEIVED, REJECTED }

internal data class QueuedMessage(
    val id: String,
    val projectId: String,
    val sequence: Int,
    val body: String,
    val createdAt: Long,
    val delivery: Delivery,
    val detail: String?,
    val receipt: DesktopReceipt?,
)

internal class DesktopConversationStore(context: Context, name: String = "desktop-conversations.db") :
    SQLiteOpenHelper(context, File(context.noBackupFilesDir, name).absolutePath, null, 3) {

    override fun onConfigure(db: SQLiteDatabase) {
        db.setForeignKeyConstraintsEnabled(true)
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                fingerprint TEXT NOT NULL
            )
            """.trimIndent(),
        )
        db.execSQL(
            """
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 2147483647),
                body TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                delivery TEXT NOT NULL,
                detail TEXT,
                retry_at INTEGER NOT NULL DEFAULT 0,
                receipt_id TEXT,
                received_at TEXT,
                UNIQUE (project_id, sequence)
            )
            """.trimIndent(),
        )
        createDialogueTable(db)
        createDialogueSubmissionsTable(db)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) createDialogueTable(db)
        if (oldVersion < 3) createDialogueSubmissionsTable(db)
    }

    private fun createDialogueTable(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS dialogue_events (
                session_id TEXT NOT NULL REFERENCES messages(id),
                sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 2147483647),
                event_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (session_id, sequence),
                UNIQUE (session_id, event_id)
            )
            """.trimIndent(),
        )
    }

    private fun createDialogueSubmissionsTable(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS dialogue_submissions (
                session_id TEXT PRIMARY KEY REFERENCES messages(id),
                sequence INTEGER NOT NULL DEFAULT 0 CHECK (sequence BETWEEN 0 AND 2147483647),
                pending_payload TEXT
            )
            """.trimIndent(),
        )
    }

    fun queueDialogueSubmission(message: QueuedMessage, type: String, head: JSONObject, text: String?): JSONObject {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val current = db.rawQuery(
                "SELECT sequence, pending_payload FROM dialogue_submissions WHERE session_id = ?",
                arrayOf(message.id),
            ).use { if (it.moveToFirst()) it.getInt(0) to (if (it.isNull(1)) null else it.getString(1)) else 0 to null }
            current.second?.let { return JSONObject(it) }
            val sequence = current.first + 1
            if (sequence > Int.MAX_VALUE) throw DesktopFailure("EVENT_SEQUENCE_CONFLICT")
            val event = JSONObject()
                .put("dialogueVersion", 1)
                .put("deviceId", head.getString("deviceId"))
                .put("projectId", message.projectId)
                .put("sessionId", message.id)
                .put("type", type)
                .put("eventId", UUID.randomUUID().toString())
                .put("sequence", sequence)
                .put("createdAt", messageTimestamp())
                .put("messageId", message.id)
                .put("inReplyTo", head.getString("eventId"))
            if (type != "phone.cancel") {
                event.put("specVersion", head.getInt("specVersion"))
                    .put("specSha256", head.getString("specSha256"))
                    .put("text", text?.trim().orEmpty())
            }
            DialogueProtocol.parse(event.toString())
            db.insertWithOnConflict("dialogue_submissions", null, ContentValues().apply {
                put("session_id", message.id)
                put("sequence", sequence)
                put("pending_payload", event.toString())
            }, SQLiteDatabase.CONFLICT_REPLACE)
            db.setTransactionSuccessful()
            return event
        } finally {
            db.endTransaction()
        }
    }

    fun pendingDialogueSubmission(sessionId: String): JSONObject? = readableDatabase.rawQuery(
        "SELECT pending_payload FROM dialogue_submissions WHERE session_id = ? AND pending_payload IS NOT NULL",
        arrayOf(sessionId),
    ).use { if (it.moveToFirst()) JSONObject(it.getString(0)) else null }

    fun acceptDialogueSubmission(sessionId: String, eventId: String) {
        val db = writableDatabase
        val pending = pendingDialogueSubmission(sessionId) ?: return
        if (pending.optString("eventId") != eventId) return
        db.execSQL("UPDATE dialogue_submissions SET pending_payload = NULL WHERE session_id = ? AND pending_payload = ?",
            arrayOf(sessionId, pending.toString()))
    }

    fun discardDialogueSubmission(sessionId: String, eventId: String) {
        val db = writableDatabase
        val pending = pendingDialogueSubmission(sessionId) ?: return
        if (pending.optString("eventId") != eventId) return
        db.execSQL(
            "UPDATE dialogue_submissions SET sequence = sequence - 1, pending_payload = NULL " +
                "WHERE session_id = ? AND sequence = ? AND pending_payload = ?",
            arrayOf(sessionId, pending.optInt("sequence"), pending.toString()),
        )
    }

    fun projects(): List<DesktopProject> = readableDatabase.rawQuery(
        "SELECT id, name, fingerprint FROM projects ORDER BY rowid DESC", null,
    ).use { cursor ->
        buildList {
            while (cursor.moveToNext()) add(DesktopProject(cursor.getString(0), cursor.getString(1), cursor.getString(2)))
        }
    }

    fun createProject(name: String, fingerprint: String, firstMessage: String? = null): DesktopProject {
        val project = DesktopProject("project-${UUID.randomUUID()}", name, fingerprint)
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.insertOrThrow("projects", null, ContentValues().apply {
                put("id", project.id)
                put("name", project.name)
                put("fingerprint", project.fingerprint)
            })
            firstMessage?.let { insertMessage(db, project.id, 1, it) }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
        return project
    }

    fun enqueue(projectId: String, body: String) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val sequence = db.rawQuery(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE project_id = ?", arrayOf(projectId),
            ).use { check(it.moveToFirst()); it.getLong(0) }
            insertMessage(db, projectId, sequence, body)
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    private fun insertMessage(db: SQLiteDatabase, projectId: String, sequence: Long, body: String) {
        db.insertOrThrow("messages", null, ContentValues().apply {
            put("id", UUID.randomUUID().toString())
            put("project_id", projectId)
            put("sequence", sequence)
            put("body", body)
            put("created_at", System.currentTimeMillis())
            put("delivery", Delivery.QUEUED.name)
        })
    }

    fun messages(projectId: String): List<QueuedMessage> = readableDatabase.rawQuery(
        "SELECT $MESSAGE_COLUMNS FROM messages WHERE project_id = ? ORDER BY sequence", arrayOf(projectId),
    ).use { cursor ->
        buildList { while (cursor.moveToNext()) add(cursor.message()) }
    }

    fun dialogueHeads(projectId: String): Map<String, JSONObject> = readableDatabase.rawQuery(
        """
        SELECT e.session_id, e.payload
        FROM dialogue_events e JOIN messages m ON m.id = e.session_id
        WHERE m.project_id = ? AND e.sequence = (
            SELECT MAX(sequence) FROM dialogue_events WHERE session_id = e.session_id
        )
        """.trimIndent(), arrayOf(projectId),
    ).use { cursor ->
        buildMap {
            while (cursor.moveToNext()) put(cursor.getString(0), DialogueProtocol.parse(cursor.getString(1)))
        }
    }

    fun applyDialoguePage(message: QueuedMessage, page: JSONObject): Int {
        val db = writableDatabase
        db.beginTransaction()
        try {
            var through = db.rawQuery(
                "SELECT COALESCE(MAX(sequence), 0) FROM dialogue_events WHERE session_id = ?", arrayOf(message.id),
            ).use { check(it.moveToFirst()); it.getInt(0) }
            if (page.getString("sessionId") != message.id || page.getString("projectId") != message.projectId ||
                page.getInt("after") > through
            ) throw DesktopFailure("DESKTOP_RESPONSE")
            val events = page.getJSONArray("events")
            for (index in 0 until events.length()) {
                val event = events.getJSONObject(index)
                if (event.getString("messageId") != message.id) throw DesktopFailure("DESKTOP_RESPONSE")
                val sequence = event.getInt("sequence")
                val payload = event.toString()
                if (sequence <= through) {
                    val previous = db.rawQuery(
                        "SELECT payload FROM dialogue_events WHERE session_id = ? AND sequence = ?",
                        arrayOf(message.id, sequence.toString()),
                    ).use { if (it.moveToFirst()) JSONObject(it.getString(0)) else null }
                    val keys = event.keys().asSequence().toSet()
                    if (previous == null || previous.keys().asSequence().toSet() != keys ||
                        keys.any { previous.opt(it) != event.opt(it) }
                    ) throw DesktopFailure("DESKTOP_RESPONSE")
                    continue
                }
                if (sequence != through + 1) throw DesktopFailure("DESKTOP_RESPONSE")
                val inserted = db.insertWithOnConflict("dialogue_events", null, ContentValues().apply {
                    put("session_id", message.id)
                    put("sequence", sequence)
                    put("event_id", event.getString("eventId"))
                    put("payload", payload)
                }, SQLiteDatabase.CONFLICT_IGNORE)
                if (inserted == -1L) throw DesktopFailure("DESKTOP_RESPONSE")
                through = sequence
            }
            db.setTransactionSuccessful()
            return through
        } finally {
            db.endTransaction()
        }
    }

    fun next(fingerprint: String, includeRetries: Boolean): QueuedMessage? {
        val condition = if (includeRetries) "delivery IN ('QUEUED', 'RETRYABLE')" else "delivery = 'QUEUED'"
        return readableDatabase.rawQuery(
            """
            SELECT $MESSAGE_COLUMNS FROM messages
            WHERE project_id IN (SELECT id FROM projects WHERE fingerprint = ?)
            AND $condition AND retry_at <= ?
            ORDER BY rowid LIMIT 1
            """.trimIndent(),
            arrayOf(fingerprint, System.currentTimeMillis().toString()),
        ).use { if (it.moveToFirst()) it.message() else null }
    }

    fun received(id: String, receipt: DesktopReceipt) {
        update(id, ContentValues().apply {
            put("delivery", Delivery.RECEIVED.name)
            put("receipt_id", receipt.id)
            put("received_at", receipt.receivedAt)
            putNull("detail")
            put("retry_at", 0)
        })
    }

    fun failed(id: String, failure: DesktopFailure, retryable: Boolean) {
        update(id, ContentValues().apply {
            put("delivery", if (retryable) Delivery.RETRYABLE.name else Delivery.REJECTED.name)
            put("detail", failure.code)
            put("retry_at", System.currentTimeMillis() + failure.retryAfterSeconds * 1000L)
        })
    }

    private fun update(id: String, values: ContentValues) {
        check(writableDatabase.update("messages", values, "id = ? AND delivery != 'RECEIVED'", arrayOf(id)) == 1)
    }

    private fun Cursor.message() = QueuedMessage(
        getString(0), getString(1), getInt(2), getString(3), getLong(4), Delivery.valueOf(getString(5)), getString(6),
        getString(7)?.let { DesktopReceipt(it, getString(8)) },
    )

    private companion object {
        const val MESSAGE_COLUMNS = "id, project_id, sequence, body, created_at, delivery, detail, receipt_id, received_at"
    }
}
