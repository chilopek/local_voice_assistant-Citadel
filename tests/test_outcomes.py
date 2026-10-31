import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from assistant import app
from assistant.memory import Memory
from assistant.outcomes import Outcomes
from assistant.speech import outcome_speech,reminder_text
from assistant.system_tools import find_files,system_status


class OutcomeTests(unittest.TestCase):
    def test_speech_never_reads_pre_edit_content_as_current_file(self):
        state=Outcomes('Прочитай файл a.txt и исправь текст',Path.cwd())
        state.observe('read_file',{}, {'ok':True,'result':{'path':str(Path.cwd()/'a.txt'),'content':'before'}})
        state.observe('edit_file',{}, {'ok':True,'result':{'path':str(Path.cwd()/'a.txt'),'verified':True}})
        self.assertIsNone(state.reading)
        state.observe('read_file',{}, {'ok':True,'result':{'path':str(Path.cwd()/'a.txt'),'content':'after'}})
        self.assertEqual(outcome_speech(state,'Готово')['text'],'after')
    def test_preserving_rest_of_existing_file_is_an_edit_not_new_file_creation(self):
        state=Outcomes('Найди текстовый файл, замени Alpha на Beta. Остальной текст сохрани. Выполни правку файла.',Path.cwd())
        self.assertFalse(state.file)
        self.assertTrue(state.edit)
        self.assertEqual(app._task_tools(state.text)[1],{'files'})
        self.assertIn('правка',state.missing()[0])
        state.observe('edit_file',{}, {'ok':True,'result':{'path':'a.txt','verified':True}})
        self.assertFalse(state.missing())

    def test_double_escaped_python_transport_is_repaired_without_changing_strings(self):
        from assistant.code_text import python_source
        code=r'def decode(text):\n    return text.replace("\\n", "")\n'
        fixed,changed=python_source(code)
        self.assertTrue(changed)
        self.assertIn('\n    return',fixed)
        self.assertIn(r'"\\n"',fixed)
        import ast
        ast.parse(fixed)
        with self.assertRaises(ValueError):
            python_source('def decode(:\n    invalid')
    def test_routes_preserve_destination_and_second_action(self):
        self.assertIn('files',app._task_tools('Напиши алгоритм дешифровщик и сохрани в файл')[1])
        self.assertIn('desktop',app._task_tools('Напиши код в уже открытом окне')[1])
        self.assertEqual(app._task_tools('Открой ВК и включи музыку')[1],{'browser','desktop'})
        self.assertEqual(app._task_tools('Покажи загрузку процессора и диска')[1],{'system'})

    def test_generated_code_is_saved_and_verified_instead_of_chat_only(self):
        model=Mock()
        model.chat.return_value={'message':{'content':'```python\ndef decode(text):\n    return text[::-1]\n```'}}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            with patch.object(app,'PROJECT_ROOT',root),patch.object(app,'WORKSPACE',root),patch('assistant.tools.WORKSPACE',root):
                answer=app.run_turn(model,Memory(root/'memory.sqlite'),[],'Напиши дешифровщик и сохрани в файл decoder.py',confirm=Mock(side_effect=AssertionError('No permission for new file')))
            self.assertIn(str(root/'decoder.py'),answer)
            self.assertIn('def decode',(root/'decoder.py').read_text())
        model.chat.assert_called_once()

    def test_false_file_completion_is_rejected_and_wrong_path_does_not_count(self):
        state=Outcomes('Сохрани в файл wanted.py',Path.cwd())
        state.observe('write_file',{}, {'ok':True,'result':{'path':str(Path.cwd()/'wrong.py'),'verified':True}})
        self.assertTrue(state.missing())
        self.assertIn('Не подтверждено',state.finish('Всё сохранено.'))

    def test_missing_cipher_parameters_can_be_clarified_without_fake_completion(self):
        model=Mock()
        model.chat.return_value={'message':{'content':'Какой шифр нужно расшифровать?'}}
        with tempfile.TemporaryDirectory() as folder,patch.object(app,'PROJECT_ROOT',Path(folder)):
            answer=app.run_turn(model,Memory(Path(folder)/'memory.sqlite'),[],'Напиши дешифровщик и сохрани в файл')
        self.assertTrue(answer.endswith('?'))
        model.chat.assert_called_once()

    def test_read_text_and_reminder_time_have_explicit_speech(self):
        state=Outcomes('Прочитай текст файла вслух',Path.cwd())
        content='Важная строка. '*80+'В конце ещё одна важная мысль.'
        state.observe('read_file',{}, {'ok':True,'result':{'content':content,'truncated':False}})
        self.assertIn('В конце ещё одна',outcome_speech(state,'Прочитал.')['text'])
        item={'id':'abcdef123','title':'Проверить архив','due_at':'2026-10-31T16:30:00+07:00','repeat':1800}
        state=Outcomes('Напомни',Path.cwd())
        state.observe('create_reminder',{}, {'ok':True,'result':item})
        spoken=outcome_speech(state,'Готово.')['text']
        self.assertIn('Проверить архив',spoken)
        self.assertNotIn(item['id'],spoken)
        self.assertIn(':',spoken)
        self.assertIn('30 минут',spoken)

    def test_media_requires_actual_playback_and_login_blocker_is_explicit(self):
        state=Outcomes('Открой ВК и включи музыку',Path.cwd())
        state.observe('browser_open',{}, {'ok':True,'result':{'login_required':True}})
        self.assertIn('Требуется вход',state.finish('Музыка включена.'))
        state.observe('browser_media',{}, {'ok':True,'result':{'media':[{'paused':False,'ready_state':4}]}})
        self.assertFalse(state.missing())

    def test_login_hands_control_to_user_without_repeating_model_steps(self):
        model=Mock()
        model.chat.return_value={'message':{'tool_calls':[{'function':{'name':'browser_open','arguments':{'url':'https://example.com/login'}}}]}}
        with tempfile.TemporaryDirectory() as folder,patch.object(app,'PROJECT_ROOT',Path(folder)),patch.object(app,'execute',return_value={'login_required':True,'url':'https://example.com/login'}) as tool:
            answer=app.run_turn(model,Memory(Path(folder)/'memory.sqlite'),[],'Открой сайт и включи музыку')
        self.assertIn('Войдите',answer)
        self.assertIn('пока не запущено',answer)
        self.assertTrue(tool.call_args.args[1]['visible'])
        model.chat.assert_called_once()

    def test_find_files_nested_unicode_and_partial_search(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            nested=root/'Документы'/'Проект'
            nested.mkdir(parents=True)
            (nested/'Шифр.py').write_text('pass')
            result=find_files('шифр',roots=[str(root)])
            self.assertEqual(len(result['matches']),1)
            self.assertFalse(result['truncated'])
            for index in range(3):
                (nested/f'other{index}.py').write_text('')
            self.assertTrue(find_files('*.py',roots=[str(root)],max_results=1)['truncated'])

    def test_live_metrics_are_numbers_and_read_only(self):
        status=system_status(.1)
        self.assertTrue(0<=status['cpu_percent']<=100)
        self.assertTrue(0<=status['ram_percent']<=100)
        self.assertGreater(status['ram_total_bytes'],0)
        self.assertTrue(status['disk_active_percent'] is None or 0<=status['disk_active_percent']<=100)

    def test_search_failure_tries_another_backend_without_model_loop(self):
        from assistant.web_tools import search_web
        provider=Mock()
        provider.text.side_effect=[RuntimeError('first backend unavailable'),[{'href':'https://example.com','title':'source','body':'answer'}]]
        with patch('ddgs.DDGS',return_value=provider):
            result=search_web('test')
        self.assertEqual(result['results'][0]['url'],'https://example.com')
        self.assertEqual(provider.text.call_count,2)

    def test_reader_prefers_article_over_navigation(self):
        import io
        from email.message import Message
        from assistant.web_tools import fetch_url
        response=io.BytesIO(b'<html><main><nav>wrong menu</nav><article><h1>Answer</h1><p>A useful verified fact.</p></article></main></html>')
        response.headers=Message()
        response.headers['Content-Type']='text/html; charset=utf-8'
        response.url='https://example.com/article'
        with patch('urllib.request.urlopen',return_value=response):
            result=fetch_url(response.url)
        self.assertIn('useful verified fact',result['text'])
        self.assertNotIn('wrong menu',result['text'])

    def test_model_setting_changes_only_after_tool_support_validation(self):
        import io
        from assistant import model_setup,voice_setup
        with tempfile.TemporaryDirectory() as folder,patch.object(voice_setup,'DATA_ROOT',Path(folder)):
            path=Path(folder)/'assistant.local.json'
            path.write_text('{"model":"old","voice":false}')
            with patch('urllib.request.urlopen',side_effect=[io.BytesIO(b'{"status":"success"}\n'),io.BytesIO(b'{"capabilities":["completion"]}')]):
                with self.assertRaises(ValueError):
                    model_setup.prepare_agent('test',Mock())
            self.assertEqual(json.loads(path.read_text())['model'],'old')
            with patch('urllib.request.urlopen',side_effect=[io.BytesIO(b'{"status":"success"}\n'),io.BytesIO(b'{"capabilities":["completion","tools"]}')]):
                model_setup.prepare_agent('test',Mock())
            config=json.loads(path.read_text())
            self.assertEqual(config['model'],'test')
            self.assertFalse(config['voice'])


if __name__=='__main__':
    unittest.main()
