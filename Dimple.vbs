Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
currentDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = currentDir
pythonExe = "pythonw"
If fso.FileExists("C:\Python314\pythonw.exe") Then
    pythonExe = "C:\Python314\pythonw.exe"
End If
WshShell.Run """" & pythonExe & """ """ & currentDir & "\desktop_app.py""", 0, False
