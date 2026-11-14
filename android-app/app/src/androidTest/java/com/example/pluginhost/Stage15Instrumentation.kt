package com.example.pluginhost

import android.app.Activity
import android.app.Instrumentation
import android.content.Context
import android.content.ContextWrapper
import android.database.DatabaseErrorHandler
import android.database.sqlite.SQLiteDatabase
import android.net.Uri
import android.os.Bundle
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/** Device tests of the production importer/SQLite store using only Android platform APIs. */
class Stage15Instrumentation : Instrumentation() {
    private var passed = 0
    private val failures = mutableListOf<String>()
    private val output = StringBuilder()
    private lateinit var sandbox: Sandbox

    override fun onCreate(arguments: Bundle?) {
        super.onCreate(arguments)
        start()
    }

    override fun onStart() {
        sandbox = Sandbox(targetContext)
        try {
            val matrix = JSONObject(context.assets.open("stage15/matrix.json").bufferedReader().use { it.readText() })
            val imports = matrix.getJSONObject("imports")
            for (name in imports.keys()) test(name) { verifyImport(name, imports.getString(name)) }
            val upgrades = matrix.getJSONObject("upgrades")
            for (name in upgrades.keys()) test(name) { verifyUpgrade(name, upgrades.getString(name)) }
            test("interrupted-install") { verifyRecovery() }
            test("record-byte-boundary") { verifyRecordBytes() }
            test("record-count-and-pagination") { verifyCountAndPages() }
            test("storage-byte-and-page-boundary") { verifyStorageBytes() }
            test("read-page-byte-at-and-over") { verifyPageBytes() }
        } catch (failure: Throwable) {
            failures += "runner: ${failure.javaClass.simpleName}: ${failure.message}"
        } finally {
            sandbox.cleanup()
        }
        output.append("\n${if (failures.isEmpty()) "STAGE15 PASSED" else "STAGE15 FAILED"}: $passed passed, ${failures.size} failed\n")
        failures.forEach { output.append("FAIL $it\n") }
        finish(if (failures.isEmpty()) Activity.RESULT_OK else Activity.RESULT_CANCELED,
            Bundle().apply { putString("stream", output.toString()) })
    }

    private fun test(name: String, action: () -> Unit) {
        try {
            sandbox.reset()
            action()
            passed++
            output.append("PASS $name\n")
        } catch (failure: Throwable) {
            failures += "$name: ${failure.javaClass.simpleName}: ${failure.message}"
        }
    }

    private fun fixture(name: String): File = File(sandbox.filesDir, "candidate.zip").also { file ->
        context.assets.open("stage15/$name").use { input -> file.outputStream().use { input.copyTo(it) } }
    }

    private fun install(store: LocalPluginStore, name: String) =
        store.install(store.inspect(sandbox, Uri.fromFile(fixture(name))))

    private fun fingerprint(): Map<String, String> {
        val root = File(sandbox.filesDir, "plugins")
        return root.walkTopDown().filter { it.isFile }.associate { file ->
            file.relativeTo(root).path to MessageDigest.getInstance("SHA-256")
                .digest(file.readBytes()).joinToString("") { "%02x".format(it) }
        }
    }

    private fun expectImport(code: String, action: () -> Unit) {
        val actual = try { action(); "ACCEPTED" } catch (failure: LocalPluginStore.ImportFailure) { failure.code }
        check(actual == code) { "expected $code, got $actual" }
    }

    private fun verifyImport(name: String, expected: String) {
        val store = LocalPluginStore(sandbox)
        if (expected != "ACCEPTED") install(store, "valid-minimal-v1.zip")
        val before = fingerprint()
        expectImport(expected) { install(store, name) }
        if (expected != "ACCEPTED") check(before == fingerprint()) { "rejected import changed installed files" }
        check(LocalPluginStore(sandbox).list().size == 1) { "installation missing or partial" }
        check(File(sandbox.filesDir, "plugins").listFiles().orEmpty().all { !it.name.startsWith('.') && !it.name.startsWith("incoming-") })
    }

    private fun verifyUpgrade(name: String, expected: String) {
        val store = LocalPluginStore(sandbox)
        install(store, "upgrade-base.zip")
        if (name == "upgrade-rollback.zip") install(store, "upgrade-optional.zip")
        val before = fingerprint()
        PluginDataStore(sandbox).use { data ->
            data.insert("org.example.quick-notes", "notes", JSONObject().put("title", "Fictional upgrade sample"))
            expectImport(expected) { install(store, name) }
            if (expected != "ACCEPTED") check(before == fingerprint()) { "failed update changed old package" }
            check(data.read("org.example.quick-notes", "notes", 0, 100).records.length() == 1)
            check(data.read("org.example.quick-notes", "notes", 0, 100).records.getJSONObject(0)
                .getJSONObject("data").getString("title") == "Fictional upgrade sample")
        }
    }

    private fun verifyRecovery() {
        val plugin = install(LocalPluginStore(sandbox), "valid-minimal-v1.zip")
        val backup = File(plugin.directory.parentFile, ".backup-${plugin.id}-12345")
        check(plugin.directory.renameTo(backup))
        File(backup.parentFile, ".stage-12345").mkdir()
        File(backup.parentFile, "incoming-12345.zip").writeText("fictional interrupted input")
        check(LocalPluginStore(sandbox).list().single().version == plugin.version)
        check(!backup.exists() && !File(backup.parentFile, ".stage-12345").exists())
        check(!File(backup.parentFile, "incoming-12345.zip").exists())
    }

    private fun record(bytes: Int): JSONObject = JSONObject().put("value", "x".repeat(bytes - 12)).also {
        check(it.toString().toByteArray().size == bytes)
    }

    private fun expectData(code: String, action: () -> Unit) {
        val actual = try { action(); "ACCEPTED" } catch (failure: PluginDataStore.DataFailure) { failure.code }
        check(actual == code) { "expected $code, got $actual" }
    }

    private fun verifyRecordBytes() = PluginDataStore(sandbox).use { data ->
        data.insert("record", "notes", record(PluginDataStore.MAX_RECORD_BYTES))
        expectData("DATA_LIMIT_EXCEEDED") { data.insert("record", "notes", record(PluginDataStore.MAX_RECORD_BYTES + 1)) }
        check(data.read("record", "notes", 0, 100).records.length() == 1)
    }

    private fun verifyCountAndPages() = PluginDataStore(sandbox).use { data ->
        repeat(1000) { data.insert("count", "notes", JSONObject().put("index", it)) }
        expectData("DATA_LIMIT_EXCEEDED") { data.insert("count", "notes", JSONObject().put("index", 1000)) }
        val seen = mutableSetOf<Int>()
        var after = 0L
        do {
            val page = data.read("count", "notes", after, 100)
            check(page.records.length() == 100)
            for (i in 0 until page.records.length()) check(seen.add(page.records.getJSONObject(i).getJSONObject("data").getInt("index")))
            if (page.nextCursor == null) break
            check(page.nextCursor.toLong() > after)
            after = page.nextCursor.toLong()
        } while (true)
        check(seen == (0 until 1000).toSet())
        check(data.read("other", "notes", 0, 100).records.length() == 0)
        check(data.read("count", "other", 0, 100).records.length() == 0)
    }

    private fun verifyStorageBytes() = PluginDataStore(sandbox).use { data ->
        // 32 * 32 KiB exactly fills the per-plugin 1 MiB budget.
        repeat(32) { data.insert("bytes", "notes", record(PluginDataStore.MAX_RECORD_BYTES)) }
        expectData("STORAGE_LIMIT_EXCEEDED") { data.insert("bytes", "notes", JSONObject()) }
        var after = 0L
        var count = 0
        do {
            val page = data.read("bytes", "notes", after, 100)
            check(page.records.length() in 1..100)
            check(page.records.toString().toByteArray().size <= PluginDataStore.MAX_READ_PAGE_BYTES + 2)
            count += page.records.length()
            if (page.nextCursor == null) break
            check(page.nextCursor.toLong() > after)
            after = page.nextCursor.toLong()
        } while (true)
        check(count == 32) { "lost or duplicated records across byte-limited pages" }
        data.insert("other", "notes", JSONObject())
        data.deletePluginData("bytes")
        check(data.read("bytes", "notes", 0, 100).records.length() == 0)
        check(data.read("other", "notes", 0, 100).records.length() == 1)
    }

    private fun verifyPageBytes() = PluginDataStore(sandbox).use { data ->
        val wrapperBytes = JSONObject().put("recordId", "x".repeat(36)).put("data", JSONObject()).toString().toByteArray().size - 2
        // Six records plus their wrappers and separators exactly reach the page payload limit.
        val payloadBudget = PluginDataStore.MAX_READ_PAGE_BYTES - 6 * wrapperBytes - 5
        val sizes = List(6) { payloadBudget / 6 + if (it < payloadBudget % 6) 1 else 0 }
        for (over in 0..1) {
            val id = "page-$over"
            sizes.forEachIndexed { index, bytes -> data.insert(id, "notes", record(bytes + if (index == 5) over else 0)) }
            val page = data.read(id, "notes", 0, 100)
            if (over == 0) {
                check(page.records.length() == 6 && page.nextCursor == null)
                check(page.records.toString().toByteArray().size == PluginDataStore.MAX_READ_PAGE_BYTES + 2)
            } else {
                check(page.records.length() == 5 && page.nextCursor != null)
                check(data.read(id, "notes", page.nextCursor.toLong(), 100).records.length() == 1)
            }
        }
    }

    /** Keep tests away from the user's installed packages, database and preferences. */
    private class Sandbox(base: Context) : ContextWrapper(base) {
        private val root = File(base.cacheDir, "stage15-${System.nanoTime()}").apply { mkdirs() }
        private val preferences = mutableSetOf<String>()
        override fun getApplicationContext(): Context = this
        override fun getFilesDir(): File = File(root, "files").apply { mkdirs() }
        override fun getDatabasePath(name: String): File = File(root, name)
        override fun getSharedPreferences(name: String, mode: Int) =
            super.getSharedPreferences("${root.name}-$name".also { preferences.add(it) }, mode)
        override fun openOrCreateDatabase(name: String, mode: Int, factory: SQLiteDatabase.CursorFactory?): SQLiteDatabase =
            SQLiteDatabase.openOrCreateDatabase(getDatabasePath(name), factory)
        override fun openOrCreateDatabase(name: String, mode: Int, factory: SQLiteDatabase.CursorFactory?, errorHandler: DatabaseErrorHandler?): SQLiteDatabase =
            SQLiteDatabase.openOrCreateDatabase(getDatabasePath(name).path, factory, errorHandler)
        fun reset() {
            root.listFiles().orEmpty().forEach { it.deleteRecursively() }
            preferences.forEach { super.getSharedPreferences(it, MODE_PRIVATE).edit().clear().commit() }
        }
        fun cleanup() { reset(); root.deleteRecursively() }
    }
}
