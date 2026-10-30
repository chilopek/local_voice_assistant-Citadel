"""Opt-in real browser test: python tests/browser_smoke.py."""
from concurrent.futures import ThreadPoolExecutor
import http.server
from pathlib import Path
import tempfile
import threading

from assistant.browser import BrowserSession
from test_web import Handler


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    with tempfile.TemporaryDirectory() as folder:
        browser = BrowserSession(headless=True, profile=Path(folder) / "profile")
        try:
            first = browser.call("navigate", url=f"http://127.0.0.1:{server.server_port}")
            assert first["title"] == "Agent test page", first
            target = next(e["element_id"] for e in first["elements"] if e["tag"] == "input")
            # Simulate text and voice requests arriving from different threads.
            with ThreadPoolExecutor(max_workers=1) as worker:
                filled = worker.submit(browser.call, "fill", element_id=target, text="Citadel agent", approved=True).result()
            button = next(e["element_id"] for e in filled["elements"] if e["tag"] == "button")
            result = browser.call("click", element_id=button, approved=True)
            assert "Saved: Citadel agent" in result["text"], result
            try:
                browser.call("click", element_id=button)
            except ValueError:
                pass
            else:
                raise AssertionError("Stale browser target was accepted")
            print("PASS: real browser navigation, inspection, text entry, click, verification, thread ownership, stale targets")
        finally:
            browser.close()
finally:
    server.shutdown()
    server.server_close()
