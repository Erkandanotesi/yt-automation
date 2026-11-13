using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Forms;

namespace YtAutomationLauncher
{
    internal static class Program
    {
        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr hWnd);

        [DllImport("user32.dll")]
        private static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);

        private const int SwRestore = 9;

        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string workspaceRoot = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string automationDir = Path.Combine(workspaceRoot, "Automation");

            if (!Directory.Exists(automationDir))
            {
                MessageBox.Show(
                    "Could not find the 'Automation' folder next to yt-automation.exe.\n\nExpected:\n" + automationDir,
                    "YT Automation",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 1;
            }

            bool forceSetup = false;
            bool setupOnly = false;
            bool launchCli = false;
            bool studioCli = false;
            var forwardedArgs = new List<string>();

            foreach (string arg in args)
            {
                if (string.Equals(arg, "--setup", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(arg, "--repair", StringComparison.OrdinalIgnoreCase))
                {
                    forceSetup = true;
                }
                else if (string.Equals(arg, "--setup-only", StringComparison.OrdinalIgnoreCase))
                {
                    forceSetup = true;
                    setupOnly = true;
                }
                else if (string.Equals(arg, "--cli", StringComparison.OrdinalIgnoreCase))
                {
                    launchCli = true;
                }
                else if (string.Equals(arg, "--studio", StringComparison.OrdinalIgnoreCase))
                {
                    studioCli = true;
                }
                else if (string.Equals(arg, "--help", StringComparison.OrdinalIgnoreCase) ||
                         string.Equals(arg, "-h", StringComparison.OrdinalIgnoreCase) ||
                         string.Equals(arg, "/?", StringComparison.OrdinalIgnoreCase))
                {
                    MessageBox.Show(
                        "yt-automation.exe\n\n" +
                        "Default (double-click): Runs first-time or relocation setup automatically if needed, then opens NewsStudio.App.\n\n" +
                        "Optional flags:\n" +
                        "  --setup        Force repair/setup check before launching\n" +
                        "  --setup-only   Run setup and exit\n" +
                        "  --cli          Launch the console pipeline (create_video.bat)\n" +
                        "  --studio ...   Run news-studio.bat with remaining arguments",
                        "YT Automation Launcher",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Information
                    );
                    return 0;
                }
                else
                {
                    forwardedArgs.Add(arg);
                }
            }

            string setupReason = forceSetup
                ? "Setup requested manually"
                : DetectSetupNeeded(workspaceRoot, automationDir, launchCli || studioCli);

            if (setupReason != null)
            {
                int setupExit = RunSetup(automationDir, setupReason);
                if (setupExit != 0)
                {
                    return setupExit;
                }
                WriteLauncherStamp(automationDir);
            }

            if (setupOnly)
            {
                return 0;
            }

            if (studioCli)
            {
                return RunBatchVisible(
                    Path.Combine(automationDir, "news-studio.bat"),
                    automationDir,
                    JoinArguments(forwardedArgs)
                );
            }

            if (launchCli)
            {
                return RunBatchVisible(
                    Path.Combine(automationDir, "create_video.bat"),
                    automationDir,
                    JoinArguments(forwardedArgs)
                );
            }

            return LaunchDesktopApp(automationDir);
        }

        private static string DetectSetupNeeded(string workspaceRoot, string automationDir, bool cliOnly)
        {
            string venvDir = Path.Combine(automationDir, ".venv");
            string venvPython = Path.Combine(venvDir, "Scripts", "python.exe");
            if (!File.Exists(venvPython))
            {
                return "Python virtual environment (.venv) not found";
            }

            string pyvenvCfg = Path.Combine(venvDir, "pyvenv.cfg");
            if (!File.Exists(pyvenvCfg))
            {
                return "Python virtual environment configuration (pyvenv.cfg) missing";
            }

            string pythonHome = ReadPyvenvValue(pyvenvCfg, "home");
            if (string.IsNullOrEmpty(pythonHome) || !Directory.Exists(pythonHome))
            {
                return "Base Python installation moved or missing on this machine";
            }

            string stampPath = Path.Combine(venvDir, ".launcher_stamp");
            string normalizedCurrent = NormalizePath(automationDir);
            if (!File.Exists(stampPath))
            {
                return "Workspace setup stamp not initialized for current folder";
            }
            string stampedPath = NormalizePath(File.ReadAllText(stampPath, Encoding.UTF8).Trim());
            if (!string.Equals(stampedPath, normalizedCurrent, StringComparison.OrdinalIgnoreCase))
            {
                return "Workspace folder was moved or renamed";
            }

            string reqPath = Path.Combine(automationDir, "requirements.txt");
            string reqMarker = Path.Combine(venvDir, ".requirements.sha256");
            if (File.Exists(reqPath))
            {
                if (!File.Exists(reqMarker))
                {
                    return "Python dependency verification stamp missing";
                }
                string currentHash = ComputeSha256Hex(reqPath);
                string savedHash = File.ReadAllText(reqMarker, Encoding.UTF8).Trim();
                if (!string.Equals(currentHash, savedHash, StringComparison.OrdinalIgnoreCase))
                {
                    return "requirements.txt changed since last setup";
                }
            }

            if (!File.Exists(Path.Combine(automationDir, ".env")) ||
                !File.Exists(Path.Combine(automationDir, "config", "feeds.opml")) ||
                !File.Exists(Path.Combine(automationDir, "config", "channel_profile.txt")))
            {
                return "Default configuration files (.env / feeds.opml / channel_profile.txt) missing";
            }

            string motionVendor = Path.Combine(workspaceRoot, "motion_engine", "vendor");
            if (Directory.Exists(Path.Combine(workspaceRoot, "motion_engine")) &&
                (!File.Exists(Path.Combine(motionVendor, "gsap.min.js")) ||
                 !File.Exists(Path.Combine(motionVendor, "lucide.min.js"))))
            {
                return "motion_engine JS vendor assets missing";
            }

            if (!HasFfmpeg(automationDir))
            {
                return "FFmpeg / FFprobe binaries missing";
            }

            if (!cliOnly)
            {
                string appExe = GetDesktopAppExe(automationDir);
                if (!File.Exists(appExe))
                {
                    return "NewsStudio.App.exe has not been built yet";
                }
            }

            return null;
        }

        private static int RunSetup(string automationDir, string reason)
        {
            string setupBat = Path.Combine(automationDir, "setup.bat");
            if (!File.Exists(setupBat))
            {
                MessageBox.Show(
                    "Setup script not found:\n" + setupBat,
                    "YT Automation",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 1;
            }

            // Run setup.bat in a visible console window so the user can watch progress,
            // and pause automatically if setup returns a non-zero exit code.
            string cmdArgs = string.Format(
                "/d /c \"echo [Launcher] {0} && echo. && call \"{1}\" --no-pause || (echo. && echo [Launcher] Setup failed with exit code %ERRORLEVEL%. && pause && exit /b %ERRORLEVEL%)\"",
                EscapeCmdEcho(reason),
                setupBat
            );

            var psi = new ProcessStartInfo("cmd.exe", cmdArgs)
            {
                WorkingDirectory = automationDir,
                UseShellExecute = true
            };

            using (var proc = Process.Start(psi))
            {
                if (proc == null)
                {
                    return 1;
                }
                proc.WaitForExit();
                return proc.ExitCode;
            }
        }

        private static int LaunchDesktopApp(string automationDir)
        {
            Process existing = Process.GetProcessesByName("NewsStudio.App").FirstOrDefault();
            if (existing != null)
            {
                try
                {
                    if (existing.MainWindowHandle != IntPtr.Zero)
                    {
                        ShowWindowAsync(existing.MainWindowHandle, SwRestore);
                        SetForegroundWindow(existing.MainWindowHandle);
                    }
                }
                catch
                {
                    // Ignore cross-session window focus restriction.
                }
                return 0;
            }

            string appExe = GetDesktopAppExe(automationDir);
            if (IsAppRebuildNeeded(automationDir, appExe) && IsCommandAvailable("dotnet"))
            {
                RunBatchCommandSilentOrConsole(
                    automationDir,
                    "dotnet build \"NewsStudio.App\\NewsStudio.App.csproj\" --configuration Release --nologo -v minimal"
                );
            }

            if (File.Exists(appExe))
            {
                var psi = new ProcessStartInfo(appExe)
                {
                    WorkingDirectory = automationDir,
                    UseShellExecute = true
                };
                Process.Start(psi);
                return 0;
            }

            DialogResult fallback = MessageBox.Show(
                "NewsStudio.App.exe is not built (the .NET 10 SDK may not be installed on this PC).\n\n" +
                "Would you like to run the console mode (create_video.bat) instead?",
                "YT Automation",
                MessageBoxButtons.YesNo,
                MessageBoxIcon.Warning
            );
            if (fallback == DialogResult.Yes)
            {
                return RunBatchVisible(Path.Combine(automationDir, "create_video.bat"), automationDir, string.Empty);
            }
            return 1;
        }

        private static bool IsAppRebuildNeeded(string automationDir, string appExe)
        {
            if (!File.Exists(appExe))
            {
                return true;
            }

            DateTime exeTime = File.GetLastWriteTimeUtc(appExe);
            string appDir = Path.Combine(automationDir, "NewsStudio.App");
            if (!Directory.Exists(appDir))
            {
                return false;
            }

            string[] patterns = { "*.cs", "*.xaml", "*.csproj" };
            foreach (string pattern in patterns)
            {
                foreach (string file in Directory.GetFiles(appDir, pattern, SearchOption.AllDirectories))
                {
                    string norm = file.Replace('/', '\\');
                    if (norm.IndexOf("\\bin\\", StringComparison.OrdinalIgnoreCase) >= 0 ||
                        norm.IndexOf("\\obj\\", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        continue;
                    }
                    if (File.GetLastWriteTimeUtc(file) > exeTime)
                    {
                        return true;
                    }
                }
            }
            return false;
        }

        private static void RunBatchCommandSilentOrConsole(string automationDir, string command)
        {
            var psi = new ProcessStartInfo("cmd.exe", "/d /c \"" + command + "\"")
            {
                WorkingDirectory = automationDir,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            using (var proc = Process.Start(psi))
            {
                if (proc != null)
                {
                    proc.WaitForExit();
                }
            }
        }

        private static int RunBatchVisible(string batPath, string workingDir, string arguments)
        {
            if (!File.Exists(batPath))
            {
                MessageBox.Show("Launcher target not found:\n" + batPath, "YT Automation", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }

            string cmdArgs = string.IsNullOrEmpty(arguments)
                ? string.Format("/d /c \"\"{0}\"\"", batPath)
                : string.Format("/d /c \"\"{0}\" {1}\"", batPath, arguments);

            var psi = new ProcessStartInfo("cmd.exe", cmdArgs)
            {
                WorkingDirectory = workingDir,
                UseShellExecute = true
            };
            using (var proc = Process.Start(psi))
            {
                if (proc == null)
                {
                    return 1;
                }
                proc.WaitForExit();
                return proc.ExitCode;
            }
        }

        private static void WriteLauncherStamp(string automationDir)
        {
            string venvDir = Path.Combine(automationDir, ".venv");
            if (!Directory.Exists(venvDir))
            {
                return;
            }
            string stampPath = Path.Combine(venvDir, ".launcher_stamp");
            File.WriteAllText(stampPath, NormalizePath(automationDir) + Environment.NewLine, new UTF8Encoding(false));
        }

        private static string GetDesktopAppExe(string automationDir)
        {
            return Path.Combine(
                automationDir,
                "NewsStudio.App",
                "bin",
                "Release",
                "net10.0-windows",
                "NewsStudio.App.exe"
            );
        }

        private static bool HasFfmpeg(string automationDir)
        {
            string toolsFfmpeg = Path.Combine(automationDir, "tools", "ffmpeg");
            if (Directory.Exists(toolsFfmpeg))
            {
                string[] found = Directory.GetFiles(toolsFfmpeg, "ffmpeg.exe", SearchOption.AllDirectories);
                if (found.Length > 0)
                {
                    return true;
                }
            }
            return IsCommandAvailable("ffmpeg");
        }

        private static bool IsCommandAvailable(string command)
        {
            string pathVar = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
            string fileName = command.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) ? command : command + ".exe";
            foreach (string dir in pathVar.Split(new[] { ';' }, StringSplitOptions.RemoveEmptyEntries))
            {
                try
                {
                    string candidate = Path.Combine(dir.Trim().Trim('"'), fileName);
                    if (File.Exists(candidate))
                    {
                        return true;
                    }
                }
                catch
                {
                    // Skip malformed PATH entries.
                }
            }
            return false;
        }

        private static string ReadPyvenvValue(string cfgPath, string key)
        {
            foreach (string line in File.ReadAllLines(cfgPath, Encoding.UTF8))
            {
                int idx = line.IndexOf('=');
                if (idx <= 0)
                {
                    continue;
                }
                if (string.Equals(line.Substring(0, idx).Trim(), key, StringComparison.OrdinalIgnoreCase))
                {
                    return line.Substring(idx + 1).Trim();
                }
            }
            return string.Empty;
        }

        private static string ComputeSha256Hex(string filePath)
        {
            using (var sha = SHA256.Create())
            using (var stream = File.OpenRead(filePath))
            {
                byte[] hash = sha.ComputeHash(stream);
                var sb = new StringBuilder(hash.Length * 2);
                foreach (byte b in hash)
                {
                    sb.Append(b.ToString("x2"));
                }
                return sb.ToString();
            }
        }

        private static string NormalizePath(string path)
        {
            return Path.GetFullPath(path)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        }

        private static string EscapeCmdEcho(string text)
        {
            return (text ?? string.Empty)
                .Replace("^", "^^")
                .Replace("&", "^&")
                .Replace("|", "^|")
                .Replace("<", "^<")
                .Replace(">", "^>")
                .Replace("(", "^(")
                .Replace(")", "^)");
        }

        private static string JoinArguments(IEnumerable<string> args)
        {
            return string.Join(" ", args.Select(a =>
                a.Contains(" ") || a.Contains("\"")
                    ? "\"" + a.Replace("\"", "\\\"") + "\""
                    : a));
        }
    }
}
