"""Opt-in real model saves code into the requested file; no code execution."""
import ast
from pathlib import Path
import tempfile
from unittest.mock import Mock,patch
from assistant import app
from assistant.config import MODEL,OLLAMA_URL
from assistant.memory import Memory
from assistant.ollama_client import OllamaClient

with tempfile.TemporaryDirectory(prefix='citadel-file-outcome-') as folder:
    root=Path(folder)
    target=root/'decoder.py'
    def progress(event):
        if event['type']=='tool_start':
            print(event['name'],flush=True)
    with patch.object(app,'PROJECT_ROOT',root),patch.object(app,'WORKSPACE',root),patch('assistant.tools.WORKSPACE',root):
        answer=app.run_turn(OllamaClient(OLLAMA_URL,MODEL),Memory(root/'memory.sqlite'),[],
            f'Напиши короткий Python-дешифровщик шифра Цезаря, функцию decode(text, shift), и сохрани в файл {target}. Не запускай код.',
            confirm=Mock(side_effect=AssertionError('Unexpected permission')),on_event=progress)
    print(answer,flush=True)
    assert target.is_file(),'No requested artifact'
    ast.parse(target.read_text(encoding='utf-8'))
    assert 'def decode' in target.read_text(encoding='utf-8')
    print('PASS: actual model created a syntactically valid Python file at requested destination; code not executed')
