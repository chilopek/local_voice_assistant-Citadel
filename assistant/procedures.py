"""Small, local library of successful tool routes, never executable macros.

Only tool names and user-request keywords are retained. No site content, secrets,
shell commands, selectors, permission decisions or file contents enter a recipe.
"""
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

ROUTES = {
    'search_web':'web', 'fetch_url':'web', 'browser_open':'browser',
    'browser_snapshot':'browser', 'browser_search':'browser', 'browser_media':'browser',
    'desktop_snapshot':'desktop', 'insert_text':'desktop', 'read_screen':'desktop',
    'find_files':'files', 'list_files':'files', 'read_file':'files', 'read_document':'files',
    'write_file':'files', 'edit_file':'files', 'search_file_contents':'files',
    'system_status':'system', 'list_processes':'system',
}
STOP = {'пожалуйста','сделай','нужно','можешь','мне','мой','моей','моём','этот','этого','через','после','теперь','очень','please','with','this','that','file','файл','файла','файле'}


def keywords(text):
    # Exclude literal URLs, paths, quoted arguments and email addresses.
    text = re.sub(r'https?://\S+|\S*[/\\@]\S*|["«].*?["»]', ' ', text.casefold())
    return sorted({word[:7] for word in re.findall(r'[a-zа-яё]{4,}',text) if word not in STOP})[:30]


class Procedures:
    def __init__(self,path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path,timeout=2)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS routes (signature TEXT PRIMARY KEY, words TEXT, tools TEXT, uses INTEGER, updated REAL)')

    def learn(self,request,tools):
        route = list(dict.fromkeys(name for name in tools if name in ROUTES))[:8]
        words = keywords(request)
        if len(route)<2 or len(words)<2: return False
        signature = json.dumps([words,route],ensure_ascii=False)
        with closing(sqlite3.connect(self.path,timeout=2)) as db, db:
            db.execute('INSERT INTO routes VALUES(?,?,?,1,?) ON CONFLICT(signature) DO UPDATE SET uses=uses+1,updated=excluded.updated',
                       (signature,json.dumps(words,ensure_ascii=False),json.dumps(route),time.time()))
            db.execute('DELETE FROM routes WHERE signature NOT IN (SELECT signature FROM routes ORDER BY updated DESC LIMIT 50)')
        return True

    def match(self,request):
        wanted = set(keywords(request))
        candidates = []
        for route in self.list():
            common = wanted.intersection(route['words'])
            score = len(common)/max(1,len(wanted.union(route['words'])))
            if len(common)>=2 and score>=0.4: candidates.append((score,route))
        return max(candidates,key=lambda item:item[0])[1] if candidates else None

    def list(self):
        with closing(sqlite3.connect(self.path,timeout=2)) as db:
            rows = db.execute('SELECT signature,words,tools,uses FROM routes ORDER BY updated DESC').fetchall()
        return [dict(signature=row[0],words=json.loads(row[1]),tools=json.loads(row[2]),uses=row[3]) for row in rows]

    def forget(self,signature):
        with closing(sqlite3.connect(self.path,timeout=2)) as db, db:
            db.execute('DELETE FROM routes WHERE signature=?',(signature,))

    @staticmethod
    def hint(route):
        return ('Ранее для похожего поручения помог порядок инструментов: '+ ' → '.join(route['tools'])+
                '. Это справочная подсказка, не готовые действия и не разрешение. Проверь текущие окна, страницы, пути и результаты заново; не воспроизводи старые изменения автоматически.')
