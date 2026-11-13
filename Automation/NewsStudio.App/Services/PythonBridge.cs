using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace NewsStudio.App.Services;

public sealed class PythonBridge : IAsyncDisposable
{
    private readonly string _automationRoot;
    private readonly string _pythonPath;
    private readonly string _bridgePath;
    private readonly SemaphoreSlim _inputLock = new(1, 1);
    private Process? _process;
    private string? _cancelFilePath;

    public PythonBridge(string automationRoot)
    {
        _automationRoot = automationRoot;
        _pythonPath = Path.Combine(automationRoot, ".venv", "Scripts", "python.exe");
        _bridgePath = Path.Combine(automationRoot, "src", "news_studio_gui_bridge.py");
    }

    public event Action<JsonElement>? EventReceived;
    public bool IsRunning => _process is { HasExited: false };

    public async Task<int> RunInteractiveAsync(
        bool createNewVideo = false,
        bool resumeLatest = false,
        bool fastDefaults = false,
        int? resumeVideoNumber = null,
        string? resumeStartStage = null,
        CancellationToken cancellationToken = default)
    {
        if (IsRunning) throw new InvalidOperationException("News Studio is already running.");
        if (!File.Exists(_pythonPath)) throw new FileNotFoundException("Python environment is missing. Run setup.bat first.", _pythonPath);
        if (!File.Exists(_bridgePath)) throw new FileNotFoundException("GUI bridge is missing.", _bridgePath);

        var info = new ProcessStartInfo
        {
            FileName = _pythonPath,
            WorkingDirectory = _automationRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        info.ArgumentList.Add(_bridgePath);
        info.Environment["PYTHONUTF8"] = "1";
        info.Environment["PYTHONIOENCODING"] = "utf-8";
        info.Environment["NEWS_STUDIO_GUI_BRIDGE"] = "1";
        info.Environment["NEWS_STUDIO_GUI_DIRECT_NEW_VIDEO"] = createNewVideo ? "1" : "0";
        info.Environment["NEWS_STUDIO_GUI_DIRECT_RESUME_LATEST"] = resumeLatest ? "1" : "0";
        info.Environment["NEWS_STUDIO_GUI_DIRECT_RESUME_VIDEO"] = resumeVideoNumber.HasValue ? resumeVideoNumber.Value.ToString() : "";
        info.Environment["NEWS_STUDIO_GUI_DIRECT_RESUME_STAGE"] = resumeStartStage ?? "";
        info.Environment["AUTOMATION_FAST_DEFAULTS"] = fastDefaults ? "1" : "0";
        _cancelFilePath = Path.Combine(Path.GetTempPath(), $"news-studio-cancel-{Guid.NewGuid():N}.signal");
        info.Environment["NEWS_STUDIO_GUI_CANCEL_FILE"] = _cancelFilePath;

        _process = new Process { StartInfo = info, EnableRaisingEvents = true };
        try
        {
            _process.Start();
            var stdoutTask = ReadEventsAsync(_process.StandardOutput, cancellationToken);
            var stderrTask = ReadErrorsAsync(_process.StandardError, cancellationToken);
            await _process.WaitForExitAsync(cancellationToken);
            await Task.WhenAll(stdoutTask, stderrTask);
            return _process.ExitCode;
        }
        finally
        {
            if (!string.IsNullOrWhiteSpace(_cancelFilePath))
                try { File.Delete(_cancelFilePath); } catch (IOException) { }
            _cancelFilePath = null;
        }
    }

    public async Task SendChoiceAsync(string choice, bool fastDefaults, string? customText = null)
    {
        if (!IsRunning || _process is null) return;
        var payload = JsonSerializer.Serialize(new
        {
            type = "choice_response",
            choice,
            fast_defaults = fastDefaults,
            custom_text = customText,
        });
        await _inputLock.WaitAsync();
        try
        {
            await _process.StandardInput.WriteLineAsync(payload);
            await _process.StandardInput.FlushAsync();
        }
        finally
        {
            _inputLock.Release();
        }
    }

    public async Task CancelChoiceAsync()
    {
        if (!IsRunning || _process is null) return;
        var payload = JsonSerializer.Serialize(new
        {
            type = "choice_response",
            cancel = true,
        });
        await _inputLock.WaitAsync();
        try
        {
            await _process.StandardInput.WriteLineAsync(payload);
            await _process.StandardInput.FlushAsync();
        }
        finally
        {
            _inputLock.Release();
        }
    }

    public async Task<bool> StopSafelyAsync(TimeSpan? gracePeriod = null)
    {
        var process = _process;
        if (process is null || process.HasExited) return true;
        // A manual checkpoint is blocked on the bridge input stream rather
        // than a child process.  Wake that path immediately; the cancel-file
        // signal below continues to cover active subprocess stages.
        try
        {
            await CancelChoiceAsync();
        }
        catch (IOException)
        {
            // The process may exit between IsRunning and the write.
        }
        catch (InvalidOperationException)
        {
            // Standard input is already closed; fall through to the signal.
        }
        try
        {
            if (!string.IsNullOrWhiteSpace(_cancelFilePath))
                await File.WriteAllTextAsync(_cancelFilePath, "stop\n", Encoding.UTF8);
        }
        catch (IOException)
        {
            await KillAndWaitAsync(process);
            return false;
        }

        using var timeout = new CancellationTokenSource(gracePeriod ?? TimeSpan.FromSeconds(20));
        try
        {
            await process.WaitForExitAsync(timeout.Token);
            return true;
        }
        catch (OperationCanceledException)
        {
            await KillAndWaitAsync(process);
            return false;
        }
    }

    private static async Task KillAndWaitAsync(Process process)
    {
        if (process.HasExited) return;
        process.Kill(entireProcessTree: true);
        using var forcedTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await process.WaitForExitAsync(forcedTimeout.Token);
        }
        catch (OperationCanceledException)
        {
            // The OS owns final cleanup from this point. The process tree has
            // already received a forced termination request.
        }
    }

    public async Task<JsonDocument?> QueryStatusAsync(string projectName, CancellationToken cancellationToken = default)
    {
        var info = new ProcessStartInfo
        {
            FileName = _pythonPath,
            WorkingDirectory = _automationRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        info.ArgumentList.Add(Path.Combine(_automationRoot, "src", "news_studio.py"));
        info.ArgumentList.Add("status");
        info.ArgumentList.Add(projectName);
        info.ArgumentList.Add("--json");
        info.Environment["PYTHONUTF8"] = "1";
        info.Environment["PYTHONIOENCODING"] = "utf-8";
        using var process = Process.Start(info) ?? throw new InvalidOperationException("Could not start the status query.");
        var outputTask = process.StandardOutput.ReadToEndAsync(cancellationToken);
        var errorTask = process.StandardError.ReadToEndAsync(cancellationToken);
        await process.WaitForExitAsync(cancellationToken);
        var output = await outputTask;
        _ = await errorTask;
        return process.ExitCode == 0 && !string.IsNullOrWhiteSpace(output)
            ? JsonDocument.Parse(output)
            : null;
    }

    public async Task<JsonDocument?> QueryProviderQuotasAsync(bool refresh, CancellationToken cancellationToken = default)
    {
        var info = new ProcessStartInfo
        {
            FileName = _pythonPath,
            WorkingDirectory = _automationRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        info.ArgumentList.Add(Path.Combine(_automationRoot, "src", "provider_quota_status.py"));
        info.ArgumentList.Add("--json");
        if (refresh) info.ArgumentList.Add("--refresh");
        info.Environment["PYTHONUTF8"] = "1";
        info.Environment["PYTHONIOENCODING"] = "utf-8";
        using var process = Process.Start(info) ?? throw new InvalidOperationException("Could not start the provider quota query.");
        var outputTask = process.StandardOutput.ReadToEndAsync(cancellationToken);
        var errorTask = process.StandardError.ReadToEndAsync(cancellationToken);
        await process.WaitForExitAsync(cancellationToken);
        var output = await outputTask;
        _ = await errorTask;
        return process.ExitCode == 0 && !string.IsNullOrWhiteSpace(output)
            ? JsonDocument.Parse(output)
            : null;
    }

    public Task<JsonDocument> GenerateVoiceoverPreviewAsync(
        int videoNumber,
        Action<string>? onProgress = null,
        CancellationToken cancellationToken = default)
        => RunProjectActionAsync("generate-voiceover", videoNumber, onProgress, cancellationToken);

    public Task<JsonDocument> SaveVoiceoverResetAsync(
        int videoNumber,
        Action<string>? onProgress = null,
        CancellationToken cancellationToken = default)
        => RunProjectActionAsync("save-voiceover", videoNumber, onProgress, cancellationToken);

    public Task<JsonDocument> DiscardVoiceoverResetAsync(
        int videoNumber,
        CancellationToken cancellationToken = default)
        => RunProjectActionAsync("discard-voiceover", videoNumber, null, cancellationToken);

    public Task<JsonDocument> UploadProjectAgainAsync(
        int videoNumber,
        Action<string>? onProgress = null,
        CancellationToken cancellationToken = default)
        => RunProjectActionAsync("upload-again", videoNumber, onProgress, cancellationToken);

    public async Task<JsonDocument> RunProjectActionAsync(
        string action,
        int videoNumber,
        Action<string>? onProgress = null,
        CancellationToken cancellationToken = default)
    {
        if (!File.Exists(_pythonPath))
            throw new FileNotFoundException("Python environment is missing. Run setup.bat first.", _pythonPath);

        var scriptPath = Path.Combine(_automationRoot, "src", "project_actions.py");
        if (!File.Exists(scriptPath))
            throw new FileNotFoundException("Project actions script is missing.", scriptPath);

        var info = new ProcessStartInfo
        {
            FileName = _pythonPath,
            WorkingDirectory = _automationRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        info.ArgumentList.Add(scriptPath);
        info.ArgumentList.Add(action);
        info.ArgumentList.Add("--video");
        info.ArgumentList.Add(videoNumber.ToString());
        info.ArgumentList.Add("--json");
        info.Environment["PYTHONUTF8"] = "1";
        info.Environment["PYTHONIOENCODING"] = "utf-8";

        using var process = Process.Start(info)
            ?? throw new InvalidOperationException($"Could not start project action '{action}'.");
        using var registration = cancellationToken.Register(() =>
        {
            try
            {
                if (!process.HasExited)
                    process.Kill(entireProcessTree: true);
            }
            catch (InvalidOperationException)
            {
            }
        });

        var outputTask = process.StandardOutput.ReadToEndAsync(cancellationToken);
        var errorLines = new List<string>();
        var errorTask = Task.Run(async () =>
        {
            while (await process.StandardError.ReadLineAsync(cancellationToken) is { } line)
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                var trimmed = line.Trim();
                errorLines.Add(trimmed);
                if (trimmed.StartsWith("[PROGRESS]", StringComparison.OrdinalIgnoreCase))
                {
                    var progressMessage = trimmed["[PROGRESS]".Length..].Trim();
                    onProgress?.Invoke(progressMessage);
                    EventReceived?.Invoke(JsonSerializer.SerializeToElement(new
                    {
                        type = "log",
                        level = "info",
                        message = progressMessage,
                    }));
                }
                else
                {
                    if (trimmed.StartsWith("Upload progress:", StringComparison.OrdinalIgnoreCase))
                        onProgress?.Invoke(trimmed);
                    else if (trimmed.Contains(" | INFO | [", StringComparison.OrdinalIgnoreCase))
                    {
                        var infoIdx = trimmed.IndexOf(" | INFO | ", StringComparison.OrdinalIgnoreCase);
                        if (infoIdx >= 0)
                            onProgress?.Invoke(trimmed[(infoIdx + " | INFO | ".Length)..].Trim());
                    }
                    EventReceived?.Invoke(JsonSerializer.SerializeToElement(new
                    {
                        type = "log",
                        level = trimmed.Contains("ERROR", StringComparison.OrdinalIgnoreCase) ? "error" : "info",
                        message = trimmed,
                    }));
                }
            }
        }, cancellationToken);

        await process.WaitForExitAsync(cancellationToken);
        var output = await outputTask;
        await errorTask;

        if (process.ExitCode != 0)
        {
            var lastError = errorLines.LastOrDefault(l => !l.StartsWith("Technical details:", StringComparison.OrdinalIgnoreCase))
                ?? $"Action '{action}' exited with code {process.ExitCode}.";
            throw new InvalidOperationException(lastError);
        }

        if (string.IsNullOrWhiteSpace(output))
            throw new InvalidOperationException($"Action '{action}' returned an empty response.");

        return JsonDocument.Parse(output);
    }

    private async Task ReadEventsAsync(StreamReader reader, CancellationToken cancellationToken)
    {
        while (await reader.ReadLineAsync(cancellationToken) is { } line)
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            try
            {
                using var document = JsonDocument.Parse(line);
                EventReceived?.Invoke(document.RootElement.Clone());
            }
            catch (JsonException)
            {
                EventReceived?.Invoke(JsonSerializer.SerializeToElement(new { type = "log", level = "info", message = line }));
            }
        }
    }

    private async Task ReadErrorsAsync(StreamReader reader, CancellationToken cancellationToken)
    {
        while (await reader.ReadLineAsync(cancellationToken) is { } line)
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            EventReceived?.Invoke(JsonSerializer.SerializeToElement(new { type = "log", level = "error", message = line }));
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (IsRunning) await StopSafelyAsync(TimeSpan.FromSeconds(5));
        _process?.Dispose();
        _inputLock.Dispose();
    }
}
