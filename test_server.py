import concurrent.futures
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from server import Handler, Quota, validate_payload


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'quota.db')

    def test_atomic_concurrency_and_persistence(self):
        quota = Quota(self.path)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: quota.reserve('one', now=100), range(10)))
        self.assertEqual(results.count(0), 3)
        self.assertEqual(Quota(self.path).reserve('one', now=110), 10)
        self.assertEqual(quota.reserve('one', now=120), 0)

    def test_daily_global_photo_and_utc_reset(self):
        quota = Quota(self.path, per_minute=99, per_day=2, global_day=4, photo_day=1)
        self.assertEqual(quota.reserve('a', photo=True, now=100), 0)
        self.assertGreater(quota.reserve('b', photo=True, now=100), 0)
        self.assertEqual(quota.reserve('a', now=100), 0)
        self.assertGreater(quota.reserve('a', now=160), 0)
        self.assertEqual(quota.reserve('b', now=100), 0)
        self.assertEqual(quota.reserve('c', now=100), 0)
        self.assertGreater(quota.reserve('new-ip', now=100), 0)
        self.assertEqual(quota.reserve('a', photo=True, now=86400), 0)

    def test_reject_remote_images_tools_and_oversized_text(self):
        for content in [[{'type': 'image_url', 'image_url': {'url': 'https://example.com/photo'}}], 'x'*60001, [{'type': 'tool', 'text': 'execute'}]]:
            with self.assertRaises(ValueError):
                validate_payload({'vision': True, 'messages': [{'role': 'user', 'content': content}]})
        self.assertEqual(validate_payload({'vision': False, 'messages': [{'role': 'user', 'content': 'food'}]})[0]['content'], 'food')

    def test_http_enforces_limits_and_server_model(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.api_key = 'gsk_synthetic_test'
        server.salt = 'synthetic-test-salt'
        server.text_model, server.photo_model = 'text-test', 'photo-test'
        server.quota = Quota(self.path, per_minute=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        address = f'http://127.0.0.1:{server.server_port}/v1/analyze'
        original = urllib.request.urlopen
        captured = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _): return b'{"choices":[]}'
        def open_url(request, *args, **kwargs):
            if request.full_url.startswith('https://api.groq.com/'):
                captured.append(json.loads(request.data))
                return Response()
            return original(request, *args, **kwargs)
        body = json.dumps({'vision': False, 'model': 'expensive-override', 'max_completion_tokens': 999999, 'messages': [{'role': 'user', 'content': 'test'}]}).encode()
        with patch('urllib.request.urlopen', side_effect=open_url), patch('server.time.time', return_value=100):
            with original(urllib.request.Request(address, data=body)) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as error:
                original(urllib.request.Request(address, data=body, headers={'X-Forwarded-For': 'different-ip', 'X-Real-IP': '192.0.2.1'}))
            self.assertEqual(error.exception.code, 429)
            self.assertIn('Retry-After', error.exception.headers)
            server.trusted_proxy_ips = {'127.0.0.1'}
            with self.assertRaises(urllib.error.HTTPError) as invalid:
                original(urllib.request.Request(address, data=body))
            self.assertEqual(invalid.exception.code, 400)
            with original(urllib.request.Request(address, data=body, headers={'X-Real-IP': '192.0.2.2'})) as response:
                self.assertEqual(response.status, 200)
        self.assertEqual(captured[0]['model'], 'text-test')
        self.assertEqual(captured[0]['max_completion_tokens'], 8192)


if __name__ == '__main__':
    unittest.main()
