"""Destination/effect requirements are separate from the model's prose."""
import re
import uuid
from pathlib import Path


def wants_file(text: str) -> bool:
    if wants_edit(text) and not re.search(r'создай\s+(?:новый|отдельный)|сохрани.{0,40}(?:отдельн|новый)\s+файл|save.{0,30}(?:new|separate)',text,re.I):
        return False
    return bool(re.search(r'(?:сохран\w*|запиш\w*|созда\w*|напиши|write|save|create).{0,160}(?:файл|file\b)|(?:в\s+файл|into\s+(?:a\s+)?file)\b',text,re.I|re.S))


def wants_edit(text: str) -> bool:
    return bool(re.search(r'\b(?:измени|замени|исправь|отредактируй|поправь|edit|replace|modify)\b',text,re.I) and
                re.search(r'файл|file\b|\.(?:py|txt|md|json|js|ts|html|css|csv|xml|toml|ini|yaml)\b',text,re.I))


def wants_editor(text: str) -> bool:
    return bool(re.search(r'напиши|вставь|впиши|набери|запиши|write|type|paste',text,re.I) and
                re.search(r'(?:открыт\w*\s+(?:окн|редактор|блокнот)|(?:окн\w*|редактор\w*|блокнот\w*).{0,30}открыт|в\s+(?:редактор|блокнот)|в\s+текущ\w*\s+окн)',text,re.I))


def wants_media(text: str) -> bool:
    return bool(re.search(r'включи|воспроизведи|запусти|play\b',text,re.I) and re.search(r'музык|песн|трек|видео|music|video',text,re.I))


class Outcomes:
    def __init__(self,text: str,workspace: Path):
        self.file = wants_file(text)
        self.edit = wants_edit(text)
        self.edited = None
        self.editor = wants_editor(text)
        self.media = wants_media(text)
        self.text, self.workspace = text, workspace
        self.requested_root = False
        location = 'desktop' if re.search(r'рабоч\w*\s+стол|desktop',text,re.I) else 'documents' if re.search(r'в\s+(?:мои\s+)?документ\w*|в\s+папк\w*\s+документ',text,re.I) else 'downloads' if re.search(r'в\s+(?:папк\w*\s+)?загрузк',text,re.I) else None
        if self.file and location:
            from .system_tools import known_folders
            self.workspace = Path(known_folders()[location])
            self.requested_root = True
        destination = re.search(r'(?:[A-Za-z]:[\\/][^\n<>"|]*?|[\w.-]+)\.(?:py|js|ts|json|html|css|txt|md|cpp|c|cs|java|rs|go|ps1|sh|sql)\b',text,re.I)
        self.destination = destination[0] if destination else None
        self.saved = None
        self.inserted = None
        self.playing = False
        self.login_required = False
        self.reminders = []
        self.reading = None
        self.last_error = ''

    def observe(self,name,args,result):
        if not result.get('ok'):
            self.last_error = str(result.get('error') or result.get('result',{}).get('error',''))
            return
        value = result.get('result')
        if not isinstance(value,dict):
            return
        if value.get('login_required'):
            self.login_required = True
            self.last_error = 'Требуется вход в аккаунт. Войдите в открытом управляемом браузере и скажите «продолжи». Профиль этого браузера отдельный.'
        if name=='write_file' and value.get('path') and value.get('verified') is not False:
            expected = Path(self.destination) if self.destination else None
            expected = (expected if expected.is_absolute() else self.workspace/expected).resolve() if expected else None
            in_root = not self.requested_root or Path(value['path']).resolve().is_relative_to(self.workspace.resolve())
            if in_root and (expected is None or Path(value['path']).resolve()==expected):
                self.saved = value
            else:
                self.last_error='Файл создан по другому пути: '+value['path']
        if name=='insert_text' and value.get('request_accepted'):
            self.inserted = value
        if name in {'edit_file','restore_file_revision','write_file'} and self.edit and value.get('verified'):
            expected=Path(self.destination) if self.destination else None
            expected=(expected if expected.is_absolute() else self.workspace/expected).resolve() if expected else None
            if value.get('path') and (expected is None or Path(value['path']).resolve()==expected):
                self.edited = value
            else:
                self.last_error='Изменён другой файл: '+str(value.get('path',''))
        if name=='create_reminder' and value.get('due_at'):
            self.reminders.append(value)
        if name in {'read_file','read_document'} and value.get('content') and re.search(r'прочитай|прочти|зачитай|озвучь|читай|читать',self.text,re.I) and not self.file:
            self.reading = value
        media = value.get('media') or value.get('snapshot',{}).get('media') or []
        if any(item.get('paused') is False and item.get('ready_state',0)>=2 for item in media):
            self.playing = True

    def missing(self):
        return (["создание файла через write_file" + (' в '+str(self.workspace) if self.requested_root else '')] if self.file and not self.saved else []) + \
               (["проверенная правка существующего файла"] if self.edit and not self.edited else []) + \
               (["вставка текста в наблюдённый редактор через insert_text"] if self.editor and not self.inserted else []) + \
               (["подтверждение воспроизведения музыки/видео"] if self.media and not self.playing else [])

    def code_to_file(self,answer):
        if not self.file or self.saved or self.editor or re.search(r'измен[иь]|отредактир|перепиши|existing|edit',self.text,re.I):
            return None
        blocks = re.findall(r'```([\w+-]*)\s*\n(.*?)```',answer,re.S)
        if len(blocks)!=1 or not blocks[0][1].strip():
            return None
        language, content = blocks[0]
        extensions = {'python':'py','py':'py','javascript':'js','typescript':'ts','json':'json','html':'html','css':'css','c':'c','cpp':'cpp','csharp':'cs','java':'java','rust':'rs','go':'go','bash':'sh','powershell':'ps1','sql':'sql','text':'txt'}
        path = self.destination or str(self.workspace/('result-'+uuid.uuid4().hex[:8]+'.'+extensions.get(language.lower(),'txt')))
        return {'name':'write_file','arguments':{'path':path,'content':content.rstrip('\r\n')+'\n'}}

    def finish(self,answer):
        missing = self.missing()
        if missing:
            return 'Не подтверждено: '+ '; '.join(missing)+'. '+(self.last_error or 'Нужное действие не получило подтверждённого результата; задачу нельзя считать выполненной.')
        if self.file and self.saved and self.saved['path'] not in answer:
            answer += '\nФайл: '+self.saved['path']
        if self.edit and self.edited and self.edited['path'] not in answer:
            answer += '\nФайл: '+self.edited['path']
        if self.editor and self.inserted and not self.inserted.get('verified'):
            answer += '\nВставка отправлена, но полный текст редактора недоступен для проверки.'
        return answer
