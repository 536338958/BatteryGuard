' Battery Guard - Startup script (silent, no window)
' Place this file in the Windows Startup folder AND keep it in the same
' folder as BatteryGuard.exe. It launches the app with --autostart at logon.
' The app lives in the system tray; click the tray icon to open the panel.

Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Resolve BatteryGuard.exe next to this .vbs file (portable, no hard-coded path)
thisDir = fso.GetParentFolderName(WScript.ScriptFullName)
exe = thisDir & "\BatteryGuard.exe"

If fso.FileExists(exe) Then
    ' window style 0 = hidden
    ws.Run """" & exe & """ --autostart", 0, False
Else
    ws.Popup "BatteryGuard.exe not found next to this script:" & vbCrLf & exe, 0, "Battery Guard", 16
End If
