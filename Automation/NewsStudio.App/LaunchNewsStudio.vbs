Option Explicit

Dim shell, files, appFolder, automationFolder, launcher, exitCode, logPath
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")

appFolder = files.GetParentFolderName(WScript.ScriptFullName)
automationFolder = files.GetParentFolderName(appFolder)
launcher = files.BuildPath(automationFolder, "create_video_app.bat")
logPath = files.BuildPath(appFolder, "build.log")

shell.Environment("PROCESS")("NEWS_STUDIO_HIDDEN_LAUNCH") = "1"
exitCode = shell.Run("cmd.exe /d /c """ & launcher & """", 0, True)

If exitCode <> 0 Then
    MsgBox "News Studio could not be built." & vbCrLf & vbCrLf & _
           "Build details: " & logPath, vbCritical, "News Studio"
End If
