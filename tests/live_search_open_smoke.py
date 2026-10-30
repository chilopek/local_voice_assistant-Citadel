"""Opt-in actual model/public search; default-browser launch is intercepted."""
from pathlib import Path
import tempfile
import time
from unittest.mock import Mock, patch
from assistant import app
from assistant.config import MODEL, OLLAMA_URL
from assistant.memory import Memory
from assistant.ollama_client import OllamaClient
from assistant.browser import BROWSER

events = []
def progress(event):
    events.append(event)
    if event['type'] == 'tool_start':
        print(event['name'], str(event.get('arguments'))[:180], flush=True)

try:
    with tempfile.TemporaryDirectory() as folder, patch.object(app,'PROJECT_ROOT',Path(folder)), patch('assistant.tools.os.startfile') as opened:
        started=time.monotonic()
        answer=app.run_turn(OllamaClient(OLLAMA_URL,MODEL),Memory(Path(folder)/'memory.sqlite'),[],
            'Найди в Google, что такое SSH, прочитай подходящую страницу с ответом и открой её мне. Если Google блокирует автоматический поиск, используй другой поисковик и укажи замену.',
            confirm=Mock(side_effect=AssertionError('Unexpected approval')),on_event=progress)
        print('ANSWER:',answer,flush=True)
        print('ELAPSED:',round(time.monotonic()-started,2),flush=True)
        assert opened.call_count==1,(opened.call_args_list,answer)
        url=opened.call_args.args[0]
        assert not app._search_or_challenge_url(url),url
        observed=[e['result'].get('result',{}).get('url') for e in events if e['type']=='tool_result' and e['result'].get('ok') and e.get('name') in {'fetch_url','browser_open','browser_action','browser_snapshot'}]
        assert url in observed,(url,observed)
        print('PASS: actual agent read a public answer source and requested it in the default browser with zero approvals',flush=True)
finally:
    BROWSER.close()
