#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Vilniaus Blokas UAB
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License only.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Convert MIDI Guide CSV files to a SQLite parameter catalog. Run from this repository's root."""

from __future__ import annotations
import argparse
from collections import Counter
from contextlib import closing
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import unicodedata

CSV_COLUMNS = "manufacturer device section parameter_name parameter_description cc_msb cc_lsb cc_min_value cc_max_value cc_default_value nrpn_msb nrpn_lsb nrpn_min_value nrpn_max_value nrpn_default_value orientation notes usage".split()
TYPES = ("CC", "NRPN")
SEGMENT = re.compile(r"^(\d+)\s*(?:([-~])\s*(\d+))?\s*:\s*(.+)$")
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE manufacturers(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE);
CREATE TABLE products(id INTEGER PRIMARY KEY CHECK(id > 0),manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),source_key TEXT NOT NULL UNIQUE,name TEXT NOT NULL,active INTEGER NOT NULL CHECK(active IN (0,1)),UNIQUE(manufacturer_id,name));
CREATE TABLE product_aliases(product_id INTEGER NOT NULL REFERENCES products(id),alias TEXT NOT NULL,PRIMARY KEY(product_id,alias));
CREATE TABLE source_files(id INTEGER PRIMARY KEY,path TEXT NOT NULL UNIQUE,sha256 TEXT NOT NULL);
CREATE TABLE enums(id INTEGER PRIMARY KEY,usage_raw TEXT NOT NULL UNIQUE,parse_status TEXT NOT NULL CHECK(parse_status IN ('valid','invalid','ambiguous')));
CREATE TABLE enum_values(id INTEGER PRIMARY KEY,enum_id INTEGER NOT NULL REFERENCES enums(id),ordinal INTEGER NOT NULL,value_min INTEGER NOT NULL CHECK(value_min>=0),value_max INTEGER NOT NULL CHECK(value_max>=value_min),kind TEXT NOT NULL CHECK(kind IN ('point','discrete','continuous')),label TEXT NOT NULL,UNIQUE(enum_id,ordinal));
CREATE TABLE parameters(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id),source_file_id INTEGER NOT NULL REFERENCES source_files(id),source_line INTEGER NOT NULL,section TEXT,parameter_name TEXT NOT NULL,parameter_description TEXT,orientation TEXT NOT NULL CHECK(orientation IN ('0-based','centered')),notes TEXT,source_json TEXT NOT NULL,UNIQUE(source_file_id,source_line));
CREATE TABLE bindings(
 id INTEGER PRIMARY KEY,parameter_id INTEGER NOT NULL REFERENCES parameters(id),
 type TEXT NOT NULL CHECK(type IN ('CC','NRPN')),msb INTEGER CHECK(msb BETWEEN 0 AND 127),lsb INTEGER CHECK(lsb BETWEEN 0 AND 127),
 resolution TEXT NOT NULL CHECK(resolution IN ('7_msb','7_lsb','14')),resolution_source TEXT NOT NULL CHECK(resolution_source IN ('derived','inferred','override')),
 min_value INTEGER NOT NULL,max_value INTEGER NOT NULL,default_value INTEGER,min_source TEXT NOT NULL,max_source TEXT NOT NULL,default_source TEXT NOT NULL,
 channel INTEGER CHECK(channel BETWEEN 1 AND 16),update_on TEXT NOT NULL CHECK(update_on IN ('any','msb','lsb')),
 always_emit_select INTEGER NOT NULL CHECK(always_emit_select IN (0,1)),always_emit_both_values INTEGER NOT NULL CHECK(always_emit_both_values IN (0,1)),always_emit_end INTEGER NOT NULL CHECK(always_emit_end IN (0,1)),
 display_min_value TEXT,display_max_value TEXT,display_precision INTEGER CHECK(display_precision>=0),display_unit TEXT,display_curve TEXT,
 enum_id INTEGER REFERENCES enums(id),notes TEXT,verified INTEGER NOT NULL CHECK(verified IN (0,1)),overridden INTEGER NOT NULL CHECK(overridden IN (0,1)),override_reason TEXT,
 CHECK(msb IS NOT NULL OR lsb IS NOT NULL),CHECK(type='CC' OR (msb IS NOT NULL AND lsb IS NOT NULL)),
 CHECK(min_value>=0 AND max_value>=min_value AND max_value<=16383),CHECK(resolution='14' OR max_value<=127),CHECK(default_value IS NULL OR default_value BETWEEN min_value AND max_value));
CREATE INDEX binding_address ON bindings(type,msb,lsb);
CREATE INDEX binding_parameter ON bindings(parameter_id);
CREATE INDEX parameter_product ON parameters(product_id);
CREATE VIEW parameter_picker AS
 SELECT b.*,p.product_id,pr.name AS product_name,m.name AS manufacturer_name,p.section,p.parameter_name,p.parameter_description,p.orientation,p.notes AS parameter_notes,e.usage_raw,e.parse_status AS usage_status,s.path AS source_path,p.source_line,
 (SELECT group_concat(type,',') FROM (SELECT DISTINCT type FROM bindings WHERE parameter_id=p.id ORDER BY type)) AS available_types
 FROM bindings b JOIN parameters p ON p.id=b.parameter_id JOIN products pr ON pr.id=p.product_id JOIN manufacturers m ON m.id=pr.manufacturer_id JOIN source_files s ON s.id=p.source_file_id LEFT JOIN enums e ON e.id=b.enum_id WHERE pr.active=1;
CREATE TABLE search_index(binding_id INTEGER PRIMARY KEY REFERENCES bindings(id),text TEXT NOT NULL);
"""


class CatalogError(ValueError):
	pass


def integer(value, context):
	if value is None or value == "":
		return None
	if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
		raise CatalogError(f"{context}: expected a non-negative integer, got {value!r}")
	return int(value)


def sha(data):
	return hashlib.sha256(data).hexdigest()


def read_json(path, default):
	return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def insert(conn, table, fields):
	return conn.execute(
		f'INSERT INTO {table}({",".join(fields)}) VALUES({",".join("?" for _ in fields)})',
		tuple(fields.values()),
	).lastrowid


def warn(report, code, context, message):
	report["warnings"].append(dict(code=code, context=context, message=message))


def parse_usage(raw, context):
	result = []
	for part in filter(None, (s.strip() for s in raw.split(";"))):
		match = SEGMENT.fullmatch(part)
		if not match:
			raise CatalogError(f"{context}: malformed usage segment {part!r}")
		lo, sep, hi, label = match.groups()
		lo, hi = int(lo), int(hi if hi is not None else lo)
		if lo > hi:
			raise CatalogError(f"{context}: reversed usage range")
		result.append(
			(lo, hi, {None: "point", "-": "discrete", "~": "continuous"}[sep], label)
		)
	return result


def allocate_products(source, registry, overrides):
	if registry.get("version") != 1 or not isinstance(registry.get("products"), dict):
		raise CatalogError("unsupported product registry")
	entries = registry["products"]
	used = {}
	for key, e in entries.items():
		ident = integer(e["id"], key)
		if ident is None or ident < 1 or ident in used:
			raise CatalogError(f"invalid or duplicate product ID: {key}: {ident}")
		e["id"] = ident
		used[ident] = key
		e["active"] = False
	files = sorted(
		p for p in source.glob("*/*.csv") if not p.name.endswith(".triggers.csv")
	)
	if not files:
		raise CatalogError(f"{source}: no manufacturer/product CSV files")
	active = []
	source_keys = {p.relative_to(source).with_suffix("").as_posix() for p in files}
	for path in files:
		key = path.relative_to(source).with_suffix("").as_posix()
		settings = overrides.get(key, {})
		if set(settings) - {"rename_from", "aliases", "bindings", "add_rows"}:
			raise CatalogError(f"{key}: unknown product override fields")
		previous = settings.get("rename_from")
		if previous:
			if previous in source_keys:
				raise CatalogError(
					f"{key}: rename source is still present in CSV input: {previous}"
				)
			if previous in entries:
				if key in entries:
					raise CatalogError(
						f"{key}: rename would merge allocated identities"
					)
				entries[key] = entries.pop(previous)
				entries[key]["aliases"] = sorted(
					set(entries[key]["aliases"] + [previous])
				)
			elif key not in entries or previous not in entries[key].get("aliases", []):
				raise CatalogError(f"{key}: unknown rename source {previous}")
		if key not in entries:
			ident = max((e["id"] for e in entries.values()), default=0) + 1
			entries[key] = dict(
				id=ident,
				manufacturer=path.parent.name,
				name=path.stem,
				aliases=[],
				active=True,
			)
		entry = entries[key]
		entry.update(manufacturer=path.parent.name, name=path.stem, active=True)
		entry["aliases"] = sorted(
			set(entry.get("aliases", []) + settings.get("aliases", []))
		)
		active.append((path, key, entry))
	if set(overrides) - {key for _, key, _ in active}:
		raise CatalogError("overrides target missing products")
	return active


BINDING_FIELDS = set(
	"msb lsb min_value max_value default_value resolution channel update_on always_emit_select always_emit_both_values always_emit_end display_min_value display_max_value display_precision display_unit display_curve usage notes verified".split()
)


def normalize_binding(row, typ, patch, context, report):
	values = {
		k: row.get(f"{typ.lower()}_{k}")
		for k in ("msb", "lsb", "min_value", "max_value", "default_value")
	}
	values.update(
		usage=row.get("usage", ""),
		notes=None,
		channel=None,
		update_on="any",
		always_emit_select=0,
		always_emit_both_values=0,
		always_emit_end=0,
		display_min_value=None,
		display_max_value=None,
		display_precision=None,
		display_unit=None,
		display_curve=None,
		verified=0,
	)
	if set(patch) - BINDING_FIELDS:
		raise CatalogError(
			f"{context}: unknown binding override fields {set(patch)-BINDING_FIELDS}"
		)
	values.update(patch)
	for k in (
		"msb",
		"lsb",
		"min_value",
		"max_value",
		"default_value",
		"channel",
		"display_precision",
	):
		values[k] = integer(values[k], f"{context}:{k}")
	if any(v is not None and not 0 <= v <= 127 for v in (values["msb"], values["lsb"])):
		raise CatalogError(f"{context}: address outside 7-bit range")
	if (
		values["msb"] is None
		and values["lsb"] is None
		or typ != "CC"
		and None in (values["msb"], values["lsb"])
	):
		raise CatalogError(
			f"{context}: missing/incomplete address; use explicit override"
		)
	if "resolution" in patch:
		resolution, provenance = patch["resolution"], "override"
	elif typ == "CC":
		resolution = (
			"14"
			if None not in (values["msb"], values["lsb"])
			else "7_msb" if values["msb"] is not None else "7_lsb"
		)
		provenance = "derived"
	else:
		resolution = (
			"14"
			if any(
				(values[k] or 0) > 127
				for k in ("min_value", "max_value", "default_value")
			)
			else "7_msb"
		)
		provenance = "inferred"
		warn(
			report,
			"inferred_resolution",
			context,
			f"{resolution}: value encoding is not supplied by CSV",
		)
	if resolution not in ("7_msb", "7_lsb", "14"):
		raise CatalogError(f"{context}: invalid resolution")
	if typ == "CC" and (
		(resolution == "14" and None in (values["msb"], values["lsb"]))
		or resolution == "7_msb"
		and values["msb"] is None
		or resolution == "7_lsb"
		and values["lsb"] is None
	):
		raise CatalogError(f"{context}: CC address incompatible with resolution")
	values.update(resolution=resolution, resolution_source=provenance)
	ceiling = 16383 if resolution == "14" else 127
	for k, fallback in [("min_value", 0), ("max_value", ceiling)]:
		values[k.split("_")[0] + "_source"] = (
			"override" if k in patch else "csv" if values[k] is not None else "inferred"
		)
		if values[k] is None:
			values[k] = fallback
			warn(report, "inferred_bound", context, f"{k}={fallback}")
	values["default_source"] = (
		"override"
		if "default_value" in patch
		else "csv" if values["default_value"] is not None else "unknown"
	)
	lo, hi, default = (values[k] for k in ("min_value", "max_value", "default_value"))
	if not 0 <= lo <= hi <= ceiling or default is not None and not lo <= default <= hi:
		raise CatalogError(
			f"{context}: bounds/default incompatible with {resolution}: {lo}, {hi}, {default}"
		)
	if resolution == "14" and hi <= 127:
		warn(
			report,
			"suspicious_14bit_range",
			context,
			"maximum <=127 preserved; requires review",
		)
	if (
		values["channel"] is not None
		and not 1 <= values["channel"] <= 16
		or values["update_on"] not in ("any", "msb", "lsb")
	):
		raise CatalogError(f"{context}: invalid channel/update_on")
	for k in (
		"always_emit_select",
		"always_emit_both_values",
		"always_emit_end",
		"verified",
	):
		if values[k] not in (0, 1):
			raise CatalogError(f"{context}: {k} must be boolean")
		values[k] = int(values[k])
	for k in ("display_min_value", "display_max_value"):
		if values[k] is not None:
			try:
				if not Decimal(str(values[k])).is_finite():
					raise InvalidOperation
			except InvalidOperation as exc:
				raise CatalogError(f"{context}: {k} must be finite decimal") from exc
			values[k] = str(values[k])
	if (values["display_min_value"] is None) != (values["display_max_value"] is None):
		raise CatalogError(f"{context}: display bounds must be supplied together")
	if values["display_min_value"] is not None:
		values["display_curve"] = values["display_curve"] or "linear"
	if values["display_curve"] not in (None, "linear"):
		raise CatalogError(
			f"{context}: nonlinear display mapping needs an implemented formula"
		)
	return values


def usage_id(conn, raw, context, report, lo, hi):
	if not raw:
		return None
	try:
		segments = parse_usage(raw, context)
		status = "valid"
	except CatalogError as exc:
		segments = []
		status = "invalid"
		warn(report, "usage_parse_error", context, str(exc))
	end = -1
	gap = False
	overlap = False
	for mn, mx, _, _ in sorted(segments):
		if mn <= end:
			overlap = True
		if mn > max(end + 1, lo):
			gap = True
		end = max(end, mx)
	if overlap:
		status = "ambiguous"
		warn(report, "usage_overlap", context, "overlapping segments: ambiguous lookup")
	if gap or end < hi:
		warn(report, "usage_gap", context, "undocumented gaps preserved")
	if any(mn < lo or mx > hi for mn, mx, _, _ in segments):
		warn(
			report,
			"usage_outside_bounds",
			context,
			"preserved source ranges exceed binding bounds",
		)
	found = conn.execute("SELECT id FROM enums WHERE usage_raw=?", (raw,)).fetchone()
	if found:
		return found[0]
	ident = insert(conn, "enums", dict(usage_raw=raw, parse_status=status))
	for ordinal, (mn, mx, kind, label) in enumerate(segments):
		insert(
			conn,
			"enum_values",
			dict(
				enum_id=ident,
				ordinal=ordinal,
				value_min=mn,
				value_max=mx,
				kind=kind,
				label=label,
			),
		)
	return ident


def build_database(conn, active, registry, overrides, report):
	conn.executescript(SCHEMA)
	mids = {
		name: insert(conn, "manufacturers", dict(name=name))
		for name in sorted({e["manufacturer"] for e in registry["products"].values()})
	}
	for key, e in sorted(registry["products"].items(), key=lambda p: p[1]["id"]):
		insert(
			conn,
			"products",
			dict(
				id=e["id"],
				manufacturer_id=mids[e["manufacturer"]],
				source_key=key,
				name=e["name"],
				active=int(e["active"]),
			),
		)
		for alias in e["aliases"]:
			insert(conn, "product_aliases", dict(product_id=e["id"], alias=alias))
	for path, key, e in active:
		settings = overrides.get(key, {})
		fid = insert(
			conn, "source_files", dict(path=key + ".csv", sha256=sha(path.read_bytes()))
		)
		with path.open(newline="", encoding="utf-8-sig") as fh:
			reader = csv.DictReader(fh)
			if (
				reader.fieldnames is None
				or len(reader.fieldnames) != len(CSV_COLUMNS)
				or set(reader.fieldnames) != set(CSV_COLUMNS)
			):
				raise CatalogError(f"{path}: unexpected CSV columns")
			rows = []
			while True:
				line = reader.line_num + 1
				try:
					row = next(reader)
				except StopIteration:
					break
				if None in row or None in row.values():
					raise CatalogError(f"{path}:{line}: incorrect column count")
				rows.append((line, {k: v.strip() for k, v in row.items()}))
			next_line = reader.line_num + 1
		for i, row in enumerate(settings.get("add_rows", [])):
			if set(row) - set(CSV_COLUMNS):
				raise CatalogError(f"{key}: unknown added-row fields")
			rows.append((next_line + i, dict(row)))
		rules = settings.get("bindings", [])
		matches = [0] * len(rules)
		for rule in rules:
			if (
				set(rule) != {"match", "set", "reason"}
				or not rule["reason"].strip()
				or not rule["match"]
				or set(rule["match"])
				- {"parameter_name", "section", "type", "source_line", "msb", "lsb"}
			):
				raise CatalogError(f"{key}: invalid binding rule")
		for line, row in rows:
			ctx = f"{key}.csv:{line}"
			name = row.get("parameter_name", "")
			if not name:
				raise CatalogError(f"{ctx}: parameter_name required")
			for field, expected in [
				("manufacturer", e["manufacturer"]),
				("device", e["name"]),
			]:
				if row.get(field) and row[field] != expected:
					warn(
						report,
						"source_name_mismatch",
						ctx,
						f"{field}={row[field]!r}; path={expected!r}",
					)
			orientation = row.get("orientation") or "0-based"
			if orientation not in ("0-based", "centered"):
				raise CatalogError(f"{ctx}: invalid orientation")
			pid = insert(
				conn,
				"parameters",
				dict(
					product_id=e["id"],
					source_file_id=fid,
					source_line=line,
					section=row.get("section") or None,
					parameter_name=name,
					parameter_description=row.get("parameter_description") or None,
					orientation=orientation,
					notes=row.get("notes") or None,
					source_json=json.dumps(row, ensure_ascii=False, sort_keys=True),
				),
			)
			found = False
			for typ in TYPES:
				if not any(
					row.get(f"{typ.lower()}_{part}") not in (None, "")
					for part in ("msb", "lsb")
				):
					continue
				found = True
				base = dict(
					row,
					type=typ,
					source_line=line,
					msb=integer(row.get(f"{typ.lower()}_msb"), ctx),
					lsb=integer(row.get(f"{typ.lower()}_lsb"), ctx),
				)
				patch = {}
				reasons = []
				for i, rule in enumerate(rules):
					if all(base.get(k) == v for k, v in rule["match"].items()):
						matches[i] += 1
						if set(patch) & set(rule["set"]):
							raise CatalogError(
								f"{ctx}: conflicting override assignments"
							)
						patch.update(rule["set"])
						reasons.append(rule["reason"])
				values = normalize_binding(row, typ, patch, f"{ctx}:{typ}", report)
				raw = values.pop("usage")
				eid = usage_id(
					conn, raw, ctx, report, values["min_value"], values["max_value"]
				)
				bid = insert(
					conn,
					"bindings",
					dict(
						parameter_id=pid,
						type=typ,
						**values,
						enum_id=eid,
						overridden=int(bool(reasons)),
						override_reason="; ".join(reasons) or None,
					),
				)
				terms = [
					e["manufacturer"],
					e["name"],
					*e["aliases"],
					row.get("section"),
					name,
					row.get("parameter_description"),
					row.get("notes"),
					values.get("notes"),
					raw,
					typ,
				]
				insert(
					conn,
					"search_index",
					dict(
						binding_id=bid,
						text=unicodedata.normalize(
							"NFC", " ".join(str(t) for t in terms if t)
						).casefold(),
					),
				)
			if not found:
				warn(
					report,
					"no_binding",
					ctx,
					"row retained, not selectable: missing address",
				)
		for i, count in enumerate(matches):
			if count != 1:
				raise CatalogError(
					f"{key}: override {i} matched {count} bindings; expected exactly one"
				)
	for typ, msb, lsb, product, count in conn.execute(
		"SELECT b.type,b.msb,b.lsb,p.product_id,count(*) FROM bindings b JOIN parameters p ON p.id=b.parameter_id GROUP BY p.product_id,b.type,b.msb,b.lsb HAVING count(*)>1"
	):
		warn(
			report,
			"shared_address",
			f"product:{product}:{typ}:{msb}:{lsb}",
			f"{count} entries preserved; reverse lookup needs context",
		)
	report["counts"] = {
		t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
		for t in ("products", "parameters", "bindings", "enums", "enum_values")
	}
	report["counts"]["active_products"] = len(active)
	report["warning_counts"] = dict(
		sorted(Counter(w["code"] for w in report["warnings"]).items())
	)


def atomic_json(path, data):
	path.parent.mkdir(parents=True, exist_ok=True)
	fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as fh:
			json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
			fh.write("\n")
		os.replace(temp, path)
	finally:
		if os.path.exists(temp):
			os.unlink(temp)


def build(source, output, registry_path, overrides_path, report_path):
	source, output, registry_path, overrides_path, report_path = map(
		Path, (source, output, registry_path, overrides_path, report_path)
	)
	if (
		len({p.resolve() for p in (output, registry_path, overrides_path, report_path)})
		!= 4
	):
		raise CatalogError(
			"output, registry, overrides and report must have distinct paths"
		)
	if not registry_path.is_file():
		raise CatalogError(
			f"{registry_path}: product registry is required; restore it from version control"
		)
	registry = json.loads(registry_path.read_text(encoding="utf-8"))
	overrides = read_json(overrides_path, dict(version=1, products={}))
	if overrides.get("version") != 1 or set(overrides) != {"version", "products"}:
		raise CatalogError("unsupported overrides document")
	active = allocate_products(
		source, registry, overrides["products"]
	)
	output.parent.mkdir(parents=True, exist_ok=True)
	fd, temp = tempfile.mkstemp(prefix=output.name + ".", dir=output.parent)
	os.close(fd)
	report = dict(warnings=[])
	try:
		with closing(sqlite3.connect(temp)) as conn, conn:
			build_database(conn, active, registry, overrides["products"], report)
			revision = subprocess.run(
				["git", "-C", str(source), "rev-parse", "HEAD"],
				capture_output=True,
				text=True,
				check=False,
			)
			metadata = dict(
				schema_version="1",
				generator_version="1.0",
				source_revision=(
					revision.stdout.strip() if revision.returncode == 0 else "unknown"
				),
				registry_sha256=sha(json.dumps(registry, sort_keys=True).encode()),
				overrides_sha256=sha(json.dumps(overrides, sort_keys=True).encode()),
				product_id_zero="custom",
				binding_ids="build-local",
				license_name="Creative Commons Attribution-ShareAlike 4.0 International",
				license_spdx="CC-BY-SA-4.0",
				license_url="https://creativecommons.org/licenses/by-sa/4.0/",
				license_text=(
					Path(__file__).parent / "licenses/CC-BY-SA-4.0.txt"
				).read_text(encoding="utf-8"),
				attribution="MIDI Guide, maintained by Pencil Research and contributors",
				source_url="https://github.com/pencilresearch/midi",
				project_url="https://midi.guide/",
				modification_notice="SQLite conversion, stable IDs, inferred encodings, and local corrections by Vilniaus Blokas UAB; database adaptation licensed CC BY-SA 4.0.",
				licensing_notice=(Path(__file__).parent / "NOTICE.txt").read_text(
					encoding="utf-8"
				),
			)
			conn.executemany("INSERT INTO metadata VALUES(?,?)", metadata.items())
			conn.execute("PRAGMA user_version=1")
			if (
				conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
				or conn.execute("PRAGMA foreign_key_check").fetchall()
			):
				raise CatalogError("SQLite integrity validation failed")
		atomic_json(report_path, report)
		# Reserve IDs before publishing: interruption may waste IDs, never reuse one.
		atomic_json(registry_path, registry)
		os.chmod(
			temp, 0o644
		)  # Public catalog artifact; readable inside packaged applications.
		os.replace(temp, output)
	finally:
		if os.path.exists(temp):
			os.unlink(temp)
	return report


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	for name, default in [
		("source", "midi"),
		("output", "ccnrpn.db"),
		("registry", "product_ids.json"),
		("overrides", "overrides.json"),
		("report", "build-report.json"),
	]:
		parser.add_argument("--" + name, type=Path, default=Path(default))
	args = parser.parse_args(argv)
	try:
		report = build(
			args.source, args.output, args.registry, args.overrides, args.report
		)
	except (CatalogError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
		parser.exit(1, f"Build failed: {exc}\n")
	print(
		f"Built {args.output}: {report['counts']}\nReview warnings in {args.report}: {report['warning_counts']}"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
