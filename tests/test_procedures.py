from pathlib import Path
import tempfile
import unittest
from assistant.procedures import Procedures, keywords


class ProcedureTests(unittest.TestCase):
    def test_retains_only_successful_route_names_without_arguments_or_grants(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Procedures(Path(folder)/'routes.sqlite')
            self.assertFalse(store.learn('привет',['get_time']))
            self.assertTrue(store.learn('Найди информацию о протоколе SSH',['search_web','run_powershell','fetch_url','fetch_url']))
            route=store.match('Найди информацию о протоколе SSH')
            self.assertEqual(route['tools'],['search_web','fetch_url'])
            self.assertIsNone(store.match('Открой калькулятор'))
            self.assertIn('не разрешение',store.hint(route))
            store.learn('Найди информацию о протоколе SSH',['search_web','fetch_url'])
            self.assertEqual(store.list()[0]['uses'],2)
            store.forget(route['signature'])
            self.assertFalse(store.list())

    def test_keywords_exclude_urls_paths_and_quoted_secret_arguments(self):
        words=keywords('Найди документы https://secret.example/password C:\\secret\\thing.txt "mysecrettoken" user@example.com')
        self.assertNotIn('secret',str(words))
        self.assertNotIn('mysecre',words)
