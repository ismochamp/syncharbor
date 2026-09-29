import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
import app


class SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        app.PORT = cls.server.server_address[1]
        cls.base = f'http://127.0.0.1:{app.PORT}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB = Path(self.tmp.name) / 'test.sqlite3'
        app.PREVIEWS.clear()
        app.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None):
        headers = {'Content-Type': 'application/json', **(headers or {})}
        req = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with urlopen(req, timeout=10) as response:
            return response.read()

    def preview(self, edition=1):
        return json.loads(self.request('/api/preview', {'url': self.base + f'/api/fixture?page=1&edition={edition}', 'mapping': app.DEFAULT_MAPPING}))

    def test_real_paginated_fetch_preview_commit_and_exports(self):
        preview = self.preview()
        self.assertEqual((preview['pages'], preview['additions'], preview['updates']), (2, 6, 0))
        self.assertEqual(len(app.state()['records']), 0)
        self.request('/api/commit', {'token': preview['token']})
        self.assertEqual(len(json.loads(self.request('/api/export.json'))), 6)
        self.assertIn(b'Northline Architecture', self.request('/api/export.csv'))
        self.assertEqual(app.state()['revision'], 1)

    def test_updates_require_explicit_approval(self):
        app.commit(self.preview()['token'])
        preview = self.preview(2)
        self.assertEqual((preview['additions'], preview['updates']), (1, 2))
        with self.assertRaisesRegex(ValueError, 'Explicitly approve'):
            app.commit(preview['token'])
        self.assertEqual(len(app.state()['records']), 6)
        app.commit(preview['token'], True)
        self.assertEqual(len(app.state()['records']), 7)
        self.assertEqual(next(r for r in app.state()['records'] if r['external_id'] == 'C-1002')['status'], 'Active')

    def test_stale_preview_cannot_overwrite_changed_dataset(self):
        first, stale = self.preview(), self.preview()
        app.commit(first['token'])
        with self.assertRaisesRegex(ValueError, 'changed after'):
            app.commit(stale['token'])

    def test_restore_is_reversible_and_revision_guarded(self):
        app.commit(self.preview()['token'])
        imported_id = app.state()['snapshots'][0]['id']
        app.restore(1, 1)
        self.assertEqual(app.state()['records'], [])
        with self.assertRaisesRegex(ValueError, 'Dataset changed'):
            app.restore(imported_id, 1)
        app.restore(imported_id, 2)
        self.assertEqual(len(app.state()['records']), 6)
        self.assertEqual(app.state()['revision'], 3)

    def test_duplicate_conflicts_and_missing_mapping_block_commit(self):
        rows = [app.FIXTURE[0], {**app.FIXTURE[0], 'company': 'Conflicting value'}, {'id': 'bad'}]
        with patch.object(app, 'fetch_pages', return_value=(rows, 1)):
            preview = app.make_preview('http://localhost', app.DEFAULT_MAPPING)
        self.assertEqual(len(preview['conflicts']), 2)
        with self.assertRaisesRegex(ValueError, 'Resolve every'):
            app.commit(preview['token'])
        self.assertEqual(app.state()['records'], [])

    def test_pagination_loop_aborts(self):
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'{"items": [], "next": "/repeat"}'
        class FakeOpener:
            def open(self, *args, **kwargs): return FakeResponse()
        with patch.object(app, 'build_opener', return_value=FakeOpener()):
            with self.assertRaisesRegex(ValueError, 'loop'):
                app.fetch_pages('http://localhost/repeat')

    def test_remote_opt_in_and_no_cross_origin_write(self):
        with patch.dict(os.environ, {'ALLOW_REMOTE_FETCH': '0'}):
            with self.assertRaisesRegex(ValueError, 'disabled'):
                app.validate_url('https://example.com/items')
        with self.assertRaises(HTTPError) as error:
            self.request('/api/preview', {}, {'Origin': 'https://untrusted.example'})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with self.assertRaises(HTTPError) as error:
            self.request('/api/state', headers={'Host': 'untrusted.example'})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_csv_formula_injection_is_neutralized(self):
        rows = [{**app.FIXTURE[0], 'company': '=1+1'}]
        with patch.object(app, 'fetch_pages', return_value=(rows, 1)):
            preview = app.make_preview('http://localhost', app.DEFAULT_MAPPING)
        app.commit(preview['token'])
        self.assertIn(b"'=1+1", self.request('/api/export.csv'))

    def test_empty_source_does_not_delete_existing_records(self):
        app.commit(self.preview()['token'])
        with patch.object(app, 'fetch_pages', return_value=([], 1)):
            preview = app.make_preview('http://localhost', app.DEFAULT_MAPPING)
        app.commit(preview['token'])
        self.assertEqual(len(app.state()['records']), 6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
