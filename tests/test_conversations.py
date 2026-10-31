import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant.conversations import Conversations, effect_key, resumed_effect
from assistant.memory import Memory
from assistant import app


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        try:
            from tests.temp_support import cleanup_directory
        except ModuleNotFoundError:
            from temp_support import cleanup_directory
        self.addCleanup(cleanup_directory,self.temp)
        self.root = Path(self.temp.name).resolve()
        self.store = Conversations(self.root/'history.sqlite3')
        self.chat = self.store.current()['id']

    def test_clarification_preserves_destination_across_restart_and_new_task_resets_it(self):
        task = self.store.begin(self.chat, 'Сохрани дешифровщик в файл decoder.py', [])
        self.store.progress(self.chat, task['turn_id'], [], {'type':'done','task_status':'waiting_input'})
        restored = Conversations(self.store.path)
        followup = restored.begin(self.chat, 'Цезаря', [])
        self.assertIn('decoder.py', followup['goal_text'])
        self.assertIn('Цезаря', followup['goal_text'])
        self.assertEqual(task['task_key'], followup['task_key'])
        self.store.progress(self.chat, followup['turn_id'], [], {'type':'done','task_status':'complete'})
        new = self.store.begin(self.chat, 'Открой Google', [])
        self.assertIsNone(new['goal_text'])
        self.assertNotEqual(new['task_key'], task['task_key'])

    def test_interrupted_transcript_marks_missing_effect_uncertain_and_rejects_late_events(self):
        task = self.store.begin(self.chat, 'Создай файл', [])
        messages = [{'role':'assistant','tool_calls':[{'id':'first','function':{'name':'write_file'}},{'id':'second','function':{'name':'open_file'}}]},
                    {'role':'tool','tool_call_id':'first','tool_name':'write_file','content':'{"ok":true}'}]
        self.store.progress(self.chat, task['turn_id'], messages, {'type':'thinking'})
        self.store.interrupt(self.chat, task['turn_id'])
        self.assertFalse(self.store.progress(self.chat, task['turn_id'], [], {'type':'done'}))
        chat = self.store.current()
        self.assertEqual(chat['status'], 'interrupted')
        repaired = self.store.repair_transcript(chat['messages'])
        self.assertEqual(repaired[-1]['tool_call_id'], 'second')
        self.assertTrue(json.loads(repaired[-1]['content'])['uncertain'])
        self.assertEqual(self.store.repair_transcript(repaired), repaired)

    def test_resume_does_not_repeat_verified_write_and_detects_external_changes(self):
        path = self.root/'answer.txt'
        path.write_text('one', encoding='utf-8')
        args = {'path':str(path),'content':'one'}
        value = {'path':str(path),'verified':True,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        task = self.store.begin(self.chat, 'Создай файл', [])
        self.store.progress(self.chat, task['turn_id'], [], {'type':'tool_result','name':'write_file','result':{'ok':True,'result':value},'effect_key':effect_key('write_file',args)})
        self.store.interrupt(self.chat, task['turn_id'])
        followup = self.store.begin(self.chat, 'продолжи', [], resume=True)
        receipts = followup['resume_receipts']
        self.assertTrue(resumed_effect('write_file',args,receipts)['reused_checkpoint'])
        self.assertIsNone(resumed_effect('write_file',{**args,'content':'two'},receipts))
        path.write_text('external change')
        with self.assertRaises(ValueError): resumed_effect('write_file',args,receipts)
        self.assertEqual(path.read_text(),'external change')

    def test_old_worker_cannot_clobber_new_turn_and_forget_is_scoped(self):
        first = self.store.begin(self.chat,'one',[])
        second = self.store.begin(self.chat,'two',[])
        self.assertFalse(self.store.progress(self.chat,first['turn_id'],[],{'type':'done'}))
        other = self.store.create()
        self.store.forget(other)
        self.assertEqual(self.store.current()['id'],self.chat)
        self.assertEqual(self.store.current()['turn_id'],second['turn_id'])

    def test_screenshots_are_not_stored_in_history(self):
        self.assertNotIn('secret-image', Conversations.encode([{'role':'user','content':'test','images':['secret-image']}]))

    def test_agent_waits_for_answer_then_saves_file_for_original_goal(self):
        model = Mock()
        model.chat.return_value = {'message':{'tool_calls':[{'function':{'name':'ask_user','arguments':{'question':'Какой шифр нужен?'}}}]}}
        messages = []
        task = self.store.begin(self.chat,'Напиши дешифровщик и сохрани в файл decoder.py',messages)
        def progress(event): self.store.progress(self.chat,self.task['turn_id'],messages,event)
        self.task = task
        with patch.object(app,'PROJECT_ROOT',self.root), patch.object(app,'WORKSPACE',self.root), patch('assistant.tools.WORKSPACE',self.root):
            answer = app.run_turn(model, Memory(self.root/'memory.sqlite'), messages, 'Напиши дешифровщик и сохрани в файл decoder.py', on_event=progress)
            self.assertEqual(answer,'Какой шифр нужен?')
            self.assertEqual(self.store.current()['status'],'waiting_input')
            self.task = self.store.begin(self.chat,'Цезаря',messages)
            model.chat.return_value = {'message':{'content':'```python\ndef decode(text, shift):\n    return text\n```'}}
            answer = app.run_turn(model, Memory(self.root/'memory.sqlite'), messages, 'Цезаря', goal_text=self.task['goal_text'],on_event=progress)
        self.assertTrue((self.root/'decoder.py').is_file())
        self.assertIn('decoder.py',answer)
        self.assertEqual(self.store.current()['status'],'complete')

    def test_gui_restores_locally_without_starting_a_worker(self):
        from assistant.tk_runtime import configure_tk_paths
        configure_tk_paths()
        import tkinter as tk
        from assistant.gui import AssistantGUI
        db = Conversations(self.root/'data/conversations.sqlite3')
        chat = db.current()['id']
        task = db.begin(chat,'Сохрани файл',[])
        db.progress(chat,task['turn_id'],[{'role':'user','content':'Сохрани файл'},{'role':'assistant','content':'Какой текст?'}],{'type':'done','needs_input':True})
        root = tk.Tk(); root.withdraw()
        with patch('assistant.gui.PROJECT_ROOT',self.root), patch('assistant.gui.SessionProcess') as process:
            gui = AssistantGUI(root,enable_hotkey=False,warm_voice=False,enable_scheduler=False)
            try:
                self.assertEqual(gui.messages[-1]['content'],'Какой текст?')
                process.assert_not_called()
                gui._show_personal()
                root.update()
            finally: gui.close()
