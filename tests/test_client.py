import io
import json
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

from assistant.ollama_client import OllamaClient, OllamaError


def stream(*chunks):
    return io.BytesIO(b"\n".join(json.dumps(c).encode() for c in chunks))


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

    def test_stream_joins_content_thinking_calls_and_done_metadata(self):
        response = stream(
            {"message": {"thinking": "consider", "content": "Hel", "tool_calls": [
                {"index": 0, "id": "first", "function": {"name": "fetch_url", "arguments": '{"url":"https://'}}]}, "done": False},
            {"message": {"thinking": "ed", "content": "lo", "tool_calls": [
                {"index": 0, "function": {"arguments": 'one.test"}'}},
                {"index": 1, "function": {"name": "get_time", "arguments": {}}}]}, "done": False},
            {"message": {"content": "!"}, "done": True, "done_reason": "stop", "eval_count": 12},
        )
        with patch("urllib.request.urlopen", return_value=response) as request:
            result = OllamaClient("http://test", "test").chat([], [])
        self.assertTrue(json.loads(request.call_args.args[0].data)["stream"])
        self.assertEqual(result["message"]["content"], "Hello!")
        self.assertEqual(result["message"]["thinking"], "considered")
        calls = result["message"]["tool_calls"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["id"], "first")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"url": "https://one.test"})
        self.assertIs(result["done"], True)
        self.assertEqual(result["eval_count"], 12)

    def test_unindexed_tool_calls_of_same_name_are_not_conflated(self):
        response = stream(
            {"message": {"tool_calls": [{"function": {"name": "fetch_url", "arguments": {"url": "https://one"}}}]}, "done": False},
            {"message": {"tool_calls": [{"function": {"name": "fetch_url", "arguments": {"url": "https://two"}}}]}, "done": True},
        )
        with patch("urllib.request.urlopen", return_value=response):
            result = OllamaClient("http://test", "test").chat([], [])
        self.assertEqual(len(result["message"]["tool_calls"]), 2)

    def test_incomplete_stream_empty_stream_and_error_are_rejected(self):
        for chunks in (
            (),
            ({"message": {"content": "partial"}, "done": False},),
            ({"error": "model missing"},),
            ({"message": {"content": "a"}}, {"message": {"content": "b"}}),
        ):
            with self.subTest(chunks=chunks), patch("urllib.request.urlopen", return_value=stream(*chunks)):
                with self.assertRaises(OllamaError):
                    OllamaClient("http://test", "test").chat([], [])

    def test_vision_capability_is_fetched_once_not_guessed_from_name(self):
        for capabilities, expected in ((["completion", "vision"], True), (["completion", "tools"], False), ([], False)):
            with self.subTest(capabilities=capabilities):
                with patch("urllib.request.urlopen", return_value=stream({"capabilities": capabilities})) as request:
                    client = OllamaClient("http://test", "looks-like-vision")
                    self.assertIs(client.supports_vision, expected)
                    self.assertIs(client.supports_vision, expected)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.args[0].full_url, "http://test/api/show")

    def test_show_failure_defaults_to_text(self):
        for error in (urllib.error.URLError("offline"), urllib.error.HTTPError("http://test", 404, "missing", {}, io.BytesIO(b"not found"))):
            with self.subTest(error=error), patch("urllib.request.urlopen", side_effect=error):
                self.assertFalse(OllamaClient("http://test", "test").supports_vision)

    def test_images_are_temporary_and_tool_links_stay_intact(self):
        messages = [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "read screen"},
            {"role": "assistant", "tool_calls": [{"id": "screen", "function": {"name": "read_screen", "arguments": {}}}]},
            {"role": "tool", "tool_name": "read_screen", "tool_call_id": "screen", "content": '{"ocr_text":"text"}'},
        ]
        original = json.dumps(messages)
        with patch("urllib.request.urlopen", return_value=stream({"message": {"content": "seen"}, "done": True})) as request:
            OllamaClient("http://test", "test").chat(messages, [], images=["pixels" * 10000])
        sent = json.loads(request.call_args.args[0].data)["messages"]
        self.assertEqual(sent[:-1], messages)
        self.assertEqual(sent[-1]["images"], ["pixels" * 10000])
        self.assertEqual(json.dumps(messages), original)

    def test_retry_preserves_source_urls_images_and_cancel_event(self):
        error = urllib.error.HTTPError("http://test", 400, "Bad Request", {}, io.BytesIO(b'request exceeds context'))
        messages = [{"role": "system", "content": "instructions"}, {"role": "user", "content": "research"},
                    {"role": "assistant", "tool_calls": [{"function": {"name": "fetch_url", "arguments": {"url": "https://source.test"}}}]},
                    {"role": "tool", "tool_name": "fetch_url", "content": json.dumps({"text": "x" * 12000, "url": "https://source.test", "retrieved_at": "2026-10-30"})}]
        cancel = threading.Event()
        with patch("urllib.request.urlopen", side_effect=[error, stream({"message": {"content": "answer"}, "done": True})]) as request:
            OllamaClient("http://test", "test").chat(messages, [], cancel_event=cancel, images=["pixels"])
        retry = json.loads(request.call_args.args[0].data)
        self.assertEqual(retry["messages"][-1]["images"], ["pixels"])
        tool = next(m for m in retry["messages"] if m["role"] == "tool")
        self.assertIn("https://source.test", tool["content"])
        self.assertIn("2026-10-30", tool["content"])
        self.assertEqual(retry["messages"][1]["content"], "research")

    def test_already_cancelled_does_not_open_http(self):
        cancel = threading.Event()
        cancel.set()
        with patch("urllib.request.urlopen") as request:
            with self.assertRaisesRegex(OllamaError, "отменён"):
                OllamaClient("http://test", "test").chat([], [], cancel_event=cancel)
        request.assert_not_called()

    def test_cancel_interrupts_blocked_connect_and_read(self):
        for block_read in (False, True):
            with self.subTest(block_read=block_read):
                cancel, entered, release = threading.Event(), threading.Event(), threading.Event()
                class Response:
                    def __enter__(self):
                        return self
                    def __exit__(self, *_):
                        pass
                    def close(self):
                        pass
                    def __iter__(self):
                        entered.set()
                        release.wait(2)
                        return iter([b'{"message":{"content":"late"},"done":true}'])
                def open_http(*_, **__):
                    if not block_read:
                        entered.set()
                        release.wait(2)
                    return Response()
                errors = []
                def run():
                    try:
                        OllamaClient("http://test", "test").chat([], [], cancel_event=cancel)
                    except OllamaError as error:
                        errors.append(str(error))
                with patch("urllib.request.urlopen", side_effect=open_http):
                    caller = threading.Thread(target=run)
                    caller.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        cancel.set()
                        caller.join(0.5)
                        self.assertFalse(caller.is_alive())
                        self.assertIn("отменён", errors[0])
                    finally:
                        release.set()
                        caller.join(1)

    def test_http_deadline_does_not_wait_for_blocked_connect(self):
        release = threading.Event()
        def open_http(*_, **__):
            release.wait(2)
            return stream({"message": {"content": "late"}, "done": True})
        with patch("urllib.request.urlopen", side_effect=open_http):
            try:
                started = time.monotonic()
                with self.assertRaisesRegex(OllamaError, "время"):
                    OllamaClient("http://test", "test").chat([], [], timeout=0.05)
                self.assertLess(time.monotonic() - started, 0.5)
            finally:
                release.set()


if __name__ == "__main__":
    unittest.main()
