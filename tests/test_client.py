import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from assistant.ollama_client import OllamaClient


class ContextTests(unittest.TestCase):
    def test_context_overflow_retries_with_smaller_results(self):
        error = urllib.error.HTTPError("http://test/api/chat", 400, "Bad Request", {}, io.BytesIO(b'{"error":"request exceeds the available context size"}'))
        response = io.BytesIO(b'{"message":{"role":"assistant","content":"checked"}}')
        messages = [
            {"role": "system", "content": "Instructions"},
            {"role": "user", "content": "Read the source"},
            {"role": "assistant", "tool_calls": [{"function": {"name": "fetch_url", "arguments": {"url": "https://example.com"}}}]},
            {"role": "tool", "tool_name": "fetch_url", "content": "x" * 20000},
        ]
        with patch("urllib.request.urlopen", side_effect=[error, response]) as request:
            result = OllamaClient("http://test", "test").chat(messages, [])
        self.assertEqual(result["message"]["content"], "checked")
        self.assertEqual(request.call_count, 2)
        first = json.loads(request.call_args_list[0].args[0].data)
        retry = json.loads(request.call_args_list[1].args[0].data)
        self.assertLess(len(retry["messages"][-1]["content"]), len(first["messages"][-1]["content"]))
        self.assertEqual(messages[-1]["content"], "x" * 20000)
        self.assertEqual(retry["messages"][1]["content"], "Read the source")


if __name__ == "__main__":
    unittest.main()
