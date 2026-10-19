Option Explicit

Dim fso, shell, projectRoot, logsDir, pythonwExe, launcherScript, startupLog, stream, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

projectRoot = fso.GetParentFolderName(WScript.ScriptFullName)
logsDir = fso.BuildPath(projectRoot, "logs")
If Not fso.FolderExists(logsDir) Then
    fso.CreateFolder(logsDir)
End If

pythonwExe = fso.BuildPath(projectRoot, ".venv\Scripts\pythonw.exe")
launcherScript = fso.BuildPath(projectRoot, "run_gui_hidden.pyw")
startupLog = fso.BuildPath(logsDir, "winkg-gui-startup.log")

If Not fso.FileExists(pythonwExe) Or Not fso.FileExists(launcherScript) Then
    Set stream = fso.OpenTextFile(startupLog, 8, True, -1)
    stream.WriteLine Now & " ERROR: GUI-Start nicht möglich. Erwartet wurden:" 
    stream.WriteLine "pythonw.exe: " & pythonwExe
    stream.WriteLine "Launcher: " & launcherScript
    stream.Close
    MsgBox "Die Python-Umgebung oder der GUI-Launcher fehlt." & vbCrLf & _
           "Bitte zuerst setup_windows.bat ausführen." & vbCrLf & _
           "Details: " & startupLog, vbExclamation, "Windows Knowledge Graph"
    WScript.Quit 1
End If

shell.CurrentDirectory = projectRoot
command = Chr(34) & pythonwExe & Chr(34) & " " & Chr(34) & launcherScript & Chr(34)
shell.Run command, 0, False
