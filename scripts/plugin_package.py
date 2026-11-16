"""Trusted static-data package checks and deterministic ZIP writer for stage 2.3.

These functions never execute plugin code, resolve URLs or extract ZIP contents.
Package validity does not grant browser, device or delivery qualification.
"""

from __future__ import annotations

import hashlib
import html.parser
import io
import json
import math
import posixpath
import re
import stat
import struct
import zipfile
import zlib
from typing import Any


MAX_ZIP_BYTES = 10 * 1024 * 1024
MAX_ENTRIES = 128
MAX_RESOURCE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
EXTENSIONS = {".html", ".js", ".css", ".json", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".woff2", ".ttf", ".ico"}
CAPABILITIES = {"data.read", "data.write", "ui.toast"}
ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
PATH = re.compile(r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*")
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


class PackageError(ValueError):
    def __init__(self, code: str, path: str | None = None, check: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.check = check


def require(condition: Any, code: str = "MANIFEST_INVALID", *, path: str | None = None,
            check: str | None = None) -> None:
    if not condition:
        raise PackageError(code, path, check)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def valid_path(value: Any) -> bool:
    return (isinstance(value, str) and len(value) <= 240 and PATH.fullmatch(value) is not None
            and all(len(part) <= 64 for part in value.split("/"))
            and posixpath.splitext(value)[1] in EXTENSIONS)


def _keys(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    require(isinstance(value, dict) and required <= value.keys()
            and value.keys() <= required | (optional or set()))


def _version(value: Any) -> tuple[tuple[int, str], ...]:
    require(isinstance(value, str) and VERSION.fullmatch(value))
    # Length/lexical comparison supports arbitrary precision without int digit limits.
    return tuple((len(part), part) for part in value.split("."))


def _number(value: Any) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # The current Android importer rejects values outside its finite
        # numeric representation, including arbitrarily large JSON integers.
        return False


def _utf16_units(value: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeError:
        raise PackageError("MANIFEST_INVALID", check="unicode_string") from None


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise PackageError("MANIFEST_INVALID")


def _depth(value: Any, level: int = 0) -> None:
    require(level <= 32)
    if isinstance(value, dict):
        for key, item in value.items():
            # JSON escaped isolated surrogates are not valid Unicode text.
            key.encode("utf-8")
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)
    elif isinstance(value, str):
        value.encode("utf-8")
    elif type(value) is float:
        require(math.isfinite(value))


def parse_manifest(raw: bytes) -> dict[str, Any]:
    require(len(raw) <= MAX_MANIFEST_BYTES, "PACKAGE_LIMIT_EXCEEDED", path="manifest.json")
    require(not raw.startswith(b"\xef\xbb\xbf"))
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        _depth(manifest)
    except PackageError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise PackageError("MANIFEST_INVALID", "manifest.json", "strict_json") from None
    require(isinstance(manifest, dict))
    return manifest


def _field(field: Any, ids: set[str]) -> None:
    _keys(field, {"id", "type", "required"}, {"maxLength", "minimum", "maximum", "default"})
    fid = field["id"]
    require(isinstance(fid, str) and ID.fullmatch(fid) and fid not in ids)
    ids.add(fid)
    kind = field["type"]
    require(isinstance(kind, str) and kind in {"string", "integer", "number", "boolean"}
            and type(field["required"]) is bool)
    require("maxLength" not in field or (kind == "string" and type(field["maxLength"]) is int
                                           and 1 <= field["maxLength"] <= 65536))
    for key in ("minimum", "maximum"):
        require(key not in field or (kind in {"integer", "number"} and _number(field[key])))
    require(not {"minimum", "maximum"} <= field.keys() or field["minimum"] <= field["maximum"])
    if "default" not in field:
        return
    value = field["default"]
    if kind == "string":
        require(isinstance(value, str) and _utf16_units(value) <= field.get("maxLength", 2**63))
    elif kind == "boolean":
        require(type(value) is bool)
    else:
        require(_number(value) and (kind != "integer" or type(value) is int or value.is_integer()))
        require("minimum" not in field or value >= field["minimum"])
        require("maximum" not in field or value <= field["maximum"])


def _manifest(manifest: dict[str, Any], expected_plugin_id: str | None) -> dict[str, str]:
    require(type(manifest.get("manifestVersion")) is int)
    require(manifest["manifestVersion"] == 1, "MANIFEST_VERSION_UNSUPPORTED")
    _keys(manifest, {"manifestVersion", "id", "name", "version", "hostApi", "entry", "capabilities", "dataModel", "files"})
    plugin_id, name = manifest["id"], manifest["name"]
    require(isinstance(plugin_id, str) and 3 <= len(plugin_id) <= 128
            and re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+", plugin_id))
    require(expected_plugin_id is None or plugin_id == expected_plugin_id,
            "PLUGIN_ID_MISMATCH", path="manifest.json", check="bound_plugin_id")
    require(isinstance(name, str) and 1 <= _utf16_units(name) <= 64
            and all(ord(char) >= 32 and ord(char) != 127 for char in name))
    _version(manifest["version"])
    api = manifest["hostApi"]
    _keys(api, {"min", "max"})
    low, high = _version(api["min"]), _version(api["max"])
    require(low[0] == high[0] and low <= high)
    require(low <= _version("1.0.0") <= high, "INCOMPATIBLE_HOST_API")
    require(valid_path(manifest["entry"]) and manifest["entry"].endswith(".html"))
    caps = manifest["capabilities"]
    require(isinstance(caps, list) and all(isinstance(cap, str) and 1 <= len(cap) <= 64 for cap in caps))
    require(len(caps) == len(set(caps)))
    require(set(caps) <= CAPABILITIES, "CAPABILITY_UNKNOWN")
    model = manifest["dataModel"]
    _keys(model, {"version", "collections"})
    _version(model["version"])
    collections = model["collections"]
    require(isinstance(collections, list) and len(collections) <= 64)
    ids: set[str] = set()
    for collection in collections:
        _keys(collection, {"id", "fields"})
        cid = collection["id"]
        require(isinstance(cid, str) and ID.fullmatch(cid) and cid not in ids)
        ids.add(cid)
        fields = collection["fields"]
        require(isinstance(fields, list) and 1 <= len(fields) <= 128)
        field_ids: set[str] = set()
        for field in fields:
            _field(field, field_ids)
    items = manifest["files"]
    require(isinstance(items, list) and 1 <= len(items) <= 127)
    declared: dict[str, str] = {}
    for item in items:
        _keys(item, {"path", "sha256"})
        path, digest = item["path"], item["sha256"]
        require(valid_path(path) and path != "manifest.json" and path not in declared)
        require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest))
        declared[path] = digest
    return declared


def _css_unescape(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        if re.fullmatch(r"[0-9a-fA-F]{1,6}\s?", raw):
            number = int(raw.strip(), 16)
            return chr(number) if 0 < number <= 0x10ffff and not 0xd800 <= number <= 0xdfff else "\ufffd"
        return raw if raw not in {"\n", "\r", "\f"} else ""
    return re.sub(r"\\([0-9a-fA-F]{1,6}\s?|[^\r\n]|\r?\n)", replace, text)


def css_references(text: str) -> list[str]:
    # Tokenize quoted strings/comments and URL functions rather than inspecting
    # arbitrary substrings (e.g. a harmless text string containing 'https:').
    text = _css_unescape(text)
    refs: list[str] = []
    position = 0
    url_pattern = re.compile(r"url\s*\(", re.I)
    import_pattern = re.compile(r"@import\s*", re.I)

    def skip_space_comments(start: int) -> int:
        while start < len(text):
            if text[start].isspace():
                start += 1
            elif text.startswith("/*", start):
                end = text.find("*/", start + 2)
                start = len(text) if end < 0 else end + 2
            else:
                break
        return start

    while position < len(text):
        if text.startswith("/*", position):
            end = text.find("*/", position + 2)
            position = len(text) if end < 0 else end + 2
            continue
        char = text[position]
        if char in "\"'":
            end = text.find(char, position + 1)
            position = len(text) if end < 0 else end + 1
            continue
        url = url_pattern.match(text, position)
        imported = import_pattern.match(text, position)
        if url and (position == 0 or not re.match(r"[a-z0-9_-]", text[position - 1], re.I)):
            start = url.end()
            end = text.find(")", start)
            require(end >= 0, "RESOURCE_REFERENCE_INVALID", check="css_url")
            refs.append(text[start:end].strip().strip("\"'"))
            position = end + 1
        elif imported:
            start = skip_space_comments(imported.end())
            if start < len(text) and text[start] in "\"'":
                end = text.find(text[start], start + 1)
                require(end >= 0, "RESOURCE_REFERENCE_INVALID", check="css_import")
                refs.append(text[start + 1:end])
                position = end + 1
            else:
                position = start
        else:
            position += 1
    return refs


class _HTMLReferences(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        require(tag != "base", "RESOURCE_REFERENCE_INVALID", check="html_base")
        self.in_style = tag == "style" or self.in_style
        for key, value in attrs:
            if not value:
                continue
            if key in {"src", "href", "poster", "data", "action", "formaction", "xlink:href", "background"}:
                self.references.append(value)
            elif key == "srcset":
                self.references.extend(candidate.strip().split()[0] for candidate in value.split(",") if candidate.strip())
            elif key == "style":
                self.references.extend(css_references(value))
        if tag == "meta" and (values.get("http-equiv") or "").lower() == "refresh":
            content = values.get("content", "") or ""
            match = re.search(r"url\s*=\s*(.*)", content, re.I)
            if match:
                self.references.append(match.group(1).strip().strip("\"'"))

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if self.in_style:
            self.references.extend(css_references(data))


def _references(files: dict[str, bytes]) -> None:
    for path, raw in files.items():
        if not path.endswith((".html", ".css")):
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise PackageError("RESOURCE_REFERENCE_INVALID", path, "resource_utf8") from None
        if path.endswith(".html"):
            parser = _HTMLReferences()
            parser.feed(text)
            parser.close()
            refs = parser.references
        else:
            refs = css_references(text)
        for reference in refs:
            clean = reference.split("#", 1)[0].split("?", 1)[0]
            if not clean:
                continue
            require(not clean.startswith("/") and not any(char in clean for char in "\\%:")
                    and not any(ord(char) < 32 for char in clean),
                    "RESOURCE_REFERENCE_INVALID", path=path, check="resource_reference")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), clean))
            require(valid_path(resolved) and resolved in files and resolved != "manifest.json",
                    "RESOURCE_REFERENCE_INVALID", path=path, check="resource_reference")


def validate_source(files: dict[str, bytes], expected_plugin_id: str | None = None) -> dict[str, Any]:
    require(isinstance(files, dict), "PACKAGE_ENTRY_INVALID")
    require(len(files) <= MAX_ENTRIES, "PACKAGE_LIMIT_EXCEEDED")
    total = 0
    for path, raw in files.items():
        require(valid_path(path) and type(raw) is bytes, "PACKAGE_ENTRY_INVALID")
        limit = MAX_MANIFEST_BYTES if path == "manifest.json" else MAX_RESOURCE_BYTES
        require(len(raw) <= limit, "PACKAGE_LIMIT_EXCEEDED", path=path)
        total += len(raw)
    require(total <= MAX_TOTAL_BYTES, "PACKAGE_LIMIT_EXCEEDED")
    require("manifest.json" in files, "MANIFEST_MISSING")
    manifest = parse_manifest(files["manifest.json"])
    declared = _manifest(manifest, expected_plugin_id)
    require(manifest["entry"] in declared, "RESOURCE_MISSING", path=manifest["entry"])
    missing = sorted(declared.keys() - files.keys())
    require(not missing, "RESOURCE_MISSING", path=missing[0] if missing else None)
    undeclared = sorted(files.keys() - declared.keys() - {"manifest.json"})
    require(not undeclared, "RESOURCE_UNDECLARED", path=undeclared[0] if undeclared else None)
    for path, digest in declared.items():
        require(sha256(files[path]) == digest, "RESOURCE_HASH_MISMATCH", path=path, check="resource_sha256")
    _references(files)
    return manifest


def build_package(files: dict[str, bytes]) -> bytes:
    validate_source(files)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", allowZip64=False) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, files[name])
    result = stream.getvalue()
    require(len(result) <= MAX_ZIP_BYTES, "PACKAGE_LIMIT_EXCEEDED")
    validate_package(result)
    return result


def _zip_metadata(blob: bytes) -> None:
    # Explicitly reject split/ZIP64/unsupported entries before zipfile reads data.
    lower = max(0, len(blob) - 65557)
    eocd = blob.rfind(b"PK\x05\x06", lower)
    while eocd >= 0:
        if eocd + 22 <= len(blob) and eocd + 22 + struct.unpack_from("<H", blob, eocd + 20)[0] == len(blob):
            break
        eocd = blob.rfind(b"PK\x05\x06", lower, eocd)
    require(eocd >= 0 and eocd + 22 <= len(blob), "PACKAGE_CORRUPT")
    _, disk, cd_disk, disk_count, count, cd_size, offset, comment = struct.unpack_from("<4s4H2IH", blob, eocd)
    require(eocd + 22 + comment == len(blob), "PACKAGE_CORRUPT")
    require(disk == cd_disk == 0 and disk_count == count and count != 65535
            and cd_size != 0xffffffff and offset != 0xffffffff, "PACKAGE_ENTRY_INVALID")
    require(offset + cd_size == eocd, "PACKAGE_CORRUPT")
    require(count <= MAX_ENTRIES, "PACKAGE_LIMIT_EXCEEDED")
    position = offset
    for _ in range(count):
        require(position + 46 <= eocd and blob[position:position + 4] == b"PK\x01\x02", "PACKAGE_CORRUPT")
        flags, method = struct.unpack_from("<HH", blob, position + 8)
        compressed, size = struct.unpack_from("<II", blob, position + 20)
        name_size, extra_size, comment_size, entry_disk = struct.unpack_from("<4H", blob, position + 28)
        external, local_offset = struct.unpack_from("<II", blob, position + 38)
        require(flags & ~(2 | 4 | 8 | 2048) == 0 and method in {0, 8} and entry_disk == 0,
                "PACKAGE_ENTRY_INVALID")
        require(size != 0xffffffff and compressed != 0xffffffff and local_offset != 0xffffffff,
                "PACKAGE_ENTRY_INVALID")
        require(external >> 16 & 0xf000 in {0, stat.S_IFREG}, "PACKAGE_ENTRY_INVALID")
        end = position + 46 + name_size + extra_size + comment_size
        require(name_size > 0 and end <= eocd, "PACKAGE_CORRUPT")
        name = blob[position + 46:position + 46 + name_size]
        try:
            decoded = name.decode("ascii")
        except UnicodeError:
            raise PackageError("PACKAGE_ENTRY_INVALID") from None
        require(valid_path(decoded), "PACKAGE_ENTRY_INVALID")
        require(local_offset + 30 <= offset and blob[local_offset:local_offset + 4] == b"PK\x03\x04", "PACKAGE_CORRUPT")
        local_flags, local_method = struct.unpack_from("<HH", blob, local_offset + 6)
        local_name_size, local_extra_size = struct.unpack_from("<HH", blob, local_offset + 26)
        require(local_flags == flags and local_method == method, "PACKAGE_CORRUPT")
        if not flags & 8:
            require(blob[local_offset + 14:local_offset + 26] == blob[position + 16:position + 28],
                    "PACKAGE_CORRUPT")
        require(blob[local_offset + 30:local_offset + 30 + local_name_size] == name, "PACKAGE_CORRUPT")
        require(local_offset + 30 + local_name_size + local_extra_size + compressed <= offset, "PACKAGE_CORRUPT")
        position = end
    require(position == eocd, "PACKAGE_CORRUPT")


def validate_package(blob: bytes, expected_plugin_id: str | None = None) -> dict[str, Any]:
    require(type(blob) is bytes, "PACKAGE_CORRUPT")
    require(len(blob) <= MAX_ZIP_BYTES, "PACKAGE_LIMIT_EXCEEDED")
    try:
        _zip_metadata(blob)
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            infos = archive.infolist()
            require(len(infos) <= MAX_ENTRIES, "PACKAGE_LIMIT_EXCEEDED")
            names = [info.filename for info in infos]
            require(len(names) == len(set(name.casefold() for name in names)), "PACKAGE_DUPLICATE_ENTRY")
            total = 0
            for info in infos:
                limit = MAX_MANIFEST_BYTES if info.filename == "manifest.json" else MAX_RESOURCE_BYTES
                require(info.file_size <= limit, "PACKAGE_LIMIT_EXCEEDED", path=info.filename)
                require(not info.file_size or info.compress_size > 0 and info.file_size <= 100 * info.compress_size,
                        "PACKAGE_LIMIT_EXCEEDED", path=info.filename)
                total += info.file_size
                require(total <= MAX_TOTAL_BYTES, "PACKAGE_LIMIT_EXCEEDED")
            files: dict[str, bytes] = {}
            actual_total = 0
            for info in infos:
                limit = MAX_MANIFEST_BYTES if info.filename == "manifest.json" else MAX_RESOURCE_BYTES
                # Read the compressed range ourselves: ZipExtFile caps output at
                # the untrusted central-directory size and could hide extra
                # deflate output. Bound actual decompression before comparing it.
                name_size, extra_size = struct.unpack_from("<HH", blob, info.header_offset + 26)
                begin = info.header_offset + 30 + name_size + extra_size
                compressed = blob[begin:begin + info.compress_size]
                if info.compress_type == zipfile.ZIP_STORED:
                    raw = compressed
                else:
                    decompressor = zlib.decompressobj(-15)
                    raw = decompressor.decompress(compressed, limit + 1)
                    require(len(raw) <= limit, "PACKAGE_LIMIT_EXCEEDED", path=info.filename)
                    require(decompressor.eof and not decompressor.unused_data and not decompressor.unconsumed_tail,
                            "PACKAGE_CORRUPT", path=info.filename)
                actual_total += len(raw)
                require(len(raw) <= limit and actual_total <= MAX_TOTAL_BYTES, "PACKAGE_LIMIT_EXCEEDED", path=info.filename)
                require(len(raw) == info.file_size and zlib.crc32(raw) & 0xffffffff == info.CRC, "PACKAGE_CORRUPT", path=info.filename)
                files[info.filename] = raw
        return validate_source(files, expected_plugin_id)
    except PackageError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, EOFError, struct.error, zlib.error,
            NotImplementedError):
        raise PackageError("PACKAGE_CORRUPT", check="zip_structure") from None
