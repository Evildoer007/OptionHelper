; Inno Setup 6.3+. Payloads have passed source and capability checks before compilation.
[Setup]
AppId=OptionHelper.Desktop
AppName=OptionHelper
AppVersion={#AppVersion}
VersionInfoVersion={#AppNumericVersion}
AppPublisher=OptionHelper
DefaultDirName={localappdata}\Programs\OptionHelper
DefaultGroupName=OptionHelper
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
DisableProgramGroupPage=yes
DisableWelcomePage=yes
DisableDirPage=yes
OutputDir={#SetupOutput}
OutputBaseFilename={#SetupName}
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\OptionHelper.exe
Compression=lzma2
SolidCompression=yes
Uninstallable=NormalInstall
CreateUninstallRegKey=NormalInstall
UsePreviousAppDir=no
UsePreviousGroup=no
UsePreviousTasks=no
UsePreviousLanguage=no
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; Check: NormalInstall

[Icons]
Name: "{userprograms}\OptionHelper"; Filename: "{app}\OptionHelper.exe"; WorkingDir: "{app}"; Check: NormalInstall
Name: "{userdesktop}\OptionHelper"; Filename: "{app}\OptionHelper.exe"; WorkingDir: "{app}"; Tasks: desktopicon; Check: NormalInstall

[Run]
Filename: "{app}\OptionHelper.exe"; Description: "启动OptionHelper"; Flags: nowait postinstall skipifsilent; Check: NormalInstall

[Code]
function NormalInstall: Boolean;
begin
  Result := ExpandConstant('{param:VERIFYINSTALL|0}') <> '1';
end;
