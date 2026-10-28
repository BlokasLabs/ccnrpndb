#!/usr/bin/env bash
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

# Run in a clean checkout with an authenticated origin and GitHub CLI.
set -euo pipefail

case "${1:-}" in
    '') check_only=false ;;
    --check) check_only=true ;;
    *) echo 'Usage: update-catalog.sh [--check]' >&2; exit 2 ;;
esac

: "${CATALOG_BRANCH:?Set CATALOG_BRANCH to the repository default branch}"
: "${GH_REPO:?Set GH_REPO to the GitHub owner/repository}"
cd "$(git rev-parse --show-toplevel)"
if [[ -n "$(git status --porcelain)" ]]; then
    echo 'Publication requires a clean checkout.' >&2
    exit 1
fi

# Follow the upstream default branch without assuming its name or executing
# anything from the upstream repository. Only csv2sqlite.py reads its CSV data.
git submodule update --init -- midi
git -C midi fetch --no-tags origin HEAD
upstream_revision=$(git -C midi rev-parse FETCH_HEAD)
catalog_revision=$(git rev-parse HEAD)
needed=true
if [[ "$upstream_revision" == "$(git rev-parse HEAD:midi)" ]]; then
    # The tag encodes this repository's exact revision. A completed release at
    # that tag proves both inputs were already published. Missing/draft releases
    # must continue so first-time publication and interrupted runs are retried.
    if draft=$(gh release view "catalog-$catalog_revision" --json isDraft --jq '.isDraft'); then
        if [[ "$draft" == false ]]; then
            needed=false
        fi
    fi
fi
if [[ "$check_only" == true ]]; then
    echo "Publication needed: $needed"
    if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
        echo "needed=$needed" >> "$GITHUB_OUTPUT"
    fi
    exit 0
fi
if [[ "$needed" == false ]]; then
    echo "No changes; already published: catalog-$catalog_revision"
    exit 0
fi
git -C midi checkout --detach "$upstream_revision"

python3 -B -m unittest -v test_csv2sqlite
python3 -B csv2sqlite.py

# Commit the registry and source pin together. Generated DB/report stay ignored.
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
git add -- midi product_ids.json
if ! git diff --cached --quiet; then
    git commit -m "Update MIDI catalog to $upstream_revision"
fi
catalog_revision=$(git rev-parse HEAD)
tag="catalog-$catalog_revision"
if git show-ref --verify --quiet "refs/tags/$tag"; then
    [[ "$(git rev-parse "$tag^{commit}")" == "$catalog_revision" ]]
else
    git tag "$tag" "$catalog_revision"
fi

# Never rebase or force-push allocated IDs. A concurrent branch update rejects
# both ref updates; a fresh run will build again from the latest registry.
git push --atomic origin "HEAD:refs/heads/$CATALOG_BRANCH" "refs/tags/$tag"

# A retry may find a complete release or an interrupted draft. Published assets
# are immutable to this script; only draft assets can be replaced.
if draft=$(gh release view "$tag" --json isDraft --jq '.isDraft'); then
    if [[ "$draft" == false ]]; then
        echo "Already published: $tag"
        exit 0
    fi
    [[ "$draft" == true ]]
else
    # If view failed for a reason other than a missing release, create will also
    # fail safely rather than overwrite an existing release.
    gh release create "$tag" --verify-tag --draft --title "MIDI catalog $catalog_revision"
fi

assets=$(mktemp -d)
trap 'rm -rf "$assets"' EXIT
cp ccnrpn.db product_ids.json overrides.json build-report.json NOTICE.txt "$assets/"
cp licenses/CC-BY-SA-4.0.txt "$assets/"
cp midi/LICENSE "$assets/upstream-LICENSE.txt"
cp midi/README.md "$assets/upstream-README.md"
(
    cd "$assets"
    sha256sum ccnrpn.db product_ids.json overrides.json build-report.json NOTICE.txt \
        CC-BY-SA-4.0.txt upstream-LICENSE.txt upstream-README.md > SHA256SUMS
)
python3 - "$assets/release-notes.md" "$catalog_revision" <<'PY'
import json
from pathlib import Path
import sqlite3
import sys

with sqlite3.connect('ccnrpn.db') as conn:
    metadata = dict(conn.execute('SELECT key,value FROM metadata'))
report = json.loads(Path('build-report.json').read_text())
revision = metadata['source_revision']
lines = [
    'MIDI Guide CSV-to-SQLite catalog.', '',
    f'Catalog commit: `{sys.argv[2]}`',
    f'Upstream revision: [{revision}](https://github.com/pencilresearch/midi/commit/{revision})',
    f"Schema version: {metadata['schema_version']}; generator: {metadata['generator_version']}",
    '', 'Catalog counts:', '',
    *(f'- {key}: {value}' for key, value in report['counts'].items()),
    '', 'Import warnings (see `build-report.json` for individual entries):', '',
    *(f'- {key}: {value}' for key, value in report['warning_counts'].items()),
    '', 'The database includes inferred values and unverified source data.',
    'Attribution and license notices are included as release assets and database metadata.',
    'Verify downloaded assets with `sha256sum -c SHA256SUMS`.', '',
]
Path(sys.argv[1]).write_text('\n'.join(lines), encoding='utf-8')
PY
# Explicit list keeps the release notes out of the checksummed asset set.
gh release upload "$tag" --clobber \
    "$assets/ccnrpn.db" "$assets/product_ids.json" "$assets/overrides.json" \
    "$assets/build-report.json" "$assets/NOTICE.txt" "$assets/CC-BY-SA-4.0.txt" \
    "$assets/upstream-LICENSE.txt" "$assets/upstream-README.md" "$assets/SHA256SUMS"
gh release edit "$tag" --notes-file "$assets/release-notes.md" --draft=false --latest
echo "Published: $tag"
