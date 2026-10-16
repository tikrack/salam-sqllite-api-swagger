"""Integration tests against the compiled Salam API and a temporary SQLite file."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.base = f'http://127.0.0.1:{cls.port}'
        cls.env = dict(os.environ, PORT=str(cls.port), DB_PATH=f'{cls.tmp.name}/test.db')
        cls.binary = Path(__file__).resolve().parent / 'api'
        cls.start()

    @classmethod
    def start(cls):
        cls.process = subprocess.Popen([str(cls.binary)], env=cls.env, cwd=cls.binary.parent,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if cls.process.poll() is not None:
                raise RuntimeError(f'API exited with {cls.process.returncode}')
            try:
                with urlopen(cls.base + '/health', timeout=1) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.05)
        cls.stop()
        raise RuntimeError('API did not become ready')

    @classmethod
    def stop(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait()

    @classmethod
    def tearDownClass(cls):
        cls.stop()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = Request(self.base + path, data=data, method=method,
                          headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            text = response.read()
            return response.status, json.loads(text) if text else None

    def test_health(self):
        self.assertEqual(self.request('GET', '/health'), (200, {'status': 'ok'}))

    def test_openapi(self):
        status, spec = self.request('GET', '/openapi.json')
        self.assertEqual(status, 200)
        self.assertTrue(spec['openapi'].startswith('3.'))
        self.assertEqual(spec['info']['title'], 'Salam Tasks API')
        self.assertEqual(set(spec['paths']), {'/health', '/tasks', '/tasks/{id}'})
        operations = [(path, method, operation) for path, methods in spec['paths'].items()
                      for method, operation in methods.items()]
        self.assertEqual(len(operations), 6)
        self.assertEqual(len({op['operationId'] for _, _, op in operations}), 6)
        for path, method, operation in operations:
            self.assertTrue(operation['summary'])
            self.assertTrue(operation['responses'])
            if '{id}' in path:
                param = next(p for p in operation['parameters'] if p['name'] == 'id')
                self.assertEqual(param['in'], 'path')
                self.assertTrue(param['required'])
        for method, path in [('post', '/tasks'), ('put', '/tasks/{id}')]:
            body = spec['paths'][path][method]['requestBody']
            self.assertTrue(body['required'])
            schema = body['content']['application/json']['schema']
            self.assertEqual(schema['required'], ['title'])
            self.assertIs(schema['properties']['completed']['default'], False)
            example = {key: prop['example'] for key, prop in schema['properties'].items()}
            if method == 'post':
                code, created = self.request('POST', '/tasks', example)
                self.assertEqual(code, 201)
                self.assertEqual(created['title'], example['title'])
                self.assertEqual(self.request('DELETE', f"/tasks/{created['id']}")[0], 204)
        deleted = spec['paths']['/tasks/{id}']['delete']['responses']['204']
        self.assertNotIn('content', deleted)
        self.assertIn('Location', spec['paths']['/tasks']['post']['responses']['201']['headers'])

    def test_swagger_ui_and_local_assets(self):
        for path in ['/docs', '/docs/']:
            with urlopen(self.base + path, timeout=3) as response:
                html = response.read().decode()
                self.assertEqual(response.status, 200)
                self.assertIn('text/html', response.headers['Content-Type'])
                self.assertIn('SwaggerUIBundle', html)
                self.assertIn("url:'/openapi.json'", html)
                self.assertNotIn('https://', html)
        for filename in ['swagger-ui-bundle.js', 'swagger-ui.css']:
            with urlopen(self.base + '/docs/assets/' + filename, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), (self.binary.parent / 'docs/assets' / filename).read_bytes())

    def test_crud_and_persistence(self):
        title = "کار فارسی '); DROP TABLE tasks; -- \"quoted\"\nnext"
        status, task = self.request('POST', '/tasks', {'title': title, 'completed': False})
        self.assertEqual(status, 201)
        self.assertEqual(task['title'], title)
        path = f"/tasks/{task['id']}"
        self.assertEqual(self.request('GET', path), (200, task))
        self.assertIn(task, self.request('GET', '/tasks')[1])
        self.stop()
        self.start()
        self.assertEqual(self.request('GET', path), (200, task))
        status, updated = self.request('PUT', path, {'title': 'updated', 'completed': True})
        self.assertEqual(status, 200)
        self.assertEqual(updated, {'id': task['id'], 'title': 'updated', 'completed': True})
        self.assertEqual(self.request('DELETE', path), (204, None))
        self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.request('DELETE', path)[0], 404)

    def test_invalid_json_and_fields(self):
        for raw in [b'{', b'[]', b'null', b'{}', b'{"title":123}',
                    b'{"title":"x","completed":"yes"}', b'{"title":"x"} trailing']:
            with self.subTest(raw=raw):
                self.assertEqual(self.request('POST', '/tasks', raw=raw)[0], 400)
        for title in ['', '   ', 'x' * 501]:
            self.assertEqual(self.request('POST', '/tasks', {'title': title})[0], 400)

    def test_missing_task(self):
        for method in ['GET', 'DELETE', 'PUT']:
            body = {'title': 'missing', 'completed': False} if method == 'PUT' else None
            self.assertEqual(self.request(method, '/tasks/999999', body)[0], 404)

    def test_invalid_ids(self):
        for task_id in ['abc', '-1', '0', '1abc', '01']:
            with self.subTest(task_id=task_id):
                self.assertEqual(self.request('GET', '/tasks/' + task_id)[0], 400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
