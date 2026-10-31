"""Own temporary Win32 editor only; never touches a user's editor or runs code."""
import json
import subprocess
import sys
from assistant.desktop import desktop_snapshot
from assistant.tools import execute,needs_confirmation

source = r'''
import ctypes as c
from ctypes import wintypes as w
import json
u=c.windll.user32
u.CreateWindowExW.restype=c.c_void_p
u.CreateWindowExW.argtypes=[w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,c.c_int,c.c_int,c.c_int,c.c_int,w.HWND,w.HMENU,w.HINSTANCE,c.c_void_p]
parent=u.CreateWindowExW(0,'STATIC','Citadel owned editor test',0x10CF0000,100,100,700,420,None,None,None,None)
edit=u.CreateWindowExW(0,'EDIT','',0x50B01044,10,10,660,350,parent,None,None,None)
assert parent and edit
print(json.dumps({'handle':parent}),flush=True)
msg=w.MSG()
while u.GetMessageW(c.byref(msg),None,0,0)>0:
    u.TranslateMessage(c.byref(msg))
    u.DispatchMessageW(c.byref(msg))
'''
child=subprocess.Popen([sys.executable,'-u','-c',source],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
try:
    line=child.stdout.readline()
    assert line,line
    handle=json.loads(line)['handle']
    snapshot=desktop_snapshot(handle)
    target=next(item for item in snapshot['controls'] if item['type']=='Edit')
    text="def decode(text):\r\n    return {\"русский\": text[::-1], \"symbols\": \"+^%{}[]~\"}\r\n"
    args={'window_handle':handle,'control_id':target['control_id'],'text':text}
    assert needs_confirmation('insert_text',args),'Unknown Python test editor should require approval'
    result=execute('insert_text',args,confirmed=True)
    assert result['verified'],result
    print('PASS: literal Unicode code, newlines, indentation and special characters verified in owned native editor; no Enter/code execution')
finally:
    child.terminate()
    child.wait(timeout=10)
