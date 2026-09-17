#!/usr/bin/env python3
"""Compare LoadRunner VuGen HTTP scripts by request structure, not test data.

Usage:
    python tools/loadrunner_structural_diff.py SIT_DIR PPE_DIR -o report.md

The implementation intentionally uses only Python's standard library. It is
designed to be useful against recorded VuGen C files, where a request can be
spread across adjacent C string literals and where JSON arrays can be vastly
different in size between environments.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit, urlunsplit
import xml.etree.ElementTree as ET


REQUEST_CALLS = ("web_url", "web_submit_data", "web_custom_request")
CORRELATION_CALL_RE = re.compile(r"\b(web_reg_save_param(?:_ex|_json)?|web_reg_save_param)\s*\(")
EVENT_RE = re.compile(
    r"\b(web_url|web_submit_data|web_custom_request|"
    r"web_reg_save_param(?:_ex|_json)?|web_reg_save_param|"
    r"lr_start_transaction|lr_end_transaction)\s*\("
)
C_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"', re.DOTALL)
PARAM_RE = re.compile(r"\{[^{}\r\n]+\}")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
INTEGER_RE = re.compile(r"^[+-]?\d+$")
NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$")
MID_RE = re.compile(r"^MID[_-]?\d+$", re.IGNORECASE)


@dataclass
class ParsedRequest:
    file_path: str
    transaction: str
    call_name: str
    logical_name: str
    fields: dict[str, list[str]]
    flags: tuple[str, ...]
    line: int
    body_schema: Any = None
    body_kind: str = "none"
    array_lengths: dict[str, int] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        url = first_field(self.fields, "url") or first_field(self.fields, "action")
        method = (first_field(self.fields, "method") or "").upper()
        if not method:
            method = "GET" if self.call_name == "web_url" else "<UNSPECIFIED>"
        path = normalize_url(url) if url else f"<unresolved:{self.logical_name or self.call_name}>"
        return f"{method} {path}"

    @property
    def signature(self) -> str:
        """Return a data-agnostic request signature used for structural diffing."""

        relevant: list[str] = []
        for key, values in sorted(self.fields.items()):
            key_lower = key.lower()
            if key_lower in {"url", "action", "body"}:
                continue
            if key_lower in {"name", "value"}:
                # Name fields are schema-relevant; Value fields are test data.
                if key_lower == "name":
                    relevant.append("parameters=" + ",".join(sorted(set(values))))
                continue
            if key_lower in {"header", "headers", "querystring", "contenttype", "enctype", "method"}:
                masked = [normalize_header(v) if key_lower in {"header", "headers"} else mask_value(v) for v in values]
                relevant.append(f"{key}=" + "|".join(sorted(masked)))
        if self.body_kind != "none":
            relevant.append(f"body[{self.body_kind}]={schema_signature(self.body_schema)}")
        if self.flags:
            relevant.append("flags=" + ",".join(sorted(self.flags)))
        return self.endpoint + " :: " + " :: ".join(relevant)

    @property
    def location(self) -> str:
        return f"{self.file_path}:{self.line} ({self.transaction})"


@dataclass
class ParsedCorrelation:
    file_path: str
    transaction: str
    call_name: str
    fields: dict[str, list[str]]
    line: int

    @property
    def signature(self) -> str:
        parts: list[str] = []
        for key, values in sorted(self.fields.items()):
            normalized = []
            for value in values:
                if key.lower() in {"requesturl", "url"}:
                    normalized.append(normalize_url(value))
                elif key.lower() in {"paramname", "jsonpath", "xpath", "lb", "rb", "ordinal", "scope", "search"}:
                    normalized.append(value.strip())
                else:
                    normalized.append(mask_value(value))
            parts.append(f"{key}=" + "|".join(sorted(normalized)))
        return self.call_name + "(" + ", ".join(parts) + ")"

    @property
    def location(self) -> str:
        return f"{self.file_path}:{self.line} ({self.transaction})"


@dataclass
class ScriptSnapshot:
    root: Path
    requests: list[ParsedRequest] = field(default_factory=list)
    correlations: list[ParsedCorrelation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def endpoint_records(self) -> dict[str, list[ParsedRequest]]:
        records: dict[str, list[ParsedRequest]] = defaultdict(list)
        for request in self.requests:
            records[request.endpoint].append(request)
        return dict(records)

    @property
    def correlation_records(self) -> dict[str, list[ParsedCorrelation]]:
        records: dict[str, list[ParsedCorrelation]] = defaultdict(list)
        for correlation in self.correlations:
            records[correlation.signature].append(correlation)
        return dict(records)


def strip_c_comments(source: str) -> str:
    """Remove C comments while preserving quoted strings and line positions."""

    result: list[str] = []
    i = 0
    in_string = False
    escaped = False
    while i < len(source):
        char = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            i += 1
        elif char == "/" and nxt == "/":
            newline = source.find("\n", i + 2)
            if newline == -1:
                result.extend(" " * (len(source) - i))
                break
            result.extend(" " * (newline - i))
            result.append("\n")
            i = newline + 1
        elif char == "/" and nxt == "*":
            end = source.find("*/", i + 2)
            if end == -1:
                result.extend(" " * (len(source) - i))
                break
            comment = source[i : end + 2]
            result.extend("\n" if c == "\n" else " " for c in comment)
            i = end + 2
        else:
            result.append(char)
            i += 1
    return "".join(result)


def find_matching_paren(source: str, opening_index: int) -> int | None:
    """Find a call's closing parenthesis while ignoring parentheses in strings."""

    depth = 0
    in_string = False
    escaped = False
    for index in range(opening_index, len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def split_arguments(argument_text: str) -> list[str]:
    """Split C arguments on top-level commas, preserving nested JSON/C values."""

    parts: list[str] = []
    start = 0
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(argument_text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(argument_text[start:index].strip())
            start = index + 1
    tail = argument_text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def decode_c_string(raw: str) -> str:
    """Decode only the C escapes commonly emitted by VuGen."""

    inner = raw[1:-1] if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"' else raw
    replacements = {
        r"\\": "\\",
        r"\"": '"',
        r"\/": "/",
        r"\n": "\n",
        r"\r": "\r",
        r"\t": "\t",
    }
    for escaped, decoded in replacements.items():
        inner = inner.replace(escaped, decoded)
    return inner


def clean_c_fragment(fragment: str) -> str:
    """Join adjacent VuGen C strings such as ``\"Body={\" \"id\": 1}``."""

    strings = [decode_c_string(match.group(0)) for match in C_STRING_RE.finditer(fragment)]
    if strings:
        # Non-string tokens are retained as markers such as ITEMDATA or LAST.
        non_string = C_STRING_RE.sub(" ", fragment)
        return "".join(strings) + (" " + non_string.strip() if non_string.strip() else "")
    return fragment.strip()


def parse_call_arguments(argument_text: str) -> tuple[str, dict[str, list[str]], tuple[str, ...]]:
    raw_args = split_arguments(argument_text)
    cleaned = [clean_c_fragment(item) for item in raw_args if item.strip()]
    first = cleaned[0].strip() if cleaned else ""
    fields: dict[str, list[str]] = defaultdict(list)
    flags: list[str] = []
    for item in cleaned[1:] if cleaned else []:
        if "=" in item:
            key, value = item.split("=", 1)
            fields[key.strip().lower()].append(value.strip())
        else:
            flags.append(item.strip().upper())
    return first.strip('"'), dict(fields), tuple(flags)


def extract_call_events(source: str) -> Iterable[tuple[str, str, int]]:
    """Yield (function name, raw args, line) for supported calls."""

    cleaned_source = strip_c_comments(source)
    cursor = 0
    while True:
        match = EVENT_RE.search(cleaned_source, cursor)
        if not match:
            return
        opening = cleaned_source.find("(", match.start(), match.end())
        closing = find_matching_paren(cleaned_source, opening)
        if closing is None:
            # A malformed call should not prevent later files from being parsed.
            cursor = match.end()
            continue
        line = cleaned_source.count("\n", 0, match.start()) + 1
        yield match.group(1), cleaned_source[opening + 1 : closing], line
        cursor = closing + 1


def first_field(fields: dict[str, list[str]], name: str) -> str:
    values = fields.get(name.lower(), [])
    return values[0] if values else ""


def normalize_url(value: str) -> str:
    """Strip scheme/host so SIT and PPE base domains compare equally."""

    value = clean_c_fragment(value).strip()
    if value.startswith("URL=") or value.startswith("Action="):
        value = value.split("=", 1)[1]
    value = value.replace("\\/", "/")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return mask_value(value)
    if parsed.scheme and parsed.netloc:
        path = PARAM_RE.sub("<PARAM>", parsed.path or "/")
        query = normalize_query(parsed.query)
        return urlunsplit(("", "", path, query, ""))
    if value.startswith("//") and parsed.netloc:
        path = PARAM_RE.sub("<PARAM>", parsed.path or "/")
        return urlunsplit(("", "", path, normalize_query(parsed.query), ""))
    if "?" in value:
        path, query = value.split("?", 1)
        path = PARAM_RE.sub("<PARAM>", path.rstrip("/") or "/")
        normalized_query = normalize_query(query)
        return path + ("?" + normalized_query if normalized_query else "")
    return PARAM_RE.sub("<PARAM>", value.rstrip("/") or "/")


def normalize_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    if not pairs:
        return ""
    return "&".join(f"{key}={mask_value(value)}" for key, value in sorted(pairs))


def mask_value(value: str) -> str:
    """Mask recorded values while preserving a useful primitive type."""

    value = value.strip()
    if not value:
        return "<EMPTY>"
    if PARAM_RE.fullmatch(value):
        return "<PARAM>"
    if INTEGER_RE.fullmatch(value):
        return "<INT>"
    if NUMBER_RE.fullmatch(value):
        return "<NUMBER>"
    if EMAIL_RE.fullmatch(value) or UUID_RE.fullmatch(value) or MID_RE.fullmatch(value):
        return "<STRING>"
    # Raw values and {p_Name}/{c_Name} embedded in a larger value are data.
    if PARAM_RE.search(value):
        return PARAM_RE.sub("<PARAM>", value)
    return "<STRING>"


def normalize_header(value: str) -> str:
    if ":" in value:
        name, raw_value = value.split(":", 1)
        return name.strip().lower() + ":" + mask_value(raw_value)
    return mask_value(value)


def schema_node(value: Any) -> dict[str, Any]:
    """Collapse JSON into types, keys, and array item structure."""

    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        item_nodes = [schema_node(item) for item in value]
        merged = merge_schema_nodes(item_nodes) if item_nodes else {"type": "any"}
        return {"type": "array", "items": merged}
    if isinstance(value, dict):
        return {
            "type": "object",
            "keys": {str(key): schema_node(value[key]) for key in sorted(value)},
        }
    return {"type": "any"}


def merge_schema_nodes(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge array element schemas so one item and 500 items compare alike."""

    if not nodes:
        return {"type": "any"}
    types = {node.get("type") for node in nodes}
    if len(types) == 1:
        node_type = next(iter(types))
        if node_type == "object":
            keys: dict[str, dict[str, Any]] = {}
            for node in nodes:
                for key, child in node.get("keys", {}).items():
                    keys[key] = merge_schema_nodes([keys[key], child]) if key in keys else child
            return {"type": "object", "keys": dict(sorted(keys.items()))}
        if node_type == "array":
            return {"type": "array", "items": merge_schema_nodes([node["items"] for node in nodes])}
        return nodes[0]
    # Treat int/float as numeric-compatible, but preserve other mixed types.
    if types <= {"integer", "number"}:
        return {"type": "number"}
    return {"type": "union", "options": sorted(nodes, key=schema_signature)}


def schema_signature(schema: Any) -> str:
    if schema is None:
        return "<none>"
    if isinstance(schema, str):
        return schema
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


def schema_diff_paths(left: Any, right: Any, path: str = "$") -> list[str]:
    """Explain useful schema changes without comparing actual values."""

    if not isinstance(left, dict) or not isinstance(right, dict):
        return [f"{path}: changed"] if left != right else []
    if left.get("type") != right.get("type"):
        return [f"{path}: type {left.get('type')} → {right.get('type')}"]
    changes: list[str] = []
    if left.get("type") == "object":
        left_keys = set(left.get("keys", {}))
        right_keys = set(right.get("keys", {}))
        changes.extend(f"{path}.{key}: removed key" for key in sorted(left_keys - right_keys))
        changes.extend(f"{path}.{key}: added key" for key in sorted(right_keys - left_keys))
        for key in sorted(left_keys & right_keys):
            changes.extend(schema_diff_paths(left["keys"][key], right["keys"][key], f"{path}.{key}"))
    elif left.get("type") == "array":
        changes.extend(schema_diff_paths(left.get("items"), right.get("items"), path + "[]"))
    elif left != right:
        changes.append(f"{path}: changed")
    return changes


def parse_json_payload(payload: str) -> tuple[Any, dict[str, int]] | None:
    candidate = payload.strip()
    if not candidate:
        return None
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    lengths: dict[str, int] = {}

    def visit(node: Any, path: str = "$") -> None:
        if isinstance(node, list):
            lengths[path] = len(node)
            for index, child in enumerate(node[:3]):
                visit(child, f"{path}[{index}]")
        elif isinstance(node, dict):
            for key, child in node.items():
                visit(child, f"{path}.{key}")

    visit(value)
    return schema_node(value), lengths


def parse_xml_payload(payload: str) -> tuple[str, dict[str, int]] | None:
    try:
        root = ET.fromstring(payload.strip())
    except (ET.ParseError, ValueError):
        return None
    counts: dict[str, int] = Counter()

    def node_signature(node: ET.Element) -> str:
        children = sorted(node_signature(child) for child in list(node))
        attributes = ",".join(sorted(node.attrib))
        counts[node.tag] += 1
        suffix = f" attrs({attributes})" if attributes else ""
        return f"<{node.tag}{suffix}>" + "".join(children) + f"</{node.tag}>"

    return node_signature(root), dict(counts)


def normalize_non_structured_body(payload: str) -> str:
    payload = re.sub(r"\s+", " ", payload.strip())
    if "&" in payload and "=" in payload:
        pairs = []
        for key, value in parse_qsl(payload, keep_blank_values=True):
            pairs.append(f"{key}={mask_value(value)}")
        if pairs:
            return "&".join(sorted(pairs))
    return re.sub(r"(?P<key>[A-Za-z][\w.-]*)\s*=\s*(?P<value>[^,;&\s]+)", lambda m: f"{m.group('key')}={mask_value(m.group('value'))}", payload)


def classify_body(payload: str) -> tuple[Any, str, dict[str, int]]:
    parsed_json = parse_json_payload(payload)
    if parsed_json:
        schema, lengths = parsed_json
        return schema, "json", lengths
    parsed_xml = parse_xml_payload(payload)
    if parsed_xml:
        signature, counts = parsed_xml
        return signature, "xml", counts
    if payload.strip():
        return normalize_non_structured_body(payload), "text", {}
    return None, "none", {}


def parse_file(path: Path, root: Path, snapshot: ScriptSnapshot) -> None:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        snapshot.warnings.append(f"{path}: could not read file ({error})")
        return
    transaction = "<unscoped>"
    for call_name, raw_args, line in extract_call_events(source):
        first, fields, flags = parse_call_arguments(raw_args)
        if call_name == "lr_start_transaction":
            transaction = first or "<unnamed>"
            continue
        if call_name == "lr_end_transaction":
            transaction = "<unscoped>"
            continue
        relative = str(path.relative_to(root))
        if call_name in REQUEST_CALLS:
            body = first_field(fields, "body")
            body_schema, body_kind, array_lengths = classify_body(body)
            snapshot.requests.append(
                ParsedRequest(
                    file_path=relative,
                    transaction=transaction,
                    call_name=call_name,
                    logical_name=first,
                    fields=fields,
                    flags=flags,
                    line=line,
                    body_schema=body_schema,
                    body_kind=body_kind,
                    array_lengths=array_lengths,
                )
            )
        elif call_name.startswith("web_reg_save_param"):
            # Correlation APIs use their first argument for ParamName=..., not
            # for a request label. Add it back to the key/value field map.
            correlation_fields = dict(fields)
            if "=" in first:
                key, value = first.split("=", 1)
                correlation_fields.setdefault(key.strip().lower(), []).append(value.strip())
            snapshot.correlations.append(
                ParsedCorrelation(
                    file_path=relative,
                    transaction=transaction,
                    call_name=call_name,
                    fields=correlation_fields,
                    line=line,
                )
            )


def scan_directory(root: Path) -> ScriptSnapshot:
    snapshot = ScriptSnapshot(root=root)
    if not root.is_dir():
        snapshot.warnings.append(f"{root}: directory does not exist or is not a directory")
        return snapshot
    files = sorted(root.rglob("*.c"))
    snapshot.files_scanned = len(files)
    for path in files:
        parse_file(path, root, snapshot)
    return snapshot


def unique_locations(items: Iterable[ParsedRequest | ParsedCorrelation]) -> str:
    locations = sorted({item.location for item in items})
    if not locations:
        return "none"
    return "; ".join(locations[:5]) + ("; …" if len(locations) > 5 else "")


def endpoint_summary(records: list[ParsedRequest]) -> str:
    transactions = sorted({request.transaction for request in records})
    count = len(records)
    tx_text = ", ".join(transactions[:4]) + ("…" if len(transactions) > 4 else "")
    return f"{count} occurrence(s); transaction(s): {tx_text or '<unscoped>'}"


def compare_snapshots(sit: ScriptSnapshot, ppe: ScriptSnapshot) -> str:
    sit_endpoints = sit.endpoint_records
    ppe_endpoints = ppe.endpoint_records
    sit_keys = set(sit_endpoints)
    ppe_keys = set(ppe_endpoints)
    removed = sorted(ppe_keys - sit_keys)
    added = sorted(sit_keys - ppe_keys)
    common = sorted(sit_keys & ppe_keys)

    structural: list[tuple[str, list[ParsedRequest], list[ParsedRequest], list[str]]] = []
    topology: list[str] = []
    for endpoint in common:
        sit_records = sit_endpoints[endpoint]
        ppe_records = ppe_endpoints[endpoint]
        sit_signatures = {record.signature for record in sit_records}
        ppe_signatures = {record.signature for record in ppe_records}
        if sit_signatures != ppe_signatures:
            notes: list[str] = []
            for sit_record in sit_records:
                for ppe_record in ppe_records:
                    if sit_record.body_kind == ppe_record.body_kind == "json":
                        notes.extend(schema_diff_paths(ppe_record.body_schema, sit_record.body_schema))
            structural.append((endpoint, sit_records, ppe_records, sorted(set(notes))))
        if len(sit_records) != len(ppe_records):
            topology.append(
                f"- `{endpoint}` — SIT {len(sit_records)} occurrence(s), PPE {len(ppe_records)} occurrence(s); "
                "repeated calls are aggregated as one endpoint pattern."
            )
        length_notes: set[str] = set()
        for sit_record in sit_records:
            for ppe_record in ppe_records:
                for path in set(sit_record.array_lengths) & set(ppe_record.array_lengths):
                    left = sit_record.array_lengths[path]
                    right = ppe_record.array_lengths[path]
                    if left != right:
                        length_notes.add(f"`{path}`: SIT {left}, PPE {right}")
        if length_notes:
            topology.append(f"- `{endpoint}` — ignored array-size difference(s): " + ", ".join(sorted(length_notes)) + ".")

    sit_correlations = sit.correlation_records
    ppe_correlations = ppe.correlation_records
    ppe_only_correlations = sorted(set(ppe_correlations) - set(sit_correlations))
    sit_only_correlations = sorted(set(sit_correlations) - set(ppe_correlations))

    lines: list[str] = [
        "# LoadRunner Structural Comparison",
        "",
        f"- **SIT:** `{sit.root}` ({sit.files_scanned} `.c` file(s))",
        f"- **PPE baseline:** `{ppe.root}` ({ppe.files_scanned} `.c` file(s))",
        "- **Comparison model:** URLs are host-agnostic; request values are masked; JSON/XML bodies are compared by schema; repeated requests are aggregated.",
        "",
        "## Summary",
        "",
        f"- 🔴 Deprecated/removed endpoint patterns: **{len(removed)}**",
        f"- 🟢 New endpoint patterns: **{len(added)}**",
        f"- 🟡 Structural/schema changes: **{len(structural)}**",
        f"- Correlation rules in PPE: **{len(ppe_correlations)}**",
        f"- Informational topology notes: **{len(topology)}**",
        "",
        "## 🔴 Deprecated/Removed Endpoints",
        "",
    ]
    if removed:
        lines.extend(f"- `{endpoint}` — {endpoint_summary(ppe_endpoints[endpoint])}" for endpoint in removed)
    else:
        lines.append("None detected.")

    lines.extend(["", "## 🟢 New Endpoints", ""])
    if added:
        lines.extend(f"- `{endpoint}` — {endpoint_summary(sit_endpoints[endpoint])}" for endpoint in added)
    else:
        lines.append("None detected.")

    lines.extend(["", "## 🟡 Structural / Schema Changes", ""])
    if structural:
        for endpoint, sit_records, ppe_records, notes in structural:
            lines.extend(
                [
                    f"### `{endpoint}`",
                    f"- PPE: {endpoint_summary(ppe_records)}",
                    f"- SIT: {endpoint_summary(sit_records)}",
                    f"- PPE locations: {unique_locations(ppe_records)}",
                    f"- SIT locations: {unique_locations(sit_records)}",
                ]
            )
            if notes:
                lines.append("- Schema notes:")
                lines.extend(f"  - {note}" for note in notes[:20])
            else:
                lines.append("- The structural signature changed in a non-JSON field, header, method, parameter name, or body format.")
            lines.append("")
    else:
        lines.append("None detected.")

    lines.extend(["", "## Correlation Audit", "", "### Existing PPE Correlations", ""])
    if ppe_correlations:
        for signature in sorted(ppe_correlations):
            lines.append(f"- `{signature}` — {unique_locations(ppe_correlations[signature])}")
    else:
        lines.append("No `web_reg_save_param_*` rules found in PPE.")

    lines.extend(["", "### Correlations Removed in SIT", ""])
    if ppe_only_correlations:
        lines.extend(f"- `{signature}`" for signature in ppe_only_correlations)
    else:
        lines.append("None detected.")

    lines.extend(["", "### Correlations Added in SIT", ""])
    if sit_only_correlations:
        lines.extend(f"- `{signature}`" for signature in sit_only_correlations)
    else:
        lines.append("None detected.")

    lines.extend(["", "## ℹ️ Data Topology / Iteration Variances", ""])
    if topology:
        lines.extend(topology)
    else:
        lines.append("No ignored occurrence-count or array-size differences detected.")

    warnings = sit.warnings + ppe.warnings
    lines.extend(["", "## Parser Warnings", ""])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("None.")

    lines.extend(
        [
            "",
            "## Interpretation Notes",
            "",
            "- A removed or new endpoint is reported only when its normalized method and path exist in one environment.",
            "- A request body with one JSON array element and a body with hundreds of elements is equivalent when its key/type schema is equivalent.",
            "- Standalone C control flow and custom helper functions are ignored; only `web_*` request/correlation calls are inspected.",
            "- Review the PPE correlation audit before deployment and copy any rules listed as removed in SIT.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare SIT and PPE LoadRunner VuGen scripts by HTTP request structure."
    )
    parser.add_argument("sit_directory", type=Path, help="SIT VuGen script directory")
    parser.add_argument("ppe_directory", type=Path, help="PPE baseline VuGen script directory")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("loadrunner_structural_diff.md"),
        help="Markdown report path (default: loadrunner_structural_diff.md)",
    )
    parser.add_argument(
        "--fail-on-changes",
        action="store_true",
        help="Exit with status 1 if endpoints, structural changes, or missing correlations are found",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sit = scan_directory(args.sit_directory)
    ppe = scan_directory(args.ppe_directory)
    report = compare_snapshots(sit, ppe)
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    except OSError as error:
        print(f"Error: could not write report {args.output}: {error}", file=sys.stderr)
        return 2

    print(f"Wrote Markdown report: {args.output}")
    if args.fail_on_changes:
        endpoints_changed = set(sit.endpoint_records) != set(ppe.endpoint_records)
        structure_changed = any(
            {request.signature for request in sit.endpoint_records[key]}
            != {request.signature for request in ppe.endpoint_records[key]}
            for key in set(sit.endpoint_records) & set(ppe.endpoint_records)
        )
        missing_correlations = bool(set(ppe.correlation_records) - set(sit.correlation_records))
        if endpoints_changed or structure_changed or missing_correlations:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())