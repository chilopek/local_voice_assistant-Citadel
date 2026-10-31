"""Read-only machine telemetry and bounded filesystem discovery."""
from __future__ import annotations

import datetime as dt
import fnmatch
import os
from pathlib import Path
import time
from collections import deque
from contextlib import contextmanager
import sys
import subprocess
import shutil


def known_folders() -> dict:
    home=Path.home()
    folders={'desktop':home/'Desktop','documents':home/'Documents','downloads':home/'Downloads'}
    if sys.platform=='win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
                for name,value in {'desktop':'Desktop','documents':'Personal','downloads':'{374DE290-123F-4565-9164-39C4925E467B}'}.items():
                    try: folders[name]=Path(os.path.expandvars(winreg.QueryValueEx(key,value)[0]))
                    except OSError: pass
        except OSError:
            pass
    return {key:str(value) for key,value in folders.items()}


@contextmanager
def _disk_counter():
    """English PDH counter names work on localized Windows as well."""
    finish = lambda: None
    query = None
    try:
        if sys.platform=='win32':
            import ctypes as c
            from ctypes import wintypes as w
            api = c.WinDLL('pdh')
            api.PdhOpenQueryW.argtypes=[w.LPCWSTR,c.c_size_t,c.POINTER(c.c_void_p)]
            api.PdhAddEnglishCounterW.argtypes=[c.c_void_p,w.LPCWSTR,c.c_size_t,c.POINTER(c.c_void_p)]
            api.PdhCollectQueryData.argtypes=[c.c_void_p]
            api.PdhCloseQuery.argtypes=[c.c_void_p]
            class Value(c.Structure):
                _fields_=[('status',w.DWORD),('value',c.c_double)]
            api.PdhGetFormattedCounterValue.argtypes=[c.c_void_p,w.DWORD,c.POINTER(w.DWORD),c.POINTER(Value)]
            query, counter = c.c_void_p(), c.c_void_p()
            if api.PdhOpenQueryW(None,0,c.byref(query))==0 and api.PdhAddEnglishCounterW(query,r'\PhysicalDisk(_Total)\% Idle Time',0,c.byref(counter))==0 and api.PdhCollectQueryData(query)==0:
                def finish():
                    value = Value()
                    if api.PdhCollectQueryData(query)==0 and api.PdhGetFormattedCounterValue(counter,0x200,None,c.byref(value))==0 and value.status in (0,1):
                        return round(max(0,min(100,100-value.value)),2)
                    return None
    except (OSError,AttributeError):
        pass
    try:
        yield finish
    finally:
        if query:
            api.PdhCloseQuery(query)


def system_status(sample_seconds: float = 0.4) -> dict:
    import psutil
    interval = max(.1, min(float(sample_seconds), 2))
    before = psutil.disk_io_counters()
    with _disk_counter() as disk_sample:
        cpu = psutil.cpu_percent(interval=interval)
        active = disk_sample()
    after = psutil.disk_io_counters()
    ram = psutil.virtual_memory()
    disks, errors = [], []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
            disks.append({'mount':partition.mountpoint,'total_bytes':usage.total,
                          'free_bytes':usage.free,'used_percent':usage.percent})
        except OSError as error:
            errors.append({'mount':partition.mountpoint,'error':str(error)})
    io = {'read_bytes_per_second':max(0,after.read_bytes-before.read_bytes)/interval,
          'write_bytes_per_second':max(0,after.write_bytes-before.write_bytes)/interval} if before and after else {}
    # Disk capacity and transfer rate are not the Windows "active time" metric.
    gpus=[]
    binary=shutil.which('nvidia-smi')
    if binary:
        try:
            report=subprocess.run([binary,'--query-gpu=name,utilization.gpu,memory.used,memory.total','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3,
                                  creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
            if report.returncode==0:
                for line in report.stdout.splitlines()[:8]:
                    name,load,used,total=[part.strip() for part in line.split(',')]
                    gpus.append({'name':name,'gpu_percent':float(load),'memory_used_mib':float(used),'memory_total_mib':float(total)})
        except (OSError,ValueError,subprocess.TimeoutExpired):
            pass
    return {'cpu_percent':cpu,'logical_cpus':psutil.cpu_count(),'ram_percent':ram.percent,'nvidia_gpus':gpus,
            'ram_available_bytes':ram.available,'ram_total_bytes':ram.total,'disks':disks,
            'disk_io':io,'disk_active_percent':active,'sample_seconds':interval,'errors':errors,
            'note':'Короткий замер. Процент занятого места на диске не является процентом его активности.',
            'time':dt.datetime.now().astimezone().isoformat()}


def list_processes(sort_by: str = 'memory', limit: int = 15) -> dict:
    import psutil
    if sort_by not in {'memory','cpu','name'}:
        raise ValueError('sort_by: memory, cpu, name')
    rows = []
    processes = list(psutil.process_iter(['pid','name','memory_info']))
    if sort_by == 'cpu':
        for process in processes:
            try: process.cpu_percent()
            except psutil.Error: pass
        time.sleep(.2)
    for process in processes:
        try:
            info = process.info
            rows.append({'pid':info['pid'],'name':info['name'],
                         'memory_bytes':info['memory_info'].rss if info['memory_info'] else 0,
                         'cpu_percent':process.cpu_percent() if sort_by=='cpu' else None})
        except psutil.Error:
            continue
    key = 'memory_bytes' if sort_by=='memory' else 'cpu_percent' if sort_by=='cpu' else 'name'
    rows.sort(key=lambda row:row[key] or ('' if key=='name' else 0),reverse=sort_by!='name')
    return {'processes':rows[:max(1,min(int(limit),50))],'sort_by':sort_by}


def find_files(query: str, roots: list[str] | None = None, max_results: int = 40,
               max_seconds: float = 8, include_system: bool = False) -> dict:
    """One breadth-first scan per root. Never follow junctions or symlinks."""
    if not query.strip() or len(query)>200:
        raise ValueError('Укажите часть имени или маску, например *.py')
    if roots is None:
        from .config import WORKSPACE
        home = Path.home()
        candidates = [WORKSPACE,*(Path(value) for value in known_folders().values())]
        for variable in ('OneDrive','OneDriveConsumer','OneDriveCommercial'):
            if os.environ.get(variable):
                candidates.extend([Path(os.environ[variable])/'Desktop',Path(os.environ[variable])/'Documents'])
        roots = [str(p) for p in candidates if p.is_dir()]
    if not isinstance(roots,list) or len(roots)>12:
        raise ValueError('Укажите не больше 12 корневых папок')
    started, matches, errors, visited, examined = time.monotonic(), [], [], set(), 0
    limit = max(1,min(int(max_results),100))
    seconds = max(.1,min(float(max_seconds),20))
    pattern = query.casefold()
    wildcard = any(c in pattern for c in '*?[')
    queue = deque(dict.fromkeys(str(Path(os.path.expandvars(p)).expanduser().absolute()) for p in roots))
    scanned_roots = list(queue)
    skipped = {'$recycle.bin','system volume information','.git','node_modules','.venv','__pycache__'}
    if not include_system:
        skipped.update({'windows','program files','program files (x86)','programdata','appdata'})
    truncated = False
    while queue:
        if time.monotonic()-started>=seconds or examined>=100000 or len(matches)>=limit:
            truncated = True
            break
        folder = Path(queue.popleft())
        try:
            if folder.is_symlink() or (hasattr(folder,'is_junction') and folder.is_junction()):
                continue
            canonical = os.path.normcase(str(folder.resolve()))
            if canonical in visited:
                continue
            visited.add(canonical)
            with os.scandir(folder) as items:
                for item in items:
                    examined += 1
                    if time.monotonic()-started>=seconds or examined>=100000 or len(matches)>=limit:
                        truncated = True
                        break
                    path = Path(item.path)
                    if item.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
                        continue
                    name = item.name.casefold()
                    if item.is_dir(follow_symlinks=False):
                        if name not in skipped:
                            queue.append(item.path)
                    elif item.is_file(follow_symlinks=False) and (fnmatch.fnmatchcase(name,pattern) if wildcard else pattern in name):
                        stat = item.stat(follow_symlinks=False)
                        matches.append({'path':item.path,'size':stat.st_size,'modified':stat.st_mtime})
        except OSError as error:
            if len(errors)<12:
                errors.append({'path':str(folder),'error':str(error)})
    return {'matches':matches,'roots':scanned_roots,'examined':examined,'truncated':truncated,
            'errors':errors,'elapsed_seconds':round(time.monotonic()-started,3),
            'note':'Поиск по именам, не по содержимому. При неполном результате уточни корневую папку и продолжи; не объявляй отсутствие файла во всей системе.'}
