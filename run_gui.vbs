Option Explicit
Dim fso, shell, projectRoot, pythonwExe, launcherScript
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
projectRoot = fso.GetParentFolderName(WScript.ScriptFullName)
pythonwExe = fso.BuildPath(projectRoot, ".venv\Scripts\pythonw.exe")
launcherScript = fso.BuildPath(projectRoot, "run_gui_hidden.pyw")
If Not fso.FileExists(pythonwExe) Or Not fso.FileExists(launcherScript) Then
    MsgBox "Die Entwicklungsumgebung fehlt. Bitte zuerst setup_windows.bat ausführen.", vbExclamation, "Enargeia"
    WScript.Quit 1
End If
shell.CurrentDirectory = projectRoot
shell.Run Chr(34) & pythonwExe & Chr(34) & " " & Chr(34) & launcherScript & Chr(34), 0, False
