import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from assistant import app, personal
from assistant.memory import Memory
from assistant.worker import WakeGate


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.chime = patch('assistant.chime.play_chime')
        self.chime.start()
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.store = personal.PersonalStore(self.root/'personal.sqlite')
    def tearDown(self):
        self.chime.stop()
        self.folder.cleanup()

    def test_restart_overdue_lease_cancellation_and_ack(self):
        one = self.store.add('reminder','Позвонить',100)
        restarted = personal.PersonalStore(self.store.path)
        self.assertFalse(restarted.poll(99))
        item = restarted.poll(150)[0]
        self.assertFalse(restarted.poll(151))
        replay = restarted.poll(451)[0]
        self.assertFalse(restarted.acknowledge(item,452))
        self.assertTrue(restarted.acknowledge(replay,452))
        self.assertFalse(restarted.poll(1000))
        self.assertTrue(restarted.snooze(one['id'],10)['rescheduled'])
        self.assertTrue(restarted.cancel(one['id'])['cancelled'])
        self.assertFalse(restarted.poll(time.time()+100))

    def test_cancel_queued_delivery_and_repeat_skip_missed_occurrences(self):
        notice = self.store.add('reminder','Разминка',100,60)
        item = self.store.poll(1000)[0]
        self.assertTrue(self.store.acknowledge(item,1000))
        self.assertEqual(self.store.list()[0]['due'],1060)
        item = self.store.poll(1060)[0]
        self.store.cancel(notice['id'])
        self.assertFalse(self.store.acknowledge(item,1061))

    def test_file_watch_requires_appearance_and_stability_across_restart(self):
        path = self.root/'download.zip'
        self.store.add('file','Архив',0,path=str(path),stable_seconds=15)
        self.assertFalse(self.store.poll(100))
        path.write_bytes(b'part')
        self.assertFalse(self.store.poll(101))
        self.assertFalse(self.store.poll(115))
        path.write_bytes(b'finished contents')
        self.assertFalse(self.store.poll(116))
        restarted = personal.PersonalStore(self.store.path)
        self.assertFalse(restarted.poll(130))
        ready = restarted.poll(131)[0]
        self.assertIn('не проверка целостности', ready['message'])

    def test_real_process_completion_and_pid_reuse(self):
        child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])
        try:
            identity = personal.process_identity(child.pid)
            self.assertTrue(identity)
            self.store.add('process','Тест',0,pid=child.pid,identity=identity)
            self.assertFalse(self.store.poll())
            child.terminate()
            child.wait(timeout=10)
            self.assertEqual(len(self.store.poll()),1)
            self.store.add('process','Другой процесс',0,pid=os.getpid(),identity='old identity')
            self.assertTrue(self.store.poll())
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)

    def test_explicit_memory_survives_restart_and_can_be_forgotten(self):
        with patch.object(personal,'DATA_ROOT',self.root):
            personal.remember_fact('язык','Русский')
            self.assertEqual(personal.list_memory(),{'язык':'Русский'})
            self.assertIn('Русский',Memory(self.root/'data/memory.sqlite3').prompt_context())
            self.assertTrue(personal.forget_fact('язык')['forgotten'])
            self.assertFalse(personal.list_memory())

    def test_timer_fast_path_uses_no_llm_and_survives_new_store(self):
        model = Mock(side_effect=AssertionError('No generation for timer'))
        with patch.object(personal,'DATA_ROOT',self.root), patch.object(app,'PROJECT_ROOT',self.root):
            result = app.run_turn(model,Memory(self.root/'memory.sqlite'),[], 'Напомни через 5 минут проверить загрузку',confirm=Mock(side_effect=AssertionError('No approval')))
            self.assertIn('проверить загрузку',result)
            self.assertEqual(personal.list_reminders()[0]['title'],'проверить загрузку')
        model.chat.assert_not_called()

    def test_future_reminder_with_i_is_not_confused_with_conversation_recall(self):
        self.assertIn('personal',app._task_tools('Напомни через десять минут, что я должен проверить архив')[1])
        self.assertIn('web',app._task_tools('Найди информацию об оперативной памяти')[1])
        self.assertIn('web',app._task_tools('Найди информацию про таймеры')[1])

    def test_stt_preparation_changes_config_only_after_success(self):
        from assistant import voice_setup
        path = self.root/'assistant.local.json'
        path.write_text('{"model":"test","stt_model":"base","voice":false}',encoding='utf-8')
        events = Mock()
        with patch.object(voice_setup,'DATA_ROOT',self.root),patch('assistant.voice.VoiceSession') as session:
            session.return_value.warmup.side_effect = RuntimeError('Incomplete model')
            with self.assertRaises(RuntimeError):
                voice_setup.prepare_stt('small',events)
            self.assertIn('base',path.read_text())
            session.return_value.warmup.side_effect = None
            voice_setup.prepare_stt('small',events)
            import json
            result = json.loads(path.read_text())
            self.assertEqual(result['stt_model'],'small')
            self.assertEqual(result['model'],'test')
            self.assertFalse(result['voice'])

    def test_wake_aliases_are_restricted_to_address_prefix(self):
        for text in ('Ситадель, открой браузер','Цита дель открой браузер','Эй, цитадел, открой браузер'):
            self.assertEqual(WakeGate('wake').accept(text,1),('открой браузер',False))
        for text in ('читатель открой браузер','цитата открой браузер','цитадельная стена','мы обсуждали ситадели вчера','Расскажи про цитадель'):
            self.assertEqual(WakeGate('wake').accept(text,1),('',False))

    def test_compound_request_is_not_swallowed_by_fast_timer_parser(self):
        self.assertIsNone(personal.quick_request('Напомни через 10 минут проверить загрузку. Потом открой папку'))
        self.assertIsNone(personal.quick_request('Запомни: мой город Новосибирск и открой Google'))
        self.assertEqual(personal.quick_request('Поставь таймер на десять минут')[1]['delay_seconds'],600)
        self.assertEqual(personal.quick_request('Напоминай каждые тридцать минут сделать перерыв')[1]['repeat_seconds'],1800)

    def test_actual_scheduler_delivers_without_agent_generation(self):
        import queue
        events = queue.Queue()
        self.store.add('reminder','Проверить задачу',time.time()+.1)
        scheduler = personal.PersonalScheduler(events.put,self.store.path)
        scheduler.start()
        try:
            item = events.get(timeout=4)
            self.assertEqual(item['message'],'Проверить задачу')
            self.assertTrue(self.store.acknowledge(item))
            self.assertTrue(events.empty())
        finally:
            scheduler.stop()
            scheduler.thread.join(timeout=3)

    def test_long_utterance_survives_pause_and_keeps_last_words(self):
        from array import array
        def pcm(level):
            return array('h',[level,-level]*400).tobytes()
        from assistant.voice import _UtteranceBuffer
        vad = _UtteranceBuffer(16000,silence_seconds=1.2)
        for _ in range(6):
            vad.feed(pcm(40))
        for _ in range(500):  # 25 seconds, beyond the former hard limit
            self.assertFalse(vad.feed(pcm(1500)))
        for _ in range(18):  # a natural 0.9s thinking pause
            self.assertFalse(vad.feed(pcm(40)))
        tail = pcm(2000)
        for _ in range(10):
            self.assertFalse(vad.feed(tail))
        for _ in range(23):
            self.assertFalse(vad.feed(pcm(40)))
        self.assertTrue(vad.feed(pcm(40)))
        self.assertEqual(vad.audio().count(tail),10)

    def test_gui_notification_waits_for_task_and_does_not_activate_agent(self):
        import tkinter as tk
        from assistant.gui import AssistantGUI
        root = tk.Tk()
        root.withdraw()
        with patch('assistant.gui.PROJECT_ROOT',self.root):
            gui = AssistantGUI(root,enable_hotkey=False,warm_voice=False,enable_scheduler=False)
        try:
            self.store.add('reminder','Перерыв',0)
            gui._personal_path = self.store.path
            gui._pending_notices = self.store.poll(1)
            gui._agent_running = True
            gui._deliver_notice()
            self.assertEqual(len(gui._pending_notices),1)
            gui._agent_running = False
            gui._deliver_notice()
            self.assertFalse(gui._pending_notices)
            self.assertIn('Перерыв',gui.transcript.get('1.0','end'))
            self.assertIsNone(gui.worker)
            gui._show_personal()
            root.update()
            panels = [child for child in root.winfo_children() if isinstance(child,tk.Toplevel) and 'дела и память' in child.title()]
            self.assertEqual(len(panels),1)
            notebook = panels[0].winfo_children()[0]
            notebook.select(2)
            root.update()
            self.assertIn('Выбор модели',gui.voice_setup_var.get())
            gui.voice_host = Mock()
            gui._notice_speech = gui._awaiting_speech = True
            gui._pending_notice_audio = ['Позднее уведомление']
            gui._stop_task()
            self.assertFalse(gui._awaiting_speech)
            self.assertFalse(gui._pending_notice_audio)
            self.assertEqual(gui.voice_host.send.call_args.args[0]['command'],'cancel')
        finally:
            gui.close()


if __name__=='__main__':
    unittest.main()
