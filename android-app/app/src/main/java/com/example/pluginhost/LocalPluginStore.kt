package com.example.pluginhost

import android.content.Context
import android.net.Uri
import android.util.JsonReader
import android.util.JsonToken
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile
import java.io.StringReader
import java.security.MessageDigest
import java.util.Locale
import java.util.zip.CRC32
import java.util.zip.ZipFile

/** Validates complete packages before writing any content beneath the private plugin store. */
internal class LocalPluginStore(context: Context) {
    private val root = File(context.filesDir, "plugins").apply { mkdirs() }
    private val prefs = context.getSharedPreferences("installed_plugins", Context.MODE_PRIVATE)

    init { recoverInterruptedInstall() }

    data class Installed(
        val id: String,
        val name: String,
        val version: String,
        val directory: File,
        val entry: String,
        val resources: List<String>,
        val capabilities: Set<String>,
        val dataModel: JSONObject,
        val rollbackVersion: String? = null,
    )
    class ImportFailure(val code: String) : Exception(code)

    @Synchronized
    fun list(): List<Installed> {
        val records = root.listFiles().orEmpty().filter { it.isDirectory && isPluginId(it.name) }
            .mapNotNull { readInstalled(it)?.withRollbackVersion() }.sortedBy { it.id }
        val json = indexJson(records)
        if (prefs.getString("items", "[]") != json) prefs.edit().putString("items", json).commit()
        return records
    }

    @Synchronized
    fun inspect(context: Context, uri: Uri): Installed {
        val incoming = File(root, "incoming-${System.nanoTime()}.zip")
        try {
            context.contentResolver.openInputStream(uri)?.use { input ->
                FileOutputStream(incoming).use { output ->
                    val buffer = ByteArray(16 * 1024)
                    var total = 0L
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        total += count
                        if (total > MAX_ZIP_BYTES) fail("PACKAGE_LIMIT_EXCEEDED")
                        output.write(buffer, 0, count)
                    }
                }
            } ?: fail("PACKAGE_CORRUPT")

            val parsed = validate(incoming)
            val previous = list().firstOrNull { it.id == parsed.id }
            if (previous != null) {
                if (compareVersions(parsed.version, previous.version) <= 0) fail("PLUGIN_VERSION_NOT_NEWER")
                if (!compatibleModel(previous, parsed.model)) fail("DATA_MODEL_INCOMPATIBLE")
            }
            // Validation only. The caller must obtain explicit update confirmation before install().
            return Installed(
                parsed.id, parsed.name, parsed.version, incoming, parsed.entry, parsed.resources,
                parsed.capabilities, parsed.model,
            ).also {
                incoming.deleteOnExit()
                pending[incoming.name] = PendingImport(parsed, previous?.version)
            }
        } catch (failure: ImportFailure) {
            incoming.delete()
            throw failure
        } catch (_: Exception) {
            incoming.delete()
            throw ImportFailure("PACKAGE_CORRUPT")
        }
    }

    private data class PendingImport(val pkg: Package, val expectedVersion: String?)
    private val pending = mutableMapOf<String, PendingImport>()

    @Synchronized
    fun install(candidate: Installed): Installed {
        val inspected = pending.remove(candidate.directory.name) ?: fail("PACKAGE_CORRUPT")
        val pkg = inspected.pkg
        try {
            val records = list()
            val current = records.firstOrNull { it.id == pkg.id }
            if (current?.version != inspected.expectedVersion) fail("INSTALLATION_CHANGED")
            if (current != null) {
                if (compareVersions(pkg.version, current.version) <= 0) fail("PLUGIN_VERSION_NOT_NEWER")
                if (!compatibleModel(current, pkg.model)) fail("DATA_MODEL_INCOMPATIBLE")
            }
            return switchInstall(pkg.id, current, records) { stage ->
                ZipFile(candidate.directory).use { zip ->
                    for (entry in pkg.entries) {
                        val out = File(stage, entry)
                        if (!out.canonicalPath.startsWith(stage.canonicalPath + File.separator)) fail("PACKAGE_ENTRY_INVALID")
                        out.parentFile?.mkdirs()
                        zip.getInputStream(zip.getEntry(entry)).use { input ->
                            FileOutputStream(out).use { output -> input.copyTo(output) }
                        }
                    }
                }
                if (current != null && canRollbackModel(current.dataModel, pkg.model)) {
                    copyResources(current, File(stage, PREVIOUS))
                }
            }
        } finally {
            candidate.directory.delete()
        }
    }

    @Synchronized
    fun rollback(expected: Installed): Installed {
        val records = list()
        val current = records.firstOrNull { it.id == expected.id && it.version == expected.version }
            ?: fail("INSTALLATION_CHANGED")
        val previous = readPrevious(current) ?: fail("ROLLBACK_UNAVAILABLE")
        if (previous.version != expected.rollbackVersion) fail("INSTALLATION_CHANGED")
        return switchInstall(current.id, current, records) { copyResources(previous, it) }
    }

    fun canRollbackModel(previous: JSONObject, current: JSONObject): Boolean =
        previous.getString("version").substringBefore('.') == current.getString("version").substringBefore('.') &&
            canonicalModelValue(previous.getJSONArray("collections")) == canonicalModelValue(current.getJSONArray("collections"))

    private fun readPrevious(current: Installed): Installed? =
        readInstalled(File(current.directory, PREVIOUS), current.id)?.takeIf {
            compareVersions(it.version, current.version) < 0 && canRollbackModel(it.dataModel, current.dataModel)
        }

    private fun Installed.withRollbackVersion() = copy(rollbackVersion = readPrevious(this)?.version)

    private fun copyResources(source: Installed, destination: File) {
        for (path in source.resources + "manifest.json") {
            File(source.directory, path).copyTo(File(destination, path))
        }
    }

    private fun indexJson(records: List<Installed>) = JSONArray().apply {
        records.sortedBy { it.id }.forEach {
            put(JSONObject().put("id", it.id).put("name", it.name).put("version", it.version))
        }
    }.toString()

    private fun switchInstall(id: String, current: Installed?, records: List<Installed>, prepare: (File) -> Unit): Installed {
        val stage = File(root, ".stage-${System.nanoTime()}")
        val target = File(root, id)
        val backup = File(root, ".backup-$id-${System.nanoTime()}")
        var switched = false
        try {
            if (!stage.mkdir()) fail("PACKAGE_CORRUPT")
            prepare(stage)
            val staged = readInstalled(stage, id)?.withRollbackVersion() ?: fail("PACKAGE_CORRUPT")
            if (File(stage, PREVIOUS).exists() && staged.rollbackVersion == null) fail("PACKAGE_CORRUPT")
            if (current != null) {
                if (!target.renameTo(backup)) fail("PACKAGE_CORRUPT")
            } else if (target.exists()) {
                fail("PACKAGE_CORRUPT")
            }
            if (!stage.renameTo(target)) fail("PACKAGE_CORRUPT")
            switched = true
            val installed = staged.copy(directory = target)
            val json = indexJson(records.filterNot { it.id == id } + installed)
            if (!prefs.edit().putString("items", json).commit()) fail("PACKAGE_CORRUPT")
            backup.deleteRecursively()
            return installed
        } catch (e: Exception) {
            if (switched && !target.renameTo(stage)) target.deleteRecursively()
            if (backup.exists() && !target.exists()) backup.renameTo(target)
            throw e as? ImportFailure ?: ImportFailure("PACKAGE_CORRUPT")
        } finally {
            stage.deleteRecursively()
        }
    }

    @Synchronized
    fun discard(candidate: Installed) {
        pending.remove(candidate.directory.name)
        candidate.directory.delete()
    }

    @Synchronized
    fun remove(id: String): Boolean {
        if (!isPluginId(id)) return false
        val installed = list().firstOrNull { it.id == id } ?: return false
        val removed = File(root, ".removed-$id-${System.nanoTime()}")
        if (!installed.directory.renameTo(removed)) return false
        val remaining = list().filterNot { it.id == id }
        val json = JSONArray().apply {
            remaining.forEach { put(JSONObject().put("id", it.id).put("name", it.name).put("version", it.version)) }
        }
        if (!prefs.edit().putString("items", json.toString()).commit()) {
            removed.renameTo(installed.directory)
            return false
        }
        removed.deleteRecursively()
        return true
    }

    private fun readInstalled(directory: File, expectedId: String = directory.name): Installed? = runCatching {
        val manifestFile = File(directory, "manifest.json")
        require(manifestFile.isFile && manifestFile.length() <= MAX_MANIFEST_BYTES)
        val manifest = parseStrictManifest(manifestFile.readBytes())
        require(manifest.optInt("manifestVersion") == 1)
        validateManifest(manifest)
        val id = manifest.getString("id")
        require(id == expectedId)
        val entry = manifest.getString("entry")
        require(validPath(entry) && entry.endsWith(".html"))
        val capsArray = manifest.getJSONArray("capabilities")
        val capabilities = (0 until capsArray.length()).map { capsArray.getString(it) }.toSet()
        require(capabilities.size == capsArray.length() && capabilities.all { it in KNOWN_CAPABILITIES })
        val dataModel = manifest.getJSONObject("dataModel")
        require(dataModel.optJSONArray("collections") != null)
        val declared = linkedMapOf<String, String>()
        val files = manifest.getJSONArray("files")
        for (index in 0 until files.length()) {
            val item = files.getJSONObject(index)
            val path = item.getString("path")
            val hash = item.getString("sha256")
            require(validPath(path) && path != "manifest.json" && Regex("[a-f0-9]{64}").matches(hash))
            require(declared.put(path, hash) == null)
        }
        require(entry in declared && declared.isNotEmpty())
        val rootPath = directory.canonicalFile.path + File.separator
        for ((path, expectedHash) in declared) {
            val resource = File(directory, path).canonicalFile
            require(resource.path.startsWith(rootPath) && resource.isFile)
            val digest = MessageDigest.getInstance("SHA-256")
            resource.inputStream().use { input ->
                val buffer = ByteArray(16 * 1024)
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    digest.update(buffer, 0, count)
                }
            }
            require(digest.digest().joinToString("") { "%02x".format(it) } == expectedHash)
        }
        Installed(
            id, manifest.getString("name"), manifest.getString("version"), directory, entry,
            declared.keys.toList(), capabilities, dataModel,
        )
    }.getOrNull()

    private data class Package(
        val id: String, val name: String, val version: String, val model: JSONObject,
        val entries: List<String>, val entry: String, val resources: List<String>, val capabilities: Set<String>,
    )

    private fun validate(file: File): Package {
        val centralDirectory = inspectCentralDirectory(file)
        val names = linkedMapOf<String, java.util.zip.ZipEntry>()
        var total = 0L
        ZipFile(file).use { zip ->
            val iterator = zip.entries()
            while (iterator.hasMoreElements()) {
                val entry = iterator.nextElement()
                if (entry.isDirectory || !validPath(entry.name)) fail("PACKAGE_ENTRY_INVALID")
                val metadata = centralDirectory[entry.name] ?: fail("PACKAGE_CORRUPT")
                if (metadata.encrypted || metadata.disk != 0 || (metadata.unixType != 0 && metadata.unixType != UNIX_REGULAR)) fail("PACKAGE_ENTRY_INVALID")
                if (metadata.method != entry.method) fail("PACKAGE_CORRUPT")
                val folded = entry.name.lowercase(Locale.ROOT)
                if (names.keys.any { it.lowercase(Locale.ROOT) == folded }) fail("PACKAGE_DUPLICATE_ENTRY")
                if (entry.method != 0 && entry.method != 8) fail("PACKAGE_ENTRY_INVALID")
                if (entry.size < 0 || entry.compressedSize < 0) fail("PACKAGE_CORRUPT")
                if (entry.size > MAX_RESOURCE_BYTES || (entry.name == "manifest.json" && entry.size > MAX_MANIFEST_BYTES)) fail("PACKAGE_LIMIT_EXCEEDED")
                if (entry.size > 0 && (entry.compressedSize == 0L || entry.size > entry.compressedSize * MAX_RATIO)) fail("PACKAGE_LIMIT_EXCEEDED")
                total += entry.size
                if (total > MAX_TOTAL_BYTES || names.size >= MAX_ENTRIES) fail("PACKAGE_LIMIT_EXCEEDED")
                // ZipFile streams do not guarantee CRC validation; compare it explicitly.
                var actual = 0L
                val crc = CRC32()
                zip.getInputStream(entry).use { input ->
                    val buffer = ByteArray(16 * 1024)
                    while (true) {
                        val n = input.read(buffer)
                        if (n < 0) break
                        actual += n
                        crc.update(buffer, 0, n)
                        if (actual > MAX_RESOURCE_BYTES || (entry.name == "manifest.json" && actual > MAX_MANIFEST_BYTES)) fail("PACKAGE_LIMIT_EXCEEDED")
                    }
                }
                if (actual != entry.size || crc.value != entry.crc) fail("PACKAGE_CORRUPT")
                names[entry.name] = entry
            }
        }
        val manifestEntry = names["manifest.json"] ?: fail("MANIFEST_MISSING")
        val raw = ZipFile(file).use { zip -> zip.getInputStream(manifestEntry).use { it.readBytes() } }
        val manifest = parseStrictManifest(raw)
        val manifestVersion = manifest.opt("manifestVersion")
        if (manifestVersion !is Int && manifestVersion !is Long) fail("MANIFEST_INVALID")
        if (manifest.optInt("manifestVersion", -1) != 1) fail("MANIFEST_VERSION_UNSUPPORTED")
        validateManifest(manifest)
        val capabilitiesJson = manifest.getJSONArray("capabilities")
        val capabilities = (0 until capabilitiesJson.length()).map { capabilitiesJson.getString(it) }.toSet()
        val files = manifest.getJSONArray("files")
        val declared = linkedMapOf<String, String>()
        for (i in 0 until files.length()) {
            val item = files.getJSONObject(i)
            if (item.keys().asSequence().toSet() != setOf("path", "sha256") || item.opt("path") !is String || item.opt("sha256") !is String) fail("MANIFEST_INVALID")
            val path = item.getString("path")
            if (!validPath(path) || path == "manifest.json" || !Regex("[a-f0-9]{64}").matches(item.getString("sha256")) || declared.keys.any { it.equals(path, true) }) fail("MANIFEST_INVALID")
            declared[path] = item.getString("sha256")
        }
        if (manifest.getString("entry") !in declared) fail("RESOURCE_MISSING")
        if (declared.keys.any { it !in names }) fail("RESOURCE_MISSING")
        if (names.keys.any { it != "manifest.json" && it !in declared }) fail("RESOURCE_UNDECLARED")
        ZipFile(file).use { zip ->
            for ((path, hash) in declared) {
                val digest = MessageDigest.getInstance("SHA-256")
                zip.getInputStream(names.getValue(path)).use { input ->
                    val buffer = ByteArray(16 * 1024)
                    while (true) { val n = input.read(buffer); if (n < 0) break; digest.update(buffer, 0, n) }
                }
                val actual = digest.digest().joinToString("") { "%02x".format(it) }
                if (actual != hash) fail("RESOURCE_HASH_MISMATCH")
            }
            validateReferences(zip, declared.keys)
        }
        return Package(
            manifest.getString("id"), manifest.getString("name"), manifest.getString("version"),
            manifest.getJSONObject("dataModel"), listOf("manifest.json") + declared.keys,
            manifest.getString("entry"), declared.keys.toList(), capabilities,
        )
    }

    private fun validateManifest(m: JSONObject) {
        requireKeys(m, setOf("manifestVersion", "id", "name", "version", "hostApi", "entry", "capabilities", "dataModel", "files"))
        if (!isPluginId(m.optString("id")) || m.optString("name").length !in 1..64 || m.optString("name").any { it.code < 32 || it.code == 127 }) fail("MANIFEST_INVALID")
        stableVersion(m.optString("version"))
        val api = m.optJSONObject("hostApi") ?: fail("MANIFEST_INVALID")
        requireKeys(api, setOf("min", "max")); stableVersion(api.optString("min")); stableVersion(api.optString("max"))
        if (api.getString("min").substringBefore('.') != api.getString("max").substringBefore('.') || compareVersions(api.getString("min"), api.getString("max")) > 0) fail("MANIFEST_INVALID")
        if (compareVersions(api.getString("min"), HOST_API) > 0 || compareVersions(api.getString("max"), HOST_API) < 0) fail("INCOMPATIBLE_HOST_API")
        if (!validPath(m.optString("entry")) || !m.optString("entry").endsWith(".html")) fail("MANIFEST_INVALID")
        val caps = m.optJSONArray("capabilities") ?: fail("MANIFEST_INVALID")
        val capSet = mutableSetOf<String>()
        for (i in 0 until caps.length()) {
            if (caps.opt(i) !is String) fail("MANIFEST_INVALID")
            val cap = caps.getString(i)
            if (cap.length !in 1..64) fail("MANIFEST_INVALID")
            if (!capSet.add(cap)) fail("MANIFEST_INVALID")
            if (cap !in KNOWN_CAPABILITIES) fail("CAPABILITY_UNKNOWN")
        }
        val model = m.optJSONObject("dataModel") ?: fail("MANIFEST_INVALID")
        requireKeys(model, setOf("version", "collections")); stableVersion(model.optString("version"))
        val collections = model.optJSONArray("collections") ?: fail("MANIFEST_INVALID")
        if (collections.length() > 64) fail("MANIFEST_INVALID")
        val collectionIds = mutableSetOf<String>()
        for (i in 0 until collections.length()) {
            val collection = collections.optJSONObject(i) ?: fail("MANIFEST_INVALID")
            requireKeys(collection, setOf("id", "fields"))
            val cid = collection.optString("id")
            if (!ID_PATTERN.matches(cid) || !collectionIds.add(cid)) fail("MANIFEST_INVALID")
            val fields = collection.optJSONArray("fields") ?: fail("MANIFEST_INVALID")
            if (fields.length() !in 1..128) fail("MANIFEST_INVALID")
            val fieldIds = mutableSetOf<String>()
            for (j in 0 until fields.length()) validateField(fields.optJSONObject(j) ?: fail("MANIFEST_INVALID"), fieldIds)
        }
        val files = m.optJSONArray("files") ?: fail("MANIFEST_INVALID")
        if (files.length() !in 1..127) fail("MANIFEST_INVALID")
    }

    private fun validateField(f: JSONObject, ids: MutableSet<String>) {
        val allowed = setOf("id", "type", "required", "maxLength", "minimum", "maximum", "default")
        if (f.keys().asSequence().any { it !in allowed } || !f.has("id") || !f.has("type") || !f.has("required")) fail("MANIFEST_INVALID")
        val id = f.optString("id"); if (!ID_PATTERN.matches(id) || !ids.add(id)) fail("MANIFEST_INVALID")
        val type = f.optString("type")
        if (type !in setOf("string", "integer", "number", "boolean") || f.opt("required") !is Boolean) fail("MANIFEST_INVALID")
        if ((type != "string" && f.has("maxLength")) || (type !in setOf("integer", "number") && (f.has("minimum") || f.has("maximum")))) fail("MANIFEST_INVALID")
        val min = if (f.has("minimum")) f.optDouble("minimum", Double.NaN) else null
        val max = if (f.has("maximum")) f.optDouble("maximum", Double.NaN) else null
        if ((min != null && !min.isFinite()) || (max != null && !max.isFinite()) || (min != null && max != null && min > max)) fail("MANIFEST_INVALID")
        if (f.has("maxLength") && (f.opt("maxLength") !is Int || f.optInt("maxLength", -1) !in 1..65536)) fail("MANIFEST_INVALID")
        if ((f.has("minimum") && f.opt("minimum") !is Number) || (f.has("maximum") && f.opt("maximum") !is Number)) fail("MANIFEST_INVALID")
        if (f.has("default")) {
            val value = f.opt("default")
            val valid = when (type) { "string" -> value is String && (!f.has("maxLength") || value.length <= f.getInt("maxLength")); "integer" -> value is Number && value.toDouble() % 1.0 == 0.0; "number" -> value is Number && value.toDouble().isFinite(); else -> value is Boolean }
            if (!valid || (value is Number && ((min != null && value.toDouble() < min) || (max != null && value.toDouble() > max)))) fail("MANIFEST_INVALID")
        }
    }

    private fun parseStrictManifest(bytes: ByteArray): JSONObject {
        if (bytes.size > MAX_MANIFEST_BYTES) fail("PACKAGE_LIMIT_EXCEEDED")
        if (bytes.startsWith(byteArrayOf(0xEF.toByte(), 0xBB.toByte(), 0xBF.toByte()))) fail("MANIFEST_INVALID")
        val text = try { bytes.toString(Charsets.UTF_8).also { if (!it.toByteArray(Charsets.UTF_8).contentEquals(bytes)) fail("MANIFEST_INVALID") } } catch (_: Exception) { fail("MANIFEST_INVALID") }
        try {
            JsonReader(StringReader(text)).use { reader ->
                reader.isLenient = false
                validateJson(reader, 0)
                if (reader.peek() != JsonToken.END_DOCUMENT) fail("MANIFEST_INVALID")
            }
            return JSONObject(text)
        } catch (failure: ImportFailure) { throw failure } catch (_: Exception) { fail("MANIFEST_INVALID") }
    }

    private fun validateJson(reader: JsonReader, depth: Int) {
        if (depth > 32) fail("MANIFEST_INVALID")
        when (reader.peek()) {
            JsonToken.BEGIN_OBJECT -> { reader.beginObject(); val keys = mutableSetOf<String>(); while (reader.hasNext()) { if (!keys.add(reader.nextName())) fail("MANIFEST_INVALID"); validateJson(reader, depth + 1) }; reader.endObject() }
            JsonToken.BEGIN_ARRAY -> { reader.beginArray(); while (reader.hasNext()) validateJson(reader, depth + 1); reader.endArray() }
            JsonToken.STRING -> reader.nextString()
            JsonToken.NUMBER -> { val n = reader.nextString(); if (n == "NaN" || n.contains("Infinity")) fail("MANIFEST_INVALID") }
            JsonToken.BOOLEAN -> reader.nextBoolean()
            JsonToken.NULL -> reader.nextNull()
            else -> fail("MANIFEST_INVALID")
        }
    }

    private fun validateReferences(zip: ZipFile, names: Set<String>) {
        for (path in names) {
            if (!path.endsWith(".html") && !path.endsWith(".css")) continue
            val text = zip.getInputStream(zip.getEntry(path)).bufferedReader(Charsets.UTF_8).use { it.readText() }
            val refs = if (path.endsWith(".html")) HtmlRefs.extract(text) else CssRefs.extract(text)
            for (ref in refs) {
                val clean = ref.substringBefore('#').substringBefore('?')
                if (clean.isEmpty()) continue
                if (clean.startsWith("/") || clean.startsWith("\\") || clean.contains('\\') || '%' in clean || ':' in clean || clean.startsWith("//")) fail("RESOURCE_REFERENCE_INVALID")
                val resolved = normalizeResource(path.substringBeforeLast('/', ""), clean) ?: fail("RESOURCE_REFERENCE_INVALID")
                if (resolved !in names) fail("RESOURCE_REFERENCE_INVALID")
            }
        }
    }

    private fun recoverInterruptedInstall() {
        root.listFiles()?.forEach { file ->
            when {
                file.name.startsWith(".backup-") -> {
                    val id = file.name.removePrefix(".backup-").substringBeforeLast('-')
                    val target = File(root, id)
                    if (isPluginId(id) && readInstalled(target) == null && readInstalled(file, id) != null) {
                        if (!target.exists() || target.deleteRecursively()) file.renameTo(target)
                    } else {
                        file.deleteRecursively()
                    }
                }
                file.name.startsWith(".stage-") || file.name.startsWith("incoming-") -> file.deleteRecursively()
                file.name.startsWith(".removed-") -> file.deleteRecursively()
            }
        }
    }

    private data class CentralEntry(val encrypted: Boolean, val method: Int, val disk: Int, val unixType: Int)

    /** Reads bounded ZIP metadata so unsupported flags and Unix special files fail before extraction. */
    private fun inspectCentralDirectory(file: File): Map<String, CentralEntry> {
        RandomAccessFile(file, "r").use { raf ->
            val length = raf.length()
            val tailSize = minOf(length, 65_557L).toInt()
            val tail = ByteArray(tailSize)
            raf.seek(length - tailSize)
            raf.readFully(tail)
            val eocd = (tailSize - 22 downTo 0).firstOrNull { u32(tail, it) == EOCD } ?: fail("PACKAGE_CORRUPT")
            if (u16(tail, eocd + 4) != 0 || u16(tail, eocd + 6) != 0 || u16(tail, eocd + 8) != u16(tail, eocd + 10)) fail("PACKAGE_ENTRY_INVALID")
            val count = u16(tail, eocd + 10)
            val cdSize = u32(tail, eocd + 12)
            val cdOffset = u32(tail, eocd + 16)
            if (count == 0xFFFF || cdSize == 0xFFFFFFFFL || cdOffset == 0xFFFFFFFFL || cdOffset + cdSize > length) fail("PACKAGE_ENTRY_INVALID")
            val result = linkedMapOf<String, CentralEntry>()
            var position = cdOffset
            repeat(count) {
                if (position + 46 > length) fail("PACKAGE_CORRUPT")
                raf.seek(position)
                val fixed = ByteArray(46); raf.readFully(fixed)
                if (u32(fixed, 0) != CENTRAL_HEADER) fail("PACKAGE_CORRUPT")
                val flags = u16(fixed, 8); val method = u16(fixed, 10); val disk = u16(fixed, 34)
                val nameLength = u16(fixed, 28); val extraLength = u16(fixed, 30); val commentLength = u16(fixed, 32)
                val external = u32(fixed, 38)
                if (nameLength == 0 || position + 46 + nameLength + extraLength + commentLength > length) fail("PACKAGE_CORRUPT")
                val nameBytes = ByteArray(nameLength); raf.readFully(nameBytes)
                val name = nameBytes.toString(Charsets.US_ASCII)
                // Some Android ZipFile versions throw on traversal names before enumeration.
                // Classify unsafe names here so the protocol error is stable across runtimes.
                if (!validPath(name)) fail("PACKAGE_ENTRY_INVALID")
                val unixType = ((external ushr 16).toInt() and UNIX_TYPE_MASK)
                if (result.put(name, CentralEntry(flags and ((1 shl 0) or (1 shl 6)) != 0, method, disk, unixType)) != null) fail("PACKAGE_DUPLICATE_ENTRY")
                position += 46L + nameLength + extraLength + commentLength
            }
            if (position != cdOffset + cdSize || result.size != count) fail("PACKAGE_CORRUPT")
            return result
        }
    }

    private fun normalizeResource(base: String, ref: String): String? {
        val stack = (if (base.isEmpty()) emptyList() else base.split('/')).toMutableList()
        for (part in ref.split('/')) when (part) { "", "." -> Unit; ".." -> if (stack.isEmpty()) return null else stack.removeAt(stack.lastIndex); else -> stack.add(part) }
        return stack.joinToString("/").takeIf { validPath(it) }
    }

    private fun compatibleModel(old: Installed, next: JSONObject): Boolean {
        val a = old.dataModel
        val av = a.optString("version").split('.'); val bv = next.optString("version").split('.')
        if (av.size != 3 || bv.size != 3 || av[0] != bv[0] || compareVersions(next.getString("version"), a.getString("version")) < 0) return false
        val oldCols = a.optJSONArray("collections") ?: return false
        val newCols = next.optJSONArray("collections") ?: return false
        // Schema changes require a new minor model version, even if the plugin version grows.
        if (av[1] == bv[1] && canonicalModelValue(oldCols) != canonicalModelValue(newCols)) return false
        val newById = (0 until newCols.length()).associate { newCols.getJSONObject(it).getString("id") to newCols.getJSONObject(it) }
        for (i in 0 until oldCols.length()) {
            val oc = oldCols.getJSONObject(i); val nc = newById[oc.getString("id")] ?: return false
            val nf = nc.getJSONArray("fields"); val byId = (0 until nf.length()).associate { nf.getJSONObject(it).getString("id") to nf.getJSONObject(it) }
            val of = oc.getJSONArray("fields")
            for (j in 0 until of.length()) {
                val oldField = of.getJSONObject(j); val field = byId[oldField.getString("id")] ?: return false
                if (field.optString("type") != oldField.optString("type") || (!oldField.optBoolean("required") && field.optBoolean("required"))) return false
                if (field.has("maxLength") && (!oldField.has("maxLength") || field.getInt("maxLength") < oldField.getInt("maxLength"))) return false
                if (field.has("minimum") && (!oldField.has("minimum") || field.get("minimum").toString().toBigDecimal() > oldField.get("minimum").toString().toBigDecimal())) return false
                if (field.has("maximum") && (!oldField.has("maximum") || field.get("maximum").toString().toBigDecimal() < oldField.get("maximum").toString().toBigDecimal())) return false
            }
            for (j in 0 until nf.length()) { val field = nf.getJSONObject(j); if (field.getString("id") !in (0 until of.length()).map { of.getJSONObject(it).getString("id") } && field.optBoolean("required")) return false }
        }
        return true
    }

    // Collection/field order and JSON key order do not change the data model.
    private fun canonicalModelValue(value: Any): String = when (value) {
        is JSONObject -> value.keys().asSequence().sorted().joinToString(prefix = "{", postfix = "}") {
            JSONObject.quote(it) + ":" + canonicalModelValue(value.get(it))
        }
        is JSONArray -> (0 until value.length()).map { canonicalModelValue(value.get(it)) }.sorted()
            .joinToString(prefix = "[", postfix = "]")
        is String -> JSONObject.quote(value)
        is Number -> value.toString().toBigDecimal().stripTrailingZeros().toString()
        else -> value.toString()
    }

    companion object {
        private var instance: LocalPluginStore? = null

        @Synchronized
        fun applicationStore(context: Context): LocalPluginStore =
            instance ?: LocalPluginStore(context.applicationContext).also { instance = it }

        const val HOST_API = "1.0.0"
        const val MAX_ZIP_BYTES = 10L * 1024 * 1024
        private const val PREVIOUS = ".previous"
        const val MAX_RESOURCE_BYTES = 2L * 1024 * 1024
        const val MAX_MANIFEST_BYTES = 256L * 1024
        const val MAX_TOTAL_BYTES = 20L * 1024 * 1024
        const val MAX_ENTRIES = 128
        const val MAX_RATIO = 100L
        private const val EOCD = 0x06054b50L
        private const val CENTRAL_HEADER = 0x02014b50L
        private const val UNIX_TYPE_MASK = 0xF000
        private const val UNIX_REGULAR = 0x8000
        val KNOWN_CAPABILITIES = setOf("data.read", "data.write", "ui.toast")
        val ID_PATTERN = Regex("[a-z][a-z0-9_]{0,63}")
        private val PATH = Regex("[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*")
        private val EXTENSIONS = setOf("html", "js", "css", "json", "png", "jpg", "jpeg", "webp", "gif", "woff2", "ttf", "ico")
        fun validPath(path: String) = path.length <= 240 && path.split('/').all { it.length <= 64 } && PATH.matches(path) && path.substringAfterLast('.', "") in EXTENSIONS
        fun isPluginId(id: String) = id.length in 3..128 && Regex("[a-z][a-z0-9-]*(\\.[a-z][a-z0-9-]*)+").matches(id)
        fun stableVersion(s: String) { if (!Regex("(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)").matches(s)) fail("MANIFEST_INVALID") }
        fun compareVersions(a: String, b: String): Int { stableVersion(a); stableVersion(b); val x = a.split('.').map(String::toBigInteger); val y = b.split('.').map(String::toBigInteger); return (0..2).firstNotNullOfOrNull { i -> x[i].compareTo(y[i]).takeIf { it != 0 } } ?: 0 }
        private fun requireKeys(o: JSONObject, keys: Set<String>) { if (o.keys().asSequence().toSet() != keys) fail("MANIFEST_INVALID") }
        private fun fail(code: String): Nothing = throw ImportFailure(code)
        private fun ByteArray.startsWith(prefix: ByteArray) = size >= prefix.size && prefix.indices.all { this[it] == prefix[it] }
        private fun u16(data: ByteArray, offset: Int) = (data[offset].toInt() and 0xFF) or ((data[offset + 1].toInt() and 0xFF) shl 8)
        private fun u32(data: ByteArray, offset: Int) = (u16(data, offset).toLong() and 0xFFFF) or ((u16(data, offset + 2).toLong() and 0xFFFF) shl 16)
    }
}

private object HtmlRefs {
    private val attrNames = setOf("src", "href", "poster", "data", "action", "formaction", "xlink:href")
    fun extract(html: String): List<String> {
        val result = mutableListOf<String>(); var i = 0
        while (i < html.length) {
            if (html.startsWith("<!--", i)) { i = html.indexOf("-->", i + 4).let { if (it < 0) html.length else it + 3 }; continue }
            if (html[i] != '<' || i + 1 >= html.length || !html[i + 1].isLetter()) { i++; continue }
            i += 2
            while (i < html.length && (html[i].isLetterOrDigit() || html[i] == '-' || html[i] == ':')) i++
            while (i < html.length) {
                while (i < html.length && html[i].isWhitespace()) i++
                if (i >= html.length || html[i] == '>') { i++; break }
                if (html[i] == '/') { i++; continue }
                val start = i
                while (i < html.length && !html[i].isWhitespace() && html[i] !in charArrayOf('=', '>', '/')) i++
                if (start == i) { i++; continue }
                val key = html.substring(start, i).lowercase(Locale.ROOT)
                while (i < html.length && html[i].isWhitespace()) i++
                var value = ""
                if (i < html.length && html[i] == '=') {
                    i++; while (i < html.length && html[i].isWhitespace()) i++
                    if (i < html.length && (html[i] == '\'' || html[i] == '"')) { val q = html[i++]; val v = i; while (i < html.length && html[i] != q) i++; value = html.substring(v, i); if (i < html.length) i++ }
                    else { val v = i; while (i < html.length && !html[i].isWhitespace() && html[i] != '>') i++; value = html.substring(v, i) }
                }
                if (key in attrNames && value.isNotEmpty()) result += value
                if (key == "srcset") result += value.split(',').map { candidate -> candidate.trim().split(Regex("\\s+"), limit = 2).first() }.filter(String::isNotEmpty)
            }
        }
        return result
    }
}

private object CssRefs {
    fun extract(css: String): List<String> {
        val refs = mutableListOf<String>(); var i = 0
        while (i < css.length) {
            if (css.startsWith("/*", i)) { i = css.indexOf("*/", i + 2).let { if (it < 0) css.length else it + 2 }; continue }
            if (css[i] == '\'' || css[i] == '"') { i = skipString(css, i); continue }
            if (css.regionMatches(i, "url", 0, 3, true) && (i == 0 || !css[i - 1].isLetterOrDigit())) {
                var j = i + 3; while (j < css.length && css[j].isWhitespace()) j++
                if (j < css.length && css[j] == '(') { j++; while (j < css.length && css[j].isWhitespace()) j++; val (value, end) = readCssValue(css, j, ')'); if (value.isNotBlank()) refs += value.trim(); i = end; continue }
            }
            if (css.regionMatches(i, "@import", 0, 7, true)) {
                var j = i + 7; while (j < css.length && css[j].isWhitespace()) j++
                if (j < css.length && (css[j] == '\'' || css[j] == '"')) { val (v, end) = readCssValue(css, j, '\u0000'); refs += v; i = end; continue }
            }
            i++
        }
        return refs
    }
    private fun skipString(s: String, start: Int): Int { val q = s[start]; var i = start + 1; while (i < s.length) { if (s[i] == '\\') i += 2 else if (s[i++] == q) break }; return i.coerceAtMost(s.length) }
    private fun readCssValue(s: String, start: Int, terminator: Char): Pair<String, Int> {
        var i = start
        if (i < s.length && (s[i] == '\'' || s[i] == '"')) { val q = s[i]; val begin = ++i; while (i < s.length && s[i] != q) { if (s[i] == '\\') i++; i++ }; return s.substring(begin, i) to (i + 1).coerceAtMost(s.length) }
        val begin = i; while (i < s.length && (terminator == '\u0000' || s[i] != terminator)) i++; return s.substring(begin, i) to (i + 1).coerceAtMost(s.length)
    }
}
