#define MyAppName "Local Assistant"
#define MyAppVersion "0.1.0"
#define MyAppExeName "LocalAssistant.exe"

[Setup]
AppId={{8468D5DE-ED95-4D85-9103-E4D25A03131C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\LocalAssistant
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputBaseFilename=LocalAssistant-{#MyAppVersion}-Setup
OutputDir=..\dist
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Components]
Name: "application"; Description: "Local Assistant application"; Types: full compact custom; Flags: fixed
Name: "models"; Description: "Offline model files"; Types: full custom
Name: "models\stt"; Description: "Speech-to-text models"; Types: full custom
Name: "models\llm"; Description: "Local language model"; Types: full custom
Name: "models\tts"; Description: "Text-to-speech voice"; Types: full custom

[Files]
Source: "..\dist\LocalAssistant.exe"; DestDir: "{app}"
Source: "..\offline-models\stt\*"; DestDir: "{localappdata}\LocalAssistant\LocalAssistant\models\stt"; Components: "models\stt"; Flags: recursesubdirs skipifsourcedoesntexist
Source: "..\offline-models\llm\*"; DestDir: "{localappdata}\LocalAssistant\LocalAssistant\models\llm"; Components: "models\llm"; Flags: recursesubdirs skipifsourcedoesntexist
Source: "..\offline-models\tts\*"; DestDir: "{localappdata}\LocalAssistant\LocalAssistant\models\tts"; Components: "models\tts"; Flags: recursesubdirs skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Tasks]
Name: "startup"; Description: "Start Local Assistant with Windows (required for on-time schedules after sign-in)"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Local Assistant"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  ModelsDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\LocalAssistant\LocalAssistant');
    ModelsDir := DataDir + '\models';
    if MsgBox('Delete downloaded local model files?', mbConfirmation, MB_YESNO) = IDYES then
      DelTree(ModelsDir, True, True, True);
    if MsgBox('Delete settings, permissions, history, and audit data?', mbConfirmation, MB_YESNO) = IDYES then
      DelTree(DataDir, True, True, True);
  end;
end;
