; SCC Multistream installer (Inno Setup 6).
;
; The installer doesn't carry the app: it downloads the latest release from
; GitHub while installing, so the same SCC-Multistream-Setup.exe always
; installs the newest version. Installs to C:\SCC Multistream (no admin rights
; needed) and adds a desktop shortcut. Settings (config.json, with the stream
; keys) are kept when it's installed again over an older version.
;
; Build:  ISCC.exe /DAppVersion=1.0.1 installer\SCC-Multistream.iss
; (the GitHub Actions workflow does this for every release)

#ifndef AppVersion
  #define AppVersion "1.0"
#endif
#define AppName "SCC Multistream"
#define Repo "owenbparker/multistream_scc"
#define ZipName "SCCMultistream-windows.zip"

[Setup]
AppId={{8C1E5A27-4B6D-4F90-9E3A-2D7C6B1F0A54}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName}
AppPublisher=St. Clair Saints
AppPublisherURL=https://github.com/{#Repo}
DefaultDirName=C:\SCC Multistream
DisableDirPage=yes
DisableProgramGroupPage=yes
UsePreviousAppDir=no
; C:\ lets any user create a folder, and the app updates itself in place,
; so no administrator prompt is needed.
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=SCC-Multistream-Setup
SetupIconFile=..\packaging\icon.ico
UninstallDisplayIcon={app}\SCCMultistream.exe
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Icons]
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\SCCMultistream.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\SCCMultistream.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\SCCMultistream.exe"; WorkingDir: "{app}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The app's files (unpacked from the download, so Setup doesn't track them).
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\_update"
Type: files; Name: "{app}\SCCMultistream.exe"
Type: files; Name: "{app}\README.md"
Type: files; Name: "{app}\smoke-test.txt"
Type: files; Name: "{app}\version.txt"
Type: files; Name: "{app}\mediamtx.yml"
Type: files; Name: "{app}\config.json"

[Messages]
ReadyLabel2a=Setup will download the latest SCC Multistream from GitHub and install it to C:\SCC Multistream.

[Code]
const
  ZipUrl = 'https://github.com/{#Repo}/releases/latest/download/{#ZipName}';

var
  DownloadPage: TDownloadWizardPage;
  Downloaded: Boolean;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Downloading SCC Multistream',
    'Getting the latest version from GitHub...', nil);
end;

{ Downloads the latest release zip into Setup's temp folder. '' on success. }
function DownloadLatest: String;
begin
  Result := '';
  if Downloaded then
    Exit;
  try
    if WizardSilent then
      DownloadTemporaryFile(ZipUrl, '{#ZipName}', '', nil)
    else begin
      DownloadPage.Clear;
      DownloadPage.Add(ZipUrl, '{#ZipName}', '');
      DownloadPage.Show;
      try
        DownloadPage.Download;
      finally
        DownloadPage.Hide;
      end;
    end;
    Downloaded := True;
  except
    Result := 'Couldn''t download SCC Multistream from GitHub. Check the internet connection and try again.'
      + #13#10#13#10 + GetExceptionMessage;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Err: String;
begin
  Result := True;
  if CurPageID = wpReady then begin
    Err := DownloadLatest;
    if Err <> '' then begin
      SuppressibleMsgBox(Err, mbCriticalError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;

{ Runs a program without a window; returns its exit code, or -1 if it couldn't start. }
function RunHidden(const Exe, Params: String): Integer;
var
  Code: Integer;
begin
  if Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Result := Code
  else
    Result := -1;
end;

{ Everything that can fail happens here, so a problem stops Setup cleanly
  with a message instead of leaving a half-installed app. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Tmp, App: String;
  Code: Integer;
begin
  Result := DownloadLatest;  { silent installs skip the Ready page }
  if Result <> '' then
    Exit;
  Tmp := ExpandConstant('{tmp}');
  App := ExpandConstant('{app}');

  { Close the app if it's running (this stops any streams) so its files can be replaced. }
  RunHidden(ExpandConstant('{sys}\taskkill.exe'), '/IM SCCMultistream.exe /F');
  Sleep(800);

  Code := RunHidden('powershell.exe', '-NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '''
    + Tmp + '\{#ZipName}'' -DestinationPath ''' + Tmp + '\new'' -Force"');
  if (Code <> 0) or not FileExists(Tmp + '\new\SCCMultistream.exe') then begin
    Result := 'Couldn''t unpack the download (code ' + IntToStr(Code) + ').';
    Exit;
  end;

  if not ForceDirectories(App) then begin
    Result := 'Couldn''t create ' + App + '.';
    Exit;
  end;
  Code := RunHidden('robocopy.exe', '"' + Tmp + '\new\_internal" "' + App + '\_internal" /MIR /R:3 /W:1 /NFL /NDL /NJH /NJS /NP');
  if (Code < 0) or (Code >= 8) then begin
    Result := 'Copying files failed (robocopy ' + IntToStr(Code) + '). Is SCC Multistream still open?';
    Exit;
  end;
  Code := RunHidden('robocopy.exe', '"' + Tmp + '\new" "' + App + '" /XD _internal /R:3 /W:1 /NFL /NDL /NJH /NJS /NP');
  if (Code < 0) or (Code >= 8) then
    Result := 'Copying files failed (robocopy ' + IntToStr(Code) + ').';
end;

function InitializeUninstall: Boolean;
begin
  RunHidden(ExpandConstant('{sys}\taskkill.exe'), '/IM SCCMultistream.exe /F');
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { Nothing of the app's is left, stream keys included. }
  if (CurUninstallStep = usPostUninstall) and DirExists(ExpandConstant('{app}')) then
    DelTree(ExpandConstant('{app}'), True, True, True);
end;
