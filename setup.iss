[Setup]
AppName=Mobile Gamepad Server
AppVersion=1.0
DefaultDirName={autopf}\GamePadServer
DefaultGroupName=Mobile Gamepad Server
OutputDir=C:\Users\HomePC\GamePad\installer_output
OutputBaseFilename=GamePadServer_Setup
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest

[Files]
; Copy everything inside your dist\gamepad_server folder (including _internal, exe, www, and DLLs)
Source: "C:\Users\HomePC\GamePad\dist\gamepad_server\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Create a Desktop Shortcut
Name: "{autodesktop}\Mobile Gamepad Server"; Filename: "{app}\gamepad_server.exe"; Parameters: "--web-root www"; IconFilename: "{app}\gamepad_server.exe"
; Create a Start Menu Shortcut
Name: "{group}\Mobile Gamepad Server"; Filename: "{app}\gamepad_server.exe"; Parameters: "--web-root www"

[Run]
; Option to launch it immediately after installation finishes
Filename: "{app}\gamepad_server.exe"; Parameters: "--web-root www"; Description: "Launch Gamepad Server now"; Flags: nowait postinstall skipifsilent