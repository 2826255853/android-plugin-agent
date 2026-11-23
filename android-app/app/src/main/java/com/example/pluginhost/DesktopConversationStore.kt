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
    SQLiteOpenHelper(context, File(context.noBackupFilesDir, name).absolutePath, null, 2) {

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
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        check(oldVersion == 1 && newVersion == 2)
        createDialogueTable(db)
    }

    private fun createDialogueTable(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE dialogue_events (
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

    fun projects(): List<DesktopProject> = readableDatabase.rawQuery(
        "SELECT id, name, fingerprint FROM projects ORDER BY rowid DESC", null,
    ).use { cursor ->
        buildList {
            while (cursor.moveToNext()) add(DesktopProject(cursor.getString(0), cursor.getString(1), cursor.getString(2)))
        }
    }

    fun createProject(name: String, fingerprint: String): DesktopProject {
        val project = DesktopProject("project-${UUID.randomUUID()}", name, fingerprint)
        writableDatabase.insertOrThrow("projects", null, ContentValues().apply {
            put("id", project.id)
            put("name", project.name)
            put("fingerprint", project.fingerprint)
        })
        return project
    }

    fun enqueue(projectId: String, body: String) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val sequence = db.rawQuery(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE project_id = ?", arrayOf(projectId),
            ).use { check(it.moveToFirst()); it.getLong(0) }
            db.insertOrThrow("messages", null, ContentValues().apply {
                put("id", UUID.randomUUID().toString())
                put("project_id", projectId)
                put("sequence", sequence)
                put("body", body)
                put("created_at", System.currentTimeMillis())
                put("delivery", Delivery.QUEUED.name)
            })
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
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
