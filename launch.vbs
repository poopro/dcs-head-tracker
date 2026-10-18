' Starts DCS Head Tracker without a console window (used by the desktop shortcut).
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run "cmd /c call """ & dir & "\run_gui.bat"" hidden", 0, False
