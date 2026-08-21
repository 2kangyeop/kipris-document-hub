Option Explicit

Dim shell, fileSystem, appDir, pythonwExe, appFile
Dim appCommand

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

appDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
pythonwExe = appDir & "\.venv\Scripts\pythonw.exe"
appFile = appDir & "\launch_web.py"

If Not fileSystem.FileExists(pythonwExe) Then
    MsgBox "The program is not installed yet." & vbCrLf & _
           "Run install_windows.bat first.", vbExclamation, "KIPRIS Document Hub"
    WScript.Quit 1
End If

If Not fileSystem.FileExists(appFile) Then
    MsgBox "The launcher file is missing." & vbCrLf & _
           "Extract the ZIP again, then run preview_windows.bat.", _
           vbCritical, "KIPRIS Document Hub"
    WScript.Quit 1
End If

shell.CurrentDirectory = appDir
appCommand = """" & pythonwExe & """ """ & appFile & """"
shell.Run appCommand, 0, False
