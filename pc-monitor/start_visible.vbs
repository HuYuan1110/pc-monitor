' Start agent in a VISIBLE window so the user can see and close it anytime.
' window style: 1 = normal visible window (NOT hidden), 0 = hidden.
Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
py = ""
p1 = ws.ExpandEnvironmentStrings("%LOCALAPPDATA%\Python\bin\python.exe")
If fso.FileExists(p1) Then py = p1
If py = "" Then
  p2 = ws.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\python.exe")
  If fso.FileExists(p2) Then py = p2
End If
If py = "" Then py = "py"
cmd = """" & py & """ """ & base & "\agent.py"""
' 1 = visible window. The agent sets its own console title on start.
ws.Run cmd, 1, False
