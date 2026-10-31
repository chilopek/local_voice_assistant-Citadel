"""Opt-in suggestions driven by local events, without model/network polling."""
import ctypes
import datetime as dt
import shutil
import sys
import threading
import time


class SuggestionPolicy:
    def __init__(self):
        self.active_since=None
        self.last={}

    def poll(self, *, now, hour, idle_seconds, free_bytes, fullscreen=False, busy=False):
        if idle_seconds>=300:
            self.active_since=None
        elif self.active_since is None:
            self.active_since=now
        if busy or fullscreen or hour<8 or hour>=22 or idle_seconds>=300:
            return None
        if free_bytes<2*1024**3 and now-self.last.get('disk',-1e12)>=6*3600:
            self.last['disk']=now
            return 'На диске с файлами Цитадели осталось меньше 2 ГБ. Могу показать, что занимает место.'
        if self.active_since is not None and now-self.active_since>=50*60 and now-self.last.get('break',-1e12)>=50*60:
            self.last['break']=now
            self.active_since=now
            return 'Вы уже около 50 минут за компьютером. Можно сделать небольшой перерыв.'
        return None


def desktop_activity():
    if sys.platform!='win32': return 0,False
    from ctypes import wintypes
    class LastInput(ctypes.Structure):
        _fields_=[('cbSize',wintypes.UINT),('dwTime',wintypes.DWORD)]
    user=ctypes.WinDLL('user32')
    info=LastInput(); info.cbSize=ctypes.sizeof(info)
    if not user.GetLastInputInfo(ctypes.byref(info)): return 3600,True
    idle=((ctypes.windll.kernel32.GetTickCount()-info.dwTime)&0xffffffff)/1000
    user.GetForegroundWindow.restype=wintypes.HWND
    handle=user.GetForegroundWindow()
    rect=wintypes.RECT()
    user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    full=False
    if handle and user.GetWindowRect(handle,ctypes.byref(rect)):
        full=rect.left<=0 and rect.top<=0 and rect.right>=user.GetSystemMetrics(0) and rect.bottom>=user.GetSystemMetrics(1)
    return idle,full


class ProactiveMonitor:
    def __init__(self,emit,path,enabled,busy):
        self.emit,self.path,self.enabled,self.busy=emit,path,enabled,busy
        self.stop_event=threading.Event()
        self.policy=SuggestionPolicy()
        self.thread=threading.Thread(target=self.run,daemon=True,name='citadel-suggestions')

    def start(self): self.thread.start()
    def stop(self): self.stop_event.set()
    def run(self):
        while not self.stop_event.wait(30):
            if not self.enabled.is_set():
                self.policy=SuggestionPolicy()
                continue
            try:
                idle,fullscreen=desktop_activity()
                text=self.policy.poll(now=time.monotonic(),hour=dt.datetime.now().hour,idle_seconds=idle,
                                      free_bytes=shutil.disk_usage(self.path).free,fullscreen=fullscreen,busy=self.busy())
                if text:self.emit(text)
            except OSError:
                pass
