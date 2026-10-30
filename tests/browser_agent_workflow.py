"""Real local-browser agent workflow: search input -> answer -> default handler."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from assistant import app
from assistant.browser import BrowserSession
from assistant.memory import Memory

PAGE = '<form action="/search" method="get" role="search"><input name="q" type="search" aria-label="Search"><button>Search</button></form><main><a href="/answer">Read answer</a></main>'
ANSWER = '<main><h1>Answer</h1>' + '<p>SSH provides an encrypted connection to a remote computer. Read the documentation before changing a server configuration.</p>' * 3 + '</main>'
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write((ANSWER if self.path == '/answer' else PAGE).encode())
    def log_message(self, *_):
        pass

def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    def call(name, **arguments):
        return {'message': {'role': 'assistant', 'tool_calls': [{'function': {'name': name, 'arguments': arguments}}]}}
    class Model:
        step = 0
        def chat(self, messages, tools):
            self.step += 1
            if self.step == 1:
                return call('browser_open', url=base)
            result = next((json.loads(m['content'])['result'] for m in reversed(messages) if m.get('role') == 'tool' and json.loads(m['content']).get('ok')), {})
            if self.step in (2, 3):
                field = next(e for e in result['elements'] if e.get('search_input'))
                return call('browser_action', action='fill' if self.step == 2 else 'press', element_id=field['element_id'], text='SSH', key='Enter')
            if self.step == 4:
                link = next(e for e in result['elements'] if e.get('text') == 'Read answer')
                return call('browser_action', action='click', element_id=link['element_id'])
            if self.step == 5:
                return call('open_url', url=result['url'])
            return {'message': {'content': 'Прочитал источник и передал страницу основному браузеру.'}}
    try:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            browser = BrowserSession(headless=True, profile=root/'profile')
            try:
                with patch('assistant.browser.BROWSER', browser), patch('assistant.tools.os.startfile') as opened, patch.object(app, 'PROJECT_ROOT', root):
                    def confirm(*_):
                        raise AssertionError('Unnecessary confirmation in a search/read workflow')
                    answer = app.run_turn(Model(), Memory(root/'memory.sqlite'), [], 'Вбей в Google вопрос про SSH, прочитай ответ и открой страницу с ответом', confirm=confirm)
                opened.assert_called_once_with(base+'/answer')
                assert 'прочитан и открыт' in answer, answer
                print('PASS: real browser search fill/Enter, source read, default URL open; zero confirmation dialogs')
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()

if __name__ == '__main__':
    main()
