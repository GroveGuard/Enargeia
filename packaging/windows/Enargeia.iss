#define MyAppName "Enargeia"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "Enargeia"
#define MyAppExeName "Enargeia.exe"
#define BrandRoot "..\..\assets\branding\Enargeia_Corporate_Design"

[Setup]
AppId={{C7A5AF77-2AF4-4BCE-90A8-4D7A0A1D11F3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppCopyright=Copyright (C) Enargeia
AppComments=Enargeia – Wissen verbindet
DefaultDirName={localappdata}\Programs\Enargeia
UsePreviousAppDir=no
DefaultGroupName={#MyAppName}
UninstallDisplayName=Enargeia
DisableProgramGroupPage=yes
DisableWelcomePage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\..\dist\installer
OutputBaseFilename=Enargeia-Setup-{#MyAppVersion}-x64
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#BrandRoot}\icons\Enargeia.ico
WizardImageFile={#BrandRoot}\installer\Enargeia_Installer_Sidebar.bmp
WizardSmallImageFile={#BrandRoot}\installer\Enargeia_Installer_Small_Icon.bmp
WizardImageBackColor=$00261307
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknüpfung erstellen"; GroupDescription: "Zusätzliche Verknüpfungen:"; Flags: unchecked

; Remove stale program binaries during an upgrade. Persistent data at %LOCALAPPDATA%\Enargeia is outside {app} and is not removed.
[InstallDelete]
Type: filesandordirs; Name: "{app}\*"

[Files]
Source: "redist\vc_redist.x64.exe"; Flags: dontcopy
Source: "{#BrandRoot}\installer\Enargeia_Splash_Screen.bmp"; Flags: dontcopy
Source: "{#BrandRoot}\installer\Enargeia_Setup_Complete.bmp"; Flags: dontcopy
Source: "..\..\dist\Enargeia\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\dist\Enargeia3D\*"; DestDir: "{app}\Enargeia3D"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Enargeia starten"; Flags: nowait postinstall skipifsilent

[Code]
var
  SplashPage: TWizardPage;
  SplashImage: TBitmapImage;
  FinishedImage: TBitmapImage;

function HasSuitableVCRuntime: Boolean;
var
  Installed, Major, Minor: Cardinal;
begin
  Result := RegQueryDWordValue(HKLM64,
    'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed)
    and (Installed = 1);
  if not Result then
    Exit;
  Result := RegQueryDWordValue(HKLM64,
    'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Major', Major)
    and RegQueryDWordValue(HKLM64,
    'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Minor', Minor);
  if Result then
    Result := (Major > 14) or ((Major = 14) and (Minor >= 40));
end;

procedure InitializeWizard;
begin
  { Branded splash page takes the place of the generic welcome page. }
  SplashPage := CreateCustomPage(wpWelcome, 'Willkommen bei Enargeia', 'WISSEN VERBINDET');
  ExtractTemporaryFile('Enargeia_Splash_Screen.bmp');
  SplashImage := TBitmapImage.Create(SplashPage);
  SplashImage.Parent := SplashPage.Surface;
  SplashImage.SetBounds(0, 0, SplashPage.Surface.Width, SplashPage.Surface.Height);
  SplashImage.Anchors := [akLeft, akTop, akRight, akBottom];
  SplashImage.Stretch := True;
  SplashImage.Bitmap.LoadFromFile(ExpandConstant('{tmp}\Enargeia_Splash_Screen.bmp'));

  { Completion illustration is displayed on the standard Finished page. }
  ExtractTemporaryFile('Enargeia_Setup_Complete.bmp');
  FinishedImage := TBitmapImage.Create(WizardForm);
  FinishedImage.Parent := WizardForm.FinishedPage;
  FinishedImage.SetBounds(WizardForm.FinishedPage.Width - 168, 74, 148, 148);
  FinishedImage.Anchors := [akTop, akRight];
  FinishedImage.Stretch := True;
  FinishedImage.Bitmap.LoadFromFile(ExpandConstant('{tmp}\Enargeia_Setup_Complete.bmp'));
  FinishedImage.Visible := False;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  FinishedImage.Visible := CurPageID = wpFinished;
  if CurPageID = wpFinished then
  begin
    WizardForm.FinishedHeadingLabel.Caption := 'Enargeia ist installiert';
    WizardForm.FinishedLabel.Caption := 'Enargeia wurde erfolgreich installiert. Klicke auf Fertigstellen, um die Einrichtung abzuschließen.';
    WizardForm.FinishedLabel.Width := WizardForm.FinishedPage.Width - 175;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if (CurStep = ssPostInstall) and (not HasSuitableVCRuntime) then
  begin
    ExtractTemporaryFile('vc_redist.x64.exe');
    if not Exec(ExpandConstant('{tmp}\vc_redist.x64.exe'), '/install /quiet /norestart', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      RaiseException('Die Microsoft Visual C++ Runtime konnte nicht gestartet werden.');
    if (ResultCode <> 0) and (ResultCode <> 3010) and (ResultCode <> 1638) then
      RaiseException(Format('Die Microsoft Visual C++ Runtime-Installation schlug fehl (Code %d).', [ResultCode]));
  end;
end;
