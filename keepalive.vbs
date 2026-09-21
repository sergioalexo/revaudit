' Started by the "RevAudit keep-alive" scheduled task (autostart.bat keepalive on).
' wscript has no console, so nothing flashes on screen every 10 minutes; it starts
' launch.bat minimised, and launch.bat --quiet exits straight away when RevAudit is
' already answering on its port. Only when the server is actually down does a
' (minimised) RevAudit window appear - the same one the Desktop shortcut gives you.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run """" & here & "\launch.bat"" --quiet", 7, False
