"""Bounded document reading and exact, reversible text edits."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET

from .paths import DATA_ROOT

MAX_TEXT_BYTES = 4 * 1024 * 1024


def decode_text(raw):
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16'), 'utf-16'
    if b'\0' in raw[:4096]:
        raise ValueError('Это двоичный файл. Для PDF/Office используй read_document.')
    try:
        return raw.decode('utf-8-sig'), 'utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8'
    except UnicodeDecodeError:
        return raw.decode('cp1251'), 'cp1251'


def read_text(path):
    with Path(path).open('rb') as source:
        raw = source.read(MAX_TEXT_BYTES+1)
    if len(raw)>MAX_TEXT_BYTES: raise ValueError('Для точного редактирования файл должен быть меньше 4 МБ.')
    text, encoding = decode_text(raw)
    return raw, text, encoding


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _revisions():
    path=DATA_ROOT/'data/file-revisions.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=2)
    db.execute('CREATE TABLE IF NOT EXISTS revisions (id TEXT PRIMARY KEY, path TEXT, before BLOB, after_hash TEXT, created REAL)')
    return db


def _replace(path, raw, expected_hash):
    # Stage beside the destination so replace stays on the same filesystem.
    descriptor, temporary=tempfile.mkstemp(prefix='.citadel-edit-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'wb') as output:
            output.write(raw); output.flush(); os.fsync(output.fileno())
        if digest(path.read_bytes())!=expected_hash:
            raise ValueError('Файл изменился во время редактирования. Прочитай его заново.')
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def edit_file(path: str, old_text: str, new_text: str, expected_sha256: str, validate_python: bool=False):
    from .tools import _resolve_workspace_path, _confirmed_execution
    if not _confirmed_execution.get(): raise PermissionError('Изменение существующего файла требует подтверждения.')
    target=_resolve_workspace_path(path)
    raw,text,encoding=read_text(target)
    if digest(raw)!=expected_sha256: raise ValueError('SHA256 не совпадает: файл изменён. Прочитай актуальную версию.')
    if not old_text or text.count(old_text)!=1: raise ValueError('old_text должен встречаться ровно один раз. Укажи более точный фрагмент.')
    result=text.replace(old_text,new_text,1)
    if validate_python or target.suffix.lower()=='.py':
        import ast
        ast.parse(result,filename=str(target))
    changed=result.encode(encoding)
    revision=uuid.uuid4().hex
    with closing(_revisions()) as db,db:
        db.execute('INSERT INTO revisions VALUES(?,?,?,?,?)',(revision,str(target),raw,digest(changed),time.time()))
        # Keep original bytes before touching the file, including on a crash.
        db.commit()
        _replace(target,changed,expected_sha256)
        db.execute('DELETE FROM revisions WHERE id NOT IN (SELECT id FROM revisions ORDER BY created DESC LIMIT 30)')
    return {'path':str(target),'verified':target.read_bytes()==changed,'sha256':digest(changed),'revision_id':revision,'replacements':1,'bytes':len(changed)}


def restore_file_revision(revision_id: str, expected_sha256: str):
    from .tools import _confirmed_execution
    if not _confirmed_execution.get(): raise PermissionError('Восстановление файла требует подтверждения.')
    with closing(_revisions()) as db:
        row=db.execute('SELECT path,before,after_hash FROM revisions WHERE id=?',(revision_id,)).fetchone()
    if row is None: raise ValueError('Резервная версия не найдена.')
    target=Path(row[0]).resolve()
    if expected_sha256!=row[2]: raise ValueError('Восстановление относится к другой версии файла.')
    _replace(target,row[1],expected_sha256)
    return {'path':str(target),'restored':True,'verified':target.read_bytes()==row[1],'sha256':digest(row[1])}


def list_file_revisions(path: str=''):
    from .tools import _resolve_workspace_path
    import datetime as dt
    with closing(_revisions()) as db:
        if path:
            rows=db.execute('SELECT id,path,after_hash,created FROM revisions WHERE path=? ORDER BY created DESC LIMIT 30',(str(_resolve_workspace_path(path)),)).fetchall()
        else:
            rows=db.execute('SELECT id,path,after_hash,created FROM revisions ORDER BY created DESC LIMIT 30').fetchall()
    current={}
    for _,name,_,_ in rows:
        if name not in current:
            try: current[name]=digest(read_text(name)[0])
            except (OSError,ValueError): current[name]=None
    return [{'revision_id':row[0],'path':row[1],'expected_sha256':row[2],
             'created':dt.datetime.fromtimestamp(row[3]).astimezone().isoformat(),
             'can_restore':current[row[1]]==row[2]} for row in rows]


def _xml(archive, name):
    item=archive.getinfo(name)
    if item.file_size>8*1024*1024: raise ValueError('Часть документа слишком велика для быстрого чтения.')
    data=archive.read(item)
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper(): raise ValueError('XML с DTD не поддерживается.')
    return ET.fromstring(data)


def read_document(path: str, start: int=0, max_chars: int=12000):
    from .tools import _resolve_workspace_path
    target=_resolve_workspace_path(path)
    if not target.is_file(): raise FileNotFoundError(str(target))
    if target.stat().st_size>32*1024*1024: raise ValueError('Документ больше 32 МБ: выбери меньший документ или часть.')
    start=max(0,min(int(start),1_000_000)); limit=max(100,min(int(max_chars),16000))
    needed=start+limit+1
    chunks=[]; total=0; more=False
    def add(text):
        nonlocal total
        chunks.append(text); total+=len(text)
        return total>=needed
    suffix=target.suffix.lower()
    note=''
    if suffix=='.pdf':
        from pypdf import PdfReader
        reader=PdfReader(target)
        if reader.is_encrypted: raise ValueError('PDF защищён паролем. Открой незашифрованную копию.')
        for index,page in enumerate(reader.pages):
            if index>=80: more=True; break
            contents=page.get_contents()
            if contents and len(contents.get_data())>8*1024*1024: raise ValueError('Слишком сложная страница PDF.')
            text=page.extract_text() or ''
            if add(f'\n[Страница {index+1}]\n'+text): more=index+1<len(reader.pages); break
        note='Извлечён текстовый слой. Изображения, сканы и точная вёрстка здесь не распознаются.'
    elif suffix in {'.docx','.pptx','.xlsx'}:
        with zipfile.ZipFile(target) as archive:
            if sum(info.file_size for info in archive.infolist())>64*1024*1024: raise ValueError('Распакованный документ слишком велик.')
            if suffix=='.docx':
                root=_xml(archive,'word/document.xml')
                for paragraph in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
                    if add(''.join(node.text or '' for node in paragraph.iter() if node.tag.endswith('}t'))+'\n'): more=True; break
            elif suffix=='.pptx':
                names=sorted((name for name in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml',name)),key=lambda name:int(re.search(r'(\d+)\.xml',name)[1]))
                for name in names[:80]:
                    root=_xml(archive,name)
                    if add('\n['+name+']\n'+'\n'.join(node.text or '' for node in root.iter() if node.tag.endswith('}t'))): more=True; break
                more=more or len(names)>80
            else:
                ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                shared=[]
                if 'xl/sharedStrings.xml' in archive.namelist():
                    shared=[''.join(n.text or '' for n in item.iter() if n.tag.endswith('}t')) for item in _xml(archive,'xl/sharedStrings.xml').findall('s:si',ns)]
                names=sorted(name for name in archive.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',name))
                for name in names[:30]:
                    if add('\n['+name+']\n'): more=True; break
                    root=_xml(archive,name)
                    for row in root.findall('.//s:row',ns):
                        cells=[]
                        for cell in row.findall('s:c',ns):
                            value=cell.findtext('s:v','',ns)
                            if cell.get('t')=='s': value=shared[int(value)] if value.isdigit() and int(value)<len(shared) else ''
                            elif cell.get('t')=='inlineStr': value=''.join(n.text or '' for n in cell.iter() if n.tag.endswith('}t'))
                            formula=cell.findtext('s:f','',ns)
                            cells.append(cell.get('r','')+'='+value+(' [формула: '+formula+']' if formula else ''))
                        if add('\t'.join(cells)+'\n'): more=True; break
                    if total>=needed: break
                more=more or len(names)>30
                note='Значения из сохранённой книги; формулы не пересчитываются.'
    else:
        raise ValueError('read_document поддерживает PDF, DOCX, XLSX и PPTX. Для текста используй read_file.')
    text=''.join(chunks)
    truncated=more or len(text)>start+limit
    return {'path':str(target),'content':text[start:start+limit],'start':start,'next_start':start+limit if len(text)>start+limit else None,'truncated':truncated,'note':note}


def search_file_contents(query: str, path: str='.', max_results: int=20):
    from .tools import _resolve_workspace_path
    root=_resolve_workspace_path(path)
    if not query.strip() or len(query)>300: raise ValueError('Запрос должен содержать 1–300 символов.')
    if not root.is_dir(): raise ValueError('Укажи папку для поиска.')
    matches=[]; scanned=0; skipped=0; limited=False
    limit=max(1,min(int(max_results),40)); deadline=time.monotonic()+5
    extensions={'.txt','.md','.py','.js','.ts','.html','.css','.json','.csv','.log','.xml','.yaml','.yml','.ini','.toml','.cs','.cpp','.c','.java','.rs','.go','.sql'}
    for folder,dirs,files in os.walk(root,followlinks=False):
        if time.monotonic()>deadline: limited=True; break
        dirs[:]=[name for name in dirs if name.casefold() not in {'.git','.venv','venv','node_modules','cache','.ssh','appdata'} and not Path(folder,name).is_symlink() and not Path(folder,name).is_junction()]
        for name in files:
            if time.monotonic()>deadline or scanned>=1000: limited=True; break
            item=Path(folder,name)
            if item.suffix.lower() not in extensions or item.is_symlink(): continue
            scanned+=1
            try:
                if item.stat().st_size>MAX_TEXT_BYTES: skipped+=1; continue
                raw,text,encoding=read_text(item)
                for number,line in enumerate(text.splitlines(),1):
                    if query.casefold() in line.casefold():
                        matches.append({'path':str(item),'line':number,'text':line[:400],'sha256':digest(raw)})
                        if len(matches)>=limit: limited=True; break
            except (OSError,UnicodeError,ValueError): skipped+=1
            if limited: break
        if limited: break
    return {'matches':matches,'scanned_files':scanned,'skipped_files':skipped,'truncated':limited,'note':'Поиск только по текстовым файлам в выбранной папке, без скрытых служебных каталогов.'}
