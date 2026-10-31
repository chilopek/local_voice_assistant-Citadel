"""Opt-in real Ollama evaluation on owned files. Never executes generated code.

Usage: python tests/live_agent_benchmark.py --model qwen3.5:4b --output dist/benchmark.json
"""
import argparse
import ast
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from assistant import app, __version__
from assistant.config import OLLAMA_URL
from assistant.conversations import Conversations
from assistant.memory import Memory
from assistant.ollama_client import OllamaClient

parser=argparse.ArgumentParser()
parser.add_argument('--model',required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--case',choices=('create_code','find_and_edit','clarification'))
args=parser.parse_args()
results=[]


class MeasuredClient(OllamaClient):
    def __init__(self,*args):
        super().__init__(*args); self.metrics=[]
    def chat(self,messages,tools,cancel_event=None,images=None,timeout=120):
        started=time.monotonic()
        result=super().chat(messages,tools,cancel_event=cancel_event,images=images,timeout=timeout)
        self.metrics.append({'seconds':round(time.monotonic()-started,2),'prompt_tokens':result.get('prompt_eval_count'),'generated_tokens':result.get('eval_count')})
        return result


for case in ([args.case] if args.case else ('create_code','find_and_edit','clarification')):
    print('START',args.model,case,flush=True)
    with tempfile.TemporaryDirectory(prefix='citadel-benchmark-') as folder:
        root=Path(folder).resolve(); memory=Memory(root/'memory.sqlite')
        history=Conversations(root/'history.sqlite'); chat=history.current()['id']
        client=MeasuredClient(OLLAMA_URL,args.model)
        messages=[]; events=[]; task={}
        def progress(event):
            events.append(event)
            history.progress(chat,task['turn_id'],messages,event)
            if event['type']=='tool_start': print(event['name'],flush=True)
        def turn(text):
            global task
            task=history.begin(chat,text,messages)
            return app.run_turn(client,memory,messages,text,goal_text=task['goal_text'],resume_receipts=task['resume_receipts'],on_event=progress,
                                confirm=lambda name,parameters:name=='edit_file' and Path(parameters.get('path','')).resolve().is_relative_to(root))
        started=time.monotonic(); error=None; answer=''
        try:
            with patch.object(app,'PROJECT_ROOT',root),patch.object(app,'WORKSPACE',root),patch('assistant.tools.WORKSPACE',root),patch('assistant.file_tools.DATA_ROOT',root):
                if case=='create_code':
                    target=root/'decoder.py'
                    answer=turn(f'Напиши короткую Python-функцию decode(text, shift) для дешифрования шифра Цезаря латинского алфавита, сохрани в файл {target}. Не запускай код.')
                    code=target.read_text(encoding='utf-8')
                    tree=ast.parse(code)
                    assert any(isinstance(node,ast.FunctionDef) and node.name=='decode' for node in tree.body),'No decode function'
                    assert str(target) in answer,'Destination not reported'
                elif case=='find_and_edit':
                    nested=root/'notes'; nested.mkdir()
                    target=nested/'settings.txt'; target.write_text('device_name=Alpha\nmode=local\n',encoding='utf-8')
                    answer=turn(f'В папке {nested} найди текстовый файл, содержащий строку device_name=Alpha. Замени только эту строку на device_name=Beta. Остальной текст сохрани. Выполни правку файла, не ограничивайся советом.')
                    assert target.read_text()=='device_name=Beta\nmode=local\n','Exact edit not applied'
                    assert any(e.get('name')=='edit_file' and e.get('result',{}).get('ok') for e in events),'No verified edit tool'
                else:
                    target=root/'cipher.py'
                    answer=turn(f'Сохрани дешифровщик в файл {target}. Какой шифр нужен, я ещё не сказал: обязательно сначала спроси меня, не выбирай сам. Не запускай код.')
                    assert history.current()['status']=='waiting_input','Did not wait for required clarification'
                    answer=turn('Шифр Цезаря латинского алфавита. Достаточно функции decode(text, shift).')
                    tree=ast.parse(target.read_text(encoding='utf-8'))
                    assert any(isinstance(node,ast.FunctionDef) and node.name=='decode' for node in tree.body),'Clarified task was not saved'
            passed=True
        except Exception as problem:
            passed=False; error=repr(problem)
        result={'case':case,'passed':passed,'seconds':round(time.monotonic()-started,2),'model_calls':client.metrics,'tools':[e['name'] for e in events if e['type']=='tool_start'],'error':error,'answer':answer[:1200]}
        results.append(result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({'citadel':__version__,'model':args.model,'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
print('PASSED',sum(item['passed'] for item in results),'/',len(results),flush=True)
