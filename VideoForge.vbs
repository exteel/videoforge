' VideoForge Silent Launcher
' Запускає VideoForge БЕЗ будь-яких вікон консолі.
' Подвійний клік на цьому файлі = повноцінний додаток.
'
' Вимоги: Python 3.11+ в PATH

Option Explicit

Dim shell, rootDir, pyw

shell   = CreateObject("WScript.Shell")
rootDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
pyw     = rootDir & "launch.pyw"

' 0 = прихована консоль; False = не чекати завершення
shell.Run "pythonw """ & pyw & """", 0, False
