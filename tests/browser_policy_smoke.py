"""Opt-in real DOM policy smoke: python -m tests.browser_policy_smoke.

Only local search/navigation are executed. Sensitive controls are assessed only.
"""
from concurrent.futures import ThreadPoolExecutor
import http.server
from pathlib import Path
import tempfile
import threading

from assistant.browser import BrowserSession


PAGE = '''<!doctype html><title>Browser policy fixture</title>
<main><a href="/answer">Read answer</a><a href="/delete">Delete</a>
<a href="/logout">Logout</a></main>
<form action="/search" method="get" role="search">
<input name="q" type="search" aria-label="Search"><button>Search</button>
<select aria-label="Filter"><option>All</option><option>Docs</option></select></form>
<form action="/send" method="post"><textarea aria-label="Message"></textarea>
<button>Send</button><input type="password" aria-label="Password">
<input type="file" aria-label="Upload"></form>
<form action="/other" method="post"><input name="q" aria-label="Post query"><button>Go</button></form>
<form action="/search" method="get"><input name="q" aria-label="Override query">
<button formaction="/delete" formmethod="post">Override</button></form>
<button>Unknown</button><button role="tab" type="button">Details</button>
<button type="button" aria-expanded="false" aria-controls="more">Expand</button>
<div id="more">Public SSH answer: Secure Shell.</div>'''


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(PAGE.encode())

    def do_POST(self):
        raise AssertionError('Smoke must never submit a non-search form')

    def log_message(self, *_):
        pass


def main():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            browser = BrowserSession(headless=True, profile=Path(folder) / 'profile')
            try:
                snapshot = browser.call('navigate', url=f'http://127.0.0.1:{server.server_port}')
                def element(text):
                    return next(e for e in snapshot['elements'] if e['text'] == text)
                def assess(e, action='click', confirm=False):
                    result = browser.call('assess', requested_action=action, element_id=e['element_id'])
                    assert result['requires_confirmation'] is confirm, (e, result)
                link = element('Read answer')
                assert link['href'].endswith('/answer'), link
                assess(link)
                for label in ['Delete', 'Logout', 'Send', 'Unknown', 'Go', 'Override']:
                    assess(element(label), confirm=True)
                for label in ['Message', 'Upload', 'Post query', 'Override query']:
                    assess(element(label), 'fill', confirm=True)
                    assess(element(label), 'press', confirm=True)
                password = next(e for e in snapshot['elements'] if e['type'] == 'password')
                assess(password, 'fill', confirm=True)
                assess(password, 'press', confirm=True)
                assess(element('Details'))
                assess(element('Expand'))
                assess(element('Filter'), 'select')
                assess(next(e for e in snapshot['elements'] if e['search_button']))
                query = next(e for e in snapshot['elements'] if e['search_input'])
                assert query['form']['search'] and query['form']['method'] == 'get'
                assess(query, 'fill')
                assess(query, 'press')
                with ThreadPoolExecutor(max_workers=1) as pool:
                    snapshot = pool.submit(browser.call, 'fill', element_id=query['element_id'], text='SSH').result()
                stale = browser.call('assess', requested_action='fill', element_id=query['element_id'])
                assert stale['requires_confirmation'], stale
                query = next(e for e in snapshot['elements'] if e['search_input'])
                snapshot = browser.call('press', element_id=query['element_id'], key='Enter')
                assert '/search?q=SSH' in snapshot['url'], snapshot
                link = next(e for e in snapshot['elements'] if e['text'] == 'Read answer')
                snapshot = browser.call('click', element_id=link['element_id'])
                assert snapshot['url'].endswith('/answer')
                assert 'Secure Shell' in snapshot['text']
                password = next(e for e in snapshot['elements'] if e['type'] == 'password')
                for action in ['fill', 'press']:
                    try:
                        browser.call(action, element_id=password['element_id'], text='never-entered', key='x')
                    except ValueError:
                        pass
                    else:
                        raise AssertionError('Password automation was accepted')
                print('PASS: real headless DOM, href/form metadata, search fill/Enter, answer navigation, '
                      'safe/unsafe assessment, stale IDs, cross-thread calls, password prohibition; no sensitive clicks')
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
