package com.example.pluginhost

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/** App-private, plugin-ID-partitioned storage for records accepted by the manifest data model. */
internal class PluginDataStore(context: Context) : SQLiteOpenHelper(
    context.applicationContext,
    DATABASE_NAME,
    null,
    DATABASE_VERSION,
) {
    data class Page(val records: JSONArray, val nextCursor: String?)

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """CREATE TABLE plugin_records (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                record_id TEXT NOT NULL UNIQUE,
                record_json TEXT NOT NULL
            )""".trimIndent(),
        )
        db.execSQL("CREATE INDEX plugin_records_partition ON plugin_records(plugin_id, collection_id, seq)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // Database migrations must be explicit; this version has no prior production schema.
        error("Unsupported plugin data database upgrade: $oldVersion -> $newVersion")
    }

    fun insert(pluginId: String, collectionId: String, record: JSONObject): String {
        val bytes = record.toString().toByteArray(Charsets.UTF_8).size
        if (bytes > MAX_RECORD_BYTES) throw DataFailure("DATA_LIMIT_EXCEEDED")

        val db = writableDatabase
        db.beginTransaction()
        try {
            db.rawQuery(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(record_json AS BLOB))), 0) FROM plugin_records WHERE plugin_id = ?",
                arrayOf(pluginId),
            ).use { cursor ->
                check(cursor.moveToFirst())
                if (cursor.getLong(0) >= MAX_RECORDS_PER_PLUGIN) throw DataFailure("DATA_LIMIT_EXCEEDED")
                if (cursor.getLong(1) + bytes > MAX_BYTES_PER_PLUGIN) throw DataFailure("STORAGE_LIMIT_EXCEEDED")
            }

            val recordId = UUID.randomUUID().toString()
            val values = ContentValues().apply {
                put("plugin_id", pluginId)
                put("collection_id", collectionId)
                put("record_id", recordId)
                put("record_json", record.toString())
            }
            db.insertOrThrow("plugin_records", null, values)
            db.setTransactionSuccessful()
            return recordId
        } finally {
            db.endTransaction()
        }
    }

    fun read(pluginId: String, collectionId: String, after: Long, limit: Int): Page {
        val records = JSONArray()
        var nextCursor: String? = null
        readableDatabase.rawQuery(
            "SELECT seq, record_id, record_json FROM plugin_records WHERE plugin_id = ? AND collection_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            arrayOf(pluginId, collectionId, after.toString(), (limit + 1).toString()),
        ).use { cursor ->
            var count = 0
            var lastSequence = after
            var pageBytes = 0
            while (cursor.moveToNext()) {
                val value = cursor.getString(2)
                val item = JSONObject()
                    .put("recordId", cursor.getString(1))
                    .put("data", JSONObject(value))
                val itemBytes = item.toString().toByteArray(Charsets.UTF_8).size + if (count == 0) 0 else 1
                if (count == limit || pageBytes + itemBytes > MAX_READ_PAGE_BYTES) {
                    nextCursor = lastSequence.toString()
                    break
                }
                lastSequence = cursor.getLong(0)
                records.put(item)
                pageBytes += itemBytes
                count += 1
            }
        }
        return Page(records, nextCursor)
    }

    fun deletePluginData(pluginId: String) {
        writableDatabase.delete("plugin_records", "plugin_id = ?", arrayOf(pluginId))
    }

    class DataFailure(val code: String) : Exception(code)

    companion object {
        const val MAX_RECORD_BYTES = 32 * 1024
        const val MAX_RECORDS_PER_PLUGIN = 1000
        const val MAX_BYTES_PER_PLUGIN = 1024 * 1024
        const val MAX_READ_PAGE_SIZE = 100
        const val MAX_READ_PAGE_BYTES = 192 * 1024
        private const val DATABASE_NAME = "plugin_data.db"
        private const val DATABASE_VERSION = 1
    }
}
