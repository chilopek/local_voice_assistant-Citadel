"""Policy unit tests; real DOM coverage lives in browser_policy_smoke.py."""
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from assistant.browser import BrowserSession, assess_browser_action, browser_search


def metadata(**changes):
    info = dict(tag='button', text='Mystery', type='button', role='', name='',
                href=None, onclick=False, download=False, form=None,
                search_input=False, search_button=False, expanded=None,
                controls=None, filter=False, disabled=False)
    info.update(changes)
    return info


class BrowserPolicyTests(unittest.TestCase):
    def setUp(self):
        self.browser = BrowserSession()
        self.browser.page = Mock(url='https://example.test/')
        self.browser.page.evaluate.return_value = True
        self.browser.snapshot_page = self.browser.page
        self.browser.snapshot_url = self.browser.page.url
        self.handle = Mock()
        self.browser.targets['current'] = self.handle

    def assess(self, action='click', **info):
        observed = metadata(**info)
        self.handle.evaluate.return_value = observed
        self.browser.target_metadata['current'] = dict(observed)
        return self.browser.classify_action(action, 'current')

    def test_http_links_are_navigation_but_not_all_clicks(self):
        self.assertFalse(self.assess(tag='a', href='https://example.test/docs')['requires_confirmation'])
        self.assertTrue(self.assess()['requires_confirmation'])
        for href in ['javascript:alert(1)', 'mailto:a@example.test', '/relative',
                     'https://example.test/logout', 'https://example.test/?action=delete',
                     'https://example.test/%64elete', 'https://example.test/placeOrder',
                     'https://example.test/pay', 'https://example.test/unsubscribe']:
            with self.subTest(href=href):
                self.assertTrue(self.assess(tag='a', href=href)['requires_confirmation'])
        for changes in [dict(onclick=True), dict(download=True), dict(text='Delete'),
                        dict(text='Выйти'), dict(text='Отправить')]:
            self.assertTrue(self.assess(tag='a', href='https://example.test/', **changes)['requires_confirmation'])

    def test_search_fill_enter_and_button(self):
        search = dict(tag='input', type='search', text='Search', search_input=True,
                      form=dict(method='get', action='https://example.test/search', search=True))
        self.assertFalse(self.assess('fill', **search)['requires_confirmation'])
        self.assertFalse(self.browser.classify_action('fill', 'current', text='how to delete files')['requires_confirmation'])
        self.assertFalse(self.browser.classify_action('press', 'current', key='Enter')['requires_confirmation'])
        self.assertTrue(self.browser.classify_action('press', 'current', key='Control+Enter')['requires_confirmation'])
        self.assertFalse(self.assess(text='Search', search_button=True)['requires_confirmation'])
        self.assertTrue(self.assess(text='Send', search_button=True)['requires_confirmation'])

    def test_sensitive_and_unknown_controls_require_confirmation(self):
        for label in ['Send', 'Delete', 'Pay', 'Place order', 'Save', 'Upload', 'Отправить', 'Удалить', 'Купить']:
            self.assertTrue(self.assess(text=label)['requires_confirmation'])
        for kind in ['password', 'file']:
            for action in ['click', 'fill', 'press', 'select']:
                self.assertTrue(self.assess(action, tag='input', type=kind)['requires_confirmation'])
        self.assertTrue(self.assess('fill', tag='textarea')['requires_confirmation'])
        self.assertTrue(self.assess('press', tag='textarea')['requires_confirmation'])
        self.assertTrue(self.assess(type='submit', form=dict(method='post', search=False))['requires_confirmation'])
        self.assertTrue(self.assess(type='submit', role='tab', form=dict(method='post', search=False))['requires_confirmation'])

    def test_structural_filters_tabs_expanders_and_navigation(self):
        self.assertFalse(self.assess(role='tab')['requires_confirmation'])
        self.assertFalse(self.assess(expanded='false', controls='details')['requires_confirmation'])
        self.assertFalse(self.assess(type='checkbox', filter=True)['requires_confirmation'])
        self.assertFalse(self.assess('select', tag='select', filter=True)['requires_confirmation'])
        self.assertTrue(self.assess('select', tag='select')['requires_confirmation'])
        for action in ['snapshot', 'back', 'scroll', 'tab', 'screenshot']:
            self.assertFalse(self.browser.classify_action(action)['requires_confirmation'])
        self.assertTrue(self.browser.classify_action('execute_script')['requires_confirmation'])

    def test_stale_detached_changed_and_evaluation_errors_fail_closed(self):
        self.assertTrue(self.browser.classify_action('click', 'old')['requires_confirmation'])
        self.assess(tag='a', href='https://example.test/')
        self.browser.page.evaluate.return_value = False
        self.assertTrue(self.browser.classify_action('click', 'current')['requires_confirmation'])
        self.browser.page.evaluate.return_value = True
        self.handle.evaluate.return_value = metadata(text='Delete')
        self.assertTrue(self.browser.classify_action('click', 'current')['requires_confirmation'])
        self.browser.page.evaluate.side_effect = RuntimeError('destroyed context')
        self.assertTrue(self.browser.classify_action('click', 'current')['requires_confirmation'])

    def test_changed_page_fails_closed(self):
        self.assess(tag='a', href='https://example.test/')
        self.browser.page.url = 'https://example.test/other'
        self.assertTrue(self.browser.classify_action('click', 'current')['requires_confirmation'])

    def test_assess_runs_on_worker_without_opening_browser(self):
        browser = BrowserSession()
        seen = []
        original = browser.classify_action
        def classify(*args, **kwargs):
            seen.append(threading.get_ident())
            return original(*args, **kwargs)
        browser.classify_action = classify
        browser._open = Mock(side_effect=AssertionError('assessment must not launch'))
        try:
            result = browser.call('assess', requested_action='click', element_id='stale')
            self.assertTrue(result['requires_confirmation'])
            self.assertEqual(seen, [browser.thread.ident])
            self.assertNotEqual(seen[0], threading.get_ident())
        finally:
            browser.close()

    def test_search_url_encoding_hidden_and_fresh_snapshot(self):
        for provider, host in [('google', 'www.google.com'), ('bing', 'www.bing.com'), ('ddg', 'duckduckgo.com')]:
            browser = BrowserSession(headless=False, profile=Path('unused'))
            browser._open = Mock()
            browser.page = Mock()
            browser._snapshot = Mock(return_value={'elements': ['fresh']})
            result = browser._perform('search', query='SSH & ключ?', provider=provider)
            url = browser.page.goto.call_args.args[0]
            self.assertEqual(urlsplit(url).hostname, host)
            self.assertEqual(parse_qs(urlsplit(url).query), {'q': ['SSH & ключ?']})
            self.assertTrue(browser.headless)
            self.assertEqual(result, {'elements': ['fresh']})
        for args in [dict(query=''), dict(query='SSH', provider='unknown')]:
            with self.assertRaises(ValueError):
                BrowserSession()._perform('search', **args)

    def test_explicit_visible_navigation_and_wrapper_contracts(self):
        browser = BrowserSession()
        browser._open = Mock()
        browser.page = Mock()
        browser._snapshot = Mock(return_value={})
        browser._perform('navigate', url='https://example.test', visible=True)
        self.assertFalse(browser.headless)
        with patch('assistant.browser.BROWSER') as shared:
            assess_browser_action('press', 'id', key='Enter')
            shared.call.assert_called_with('assess', requested_action='press', element_id='id', text='', key='Enter')
            browser_search('SSH')
            shared.call.assert_called_with('search', query='SSH', provider='google')


if __name__ == '__main__':
    unittest.main()
