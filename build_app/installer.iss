; Script generated for Inno Setup 6
; ZM AIO TOOL - Windows Installer
; Supports English & Vietnamese

#ifndef MyAppVersion
#define MyAppVersion "7.0.1"
#endif

#define MyAppName "ZM AIO TOOL"
#define MyAppPublisher "ZM AIO Tools"
#define MyAppURL "https://github.com/manhgdev/zm_aio_tools"
#define MyAppExeName "ZM AIO TOOL.exe"

#ifndef MyAppSourceDir
#define MyAppSourceDir "release\ZM_AIO_TOOL_v" + MyAppVersion
#endif

#ifndef MyAppOutputDir
#define MyAppOutputDir "release"
#endif

#ifndef MyAppOutputBaseFilename
#define MyAppOutputBaseFilename "ZM_AIO_TOOL_v" + MyAppVersion + "-windows-x64-Setup"
#endif

[Setup]
; NOTE: The value of AppId uniquely identifies this application.
; Do not use the same AppId value in installers for other applications!
AppId={{8B8A31D0-2BC3-4D90-9CE4-64491974DF42}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir={#MyAppOutputDir}
OutputBaseFilename={#MyAppOutputBaseFilename}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes
RestartApplications=no
DisableDirPage=no
DisableProgramGroupPage=yes

[Languages]
Name: "vi"; MessagesFile: "languages\Vietnamese.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; NOTE: Don't use "Flags: ignoreversion" on any shared system files

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up runtime cache and temporary files generated during run if inside install directory
Type: filesandordirs; Name: "{app}\tmp"
Type: files; Name: "{app}\app.log"
Type: files; Name: "{app}\last_crash.txt"
