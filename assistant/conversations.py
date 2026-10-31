"""Durable conversations, pending questions and receipts. Never resumes actions."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid
import hashlib


class Conversations:
    def __init__(self,path: Path, *, timeout=2):
        self.path=Path(path)
        self.timeout=timeout
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,updated REAL NOT NULL,
                    messages TEXT NOT NULL DEFAULT '[]',goal TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'idle',turn_id TEXT NOT NULL DEFAULT '',
                    plan TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS receipts (
                    chat_id TEXT NOT NULL,turn_id TEXT NOT NULL,seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    time REAL NOT NULL,tool TEXT NOT NULL,ok INTEGER NOT NULL,details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            ''')
            for table in ('chats','receipts'):
                if 'task_key' not in {r[1] for r in db.execute('PRAGMA table_info('+table+')')}:
                    db.execute('ALTER TABLE '+table+" ADD COLUMN task_key TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=self.timeout)
        db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def create(self) -> str:
        identifier=uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO chats(id,title,updated) VALUES(?,?,?)',(identifier,'Новая задача',time.time()))
            db.execute("INSERT OR REPLACE INTO preferences(key,value) VALUES('current',?)",(identifier,))
        return identifier

    def current(self) -> dict:
        with self.db() as db:
            row=db.execute("SELECT c.* FROM chats c JOIN preferences p ON c.id=p.value WHERE p.key='current'").fetchone()
        if row is None:
            recent=self.recent(limit=1)
            return self.activate(recent[0]['id']) if recent else self.load(self.create())
        return self._decode(row)

    @staticmethod
    def _decode(row):
        result=dict(row)
        result['messages']=json.loads(result['messages'])
        result['plan']=json.loads(result['plan'])
        return result

    def load(self,identifier: str) -> dict:
        with self.db() as db:
            row=db.execute('SELECT * FROM chats WHERE id=?',(identifier,)).fetchone()
        if row is None:raise ValueError('Диалог не найден')
        return self._decode(row)

    def activate(self,identifier: str) -> dict:
        result=self.load(identifier)
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO preferences(key,value) VALUES('current',?)",(identifier,))
        return result

    def recent(self,limit=30):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT id,title,updated,status,goal FROM chats ORDER BY updated DESC LIMIT ?',(max(1,min(int(limit),100)),))]

    def begin(self,identifier: str,text: str,messages: list,*,resume=False) -> dict:
        old=self.load(identifier)
        pending=old['status'] in {'waiting_input','waiting_auth'} or (resume and old['status'] in {'interrupted','blocked','running','partial','cancelled'})
        goal=(old['goal']+'\nУточнение пользователя: '+text)[-16000:] if pending and old['goal'] else text
        turn=uuid.uuid4().hex
        task_key=old.get('task_key') if pending or resume else None
        task_key=task_key or uuid.uuid4().hex
        previous=self.task_receipts(identifier,task_key) if pending or resume else []
        with self.db() as db:
            db.execute('UPDATE chats SET title=?,updated=?,messages=?,goal=?,status=?,turn_id=?,plan=?,task_key=? WHERE id=?',
                       (old['title'] if old['title']!='Новая задача' else text[:100],time.time(),
                        self.encode([*messages,{'role':'user','content':text}]),goal,'running',turn,
                        json.dumps(old['plan'] if pending or resume else [],ensure_ascii=False),task_key,identifier))
        return {'chat_id':identifier,'turn_id':turn,'task_key':task_key,'goal_text':goal if pending or resume else None,'resume_receipts':previous}

    @staticmethod
    def repair_transcript(messages):
        repaired=[]
        index=0
        while index<len(messages):
            message=messages[index]
            repaired.append(message)
            calls=(message.get('tool_calls') or []) if message.get('role')=='assistant' else []
            index+=1
            if not calls:continue
            following=[]
            while index<len(messages) and messages[index].get('role')=='tool':
                following.append(messages[index]);index+=1
            repaired.extend(following)
            available=list(following)
            for call in calls:
                name=call.get('function',{}).get('name','')
                match=next((item for item in available if (call.get('id') and item.get('tool_call_id')==call['id']) or (not call.get('id') and item.get('tool_name')==name)),None)
                if match is not None:
                    available.remove(match);continue
                missing={'role':'tool','tool_name':name,'content':json.dumps({'ok':False,'uncertain':True,'error':'Процесс завершился до подтверждения результата. Эффект неизвестен: проверь текущее состояние, не повторяй изменение/отправку вслепую.'},ensure_ascii=False)}
                if call.get('id'):missing['tool_call_id']=call['id']
                repaired.append(missing)
        return repaired

    @staticmethod
    def encode(messages):
        # Keep complete tool groups; trim only at a user boundary. Never store
        # image payloads. Actual image files remain ordinary user data.
        cleaned=[{key:value for key,value in message.items() if key not in {'images','image_base64'}} for message in messages]
        while len(json.dumps(cleaned,ensure_ascii=False,default=str))>800000:
            next_user=next((i for i,m in enumerate(cleaned[2:],2) if m.get('role')=='user'),None)
            if next_user is None:
                cleaned=[{**m,'content':str(m.get('content',''))[:40000]} for m in cleaned]
                break
            cleaned=[cleaned[0],*cleaned[next_user:]] if cleaned[0].get('role')=='system' else cleaned[next_user:]
        return json.dumps(cleaned,ensure_ascii=False,default=str)

    def progress(self,identifier: str,turn: str,messages: list,event: dict):
        kind=event.get('type')
        with self.db() as db:
            current=db.execute('SELECT turn_id,task_key FROM chats WHERE id=?',(identifier,)).fetchone()
            if current is None or current['turn_id']!=turn:return False
            db.execute('UPDATE chats SET messages=?,updated=? WHERE id=?',(self.encode(messages),time.time(),identifier))
            if kind=='tool_result':
                result=event.get('result',{})
                # The transcript already has full tool output. Receipts contain
                # bounded metadata, not a second copy of files/screenshots.
                value=result.get('result')
                keys=('path','url','bytes','verified','request_accepted','syntax_checked','login_required','error','sha256','revision_id','id','title','due_at','characters','window_handle')
                details={key:value[key] for key in keys if isinstance(value,dict) and key in value}
                if event.get('effect_key'):details['effect_key']=event['effect_key']
                if result.get('error'):details['error']=str(result['error'])[:500]
                db.execute('INSERT INTO receipts(chat_id,turn_id,time,tool,ok,details,task_key) VALUES(?,?,?,?,?,?,?)',
                           (identifier,turn,time.time(),event.get('name',''),int(bool(result.get('ok'))),json.dumps(details,ensure_ascii=False),current['task_key']))
                if event.get('name')=='update_plan' and result.get('ok') and isinstance(value,dict):
                    db.execute('UPDATE chats SET plan=? WHERE id=?',(json.dumps(value.get('steps',[])[:10],ensure_ascii=False),identifier))
            if kind=='done':
                status=event.get('task_status') or ('waiting_input' if event.get('needs_input') else 'complete')
                if status not in {'complete','waiting_input','waiting_auth','blocked','cancelled','partial'}:status='partial'
                db.execute('UPDATE chats SET status=? WHERE id=?',(status,identifier))
        return True

    def interrupt(self,identifier: str,turn: str):
        with self.db() as db:
            db.execute("UPDATE chats SET status='interrupted',turn_id='',updated=? WHERE id=? AND turn_id=? AND status='running'",(time.time(),identifier,turn))

    def receipts(self,identifier: str,limit=20):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT time,tool,ok,details FROM receipts WHERE chat_id=? ORDER BY seq DESC LIMIT ?',(identifier,max(1,min(int(limit),100))))]

    def task_receipts(self,identifier,task_key):
        with self.db() as db:
            rows=db.execute('SELECT tool,ok,details FROM receipts WHERE chat_id=? AND task_key=? AND ok=1 ORDER BY seq LIMIT 100',(identifier,task_key)).fetchall()
        return [{'name':row['tool'],'result':json.loads(row['details'])} for row in rows]

    def forget(self,identifier: str):
        with self.db() as db:
            db.execute('DELETE FROM receipts WHERE chat_id=?',(identifier,))
            db.execute('DELETE FROM chats WHERE id=?',(identifier,))
            db.execute("DELETE FROM preferences WHERE key='current' AND value=?",(identifier,))

    def setting(self,key,default=None):
        with self.db() as db:
            row=db.execute('SELECT value FROM preferences WHERE key=?',('setting:'+key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self,key,value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO preferences(key,value) VALUES(?,?)',('setting:'+key,json.dumps(value)))


def effect_key(name,args):
    if name not in {'write_file','edit_file','restore_file_revision','insert_text','create_reminder','watch_process','watch_file'}:return None
    return hashlib.sha256(json.dumps([name,args],ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def resumed_effect(name,args,receipts):
    key=effect_key(name,args)
    if not key:return None
    previous=next((item['result'] for item in reversed(receipts or []) if item.get('name')==name and item.get('result',{}).get('effect_key')==key),None)
    if previous is None:return None
    if name in {'write_file','edit_file','restore_file_revision'}:
        path=Path(previous.get('path',''))
        if not previous.get('sha256') or not path.is_file():
            raise ValueError('Ранее подтверждённый файл сейчас не найден. Уточни у пользователя новое создание.')
        with path.open('rb') as source:
            digest=hashlib.file_digest(source,'sha256').hexdigest()
        if digest!=previous['sha256']:
            raise ValueError('Файл изменился после предыдущей попытки. Не перезаписывай его автоматически: прочитай изменения и уточни намерение.')
    return {**previous,'reused_checkpoint':True}
