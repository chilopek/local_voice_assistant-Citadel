from __future__ import annotations

import http.server
import json
import gzip
import threading
import unittest

from assistant.web_tools import fetch_url, validate_url


PAGE = b'''<html><head><title>Agent test page</title><script>secret_script</script></head>
<body><h1>Test document</h1><input aria-label="Name" id="name"><button onclick="document.querySelector('#result').textContent='Saved: '+document.querySelector('#name').value">Save</button><p id="result"></p><a href="/data">API</a></body></html>'''


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8" if self.path == "/data" else "text/html; charset=utf-8")
        if self.path == "/compressed":
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(json.dumps({"value": 42}).encode() if self.path == "/data" else gzip.compress(PAGE) if self.path == "/compressed" else PAGE)

    def log_message(self, *_):
        pass


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_html_and_links_are_read_without_scripts(self):
        result = fetch_url(self.url)
        self.assertEqual(result["title"], "Agent test page")
        self.assertIn("Test document", result["text"])
        self.assertNotIn("secret_script", result["text"])
        self.assertEqual(result["links"][0]["url"], self.url + "/data")

    def test_arbitrary_json_api(self):
        self.assertEqual(json.loads(fetch_url(self.url + "/data")["text"])["value"], 42)

    def test_compressed_html_is_decoded_before_model_reads_it(self):
        result = fetch_url(self.url + "/compressed")
        self.assertEqual(result["title"], "Agent test page")
        self.assertIn("Test document", result["text"])

    def test_local_file_and_javascript_are_rejected(self):
        for url in ["file:///C:/private", "javascript:alert(1)", "https://user:password@example.com"]:
            with self.assertRaises(ValueError):
                validate_url(url)


if __name__ == "__main__":
    unittest.main()
