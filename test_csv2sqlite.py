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

"""Regression tests for catalog identity, lossless conversion and publication."""

import csv
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import csv2sqlite as catalog


class CatalogTests(unittest.TestCase):
	def setUp(self):
		self.temporary = tempfile.TemporaryDirectory()
		self.addCleanup(self.temporary.cleanup)
		self.root = Path(self.temporary.name)
		self.source = self.root / "source"
		self.output = self.root / "ccnrpn.db"
		self.registry = self.root / "ids.json"
		self.registry.write_text(json.dumps(dict(version=1, products={})))
		self.overrides = self.root / "overrides.json"
		self.report = self.root / "report.json"

	def write_product(self, key="Maker/Synth", rows=None):
		path = self.source / (key + ".csv")
		path.parent.mkdir(parents=True, exist_ok=True)
		with path.open("w", newline="") as fh:
			writer = csv.DictWriter(fh, fieldnames=catalog.CSV_COLUMNS)
			writer.writeheader()
			for row in (
				rows
				if rows is not None
				else [{"parameter_name": "Cutoff", "cc_msb": 74}]
			):
				writer.writerow(
					dict(manufacturer=key.split("/")[0], device=path.stem, **row)
				)
		return path

	def build(self):
		return catalog.build(
			self.source, self.output, self.registry, self.overrides, self.report
		)

	def query(self, sql):
		with closing(sqlite3.connect(self.output)) as conn, conn:
			return conn.execute(sql).fetchall()

	def set_overrides(self, products):
		self.overrides.write_text(json.dumps(dict(version=1, products=products)))

	def test_shared_addresses_and_dual_protocols_are_preserved_on_rebuild(self):
		rows = [
			dict(
				parameter_name="Cutoff",
				section="Mode A",
				cc_msb=74,
				nrpn_msb=1,
				nrpn_lsb=2,
				nrpn_max_value=16383,
			),
			dict(parameter_name="Cutoff", section="Mode B", cc_msb=74),
		]
		self.write_product(rows=rows)
		for _ in range(2):
			self.build()
			self.assertEqual(self.query("SELECT count(*) FROM bindings"), [(3,)])
			self.assertEqual(self.query("SELECT count(*) FROM parameters"), [(2,)])
			self.assertEqual(
				self.query(
					"SELECT DISTINCT available_types FROM parameter_picker WHERE section='Mode A'"
				),
				[("CC,NRPN",)],
			)
		rows[0]["nrpn_lsb"] = 3
		self.write_product(rows=rows)
		self.build()
		self.assertEqual(
			self.query("SELECT count(*) FROM bindings WHERE type='CC'"), [(2,)]
		)
		self.assertEqual(
			self.query("SELECT lsb FROM bindings WHERE type='NRPN'"), [(3,)]
		)

	def test_incomplete_nrpn_addresses_are_reported_and_omitted(self):
		notes = "NRPN LSB is the drum instrument note number 12-108 (C0-C8)."
		self.write_product(rows=[
			dict(parameter_name="Drum cutoff", nrpn_msb=20, notes=notes),
			dict(parameter_name="Missing MSB", nrpn_lsb=12),
			dict(parameter_name="Fixed control", nrpn_msb=1, nrpn_lsb=2),
			dict(parameter_name="Dual control", cc_msb=74, nrpn_msb=21, notes=notes),
		])
		self.build()
		self.assertEqual(self.query("SELECT parameter_name FROM parameters ORDER BY id"),
			[("Fixed control",), ("Dual control",)])
		self.assertEqual(self.query("SELECT type,msb,lsb FROM bindings ORDER BY id"),
			[("NRPN", 1, 2), ("CC", 74, None)])
		self.assertEqual(self.query("SELECT count(*) FROM search_index"), [(2,)])
		report = json.loads(self.report.read_text())
		self.assertEqual(report["warning_counts"]["incomplete_nrpn_address"], 3)
		warnings = [w for w in report["warnings"] if w["code"] == "incomplete_nrpn_address"]
		self.assertEqual(warnings[0]["context"], "Maker/Synth.csv:2:NRPN")
		self.assertIn("Drum cutoff", warnings[0]["message"])
		self.assertIn("MSB=20, LSB=None", warnings[0]["message"])
		self.assertIn(notes, warnings[0]["message"])
		self.assertNotIn("no_binding", report["warning_counts"])

	def test_override_can_supply_a_concrete_nrpn_address(self):
		self.write_product(rows=[dict(parameter_name="Drum cutoff", nrpn_msb=20)])
		self.set_overrides({"Maker/Synth": {"bindings": [{
			"match": {"parameter_name": "Drum cutoff", "type": "NRPN"},
			"set": {"lsb": 36}, "reason": "Select one specific drum note",
		}]}})
		self.build()
		self.assertEqual(self.query("SELECT msb,lsb,overridden FROM bindings"), [(20, 36, 1)])
		self.assertNotIn("incomplete_nrpn_address", json.loads(self.report.read_text())["warning_counts"])

	def test_invalid_nrpn_addresses_and_overrides_still_fail(self):
		self.write_product()
		self.build()
		before = self.output.read_bytes(), self.registry.read_bytes()
		self.write_product(rows=[dict(parameter_name="Bad address", nrpn_msb=128)])
		with self.assertRaisesRegex(catalog.CatalogError, "outside 7-bit"):
			self.build()
		self.write_product(rows=[dict(parameter_name="Bad address", nrpn_msb="note number")])
		with self.assertRaises(catalog.CatalogError):
			self.build()
		self.write_product(rows=[dict(parameter_name="Bad address", nrpn_msb=20)])
		self.set_overrides({"Maker/Synth": {"bindings": [{
			"match": {"parameter_name": "Bad address", "type": "NRPN"},
			"set": {"lsb": None}, "reason": "Invalid incomplete override",
		}]}})
		with self.assertRaisesRegex(catalog.CatalogError, "missing/incomplete address"):
			self.build()
		self.assertEqual((self.output.read_bytes(), self.registry.read_bytes()), before)

	def test_registry_preserves_ids_independently_of_output(self):
		self.write_product("Z/Z")
		self.build()
		self.write_product("A/A")
		self.build()
		expected = [("Z", 1), ("A", 2)]
		self.assertEqual(self.query("SELECT name,id FROM products ORDER BY id"), expected)
		registry = self.registry.read_bytes()
		# Neither absent nor corrupt output affects the generated result.
		for contents in (None, b"not a database"):
			with self.subTest(contents=contents):
				self.output.unlink()
				if contents is not None:
					self.output.write_bytes(contents)
				self.build()
				self.assertEqual(
					self.query("SELECT name,id FROM products ORDER BY id"), expected
				)
				self.assertEqual(self.registry.read_bytes(), registry)
		# Conflicting identities in an old database are ignored too.
		with closing(sqlite3.connect(self.output)) as conn, conn:
			conn.execute("UPDATE products SET id=id+100")
		self.build()
		self.assertEqual(self.query("SELECT name,id FROM products ORDER BY id"), expected)
		self.assertEqual(self.registry.read_bytes(), registry)

	def test_missing_registry_fails_even_with_existing_database(self):
		self.write_product()
		self.build()
		before = self.output.read_bytes(), self.report.read_bytes()
		self.registry.unlink()
		with self.assertRaisesRegex(catalog.CatalogError, "product registry is required"):
			self.build()
		self.assertEqual((self.output.read_bytes(), self.report.read_bytes()), before)
		self.assertFalse(self.registry.exists())
		self.output.unlink()
		with self.assertRaisesRegex(catalog.CatalogError, "product registry is required"):
			self.build()
		self.assertFalse(self.output.exists())

	def test_renames_retirement_and_new_products_do_not_reuse_ids(self):
		old = self.write_product()
		self.build()
		new = old.with_name("New.csv")
		old.rename(new)
		self.set_overrides(
			{"Maker/New": {"rename_from": "Maker/Synth", "aliases": ["Old synth"]}}
		)
		self.build()
		self.build()  # Persistent rename instruction is idempotent.
		self.assertEqual(self.query("SELECT id,name FROM products"), [(1, "New")])
		self.set_overrides({})
		new.unlink()
		self.write_product("Maker/Other")
		self.build()
		self.assertEqual(
			self.query("SELECT id,active FROM products ORDER BY id"), [(1, 0), (2, 1)]
		)

	def test_enum_kinds_gaps_defaults_and_midi_value_domain(self):
		self.write_product(
			rows=[
				dict(
					parameter_name="Mode",
					cc_msb=74,
					cc_default_value=32,
					usage="0: Off; 16-31: Square; 32~63: Morph",
				)
			]
		)
		report = self.build()
		self.assertEqual(
			self.query("SELECT kind FROM enum_values ORDER BY ordinal"),
			[("point",), ("discrete",), ("continuous",)],
		)
		self.assertEqual(
			self.query(
				"SELECT default_value,min_value,max_value FROM parameter_picker"
			),
			[(32, 0, 127)],
		)
		self.assertIn("usage_gap", report["warning_counts"])
		self.assertEqual(self.query("SELECT max(value_max) FROM enum_values"), [(63,)])

	def test_malformed_and_overlapping_usage_have_explicit_status(self):
		self.write_product(
			rows=[
				dict(parameter_name="Bad", cc_msb=1, usage="1-0: Reverse"),
				dict(parameter_name="Overlap", cc_msb=2, usage="0-2: A; 2-3: B"),
			]
		)
		self.build()
		self.assertEqual(
			self.query(
				"SELECT parameter_name,usage_status FROM parameter_picker ORDER BY id"
			),
			[("Bad", "invalid"), ("Overlap", "ambiguous")],
		)
		self.assertEqual(self.query("SELECT count(*) FROM enum_values"), [(2,)])

	def test_override_resolution_display_transport_and_cc_additions(self):
		self.write_product(
			rows=[
				dict(
					parameter_name="Cutoff", nrpn_msb=1, nrpn_lsb=2, nrpn_max_value=127
				)
			]
		)
		self.set_overrides(
			{
				"Maker/Synth": {
					"bindings": [
						dict(
							match=dict(parameter_name="Cutoff", type="NRPN"),
							set=dict(
								resolution="7_lsb",
								channel=10,
								display_min_value="-12.00",
								display_max_value="12.00",
								display_precision=2,
								always_emit_select=True,
								verified=True,
							),
							reason="Test fixture: explicit device mapping",
						)
					],
					"add_rows": [
						dict(
							parameter_name="Resonance",
							cc_msb=71,
							cc_max_value=127,
						)
					],
				}
			}
		)
		self.build()
		self.assertEqual(
			self.query(
				"SELECT resolution,resolution_source,channel,display_min_value,display_curve,verified FROM bindings WHERE type='NRPN'"
			),
			[("7_lsb", "override", 10, "-12.00", "linear", 1)],
		)
		self.assertEqual(
			self.query("SELECT msb,max_value FROM bindings WHERE type='CC'"), [(71, 127)]
		)

	def test_unsupported_protocol_fields_are_rejected(self):
		self.write_product()
		self.build()
		before = self.output.read_bytes(), self.registry.read_bytes()
		for field in ("rpn_msb", "rpn_lsb", "rpn_min_value",
						"rpn_max_value", "rpn_default_value"):
			with self.subTest(field=field):
				self.set_overrides({"Maker/Synth": {"add_rows": [
					{"parameter_name": "Unsupported", "cc_msb": 71, field: 0}
				]}})
				with self.assertRaisesRegex(catalog.CatalogError, "unknown added-row fields"):
					self.build()
				self.assertEqual(
					(self.output.read_bytes(), self.registry.read_bytes()), before
				)
		with self.assertRaises(sqlite3.IntegrityError):
			self.query("UPDATE bindings SET type='RPN'")

	def test_failure_preserves_previous_database_and_registry(self):
		self.write_product()
		self.build()
		before = self.output.read_bytes(), self.registry.read_bytes()
		self.set_overrides(
			{
				"Maker/Synth": {
					"bindings": [
						dict(
							match=dict(parameter_name="Missing"),
							set=dict(resolution="14"),
							reason="stale rule",
						)
					]
				}
			}
		)
		with self.assertRaisesRegex(catalog.CatalogError, "matched 0"):
			self.build()
		self.assertEqual((self.output.read_bytes(), self.registry.read_bytes()), before)
		self.assertEqual(list(self.root.glob("ccnrpn.db.*")), [])

	def test_product_allocation_exceeds_old_limit_and_rejects_invalid_numbers(self):
		self.write_product()
		self.registry.write_text(
			json.dumps(
				dict(
					version=1,
					products={
						"Retired/Last": dict(
							id=2047,
							manufacturer="Retired",
							name="Last",
							aliases=[],
							active=False,
						)
					},
				)
			)
		)
		self.build()
		self.assertEqual(
			self.query("SELECT id,active FROM products ORDER BY id"),
			[(2047, 0), (2048, 1)],
		)
		self.output.unlink()
		self.build()
		self.assertEqual(
			self.query("SELECT id,active FROM products ORDER BY id"),
			[(2047, 0), (2048, 1)],
		)
		self.registry.write_text(json.dumps(dict(version=1, products={})))
		self.write_product(rows=[dict(parameter_name="Broken", cc_msb="not-a-number")])
		with self.assertRaisesRegex(catalog.CatalogError, "integer"):
			self.build()

	def test_ambiguous_override_and_duplicate_registry_ids_fail(self):
		self.write_product(
			rows=[
				dict(parameter_name="Same", cc_msb=1),
				dict(parameter_name="Same", cc_msb=2),
			]
		)
		self.set_overrides(
			{
				"Maker/Synth": {
					"bindings": [
						dict(
							match=dict(parameter_name="Same"),
							set=dict(channel=10),
							reason="ambiguous",
						)
					]
				}
			}
		)
		with self.assertRaisesRegex(catalog.CatalogError, "matched 2"):
			self.build()
		self.set_overrides({})
		self.build()
		registry = json.loads(self.registry.read_text())
		registry["products"]["Maker/Duplicate"] = dict(registry["products"]["Maker/Synth"])
		self.registry.write_text(json.dumps(registry))
		before = self.output.read_bytes()
		with self.assertRaisesRegex(catalog.CatalogError, "duplicate product ID"):
			self.build()
		self.assertEqual(self.output.read_bytes(), before)

	def test_override_can_correct_invalid_source_bound(self):
		self.write_product(
			rows=[dict(parameter_name="Cutoff", cc_msb=74, cc_max_value="incorrect")]
		)
		self.set_overrides(
			{
				"Maker/Synth": {
					"bindings": [
						dict(
							match=dict(parameter_name="Cutoff", type="CC"),
							set=dict(max_value=127),
							reason="Correct invalid source bound",
						)
					]
				}
			}
		)
		self.build()
		self.assertEqual(
			self.query("SELECT max_value,max_source FROM bindings"), [(127, "override")]
		)
		self.assertIn(
			"incorrect", self.query("SELECT source_json FROM parameters")[0][0]
		)

	def test_embedded_license_and_large_stable_ids(self):
		self.write_product()
		self.registry.write_text(
			json.dumps(
				dict(
					version=1,
					products={
						"Maker/Synth": dict(
							id=4294967296,
							manufacturer="Maker",
							name="Synth",
							aliases=[],
							active=True,
						)
					},
				)
			)
		)
		self.build()
		self.assertEqual(self.query("SELECT id FROM products"), [(4294967296,)])
		metadata = dict(self.query("SELECT key,value FROM metadata"))
		self.assertNotIn("product_id_bits", metadata)
		self.assertEqual(metadata["product_id_zero"], "custom")
		self.assertEqual(metadata["license_spdx"], "CC-BY-SA-4.0")
		self.assertEqual(self.output.stat().st_mode & 0o777, 0o644)
		self.assertIn("Section 3", metadata["license_text"])
		self.assertIn("Pencil Research", metadata["attribution"])
		self.assertIn("local corrections", metadata["modification_notice"])
		self.assertTrue(metadata["source_url"].startswith("https://github.com/"))
		self.assertEqual(self.query("PRAGMA user_version"), [(1,)])

	def test_templates_and_triggers_are_not_imported(self):
		self.write_product()
		(self.source / "template.csv").write_text("invalid template")
		(self.source / "Maker" / "Synth.triggers.csv").write_text("different columns")
		self.build()
		self.assertEqual(self.query("SELECT count(*) FROM products"), [(1,)])


if __name__ == "__main__":
	unittest.main()
