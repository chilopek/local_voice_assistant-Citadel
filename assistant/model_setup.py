"""Explicit model download and tool-capability validation, with atomic settings."""
import json
import re
import time
import urllib.request
from .config import OLLAMA_URL
from .voice_setup import save_updates


def prepare_agent(model,emit):
    if not isinstance(model,str) or not re.fullmatch(r'[\w./:-]{1,160}',model):
        raise ValueError('Укажите имя модели Ollama, например qwen3:8b')
    request = urllib.request.Request(OLLAMA_URL+'/api/pull',data=json.dumps({'model':model,'stream':True}).encode(),headers={'Content-Type':'application/json'})
    emit('status',{'message':'Подготовка '+model+'. При отсутствии модели начнётся загрузка; большая модель требует больше памяти.'})
    previous = 0
    with urllib.request.urlopen(request,timeout=900) as source:
        for line in source:
            item = json.loads(line)
            if item.get('error'):
                raise RuntimeError(item['error'])
            if time.monotonic()-previous>1:
                previous=time.monotonic()
                progress = f" {100*item.get('completed',0)/item['total']:.0f}%" if item.get('total') else ''
                emit('status',{'message':str(item.get('status','Подготовка'))+progress})
    request = urllib.request.Request(OLLAMA_URL+'/api/show',data=json.dumps({'model':model}).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=15) as source:
        details = json.load(source)
    if 'tools' not in details.get('capabilities',[]):
        raise ValueError('Модель не сообщает поддержку tools. Выбор агента не изменён.')
    save_updates({'model':model,'think':False})
    emit('ready',{'message':'Модель с поддержкой инструментов готова. Перезапустите Цитадель для применения.'})
