import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from assistant import app
from assistant.memory import Memory

def call(name, **args):
    return {'message':{'tool_calls':[{'function':{'name':name,'arguments':args}}]}}

class AnswerPageTests(unittest.TestCase):
    def test_plain_open_google_does_not_require_research(self):
        self.assertFalse(app._answer_page_request('открой гугл'))
        self.assertTrue(app._answer_page_request('вбей в гугл вопрос и открой страницу с ответом'))

    def test_unread_result_is_not_opened_and_read_source_opens_without_approval(self):
        responses = iter([
            call('open_url',url='https://www.google.com/search?q=test'),
            call('fetch_url',url='https://example.com/answer'),
            call('open_url',url='https://example.com/answer'),
            {'message':{'content':'Источник прочитан и открыт.'}},
        ])
        model = Mock()
        model.chat.side_effect = lambda *_: next(responses)
        effects = []
        def execute(name,args):
            effects.append(name)
            if name=='fetch_url':
                return {'url':args['url'],'text':'Verified factual explanation. '*15}
            return {'url':args['url'],'request_accepted':True,'launched':True}
        with tempfile.TemporaryDirectory() as folder, patch.object(app,'PROJECT_ROOT',Path(folder)), patch.object(app,'execute',side_effect=execute):
            confirm = Mock(side_effect=AssertionError('Unexpected approval'))
            messages=[]
            answer=app.run_turn(model,Memory(Path(folder)/'memory.sqlite'),messages,'Найди ответ в Google и открой страницу с ответом',confirm=confirm)
        self.assertEqual(effects,['fetch_url','open_url'])
        self.assertIn('открыт',answer)
        confirm.assert_not_called()

    def test_app_passes_real_overwrite_approval_to_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'notes.txt'
            path.write_text('before')
            responses=iter([call('write_file',path=str(path),content='after'),{'message':{'content':'Изменено.'}}])
            model=Mock()
            model.chat.side_effect=lambda *_:next(responses)
            with patch.object(app,'PROJECT_ROOT',Path(folder)):
                confirm=Mock(return_value=True)
                app.run_turn(model,Memory(Path(folder)/'memory.sqlite'),[],'Перепиши файл',confirm=confirm)
            self.assertEqual(path.read_text(),'after')
            confirm.assert_called_once()

    def test_search_results_are_read_and_opened_without_extra_model_roundtrips(self):
        model = Mock()
        model.chat.return_value = call('search_web', query='SSH')
        effects = []
        def execute(name, args):
            effects.append(name)
            if name == 'search_web':
                return {'results': [{'url': 'https://example.com/ssh'}]}
            if name == 'fetch_url':
                return {'url': args['url'], 'text': 'Verified SSH explanation. ' * 20}
            return {'url': args['url'], 'request_accepted': True}
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'PROJECT_ROOT', Path(folder)), patch.object(app, 'execute', side_effect=execute):
            app.run_turn(model, Memory(Path(folder)/'memory.sqlite'), [],
                         'Найди в Google ответ и открой страницу',
                         confirm=Mock(side_effect=AssertionError('Unexpected approval')))
        self.assertEqual(effects, ['search_web', 'fetch_url', 'open_url'])
        model.chat.assert_called_once()

    def test_blocked_search_uses_transparent_fallback_without_model_roundtrip(self):
        model = Mock()
        model.chat.return_value = call('browser_search', query='SSH', provider='google')
        effects = []
        def execute(name, args):
            effects.append(name)
            if name == 'browser_search':
                raise RuntimeError('Сайт требует проверки человека')
            if name == 'search_web':
                return {'results': [{'url': 'https://example.com/ssh'}]}
            if name == 'fetch_url':
                return {'url': args['url'], 'text': 'Verified SSH explanation. ' * 20}
            return {'url': args['url'], 'request_accepted': True}
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'PROJECT_ROOT', Path(folder)), patch.object(app, 'execute', side_effect=execute):
            answer = app.run_turn(model, Memory(Path(folder)/'memory.sqlite'), [],
                         'Найди в Google ответ и открой страницу',
                         confirm=Mock(side_effect=AssertionError('Unexpected approval')))
        self.assertEqual(effects, ['browser_search', 'search_web', 'fetch_url', 'open_url'])
        self.assertIn('резервный поиск', answer)
        model.chat.assert_called_once()
