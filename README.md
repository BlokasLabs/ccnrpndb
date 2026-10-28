# MIDI CSV-to-SQLite catalog

Convert [Pencil Research MIDI Guide](https://github.com/pencilresearch/midi) CSVs
into `ccnrpn.db`, a SQLite catalog of MIDI CC and NRPN parameters.

## Build

Requires Git and Python 3.9+; no additional Python packages are needed. From the
repository root:

```sh
git submodule update --init
python3 csv2sqlite.py
```

The build uses the pinned `midi` revision and produces:

- `ccnrpn.db` — generated database; never used as input.
- `product_ids.json` — persistent product registry, updated for new/retired products.
- `build-report.json` — counts and warnings about source data and inferred values.

Commit registry changes along with source updates. Review build warnings before
using the data. Invalid input stops the build without replacing the database.
Run one build at a time.

Use `python3 csv2sqlite.py --help` for custom input/output paths. Input files must
follow `<manufacturer>/<product>.csv`; templates and `*.triggers.csv` are excluded.
Use [overrides.json](overrides.json) for local corrections without editing upstream
CSVs. Binding overrides require a reason and must match exactly one binding.
Product renames require an explicit `rename_from` override to preserve their ID.

## Database use and ID stability

`parameter_picker` joins products, parameters, bindings, and source information.
`search_index` provides searchable text. The database also retains original row
data, provenance, usage labels, and license notices. Check `PRAGMA user_version`
for schema compatibility; the initial schema version is **1**.

**Only product IDs persist across builds.** `product_ids.json` is their sole source;
a missing registry fails the build. Never reset it or reuse retired IDs. Product
IDs are positive SQLite integers; zero is reserved for an unspecified product.

All other row IDs—including manufacturer, parameter, binding, enum, enum-value,
and source-file IDs—are build-local and may identify different entries after a
rebuild. Foreign keys are valid only within the same database snapshot. Invalidate
caches when replacing the database.

For cross-build lookups, use product ID, protocol, address, and resolution, and
handle missing or ambiguous matches. This combination is not a guaranteed unique
parameter identifier. Bounds and resolution may be inferred; consult provenance
fields, notes, and the build report rather than assuming device verification.

## Automatic releases

The [GitHub workflow](.github/workflows/update-catalog.yml) checks upstream every
six hours. It exits before tests/builds when neither repository has changed and
the current catalog already has a published release. Otherwise, it builds,
commits the source pin and registry together, and publishes a release tagged
`catalog-<commit SHA>`. Incomplete releases are retried.

Releases include the database, registry, overrides, build report, license notices,
and `SHA256SUMS`. The latest completed release is marked **Latest**.

## Tests

```sh
python3 -B -m unittest -v
python3 -B -m unittest discover -s tests -v
```

## Licensing

Copyright (C) 2026 Vilniaus Blokas UAB.

- **Code, tests, automation, and original documentation:**
  [GPL-3.0-only](LICENSE).
- **Generated database, product registry, overrides, and catalog reports:**
  [CC BY-SA 4.0](licenses/CC-BY-SA-4.0.txt), with attribution in [NOTICE.txt](NOTICE.txt).
- **Upstream CSVs:** retain [their license](midi/LICENSE) and
  [notices](midi/README.md), including the device-manufacturer ownership caveat.
  Third-party license texts and notices retain their own terms.

The converter's GPL license does not apply to its catalog output. Include the
applicable attribution and license notices when redistributing the data.
