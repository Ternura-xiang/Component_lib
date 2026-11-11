#define AppName "元件库"
#define AppVersion "0.9.4"

[Setup]
AppId={{B83CB0AA-4C40-49C8-9183-4BC54707A321}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\ComponentLibrary
DefaultGroupName={#AppName}
OutputBaseFilename=component-library-{#AppVersion}-windows-x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
SetupIconFile=..\..\build\component-library\component-library.ico

[Files]
Source: "..\..\dist\component-library\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\component-library.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\component-library.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"
