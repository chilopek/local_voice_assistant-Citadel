"""Paste literal Unicode into a previously observed editor. Never press Enter."""
import time
import re


EDITORS = {'notepad.exe','notepad++.exe','code.exe','codium.exe','wordpad.exe',
           'winword.exe','devenv.exe','pycharm64.exe','idea64.exe','sublime_text.exe'}
TERMINALS = {'cmd.exe','powershell.exe','pwsh.exe','windowsterminal.exe','conhost.exe',
             'bash.exe','wsl.exe','mintty.exe','wezterm-gui.exe','alacritty.exe'}


def assess_insert(window_handle: int, control_id: str = '') -> dict:
    from .desktop import _observed_window
    import psutil
    _, window = _observed_window(window_handle)
    program = psutil.Process(window.process_id()).name().casefold()
    if program in TERMINALS:
        raise PermissionError('Вставка многострочного кода в терминал может его выполнить. Используй файл или подтверждённый запуск кода.')
    return {'requires_confirmation':program not in EDITORS,'program':program}


def insert_text(window_handle: int, text: str, control_id: str = '', approved: bool = False, language: str = 'text') -> dict:
    from .desktop import _observed_window, _observed_control, desktop_snapshot
    import ctypes
    import pythoncom
    import win32clipboard
    from pywinauto.keyboard import VirtualKeyAction
    if not text or len(text)>100000:
        raise ValueError('Текст: от 1 до 100000 символов')
    if language=='python' or re.match(r'\s*(?:def\s+\w+\(|class\s+\w+|from\s+\w+\s+import\b)',text):
        from .code_text import python_source
        text,_=python_source(text)
    decision = assess_insert(window_handle,control_id)
    if decision['requires_confirmation'] and not approved:
        raise PermissionError('Неизвестное приложение для вставки текста требует подтверждения.')
    _, window = _observed_window(window_handle)
    if control_id:
        target = _observed_control(window,window_handle,control_id)
    else:
        controls = [c for c in window.descendants()[:250] if c.is_visible() and c.is_enabled() and c.element_info.control_type in {'Edit','Document'}]
        if len(controls)!=1:
            raise ValueError('Выбери control_id редактора из desktop_snapshot; доступно несколько полей или ни одного.')
        target = controls[0]
    if target.element_info.control_type not in {'Edit','Document'}:
        raise ValueError('Выбранный элемент не является текстовым редактором')
    ancestors=[target]
    for _ in range(4):
        parent=ancestors[-1].parent()
        if parent is None or parent==window:
            break
        ancestors.append(parent)
    if any(re.search(r'\bterminal\b|\bconsole\b|\bbash\b|\bcmd\b|\bpowershell\b|\bpwsh\b|терминал|консоль|командная строка',item.element_info.name or '',re.I) for item in ancestors if item!=window):
        raise PermissionError('Похоже на встроенный терминал. Вставка многострочного кода без выполнения здесь не гарантируется.')
    try:
        if target.element_info.element.CurrentIsPassword:
            raise PermissionError('Пароли пользователь вводит самостоятельно')
    except AttributeError:
        pass
    native_handle = int(target.handle or 0)
    native_class = target.class_name().casefold()
    direct_native = bool(native_handle and (native_class=='edit' or native_class.startswith('richedit')))
    if not direct_native:
        window.set_focus()
        target.set_focus()
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    if not direct_native and int(user32.GetForegroundWindow() or 0)!=int(window_handle):
        raise RuntimeError('Фокус ушёл из выбранного окна; текст не вставлен')
    saved = pythoncom.OleGetClipboard()
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text,win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        if not direct_native and int(user32.GetForegroundWindow() or 0)!=int(window_handle):
            raise RuntimeError('Фокус изменился до вставки; действие остановлено')
        if direct_native:
            # Address the observed native control directly. A localized keyboard
            # layout and a simultaneous focus change cannot redirect this paste.
            user32.SendMessageTimeoutW.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_size_t,ctypes.c_ssize_t,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_size_t)]
            user32.SendMessageTimeoutW.restype=ctypes.c_ssize_t
            value=ctypes.c_size_t()
            if not user32.SendMessageTimeoutW(native_handle,0x0302,0,0,2,1000,ctypes.byref(value)):
                raise RuntimeError('Редактор не подтвердил обработку вставки. Проверь текст перед повтором.')
        else:
            # Use physical virtual keys, not a letter resolved through the current
            # RU/EN layout (VkKeyScan("v") can return an invalid key under RU).
            control=VirtualKeyAction(0x11,up=False)
            control.run()
            try:
                VirtualKeyAction(0x56).run()
            finally:
                VirtualKeyAction(0x11,down=False).run()
        time.sleep(.15)
    finally:
        # Restore all clipboard formats through OLE, including images. Do not
        # overwrite a new clipboard value another application/user put there.
        try:
            win32clipboard.OpenClipboard()
            try:
                ours = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)==text
            finally:
                win32clipboard.CloseClipboard()
            if ours:
                pythoncom.OleSetClipboard(saved)
        except Exception:
            pass
    try:
        try:
            observed = target.get_value()
        except Exception:
            try:
                observed = target.iface_text.DocumentRange.GetText(-1)
            except Exception:
                observed = target.window_text()
        verified = text.replace('\r\n','\n') in observed.replace('\r\n','\n')
    except Exception:
        verified = False
    snapshot = desktop_snapshot(window_handle)
    return {'request_accepted':True,'verified':verified,'window_handle':window_handle,
            'characters':len(text),'snapshot':snapshot,
            'note':'Текст найден в редакторе.' if verified else 'Команда вставки отправлена, но полный текст недоступен через UI Automation. Не повторяй вставку вслепую; проверь экран.'}
