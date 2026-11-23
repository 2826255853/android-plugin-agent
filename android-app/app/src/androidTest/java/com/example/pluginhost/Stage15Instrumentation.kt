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
import java.security.KeyStore
import java.util.UUID

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
            test("rollback-retains-data-and-rejects-stale-update") { verifyRollback() }
            test("rollback-rejects-damaged-snapshot") { verifyRollbackRejection("upgrade-same.zip", damage = true) }
            test("rollback-unavailable-after-compatible-schema-change") { verifyRollbackRejection("upgrade-optional.zip") }
            test("record-byte-boundary") { verifyRecordBytes() }
            test("record-count-and-pagination") { verifyCountAndPages() }
            test("storage-byte-and-page-boundary") { verifyStorageBytes() }
            test("read-page-byte-at-and-over") { verifyPageBytes() }
            test("message-protocol-fixtures") { verifyMessageProtocolFixtures() }
            test("dialogue-protocol-fixtures") { verifyDialogueProtocolFixtures(assetText("fixtures/stage-b1.json")) }
            test("desktop-conversation-outbox") { verifyDesktopOutbox() }
            test("desktop-dialogue-cursor-and-upgrade") { verifyDesktopDialogueCursor() }
            test("desktop-pairing-vault") { verifyDesktopPairingVault() }
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

    private fun verifyMessageProtocolFixtures() {
        val valid = listOf(
            "valid-pair-request.json",
            "valid-pair-confirm.json",
            "valid-pair-rejected.json",
            "valid-pair-revoke.json",
            "valid-pair-revoked.json",
            "valid-message-send.json",
            "valid-message-receipt.json",
            "valid-status-query.json",
            "valid-status-response.json",
            "valid-error.json",
        )
        valid.forEach { name -> MessageProtocol.parse(assetText("fixtures/$name")) }
        val invalid = mapOf(
            "invalid-unknown-field.json" to "INVALID_REQUEST",
            "invalid-duplicate-json-key.json" to "INVALID_REQUEST",
            "invalid-project-id.json" to "INVALID_REQUEST",
            "invalid-message-hash.json" to "INVALID_REQUEST",
            "invalid-empty-body.json" to "INVALID_REQUEST",
            "invalid-pair-code.json" to "INVALID_REQUEST",
        )
        invalid.forEach { (name, expected) ->
            try {
                MessageProtocol.parse(assetText("fixtures/$name"))
                error("fixture accepted: $name")
            } catch (failure: MessageProtocol.ProtocolException) {
                check(failure.code == expected) { "$name returned ${failure.code}" }
            }
        }
        val tooLong = JSONObject(assetText("fixtures/valid-message-send.json"))
            .put("body", "x".repeat(MessageProtocol.MAX_BODY_CHARS + 1))
            .put("bodySha256", "0".repeat(64))
        try {
            MessageProtocol.parse(tooLong.toString())
            error("oversized body accepted")
        } catch (failure: MessageProtocol.ProtocolException) {
            check(failure.code == "INVALID_REQUEST") { "oversized body returned ${failure.code}" }
        }
    }

    private fun assetText(path: String): String = context.assets.open(path).bufferedReader().use { it.readText() }

    private fun verifyDesktopOutbox() {
        val fingerprint = "a".repeat(64)
        val body = "虚构需求：保存便签。"
        lateinit var project: DesktopProject
        lateinit var first: QueuedMessage
        DesktopConversationStore(sandbox).use { store ->
            project = store.createProject("测试项目", fingerprint)
            check(Regex("[a-z][a-z0-9-]{2,63}").matches(project.id))
            store.enqueue(project.id, body)
            first = checkNotNull(store.next(fingerprint, includeRetries = false))
            check(first.body == body && first.sequence == 1 && first.delivery == Delivery.QUEUED)
            check(store.next("b".repeat(64), includeRetries = true) == null)
            store.failed(first.id, DesktopFailure("DESKTOP_UNREACHABLE", retryable = true), retryable = true)
        }
        DesktopConversationStore(sandbox).use { store ->
            check(store.projects().single() == project)
            check(store.next(fingerprint, includeRetries = false) == null)
            val retry = checkNotNull(store.next(fingerprint, includeRetries = true))
            check(retry == first.copy(delivery = Delivery.RETRYABLE, detail = "DESKTOP_UNREACHABLE"))
            val receipt = DesktopReceipt(UUID.randomUUID().toString(), messageTimestamp())
            store.received(retry.id, receipt)
            check(store.next(fingerprint, includeRetries = true) == null)
            check(store.messages(project.id).single() == first.copy(delivery = Delivery.RECEIVED, receipt = receipt))
            store.enqueue(project.id, "第二条虚构需求。")
            val second = checkNotNull(store.next(fingerprint, includeRetries = true))
            check(second.sequence == 2 && second.id != first.id)
            store.failed(second.id, DesktopFailure("RATE_LIMITED", retryable = true, retryAfterSeconds = 60), retryable = true)
            check(store.next(fingerprint, includeRetries = true) == null)
        }
    }

    private fun verifyDesktopDialogueCursor() {
        val before = DesktopConversationStore(sandbox).use { store ->
            val project = store.createProject("Dialogue fixture", "a".repeat(64))
            store.enqueue(project.id, "Fictional requirement")
            val message = store.messages(project.id).single()
            store.received(message.id, DesktopReceipt(UUID.randomUUID().toString(), messageTimestamp()))
            // Prepare an outbox with the v1 tables and a persisted receipt.
            store.writableDatabase.execSQL("DROP TABLE dialogue_events")
            store.writableDatabase.version = 1
            store.messages(project.id).single()
        }
        val fixture = JSONObject(assetText("fixtures/stage-b1.json")).getJSONObject("samples")
        val status = fixture.getJSONObject("status").put("projectId", before.projectId)
            .put("sessionId", before.id).put("messageId", before.id).put("state", "queued")
        val running = JSONObject(status.toString()).put("sequence", 2).put("state", "running")
            .put("eventId", UUID.randomUUID().toString())
        val first = fixture.getJSONObject("page").put("projectId", before.projectId).put("sessionId", before.id)
            .put("after", 0).put("next", 1).put("events", org.json.JSONArray().put(status))
        val second = JSONObject(first.toString()).put("after", 1).put("next", 2)
            .put("hasMore", false).put("events", org.json.JSONArray().put(running))
        val pages = listOf(first, second).map { DialogueProtocol.parse(it.toString()) }

        DesktopConversationStore(sandbox).use { store ->
            check(store.messages(before.projectId).single() == before)
            try {
                store.applyDialoguePage(before, pages[1])
                error("Out-of-order page accepted")
            } catch (failure: DesktopFailure) {
                check(failure.code == "DESKTOP_RESPONSE")
            }
            check(store.dialogueHeads(before.projectId).isEmpty())
            check(store.applyDialoguePage(before, pages[0]) == 1)
            check(store.applyDialoguePage(before, pages[0]) == 1)
        }
        DesktopConversationStore(sandbox).use { store ->
            check(store.dialogueHeads(before.projectId).getValue(before.id).getInt("sequence") == 1)
            check(store.applyDialoguePage(before, pages[1]) == 2)
            check(store.applyDialoguePage(before, pages[0]) == 2)
            check(store.dialogueHeads(before.projectId).getValue(before.id).getString("state") == "running")
        }
    }

    private fun verifyDesktopPairingVault() {
        val namespace = "test-desktop-${UUID.randomUUID()}"
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        try {
            val vault = DesktopPairingStore(sandbox, namespace)
            check(vault.read() == null)
            val device = vault.device()
            val endpoint = DesktopEndpoint.parse("https://10.0.2.2:8443", "A".repeat(64))
            check(endpoint.fingerprint == "a".repeat(64))
            listOf("http://10.0.2.2:8443", "https://8.8.8.8:8443", "https://10.0.2.2:8443/path").forEach {
                check(runCatching { DesktopEndpoint.parse(it, endpoint.fingerprint) }.exceptionOrNull() is DesktopFailure)
            }
            val link = DesktopLink(endpoint)
            val credential = DesktopCredential(UUID.randomUUID().toString(), "fixture-token-" + "x".repeat(32), messageTimestamp())
            vault.save(DesktopPairing(link, credential))
            val stored = File(sandbox.noBackupFilesDir, namespace).readBytes().toString(Charsets.ISO_8859_1)
            check(credential.token !in stored && credential.id !in stored)
            val reopened = DesktopPairingStore(sandbox, namespace)
            check(reopened.device() == device)
            val paired = checkNotNull(reopened.read())
            check(paired.link == link)
            check(paired.credential?.token == credential.token && paired.credential.id == credential.id)
            val blocked = link.copy(blockedReason = "DEVICE_REVOKED")
            reopened.save(DesktopPairing(blocked, null))
            val revoked = checkNotNull(vault.read())
            check(revoked.link == blocked && revoked.credential == null)
        } finally {
            keyStore.deleteEntry(namespace)
            keyStore.deleteEntry("$namespace-device")
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

    private fun verifyRollback() {
        val store = LocalPluginStore(sandbox)
        val baseline = install(store, "upgrade-base.zip")
        val original = fingerprint()
        val stale = store.inspect(sandbox, Uri.fromFile(fixture("upgrade-optional.zip")))
        val upgraded = install(store, "upgrade-same.zip")
        expectImport("INSTALLATION_CHANGED") { store.install(stale) }
        check(upgraded.rollbackVersion == baseline.version)
        val backup = File(upgraded.directory.parentFile, ".backup-${upgraded.id}-12345")
        check(upgraded.directory.renameTo(backup))  // Interrupted switch before publishing a rollback.
        val reopened = LocalPluginStore(sandbox)
        val current = reopened.list().single()
        check(current.version == upgraded.version && current.rollbackVersion == baseline.version)
        PluginDataStore(sandbox).use { data ->
            data.insert(current.id, "notes", JSONObject().put("title", "Fictional note saved after update"))
            val restored = reopened.rollback(current)
            check(restored.version == baseline.version && restored.rollbackVersion == null)
            check(original == fingerprint())
            check(data.read(current.id, "notes", 0, 100).records.length() == 1)
        }
    }

    private fun verifyRollbackRejection(upgrade: String, damage: Boolean = false) {
        val store = LocalPluginStore(sandbox)
        install(store, "upgrade-base.zip")
        val upgraded = install(store, upgrade)
        if (damage) {
            check(upgraded.rollbackVersion != null)
            File(upgraded.directory, ".previous/${upgraded.entry}").writeText("Fictional damaged snapshot")
        }
        val before = fingerprint()
        expectImport("ROLLBACK_UNAVAILABLE") { store.rollback(upgraded) }
        check(store.list().single().rollbackVersion == null)
        check(before == fingerprint())
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
        override fun getNoBackupFilesDir(): File = File(root, "no_backup").apply { mkdirs() }
        override fun getDatabasePath(name: String): File = File(name).let { if (it.isAbsolute) it else File(root, name) }
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
