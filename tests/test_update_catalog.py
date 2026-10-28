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

"""Exercise publication against local Git remotes and a simulated GitHub CLI."""

import csv
import json
import os
from pathlib import Path
import shutil
import shlex
import sys
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import sys

root = Path(os.environ['FAKE_GH_STATE'])
root.mkdir(exist_ok=True)
args = sys.argv[1:]
assert args[0] == 'release', args
command, tag = args[1:3]
state = root / (tag + '.json')
if command == 'view':
	if not state.exists():
		sys.exit(1)
	print('true' if json.loads(state.read_text())['draft'] else 'false')
elif command == 'create':
	assert not state.exists()
	assert '--draft' in args and '--verify-tag' in args
	state.write_text(json.dumps({'draft': True}))
elif command == 'upload':
	assert json.loads(state.read_text())['draft']
	if os.environ.get('FAIL_UPLOAD'):
		sys.exit(1)
	destination = root / tag
	destination.mkdir(exist_ok=True)
	for asset in args[3:]:
		if not asset.startswith('--'):
			shutil.copyfile(asset, destination / Path(asset).name)
elif command == 'edit':
	assert '--draft=false' in args
	assets = root / tag
	assert (assets / 'ccnrpn.db').exists()
	assert (assets / 'SHA256SUMS').exists()
	notes = Path(args[args.index('--notes-file') + 1])
	shutil.copyfile(notes, assets / 'release-notes.md')
	state.write_text(json.dumps({'draft': False}))
else:
	raise AssertionError(args)
'''


class UpdateCatalogTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		self.upstream = self.root / 'upstream'
		self.repo = self.root / 'catalog'
		self.remote = self.root / 'remote.git'
		self.state = self.root / 'releases'
		bin_dir = self.root / 'bin'
		bin_dir.mkdir()
		(bin_dir / 'gh').write_text(FAKE_GH)
		(bin_dir / 'gh').chmod(0o755)
		self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
						GIT_CONFIG_SYSTEM=os.devnull, GIT_ALLOW_PROTOCOL='file',
						GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.com',
						GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.com',
						CATALOG_BRANCH='main', GH_REPO='test/catalog',
						FAKE_GH_STATE=str(self.state), PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])
		self.git(self.root, 'init', '-b', 'main', str(self.upstream))
		self.write_product('Z/Original')
		(self.upstream / 'LICENSE').write_text('Fixture license\n')
		(self.upstream / 'README.md').write_text('Fixture attribution\n')
		self.commit(self.upstream)
		self.git(self.root, 'init', '-b', 'main', str(self.repo))
		for name in ('csv2sqlite.py', 'test_csv2sqlite.py', 'NOTICE.txt', '.gitignore'):
			shutil.copyfile(PROJECT / name, self.repo / name)
		shutil.copytree(PROJECT / 'licenses', self.repo / 'licenses')
		(self.repo / 'scripts').mkdir()
		shutil.copyfile(PROJECT / 'scripts/update-catalog.sh', self.repo / 'scripts/update-catalog.sh')
		self.git(self.repo, 'submodule', 'add', str(self.upstream), 'midi')
		(self.repo / 'overrides.json').write_text('{"version": 1, "products": {}}')
		(self.repo / 'product_ids.json').write_text(json.dumps({'version': 1, 'products': {
			'Z/Original': {'id': 1, 'manufacturer': 'Z', 'name': 'Original',
							'aliases': [], 'active': True}}}))
		self.commit(self.repo)
		self.git(self.root, 'clone', '--bare', str(self.repo), str(self.remote))
		self.git(self.repo, 'remote', 'add', 'origin', str(self.remote))
		self.write_product('A/New')
		self.commit(self.upstream)

	def run_command(self, cwd, args, **kwargs):
		return subprocess.run(args, cwd=cwd, env=self.env, text=True,
								stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)

	def git(self, cwd, *args):
		return self.run_command(cwd, ['git', *args], check=True).stdout.strip()

	def commit(self, cwd):
		self.git(cwd, 'add', '.')
		self.git(cwd, 'commit', '-m', 'Fixture update')

	def write_product(self, key):
		# Use the actual importer header without importing its implementation.
		header = self.run_command(PROJECT, ['python3', '-B', '-c',
			'import csv2sqlite; print(",".join(csv2sqlite.CSV_COLUMNS))'], check=True).stdout.strip().split(',')
		path = self.upstream / (key + '.csv')
		path.parent.mkdir(parents=True, exist_ok=True)
		with path.open('w', newline='') as stream:
			writer = csv.DictWriter(stream, fieldnames=header)
			writer.writeheader()
			writer.writerow({'manufacturer': key.split('/')[0], 'device': path.stem,
								'parameter_name': 'Cutoff', 'cc_msb': 74})

	def publish(self, succeeds=True):
		result = self.run_command(self.repo, ['bash', 'scripts/update-catalog.sh'])
		self.assertEqual(result.returncode == 0, succeeds, result.stdout)
		return result

	def test_update_persists_registry_and_publishes_once(self):
		self.publish()
		head = self.git(self.remote, 'rev-parse', 'main')
		tag = 'catalog-' + head
		self.assertEqual(self.git(self.remote, 'rev-parse', tag), head)
		registry = json.loads(self.git(self.remote, 'show', 'main:product_ids.json'))
		self.assertEqual(registry['products']['Z/Original']['id'], 1)
		self.assertEqual(registry['products']['A/New']['id'], 2)
		self.assertEqual(self.git(self.remote, 'rev-parse', 'main:midi'),
							self.git(self.upstream, 'rev-parse', 'HEAD'))
		self.assertEqual(json.loads((self.state / (tag + '.json')).read_text()), {'draft': False})
		assets = self.state / tag
		self.assertEqual(json.loads((assets / 'product_ids.json').read_text()), registry)
		self.run_command(assets, ['sha256sum', '-c', 'SHA256SUMS'], check=True)
		original_db = (assets / 'ccnrpn.db').read_bytes()
		# Fail if the no-op run attempts any Python tests or converter commands.
		# The fake GitHub CLI still needs the Python interpreter for API responses.
		shim = self.root / 'bin/python3'
		shim.write_text('#!/bin/sh\n'
						'if [ "$1" = "-B" ]; then exit 91; fi\n'
						f'exec {shlex.quote(sys.executable)} "$@"\n')
		shim.chmod(0o755)
		(self.repo / 'ccnrpn.db').unlink()
		(self.repo / 'build-report.json').unlink()
		output = self.root / 'check-output'
		self.env['GITHUB_OUTPUT'] = str(output)
		result = self.run_command(self.repo, ['bash', 'scripts/update-catalog.sh', '--check'], check=True)
		self.assertEqual(output.read_text(), 'needed=false\n')
		self.assertIn('No changes; already published:', self.publish().stdout)
		self.assertFalse((self.repo / 'ccnrpn.db').exists())
		self.assertFalse((self.repo / 'build-report.json').exists())
		self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), head)
		self.assertEqual(len(list(self.state.glob('*.json'))), 1)
		self.assertEqual((assets / 'ccnrpn.db').read_bytes(), original_db)

	def test_source_change_publishes_with_unchanged_upstream(self):
		self.publish()
		upstream = self.git(self.upstream, 'rev-parse', 'HEAD')
		registry = (self.repo / 'product_ids.json').read_bytes()
		with (self.repo / 'csv2sqlite.py').open('a') as stream:
			stream.write('\n# Source-only change for publication regression test.\n')
		self.commit(self.repo)
		self.git(self.repo, 'push', 'origin', 'main')
		self.publish()
		self.assertEqual(len(list(self.state.glob('*.json'))), 2)
		self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD:midi'), upstream)
		self.assertEqual((self.repo / 'product_ids.json').read_bytes(), registry)

	def test_check_does_not_change_the_source_pin(self):
		pinned = self.git(self.repo, 'rev-parse', 'HEAD:midi')
		output = self.root / 'check-output'
		self.env['GITHUB_OUTPUT'] = str(output)
		self.run_command(self.repo, ['bash', 'scripts/update-catalog.sh', '--check'], check=True)
		self.assertEqual(output.read_text(), 'needed=true\n')
		self.assertEqual(self.git(self.repo / 'midi', 'rev-parse', 'HEAD'), pinned)
		self.assertEqual(self.git(self.repo, 'status', '--porcelain'), '')
		self.publish()

	def test_interrupted_upload_resumes_same_draft(self):
		self.env['FAIL_UPLOAD'] = '1'
		self.publish(succeeds=False)
		head = self.git(self.remote, 'rev-parse', 'main')
		state = self.state / ('catalog-' + head + '.json')
		self.assertTrue(json.loads(state.read_text())['draft'])
		del self.env['FAIL_UPLOAD']
		self.publish()
		self.assertFalse(json.loads(state.read_text())['draft'])
		self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), head)
		self.assertEqual(len(list(self.state.glob('*.json'))), 1)

	def test_invalid_upstream_does_not_push_or_release(self):
		head = self.git(self.remote, 'rev-parse', 'main')
		(self.upstream / 'A/New.csv').write_text('unexpected,columns\n')
		self.commit(self.upstream)
		self.publish(succeeds=False)
		self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), head)
		self.assertEqual(self.git(self.remote, 'tag'), '')
		self.assertFalse(self.state.exists())

	def test_concurrent_remote_update_rejects_branch_and_tag(self):
		peer = self.root / 'peer'
		self.git(self.root, 'clone', str(self.remote), str(peer))
		(peer / 'unrelated.txt').write_text('Concurrent maintainer change\n')
		self.commit(peer)
		self.git(peer, 'push', 'origin', 'main')
		head = self.git(self.remote, 'rev-parse', 'main')
		self.publish(succeeds=False)
		self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), head)
		self.assertEqual(self.git(self.remote, 'tag'), '')
		self.assertFalse(self.state.exists())


if __name__ == '__main__':
	unittest.main()
