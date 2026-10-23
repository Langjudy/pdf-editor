; PDF 编辑器 安装程序（Inno Setup 6）
; 先用 PyInstaller 生成 packaging\dist\PdfEditor，再编译：ISCC packaging\installer.iss

#define AppName "PDF 编辑器"
#define AppVersion "1.0.1"
#define AppExe "PdfEditor.exe"
#define AppURL "https://github.com/Langjudy/pdf-editor"

[Setup]
AppId={{F747A45C-F402-5A18-AF08-BC89C3BD48AA}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Langjudy
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\PdfEditor
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=Output
OutputBaseFilename=PdfEditor-{#AppVersion}-Setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 可选择“仅为我安装”（无需管理员）或“为所有用户安装”
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
ChangesAssociations=yes
CloseApplications=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "assocproj"; Description: "双击 .pdfproj 项目文件时用本程序打开"; GroupDescription: "文件关联："
Name: "openwithpdf"; Description: "在 PDF 文件右键「打开方式」中加入本程序（不改变默认阅读器）"; GroupDescription: "文件关联："

[Files]
Source: "dist\PdfEditor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; .pdfproj 项目文件
Root: HKA; Subkey: "Software\Classes\.pdfproj"; ValueType: string; ValueName: ""; ValueData: "PdfEditor.Project"; Flags: uninsdeletevalue; Tasks: assocproj
Root: HKA; Subkey: "Software\Classes\PdfEditor.Project"; ValueType: string; ValueName: ""; ValueData: "PDF 编辑器项目"; Flags: uninsdeletekey; Tasks: assocproj
Root: HKA; Subkey: "Software\Classes\PdfEditor.Project\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"; Tasks: assocproj
Root: HKA; Subkey: "Software\Classes\PdfEditor.Project\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: assocproj
; PDF：只加入“打开方式”列表
Root: HKA; Subkey: "Software\Classes\PdfEditor.Pdf"; ValueType: string; ValueName: ""; ValueData: "PDF 文档"; Flags: uninsdeletekey; Tasks: openwithpdf
Root: HKA; Subkey: "Software\Classes\PdfEditor.Pdf\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"; Tasks: openwithpdf
Root: HKA; Subkey: "Software\Classes\PdfEditor.Pdf\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: openwithpdf
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: none; ValueName: "PdfEditor.Pdf"; Flags: uninsdeletevalue; Tasks: openwithpdf

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
