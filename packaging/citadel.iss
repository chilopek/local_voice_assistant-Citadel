; Requires Inno Setup 6.4+ (ExecAndLogOutput). No target-side pip/Git/Python install.
; ISCC /DPayloadDir=<absolute staged dist\payload> /DOutputDir=<absolute output> citadel.iss
; Add /DTestBuild for a completely separate app, mutex, shortcuts and data root.
; Optional setup switches: /MODEL=auto|fast|balanced|power /VOICE=on|off
; /SKIPRUNTIMESETUP=1 is for offline packaging tests: never invokes the helper.
; Optional uninstall switch: /PURGEDATA=1. Silent uninstall otherwise keeps data.
#ifndef PayloadDir
  #define PayloadDir SourcePath + "..\dist\payload"
#endif
#ifndef OutputDir
  #define OutputDir SourcePath + "..\dist"
#endif
#ifdef TestBuild
  #define ProductName "CitadelPackagingTest"
  #define ProductId "CitadelPackagingTest"
  #define DataDirName "LocalAssistantPackagingTest"
  #define MutexName "CitadelPackagingTest"
  #define SetupName "LocalAssistantSetupTest"
#else
  #define ProductName "Citadel"
  #define ProductId "CitadelLocalAssistant"
  #define DataDirName "LocalAssistant"
  #define MutexName "CitadelLocalAssistant"
  #define SetupName "LocalAssistantSetup"
#endif

[Setup]
AppId={#ProductId}
AppName={#ProductName}
AppVersion=0.3.0
AppVerName={#ProductName} 0.3.0
DefaultDirName={localappdata}\Programs\{#ProductName}
DisableDirPage=yes
UsePreviousAppDir=no
PrivilegesRequired=lowest
AppMutex={#MutexName}
SetupMutex={#MutexName}Setup
DefaultGroupName={#ProductName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename={#SetupName}
SetupIconFile={#PayloadDir}\app\assets\citadel.ico
UninstallDisplayIcon={app}\app\assets\citadel.ico
UninstallDisplayName={#ProductName}
Uninstallable=yes
CreateUninstallRegKey=yes
#ifdef TestBuild
Compression=none
SolidCompression=no
#else
Compression=lzma2/fast
SolidCompression=yes
#endif
WizardStyle=modern
MinVersion=10.0
ArchitecturesAllowed=x64compatible
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
ShowLanguageDialog=no
LanguageDetectionMethod=none

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "{#PayloadDir}\Citadel.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#PayloadDir}\bundled-packages.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; These two directories are application-owned. Native DelTree below removes
; caches/bytecode too, without following junctions. Never delete {app}\*.
Source: "{#PayloadDir}\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs uninsneveruninstall
Source: "{#PayloadDir}\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs uninsneveruninstall

[Icons]
Name: "{userdesktop}\{#ProductName}"; Filename: "{app}\Citadel.exe"; WorkingDir: "{app}"; IconFilename: "{app}\app\assets\citadel.ico"
Name: "{userprograms}\{#ProductName}"; Filename: "{app}\Citadel.exe"; WorkingDir: "{app}"; IconFilename: "{app}\app\assets\citadel.ico"

[Tasks]
Name: "runtimesetup"; Description: "Настроить Ollama (установить при необходимости), загрузить модель и голосовые ресурсы"

[Code]
const
  InvalidAttributes = $FFFFFFFF;
  DirectoryAttribute = $10;
  ReparseAttribute = $400;

function NativeGetFileAttributes(Name: String): Cardinal;
  external 'GetFileAttributesW@kernel32.dll stdcall';
function GetPhysicallyInstalledSystemMemory(var TotalMemoryKB: Int64): Boolean;
  external 'GetPhysicallyInstalledSystemMemory@kernel32.dll stdcall';

var
  ModelPage: TInputOptionWizardPage;
  VoiceBox: TNewCheckBox;
  ExistingConfig, RuntimeFailed, PurgeRequested: Boolean;
  ModelArgument, VoiceArgument: String;

function DataRoot: String;
begin
  Result := ExpandConstant('{localappdata}\{#DataDirName}');
end;

function Quoted(const S: String): String;
begin
  Result := '"' + S + '"';
end;

function ValidMode(const S: String): Boolean;
begin
  Result := (S = 'auto') or (S = 'fast') or (S = 'balanced') or (S = 'power');
end;

function InitializeSetup: Boolean;
begin
  ModelArgument := Lowercase(ExpandConstant('{param:MODEL|}'));
  VoiceArgument := Lowercase(ExpandConstant('{param:VOICE|}'));
  Result := ((ModelArgument = '') or ValidMode(ModelArgument)) and
    ((VoiceArgument = '') or (VoiceArgument = 'on') or (VoiceArgument = 'off'));
  if not Result then
    SuppressibleMsgBox('Неверное значение /MODEL или /VOICE. Используйте /MODEL=auto|fast|balanced|power и /VOICE=on|off.', mbCriticalError, MB_OK, IDOK);
end;

procedure ModelChanged(Sender: TObject);
begin
  VoiceBox.Enabled := ModelPage.SelectedValueIndex <> 0;
end;

procedure InitializeWizard;
begin
  ExistingConfig := FileExists(DataRoot + '\assistant.local.json');
  ModelPage := CreateInputOptionPage(wpSelectDir, 'Модель и голос',
    'Выберите настройки локального помощника',
    'Для работы нужна Ollama. Установщик может установить её и загрузить выбранную модель. Для голоса могут потребоваться дополнительные загрузки.' + #13#10 +
    'При обновлении настройки сохраняются. Чтобы заменить их, выберите модель ниже.', True, False);
  ModelPage.Add('Сохранить текущие настройки модели и голоса');
  ModelPage.Add('Автоматически: 8B при ОЗУ от 16 ГБ, иначе 4B');
  ModelPage.Add('Быстрая: qwen3:1.7b-instruct');
  ModelPage.Add('Сбалансированная: qwen3:4b-instruct');
  ModelPage.Add('Мощная: qwen3:8b-instruct');
  ModelPage.SelectedValueIndex := 1;
  if ExistingConfig then ModelPage.SelectedValueIndex := 0;
  if ModelArgument = 'auto' then ModelPage.SelectedValueIndex := 1;
  if ModelArgument = 'fast' then ModelPage.SelectedValueIndex := 2;
  if ModelArgument = 'balanced' then ModelPage.SelectedValueIndex := 3;
  if ModelArgument = 'power' then ModelPage.SelectedValueIndex := 4;
  if (VoiceArgument <> '') and (ModelPage.SelectedValueIndex = 0) then
    ModelPage.SelectedValueIndex := 1;
  VoiceBox := TNewCheckBox.Create(WizardForm);
  VoiceBox.Parent := ModelPage.Surface;
  VoiceBox.Top := ModelPage.SurfaceHeight - ScaleY(28);
  ModelPage.CheckListBox.Height := VoiceBox.Top - ModelPage.CheckListBox.Top - ScaleY(8);
  VoiceBox.Left := 0;
  VoiceBox.Width := ModelPage.SurfaceWidth;
  VoiceBox.Caption := 'Включить голос при применении выбранных настроек';
  VoiceBox.Checked := VoiceArgument <> 'off';
  ModelPage.CheckListBox.OnClickCheck := @ModelChanged;
  ModelChanged(nil);
end;

function GetModel(Param: String): String;
var
  MemoryKB: Int64;
begin
  case ModelPage.SelectedValueIndex of
    2: Result := 'qwen3:1.7b-instruct';
    3: Result := 'qwen3:4b-instruct';
    4: Result := 'qwen3:8b-instruct';
  else
    begin
      Result := 'qwen3:4b-instruct';
      if GetPhysicallyInstalledSystemMemory(MemoryKB) then
        if MemoryKB >= 16777216 then Result := 'qwen3:8b-instruct';
    end;
  end;
end;

function GetVoice: String;
begin
  Result := 'off';
  if VoiceBox.Checked then Result := 'on';
end;

procedure RuntimeError(const Detail: String);
var
  MessageText: String;
begin
  RuntimeFailed := True;
  MessageText := 'Файлы Citadel установлены, но настройка завершилась ОШИБКОЙ. ' + Detail + #13#10 +
    'Подробности: ' + DataRoot + '\setup-runtime.log и журнал установщика. Проверьте доступ к интернету и работу Ollama/winget, затем повторите установку.';
  Log(MessageText);
  WizardForm.FinishedLabel.Caption := MessageText;
  RaiseException(MessageText);
end;

procedure RuntimeProgress(const S: String; const Error, FirstLine: Boolean);
var
  Status: String;
begin
  // Supplying a callback replaces automatic output logging; retain every line.
  if Error then Log('Ошибка вывода помощника: ' + S)
  else Log(S);
  Status := Trim(S);
  if Status <> '' then
    WizardForm.StatusLabel.Caption := Copy(Status, 1, 300);
end;

#ifndef TestBuild
function ArgumentBoundary(const C: String): Boolean;
begin
  Result := (C = ' ') or (C = #9) or (C = '"') or (C = #39);
end;

function ContainsExactArgumentPath(const Arguments, Path: String): Boolean;
var
  Search, Expected: String;
  I: Integer;
begin
  Result := False;
  Search := ' ' + Lowercase(Arguments) + ' ';
  Expected := Lowercase(Path);
  for I := 2 to Length(Search) - Length(Expected) do
    if Copy(Search, I, Length(Expected)) = Expected then
      if ArgumentBoundary(Copy(Search, I - 1, 1)) and
        ArgumentBoundary(Copy(Search, I + Length(Expected), 1)) then
      begin
        Result := True;
        Exit;
      end;
end;

procedure MigrateLegacyShortcut;
var
  ShortcutPath, Target, Arguments, TargetName: String;
  Shell, Shortcut: Variant;
  Owned: Boolean;
begin
  ShortcutPath := ExpandConstant('{userdesktop}\Локальный помощник.lnk');
  if not FileExists(ShortcutPath) then Exit;
  try
    // CreateShortcut reads an existing link; never call Save or rewrite it.
    Shell := CreateOleObject('WScript.Shell');
    Shortcut := Shell.CreateShortcut(ShortcutPath);
    Target := Shortcut.TargetPath;
    Arguments := Shortcut.Arguments;
    TargetName := ExtractFileName(Target);
    Owned := CompareText(Target, DataRoot + '\.venv\Scripts\pythonw.exe') = 0;
    if (CompareText(TargetName, 'powershell.exe') = 0) or
      (CompareText(TargetName, 'pwsh.exe') = 0) then
      Owned := ContainsExactArgumentPath(Arguments, DataRoot + '\scripts\run.ps1');
    // Name alone is never proof of ownership. Leave unrelated links intact.
    if Owned then
    begin
      if DeleteFile(ShortcutPath) then Log('Удалён проверенный ярлык прежней версии: ' + ShortcutPath)
      else Log('Не удалось удалить ярлык прежней версии: ' + ShortcutPath);
    end
    else Log('Ярлык с таким именем не принадлежит прежней версии; сохранён: ' + ShortcutPath);
  except
    Log('Не удалось проверить прежний ярлык; он сохранён: ' + GetExceptionMessage);
  end;
end;
#endif

procedure CurStepChanged(CurStep: TSetupStep);
var
  Manifest, Parameters: String;
  ResultCode: Integer;
  Started: Boolean;
begin
  if CurStep <> ssPostInstall then Exit;
  Manifest := '{"data_dir_name":"{#DataDirName}","mutex_name":"{#MutexName}"}' + #13#10;
  if not SaveStringToFile(ExpandConstant('{app}\citadel.install.json'), Manifest, False) then
    RuntimeError('Не удалось записать citadel.install.json.');
#ifndef TestBuild
  MigrateLegacyShortcut;
#endif
  if ExpandConstant('{param:SKIPRUNTIMESETUP|0}') = '1' then
  begin
    Log('АВТОНОМНЫЙ ТЕСТ УПАКОВКИ: настройка пропущена; Ollama не запускалась.');
    Exit;
  end;
  if not WizardIsTaskSelected('runtimesetup') then
  begin
    Log('Пользователь отключил настройку: Ollama, модель и голос не настраивались.');
    WizardForm.FinishedLabel.Caption := 'Файлы Citadel установлены. Настройка Ollama и модели не выбрана. Перед использованием Citadel настройте Ollama и загрузите модель.';
    Exit;
  end;
  Parameters := '-B -X utf8 -u ' + Quoted(ExpandConstant('{app}\app\assistant\setup_runtime.py')) +
    ' --model ' + GetModel('') + ' --voice ' + GetVoice;
  // Keep existing config by default. The helper must use its saved model/voice
  // for downloads when config exists and --replace-config is absent.
  if ModelPage.SelectedValueIndex <> 0 then Parameters := Parameters + ' --replace-config';
  WizardForm.StatusLabel.Caption := 'Настройка Ollama, модели и голоса. Это может занять несколько минут...';
  ResultCode := -1;
  // ExecAndLogOutput is the [Run] logoutput/runhidden/waituntilterminated
  // equivalent, with a checked process exit code (a [Run] entry ignores it).
  try
    Started := ExecAndLogOutput(ExpandConstant('{app}\runtime\python.exe'), Parameters,
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode, @RuntimeProgress);
  except
    RuntimeError('Не удалось выполнить помощник настройки: ' + GetExceptionMessage);
  end;
  if not Started then
    RuntimeError('Не удалось запустить помощник на встроенном Python (' + IntToStr(ResultCode) + ').')
  else if ResultCode <> 0 then RuntimeError('Код завершения помощника: ' + IntToStr(ResultCode) + '.');
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := 0;
  if RuntimeFailed then Result := 10;
end;

function ValidatePurgeRoot(var Reason: String): Boolean;
var
  Root, Sentinel: String;
  Attributes: Cardinal;
begin
  Result := False;
  Root := DataRoot;
  if CompareText(RemoveBackslashUnlessRoot(ExpandFileName(Root)),
    RemoveBackslashUnlessRoot(ExpandConstant('{localappdata}\{#DataDirName}'))) <> 0 then
  begin
    Reason := 'Путь к данным не совпадает с известным каталогом данных приложения.';
    Exit;
  end;
  Attributes := NativeGetFileAttributes(Root);
  if Attributes = InvalidAttributes then
  begin
    // Missing data is harmless; all other errors must be reported.
    if (DLLGetLastError = 2) or (DLLGetLastError = 3) then Result := True
    else Reason := 'Не удалось проверить атрибуты каталога данных.';
    Exit;
  end;
  if ((Attributes and DirectoryAttribute) = 0) or ((Attributes and ReparseAttribute) <> 0) then
  begin
    Reason := 'Корневой каталог данных должен быть обычной папкой, а не точкой соединения или символической ссылкой.';
    Exit;
  end;
  Sentinel := Root + '\.citadel-data-root';
  Attributes := NativeGetFileAttributes(Sentinel);
  if (Attributes = InvalidAttributes) or ((Attributes and (DirectoryAttribute or ReparseAttribute)) <> 0) then
  begin
    Reason := 'Отсутствует обычный файл-маркер .citadel-data-root. Данные сохранены.';
    Exit;
  end;
  Result := True;
end;

function InitializeUninstall: Boolean;
var
  Reason: String;
begin
  Result := True;
  PurgeRequested := ExpandConstant('{param:PURGEDATA|0}') = '1';
  if (not UninstallSilent) and (not PurgeRequested) then
    PurgeRequested := SuppressibleMsgBox(
      'Удалить ВСЕ локальные настройки Citadel, историю, профиль браузера и рабочие файлы из папки:' + #13#10 +
      DataRoot + #13#10 + #13#10 + 'Это действие нельзя отменить. Выберите «Нет», чтобы СОХРАНИТЬ данные (рекомендуется). ' +
      'Общие Python, Git, Ollama и её модели будут сохранены.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES;
  if PurgeRequested and (not ValidatePurgeRoot(Reason)) then
  begin
    Log('Удаление данных запрещено: ' + Reason);
    SuppressibleMsgBox('Удаление остановлено до удаления файлов приложения. ' + Reason,
      mbCriticalError, MB_OK, IDOK);
    Result := False;
  end;
end;

procedure UninstallError(const Detail: String);
begin
  Log('Удаление остановлено: ' + Detail);
  SuppressibleMsgBox('Удаление остановлено: ' + Detail + #13#10 +
    'Закройте Citadel и программы, использующие эти файлы, затем повторите попытку. Если было выбрано удаление данных, они могли быть удалены частично.',
    mbCriticalError, MB_OK, IDOK);
  Abort;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Reason: String;
begin
  if CurUninstallStep <> usUninstall then Exit;
  if PurgeRequested then
  begin
    // Recheck immediately before deleting. DelTree removes nested reparse
    // points themselves, never their targets (primary Inno documentation):
    // https://jrsoftware.org/ishelp/topic_isxfunc_deltree.htm
    if not ValidatePurgeRoot(Reason) then UninstallError(Reason);
    if DirExists(DataRoot) then
      if not DelTree(DataRoot, True, True, True) then
        UninstallError('Не удалось полностью удалить ' + DataRoot + '.');
  end;
  if DirExists(ExpandConstant('{app}\app')) then
    if not DelTree(ExpandConstant('{app}\app'), True, True, True) then
      UninstallError('Не удалось удалить каталог приложения app.');
  if DirExists(ExpandConstant('{app}\runtime')) then
    if not DelTree(ExpandConstant('{app}\runtime'), True, True, True) then
      UninstallError('Не удалось удалить встроенную среду runtime.');
  if FileExists(ExpandConstant('{app}\citadel.install.json')) then
    if not DeleteFile(ExpandConstant('{app}\citadel.install.json')) then
      UninstallError('Не удалось удалить citadel.install.json.');
  // Inno removes tracked root files and empty directories normally. Unknown
  // files at {app}, all parent directories and all shared tools are preserved.
end;
