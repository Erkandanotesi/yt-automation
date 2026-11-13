using Microsoft.Web.WebView2.Core;
using NewsStudio.App.Controls;
using NewsStudio.App.Services;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Media;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Shell;
using System.Windows.Threading;

namespace NewsStudio.App;

public partial class MainWindow : Window
{
    private const int MaximumQueueCount = 20;
    private const int ProductionHistoryPageSize = 10;

    private enum ProductionRunOutcome
    {
        Completed,
        Cancelled,
        Failed,
        NotStarted,
    }

    private static readonly (string Key, string Label)[] StageDefinitions =
    [
        ("news", "Collect and write news"),
        ("tts", "Create ElevenLabs voiceovers"),
        ("alignment", "Align narration timestamps"),
        ("manual_separators", "Resolve news separators"),
        ("storyboard", "Plan and generate AI images"),
        ("ai_story_videos", "Resolve optional AI story videos"),
        ("archive_visuals", "Select contextual archive images"),
        ("captions", "Create timed captions"),
        ("effects", "Plan frames, effects, and music"),
        ("preview_approval", "Prepare and approve preview"),
        ("render", "Encode the final video"),
        ("publish", "Create thumbnail and YouTube metadata"),
    ];

    private static readonly HashSet<string> ShortDurationStageKeys = new(StringComparer.OrdinalIgnoreCase)
    {
        "manual_separators",
        "storyboard",
        "ai_story_videos",
        "captions",
        "effects",
    };

    // Main menu omits sub-2s utility stages to stay focused on substantive work; history still lists all 12.
    private static readonly (string Key, string Label)[] MainStageDefinitions =
        [.. StageDefinitions.Where(stage => !ShortDurationStageKeys.Contains(stage.Key))];

    private readonly ObservableCollection<StageViewModel> _stages = [];
    private readonly ObservableCollection<ProviderQuotaViewModel> _providerQuotas = [];
    private readonly ObservableCollection<ProductionTimerLogViewModel> _productionHistory = [];
    private readonly ObservableCollection<ProductionStoryDetailViewModel> _selectedHistoryStories = [];
    private readonly ObservableCollection<ProductionStageDetailViewModel> _selectedHistoryStages = [];
    private readonly ObservableCollection<string> _selectedHistoryTags = [];
    private readonly ObservableCollection<ChannelProfileOptionViewModel> _channelProfiles = [];
    private readonly List<ActivityLogEntry> _allActivityLogs = [];
    private readonly ObservableCollection<ActivityLogEntry> _filteredActivityLogs = [];
    private readonly Dictionary<string, StageViewModel> _stageLookup = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, FrameworkElement> _settingEditors = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, Border> _settingEditedBadges = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, Button> _settingResetButtons = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, (Border Badge, TextBlock Label)> _categoryModifiedBadges = new(StringComparer.OrdinalIgnoreCase);
    private readonly List<Expander> _settingsCategoryExpanders = [];
    private readonly HashSet<string> _expandedSettingsCategories = new(StringComparer.OrdinalIgnoreCase);
    private readonly HashSet<string> _decisionKeys = new(StringComparer.OrdinalIgnoreCase);
    private string _activeSettingsSection = "All";
    private string _activeActivityFilter = "All";
    private bool _modifiedOnlySettings;
    private bool _suppressSettingsExpandTracking;
    private List<EnvSetting> _settings = [];
    private EnvFileService? _envService;
    private PythonBridge? _bridge;
    private DispatcherTimer? _decisionTimer;
    private DateTime _decisionDeadline;
    private double _decisionDurationSeconds;
    private string _decisionDefault = "";
    private string? _decisionAssetPath;
    private bool _decisionEditableActive;
    private string _decisionInitialEditableText = "";
    private int _decisionEditableMaxLength = 100;
    private bool _suppressDecisionTextChanged;
    private TextBlock? _decisionDefaultOptionTextBlock;
    private string? _currentProject;
    private string? _currentProjectPath;
    private int _productionHistoryLimit = ProductionHistoryPageSize;
    private ProductionTimerLogViewModel? _selectedHistoryItem;
    private string? _selectedHistoryWatchUrl;
    private string? _selectedHistoryPublishDescription;
    private string? _selectedHistoryScriptFullText;
    private string _automationRoot = "";
    private string _activeTheme = "light";
    private bool _sidebarCollapsed;
    private bool _techBranchExpanded = true;
    private string _lastQuotaUpdatedText = "Checking providers…";
    private bool _quotaWebViewReady;
    private bool _quotaBrowserExpanded;
    private string? _quotaBrowserCurrentUrl;
    private bool _decisionPending;
    private bool _decisionWaitIndefinitely;
    private bool _windowLoaded;
    private FrameworkElement? _activePage;
    private bool _navigationAnimating;
    private HwndSource? _windowSource;
    private bool _closingAfterSafeStop;
    private bool _queueRunning;
    private bool _queueCancellationRequested;
    private bool _latestProductionComplete;
    private bool _latestUploadComplete;
    private Task? _activeQueueTask;
    private readonly DispatcherTimer _productionTimer = new() { Interval = TimeSpan.FromSeconds(1) };
    private readonly MediaPlayer _voiceoverPlayer = new();
    private readonly DispatcherTimer _voiceoverPlayerTimer = new() { Interval = TimeSpan.FromMilliseconds(150) };
    private int _voiceoverResetVideoNumber;
    private string? _voiceoverPreviewTempFile;
    private double _voiceoverDurationSeconds;
    private bool _voiceoverIsPlaying;
    private bool _voiceoverBusy;
    private bool _voiceoverSaving;
    private CancellationTokenSource? _voiceoverCts;
    private bool _projectActionRunning;

    public MainWindow()
    {
        InitializeComponent();
        for (var index = 0; index < MainStageDefinitions.Length; index++)
        {
            var item = new StageViewModel
            {
                Key = MainStageDefinitions[index].Key,
                Number = (index + 1).ToString("00", CultureInfo.InvariantCulture),
                Label = MainStageDefinitions[index].Label,
            };
            _stages.Add(item);
            _stageLookup[item.Key] = item;
            SetStageState(item.Key, "NOT_STARTED");
        }
        OverallProgress.Maximum = MainStageDefinitions.Length;
        StepsCardProgress.Maximum = MainStageDefinitions.Length;
        StagesList.ItemsSource = _stages;
        ActivityLogList.ItemsSource = _filteredActivityLogs;
        ProductionsMasterList.ItemsSource = _productionHistory;
        HistoryDetailStoriesList.ItemsSource = _selectedHistoryStories;
        HistoryDetailStagesList.ItemsSource = _selectedHistoryStages;
        HistoryDetailTagsList.ItemsSource = _selectedHistoryTags;
        QuotaItems.ItemsSource = _providerQuotas;
        ChannelProfilesList.ItemsSource = _channelProfiles;
        _activeTheme = (Application.Current as App)?.ActiveTheme ?? "light";
        SyncThemeModeToggle(_activeTheme);
        _productionTimer.Tick += ProductionTimer_Tick;
        _productionTimer.Start();
        _voiceoverPlayerTimer.Tick += VoiceoverPlayerTimer_Tick;
        _voiceoverPlayer.MediaEnded += VoiceoverPlayer_MediaEnded;
        _activePage = ProductionPage;
        UpdateActivityEmptyState();
        SetQuotaPlaceholders();
        RefreshChannelProfiles();
    }

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        var startupTheme = (Application.Current as App)?.ActiveTheme ?? "light";
        ApplyBackdrop(ThemeService.IsDark(startupTheme));
        _windowSource = HwndSource.FromHwnd(new WindowInteropHelper(this).Handle);
        _windowSource?.AddHook(WindowMessageHook);
    }

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        _windowLoaded = true;
        try
        {
            _automationRoot = LocateAutomationRoot();
            RefreshChannelProfiles();
            _envService = new EnvFileService(_automationRoot);
            LoadSettings();
            _bridge = new PythonBridge(_automationRoot);
            await Task.WhenAll(RefreshStatusAsync(), RefreshProviderQuotasAsync(false));
        }
        catch (Exception exc)
        {
            SetHeaderState("Setup needed", "danger");
            CurrentActionTitle.Text = "The app could not initialize";
            CurrentActionDetail.Text = exc.Message;
            AppendLog($"ERROR  {exc.Message}");
            StartButton.IsEnabled = false;
        }
    }

    private static string LocateAutomationRoot()
    {
        var cursor = new DirectoryInfo(AppContext.BaseDirectory);
        while (cursor is not null)
        {
            if (File.Exists(Path.Combine(cursor.FullName, "create_video.bat")) &&
                File.Exists(Path.Combine(cursor.FullName, "src", "news_studio.py")))
                return cursor.FullName;
            cursor = cursor.Parent;
        }
        throw new DirectoryNotFoundException("Could not locate the Automation folder from the app directory.");
    }

    private string GetProjectsRoot()
    {
        var workspaceRoot = Directory.GetParent(_automationRoot)?.FullName ?? _automationRoot;
        var configured = GetEnvSettingValue("PROJECTS_ROOT");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            var expanded = Environment.ExpandEnvironmentVariables(configured.Trim());
            var resolved = Path.IsPathRooted(expanded)
                ? Path.GetFullPath(expanded)
                : Path.GetFullPath(Path.Combine(_automationRoot, expanded));
            if (Directory.Exists(resolved))
                return resolved;
        }
        var defaultProjects = Path.Combine(workspaceRoot, "Projects");
        if (Directory.Exists(defaultProjects))
            return defaultProjects;
        return workspaceRoot;
    }

    private string? ResolvePortableProjectFile(string projectPath, string? rawPath)
    {
        if (string.IsNullOrWhiteSpace(rawPath)) return null;
        var normalized = rawPath.Trim().Replace('/', Path.DirectorySeparatorChar).Replace('\\', Path.DirectorySeparatorChar);
        if (Path.IsPathRooted(normalized) && File.Exists(normalized))
            return normalized;
        var match = Regex.Match(normalized, @"(?:^|[\\/])Video \d+[\\/](.+)$", RegexOptions.IgnoreCase);
        if (match.Success)
        {
            var relocated = Path.Combine(projectPath, match.Groups[1].Value);
            if (File.Exists(relocated)) return relocated;
        }
        var candidates = new[]
        {
            Path.Combine(projectPath, normalized.TrimStart(Path.DirectorySeparatorChar)),
            Path.Combine(projectPath, Path.GetFileName(normalized)),
            Path.Combine(_automationRoot, normalized.TrimStart(Path.DirectorySeparatorChar)),
        };
        return candidates.FirstOrDefault(File.Exists);
    }

    private async Task RefreshStatusAsync()
    {
        if (_bridge is null) return;
        var projectsRoot = GetProjectsRoot();
        var latest = (Directory.Exists(projectsRoot)
                ? Directory.EnumerateDirectories(projectsRoot, "Video *", SearchOption.TopDirectoryOnly)
                : [])
            .Select(path => new { Path = path, Match = Regex.Match(Path.GetFileName(path), @"^Video (\d+)$", RegexOptions.IgnoreCase) })
            .Where(item => item.Match.Success)
            .Select(item => new { item.Path, Number = int.Parse(item.Match.Groups[1].Value, CultureInfo.InvariantCulture) })
            .OrderByDescending(item => item.Number)
            .FirstOrDefault();

        var latestProject = latest is null ? null : $"Video {latest.Number}";
        _currentProject = latestProject;
        _currentProjectPath = latest?.Path;
        RefreshProductionHistory(projectsRoot);

        if (latest is null)
        {
            _latestProductionComplete = false;
            _latestUploadComplete = false;
            ProjectSummaryText.Text = "No video project yet";
            ActiveProjectChip.Visibility = Visibility.Collapsed;
            TotalStagesTimeBorder.Visibility = Visibility.Collapsed;
            StartButton.Content = "Start production";
            StartButton.Visibility = _queueRunning ? Visibility.Collapsed : Visibility.Visible;
            QuickRunButton.Visibility = Visibility.Collapsed;
            foreach (var stage in _stages)
            {
                stage.ElapsedText = "";
                stage.LogFilePath = null;
                stage.StartedAtUtc = null;
                SetStageState(stage.Key, "NOT_STARTED");
            }
            UpdateOverallProgress();
            CurrentProgressStepText.Text = "Waiting to start";
            UpdatePipelineSnapshot();
            return;
        }

        _latestProductionComplete = false;
        _latestUploadComplete = false;
        ProjectSummaryText.Text = $"Latest: {_currentProject}";
        ActiveProjectChipText.Text = _currentProject;
        ActiveProjectChip.Visibility = Visibility.Visible;
        using var status = await _bridge.QueryStatusAsync(latestProject!);
        if (status is null)
        {
            UpdatePipelineSnapshot();
            return;
        }
        var root = status.RootElement;
        var stageDetails = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (root.TryGetProperty("stages", out var stages))
        {
            var stageStatuses = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var stageProperty in stages.EnumerateObject())
            {
                var statusText = stageProperty.Value.TryGetProperty("status", out var state)
                    ? state.GetString() ?? "NOT_STARTED"
                    : "NOT_STARTED";
                stageStatuses[stageProperty.Name] = statusText;
                if (StringProperty(stageProperty.Value, "detail") is { Length: > 0 } stageDetail)
                    stageDetails[stageProperty.Name] = stageDetail;
            }
            var reachedFrontier = false;
            foreach (var (stageKey, _) in StageDefinitions)
            {
                var value = stageStatuses.GetValueOrDefault(stageKey, "NOT_STARTED");
                if (reachedFrontier && IsTerminalStageStatus(value))
                    value = "NOT_STARTED";
                if (_stageLookup.TryGetValue(stageKey, out var stageVm))
                {
                    stageVm.StartedAtUtc = null;
                    if (!IsTerminalStageStatus(value))
                        stageVm.ElapsedText = "";
                }
                SetStageState(stageKey, value);
                if (!IsTerminalStageStatus(value))
                    reachedFrontier = true;
            }
        }
        ApplyStageEventMetrics(latest.Path, stageDetails, _bridge.IsRunning || HasLiveProjectLock(latest.Path));
        UpdateOverallProgress();

        var isComplete = root.TryGetProperty("complete", out var completeValue) && completeValue.GetBoolean();
        var locked = root.TryGetProperty("locked", out var lockValue) && lockValue.GetBoolean();
        var nextStageKey = root.TryGetProperty("next_stage", out var nextStageValue) &&
                           nextStageValue.ValueKind == JsonValueKind.String
            ? nextStageValue.GetString()
            : null;
        string? nextStageStatus = null;
        string? nextStageDetail = null;
        if (nextStageKey is not null &&
            root.TryGetProperty("stages", out var currentStages) &&
            currentStages.TryGetProperty(nextStageKey, out var nextStageInfo))
        {
            nextStageStatus = StringProperty(nextStageInfo, "status");
            nextStageDetail = StringProperty(nextStageInfo, "detail");
        }
        var uploadComplete = IsProjectUploadComplete(latest.Path);
        var uploadPending = isComplete && !uploadComplete;
        _latestProductionComplete = isComplete;
        _latestUploadComplete = uploadComplete;
        StartButton.Content = uploadPending
            ? "Continue to YouTube"
            : isComplete
                ? "Start with settings"
                : "Continue production";
        StartButton.Visibility = _queueRunning ? Visibility.Collapsed : Visibility.Visible;
        QuickRunButton.Visibility = isComplete && !_queueRunning
            ? Visibility.Visible
            : Visibility.Collapsed;
        var nextStageLabel = GetStageLabel(nextStageKey);
        if (nextStageLabel is not null)
            CurrentProgressStepText.Text = $"Next: {nextStageLabel}";
        else if (isComplete)
            CurrentProgressStepText.Text = uploadComplete
                ? "Complete and uploaded to YouTube"
                : "Ready for YouTube upload";
        else
            CurrentProgressStepText.Text = "Ready";

        if (!_bridge.IsRunning)
        {
            var waiting = nextStageStatus?.Equals("WAITING", StringComparison.OrdinalIgnoreCase) == true;
            SetHeaderState(
                locked ? "In use" : waiting ? "Waiting" : "Ready",
                locked || waiting ? "warning" : "muted");
            if (nextStageKey is not null)
            {
                CurrentActionTitle.Text = nextStageLabel ?? "Continue production";
                CurrentActionDetail.Text = waiting
                    ? $"{nextStageDetail} Select Continue production when the files are ready; they will be checked before anything is regenerated."
                    : "The app detected the next unfinished section and will keep every completed artifact cached.";
            }
            else if (isComplete)
            {
                CurrentActionTitle.Text = uploadComplete
                    ? "Ready for a new production"
                    : "Production package complete";
                CurrentActionDetail.Text = uploadComplete
                    ? $"{_currentProject} is already uploaded. Start creates the next video without reopening its YouTube step."
                    : "You can upload to YouTube, rebuild a section, or start a new video.";
            }
        }

        UpdatePreviewButton();
        UpdatePipelineSnapshot();
    }

    private void SetQuotaPlaceholders()
    {
        _providerQuotas.Clear();
        foreach (var (id, name) in new[]
        {
            ("openai", "OpenAI API"),
            ("gemini", "Gemini API"),
            ("elevenlabs", "ElevenLabs"),
            ("pexels", "Pexels Videos"),
            ("pixabay", "Pixabay Videos"),
            ("unsplash", "Unsplash"),
            ("youtube", "YouTube Data"),
        })
        {
            _providerQuotas.Add(BuildQuotaViewModel(
                id: id,
                name: name,
                status: "loading",
                primary: "Checking…",
                detail: "Loading provider status",
                dashboardUrl: "",
                hasMeter: false,
                meterPercent: 0));
        }
        UpdateQuotaSummary();
    }

    private ProviderQuotaViewModel BuildQuotaViewModel(
        string id,
        string name,
        string status,
        string primary,
        string detail,
        string dashboardUrl,
        bool hasMeter,
        double meterPercent)
    {
        var (statusLabel, glyph, brush, tintBrush) = ResolveQuotaStatusPresentation(status);
        var (categoryLabel, providerGlyph) = ResolveQuotaProviderMetadata(id);
        return new ProviderQuotaViewModel
        {
            Id = id,
            Name = name,
            Primary = primary,
            Detail = detail,
            DashboardUrl = dashboardUrl,
            DashboardDomain = ExtractDomainLabel(dashboardUrl),
            StatusKey = status,
            StatusLabel = statusLabel,
            CategoryLabel = categoryLabel,
            ProviderGlyph = providerGlyph,
            StatusBrush = brush,
            StatusTintBrush = tintBrush,
            StatusGlyph = glyph,
            HasMeter = hasMeter,
            MeterPercent = meterPercent,
        };
    }

    private (string Label, string Glyph, Brush Brush, Brush Tint) ResolveQuotaStatusPresentation(string status) =>
        status.ToLowerInvariant() switch
        {
            "available" => ("Available", "\uE73E", ResourceBrush("SuccessBrush"), ResourceBrush("SuccessTintBrush")),
            "warning" => ("Warning", "\uE7BA", ResourceBrush("WarningBrush"), ResourceBrush("WarningTintBrush")),
            "limited" => ("Limited", "\uE946", ResourceBrush("AccentBrush"), ResourceBrush("AccentTintBrush")),
            "loading" => ("Checking", "\uE895", ResourceBrush("MutedBrush"), ResourceBrush("SurfaceAltBrush")),
            "not_configured" => ("Not configured", "\uE711", ResourceBrush("MutedBrush"), ResourceBrush("SurfaceAltBrush")),
            _ => ("Error", "\uE783", ResourceBrush("DangerBrush"), ResourceBrush("DangerTintBrush")),
        };

    private static (string Category, string Glyph) ResolveQuotaProviderMetadata(string id) =>
        id.ToLowerInvariant() switch
        {
            "openai" => ("Language & Vision AI", "\uE99A"),
            "gemini" => ("Language & Image AI", "\uE945"),
            "elevenlabs" => ("Voice Synthesis & Alignment", "\uE720"),
            "pexels" => ("Stock Video & Photo", "\uE8B9"),
            "pixabay" => ("Stock Media Library", "\uEB9F"),
            "unsplash" => ("High-Res Photography", "\uE91B"),
            "youtube" => ("Channel Publishing API", "\uE714"),
            _ => ("API Provider", "\uE946"),
        };

    private static string ExtractDomainLabel(string? url)
    {
        if (string.IsNullOrWhiteSpace(url) || !Uri.TryCreate(url, UriKind.Absolute, out var uri))
            return "No dashboard link";
        return uri.Host.StartsWith("www.", StringComparison.OrdinalIgnoreCase)
            ? uri.Host[4..]
            : uri.Host;
    }

    private void RefreshQuotaThemeBrushes()
    {
        foreach (var provider in _providerQuotas)
        {
            var (_, _, brush, tint) = ResolveQuotaStatusPresentation(provider.StatusKey);
            provider.StatusBrush = brush;
            provider.StatusTintBrush = tint;
        }
    }

    private void UpdateQuotaSummary()
    {
        if (QuotaSummaryText is null) return;
        var configured = _providerQuotas.Count(item => item.IsConfigured && !string.Equals(item.StatusKey, "loading", StringComparison.OrdinalIgnoreCase));
        var warnings = _providerQuotas.Count(item => string.Equals(item.StatusKey, "warning", StringComparison.OrdinalIgnoreCase) ||
                                                     string.Equals(item.StatusKey, "error", StringComparison.OrdinalIgnoreCase));
        if (_providerQuotas.All(item => string.Equals(item.StatusKey, "loading", StringComparison.OrdinalIgnoreCase)))
        {
            QuotaSummaryText.Text = "Checking providers…";
            return;
        }

        QuotaSummaryText.Text = warnings > 0
            ? $"{configured} of {_providerQuotas.Count} configured · {warnings} attention"
            : $"{configured} of {_providerQuotas.Count} configured";
    }

    private async Task RefreshProviderQuotasAsync(bool force)
    {
        if (_bridge is null) return;
        RefreshQuotaButton.IsEnabled = false;
        _lastQuotaUpdatedText = force ? "Refreshing provider data…" : "Loading provider data…";
        QuotaUpdatedText.Text = _lastQuotaUpdatedText;
        try
        {
            using var document = await _bridge.QueryProviderQuotasAsync(force);
            if (document is null ||
                !document.RootElement.TryGetProperty("providers", out var providers))
                throw new InvalidOperationException("Provider quota status was unavailable.");

            _providerQuotas.Clear();
            var orderedProviders = providers.EnumerateArray()
                .Select((provider, index) => new
                {
                    Provider = provider.Clone(),
                    Index = index,
                    Configured = !string.Equals(
                        StringProperty(provider, "status"),
                        "not_configured",
                        StringComparison.OrdinalIgnoreCase),
                })
                .OrderByDescending(item => item.Configured)
                .ThenBy(item => item.Index);
            foreach (var item in orderedProviders)
            {
                var provider = item.Provider;
                var status = StringProperty(provider, "status") ?? "error";
                var meter = 0.0;
                var hasMeter = provider.TryGetProperty("meter", out var meterElement) && meterElement.TryGetDouble(out meter);
                _providerQuotas.Add(BuildQuotaViewModel(
                    id: StringProperty(provider, "id") ?? "provider",
                    name: StringProperty(provider, "name") ?? "Provider",
                    status: status,
                    primary: StringProperty(provider, "primary") ?? "Unavailable",
                    detail: StringProperty(provider, "detail") ?? "",
                    dashboardUrl: StringProperty(provider, "dashboard_url") ?? "",
                    hasMeter: hasMeter,
                    meterPercent: hasMeter ? meter * 100 : 0));
            }

            UpdateQuotaSummary();

            var cached = BoolProperty(document.RootElement, "cached");
            var dataSource = StringProperty(document.RootElement, "data_source");
            var updatedText = StringProperty(document.RootElement, "updated_at_utc");
            var time = DateTimeOffset.TryParse(updatedText, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var updated)
                ? updated.ToLocalTime().ToString("HH:mm", CultureInfo.CurrentCulture)
                : "now";
            var source = string.Equals(dataSource, "local_observations", StringComparison.Ordinal)
                ? " · quota-safe local snapshot"
                : cached ? " · cached for up to 5 minutes" : " · live";
            _lastQuotaUpdatedText = $"Updated {time}{source}";
            QuotaUpdatedText.Text = _lastQuotaUpdatedText;
        }
        catch (Exception exc)
        {
            SetQuotaPlaceholders();
            _lastQuotaUpdatedText = $"Could not refresh · {exc.Message}";
            QuotaUpdatedText.Text = _lastQuotaUpdatedText;
        }
        finally
        {
            RefreshQuotaButton.IsEnabled = true;
        }
    }

    private async void RefreshQuotaButton_Click(object sender, RoutedEventArgs e)
    {
        await RefreshProviderQuotasAsync(true);
    }

    private async void QuotaCard_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (sender is FrameworkElement { DataContext: ProviderQuotaViewModel provider } && provider.HasDashboardUrl)
            await OpenQuotaUrlInAppAsync(provider.Name, provider.DashboardUrl);
    }

    private async void OpenQuotaInAppButton_Click(object sender, RoutedEventArgs e)
    {
        e.Handled = true;
        if (sender is FrameworkElement { DataContext: ProviderQuotaViewModel provider } && provider.HasDashboardUrl)
            await OpenQuotaUrlInAppAsync(provider.Name, provider.DashboardUrl);
    }

    private void OpenQuotaExternalButton_Click(object sender, RoutedEventArgs e)
    {
        e.Handled = true;
        if (sender is FrameworkElement { DataContext: ProviderQuotaViewModel provider } && provider.HasDashboardUrl)
            OpenPath(provider.DashboardUrl);
    }

    private async Task OpenQuotaUrlInAppAsync(string title, string url)
    {
        if (string.IsNullOrWhiteSpace(url)) return;
        _quotaBrowserCurrentUrl = url;
        QuotaBrowserTitleText.Text = title;
        QuotaBrowserUrlText.Text = url;
        QuotaBrowserFallbackPanel.Visibility = Visibility.Collapsed;
        QuotaWebView.Visibility = Visibility.Visible;
        ShowQuotaBrowserPane();

        var ready = await EnsureQuotaWebViewReadyAsync();
        if (!ready)
        {
            QuotaWebView.Visibility = Visibility.Collapsed;
            QuotaBrowserProgress.Visibility = Visibility.Collapsed;
            QuotaBrowserFallbackText.Text =
                $"Embedded browser could not start on this system. Open {title} in your default browser instead.";
            QuotaBrowserFallbackPanel.Visibility = Visibility.Visible;
            return;
        }

        QuotaWebView.CoreWebView2.Navigate(url);
    }

    private async Task<bool> EnsureQuotaWebViewReadyAsync()
    {
        if (_quotaWebViewReady && QuotaWebView.CoreWebView2 is not null)
            return true;

        try
        {
            var userDataFolder = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "NewsStudio.App",
                "WebView2");
            Directory.CreateDirectory(userDataFolder);
            var env = await CoreWebView2Environment.CreateAsync(userDataFolder: userDataFolder);
            await QuotaWebView.EnsureCoreWebView2Async(env);
            if (QuotaWebView.CoreWebView2 is null) return false;

            QuotaWebView.CoreWebView2.Settings.AreDevToolsEnabled = false;
            QuotaWebView.CoreWebView2.Settings.IsStatusBarEnabled = false;
            SyncWebViewColorScheme(GetSelectedTheme());

            QuotaWebView.CoreWebView2.NavigationStarting += (_, args) =>
            {
                Dispatcher.BeginInvoke(() =>
                {
                    QuotaBrowserProgress.Visibility = Visibility.Visible;
                    _quotaBrowserCurrentUrl = args.Uri;
                    QuotaBrowserUrlText.Text = args.Uri;
                    UpdateQuotaBrowserNavButtons();
                });
            };
            QuotaWebView.CoreWebView2.NavigationCompleted += (_, _) =>
            {
                Dispatcher.BeginInvoke(() =>
                {
                    QuotaBrowserProgress.Visibility = Visibility.Collapsed;
                    UpdateQuotaBrowserNavButtons();
                });
            };
            QuotaWebView.CoreWebView2.SourceChanged += (_, _) =>
            {
                Dispatcher.BeginInvoke(() =>
                {
                    var current = QuotaWebView.CoreWebView2.Source;
                    if (!string.IsNullOrWhiteSpace(current))
                    {
                        _quotaBrowserCurrentUrl = current;
                        QuotaBrowserUrlText.Text = current;
                    }
                    UpdateQuotaBrowserNavButtons();
                });
            };
            QuotaWebView.CoreWebView2.DocumentTitleChanged += (_, _) =>
            {
                Dispatcher.BeginInvoke(() =>
                {
                    var docTitle = QuotaWebView.CoreWebView2.DocumentTitle;
                    if (!string.IsNullOrWhiteSpace(docTitle))
                        QuotaBrowserTitleText.Text = docTitle;
                });
            };

            _quotaWebViewReady = true;
            return true;
        }
        catch
        {
            return false;
        }
    }

    private void ShowQuotaBrowserPane()
    {
        QuotaBrowserPanel.Visibility = Visibility.Visible;
        QuotaBrowserSplitterColumn.Width = new GridLength(12);
        ApplyQuotaBrowserColumnWidths();
    }

    private void ApplyQuotaBrowserColumnWidths()
    {
        if (QuotaBrowserPanel.Visibility != Visibility.Visible)
        {
            QuotaCardsColumn.Width = new GridLength(1, GridUnitType.Star);
            QuotaBrowserSplitterColumn.Width = new GridLength(0);
            QuotaBrowserColumn.MinWidth = 0;
            QuotaBrowserColumn.Width = new GridLength(0);
            return;
        }

        QuotaBrowserColumn.MinWidth = 380;
        if (_quotaBrowserExpanded)
        {
            QuotaCardsColumn.Width = new GridLength(1, GridUnitType.Star);
            QuotaBrowserColumn.Width = new GridLength(1.65, GridUnitType.Star);
            QuotaBrowserExpandGlyph.Text = "\uE73F";
            QuotaBrowserExpandButton.ToolTip = "Restore compact browser pane";
        }
        else
        {
            QuotaCardsColumn.Width = new GridLength(1, GridUnitType.Star);
            QuotaBrowserColumn.Width = new GridLength(520);
            QuotaBrowserExpandGlyph.Text = "\uE740";
            QuotaBrowserExpandButton.ToolTip = "Expand in-app browser";
        }
    }

    private void UpdateQuotaBrowserNavButtons()
    {
        var core = QuotaWebView.CoreWebView2;
        QuotaBrowserBackButton.IsEnabled = _quotaWebViewReady && core?.CanGoBack == true;
        QuotaBrowserForwardButton.IsEnabled = _quotaWebViewReady && core?.CanGoForward == true;
    }

    private void SyncWebViewColorScheme(string theme)
    {
        if (!_quotaWebViewReady || QuotaWebView.CoreWebView2 is null) return;
        QuotaWebView.CoreWebView2.Profile.PreferredColorScheme = ThemeService.IsDark(theme)
            ? CoreWebView2PreferredColorScheme.Dark
            : CoreWebView2PreferredColorScheme.Light;
    }

    private void QuotaBrowserBackButton_Click(object sender, RoutedEventArgs e)
    {
        if (_quotaWebViewReady && QuotaWebView.CoreWebView2?.CanGoBack == true)
            QuotaWebView.CoreWebView2.GoBack();
    }

    private void QuotaBrowserForwardButton_Click(object sender, RoutedEventArgs e)
    {
        if (_quotaWebViewReady && QuotaWebView.CoreWebView2?.CanGoForward == true)
            QuotaWebView.CoreWebView2.GoForward();
    }

    private void QuotaBrowserReloadButton_Click(object sender, RoutedEventArgs e)
    {
        if (_quotaWebViewReady) QuotaWebView.CoreWebView2?.Reload();
    }

    private void QuotaBrowserExternalButton_Click(object sender, RoutedEventArgs e) =>
        OpenPath(_quotaBrowserCurrentUrl);

    private void QuotaBrowserExpandButton_Click(object sender, RoutedEventArgs e)
    {
        _quotaBrowserExpanded = !_quotaBrowserExpanded;
        ApplyQuotaBrowserColumnWidths();
    }

    private void QuotaBrowserCloseButton_Click(object sender, RoutedEventArgs e)
    {
        QuotaBrowserPanel.Visibility = Visibility.Collapsed;
        _quotaBrowserExpanded = false;
        ApplyQuotaBrowserColumnWidths();
    }

    private async void StartButton_Click(object sender, RoutedEventArgs e)
    {
        if (_bridge is null || _bridge.IsRunning) return;
        if (_latestProductionComplete && _latestUploadComplete)
        {
            if (QueueShortcutPressed())
            {
                ShowQueueDialog();
                return;
            }
            ProductionPreferencesExpander.IsExpanded = false;
            ProductionOptionsStatusText.Text = "Saved automatically";
            ProductionOptionsStatusText.SetResourceReference(TextBlock.ForegroundProperty, "TextSecondaryBrush");
            AnimateOverlayIn(ProductionOptionsOverlay, ProductionOptionsCard);
            return;
        }
        await RunProductionAsync(resumeLatest: true);
    }

    private async void QuickRunButton_Click(object sender, RoutedEventArgs e)
    {
        if (_bridge is null || _bridge.IsRunning) return;
        if (QueueShortcutPressed())
        {
            ShowQueueDialog();
            return;
        }
        var fastDefaults = Keyboard.Modifiers.HasFlag(ModifierKeys.Shift);
        AppendLog(fastDefaults
            ? "RUN    Creating a new video in fast mode"
            : "RUN    Creating a new video");
        await RunProductionAsync(createNewVideo: true, fastDefaults: fastDefaults);
    }

    private static bool QueueShortcutPressed() =>
        Keyboard.Modifiers.HasFlag(ModifierKeys.Control | ModifierKeys.Shift);

    private void ShowQueueDialog()
    {
        QueueCountTextBox.Text = "2";
        QueueErrorText.Text = "";
        AnimateOverlayIn(QueueOverlay, QueueCard);
        Dispatcher.BeginInvoke(DispatcherPriority.Input, () =>
        {
            QueueCountTextBox.Focus();
            QueueCountTextBox.SelectAll();
        });
    }

    private void CancelQueue_Click(object sender, RoutedEventArgs e) => HideQueueDialog();

    private void HideQueueDialog() => AnimateOverlayOut(QueueOverlay, QueueCard);

    private async void ConfirmQueue_Click(object sender, RoutedEventArgs e)
    {
        if (!TryParseQueueCount(QueueCountTextBox.Text, out var count, out var error))
        {
            QueueErrorText.Text = error;
            SystemSounds.Hand.Play();
            QueueCountTextBox.Focus();
            QueueCountTextBox.SelectAll();
            return;
        }

        HideQueueDialog();
        if (_activeQueueTask is { IsCompleted: false }) return;
        _activeQueueTask = RunProductionQueueAsync(count);
        try
        {
            await _activeQueueTask;
        }
        finally
        {
            if (_activeQueueTask?.IsCompleted != false) _activeQueueTask = null;
        }
    }

    private static bool TryParseQueueCount(string? value, out int count, out string error)
    {
        if (!int.TryParse(value?.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out count))
        {
            error = $"Enter a whole number from 1 to {MaximumQueueCount}.";
            return false;
        }
        if (count < 1 || count > MaximumQueueCount)
        {
            error = $"Generation count must be between 1 and {MaximumQueueCount}.";
            return false;
        }
        error = "";
        return true;
    }

    private async Task RunProductionQueueAsync(int count)
    {
        if (_bridge is null || _bridge.IsRunning || _queueRunning) return;
        _queueRunning = true;
        _queueCancellationRequested = false;
        var completed = 0;
        var stoppedBecause = "";
        StartButton.IsEnabled = false;
        QuickRunButton.IsEnabled = false;
        StartButton.Visibility = Visibility.Collapsed;
        QuickRunButton.Visibility = Visibility.Collapsed;
        StopButton.Visibility = Visibility.Visible;
        CancelQueueButton.Visibility = Visibility.Visible;
        CancelQueueButton.IsEnabled = true;
        UpdateCancelQueueCount(count);
        SetHeaderState("Queue running", "accent");
        AppendLog($"QUEUE  Scheduled {count} video generation{(count == 1 ? "" : "s")}");

        try
        {
            for (var index = 1; index <= count; index++)
            {
                if (_queueCancellationRequested) break;
                UpdateCancelQueueCount(count - completed);
                SetHeaderState($"Queue {index} of {count}", "accent");
                CurrentActionTitle.Text = $"Generating queued video {index} of {count}";
                CurrentActionDetail.Text = "This video must finish successfully before the next queued item starts.";
                AppendLog($"QUEUE  Starting item {index} of {count}");

                var outcome = await RunProductionAsync(
                    createNewVideo: true,
                    fastDefaults: true,
                    refreshProviderQuotas: false);
                if (outcome != ProductionRunOutcome.Completed)
                {
                    stoppedBecause = outcome == ProductionRunOutcome.Cancelled
                        ? "the active generation was cancelled"
                        : outcome == ProductionRunOutcome.NotStarted
                            ? "the production runner was unavailable"
                            : "the active generation failed";
                    break;
                }

                if (!await CurrentProjectIsCompleteAsync())
                {
                    stoppedBecause = "the active video requires manual attention before another video can start";
                    break;
                }

                completed++;
                UpdateCancelQueueCount(count - completed);
                AppendLog($"QUEUE  Completed item {index} of {count}");
            }
        }
        catch (Exception exc)
        {
            stoppedBecause = $"an unexpected queue error occurred: {exc.Message}";
            AppendLog($"ERROR  Queue stopped safely: {exc.Message}");
        }
        finally
        {
            var cancelled = _queueCancellationRequested;
            _queueRunning = false;
            _queueCancellationRequested = false;
            RestoreControlsAfterQueue();
            HideDecision();

            try
            {
                await Task.WhenAll(RefreshStatusAsync(), RefreshProviderQuotasAsync(true));
            }
            catch (Exception exc)
            {
                AppendLog($"ERROR  Queue finished, but status refresh failed: {exc.Message}");
            }

            if (completed == count)
            {
                AppendLog($"QUEUE  All {count} queued generations completed");
                SetHeaderState("Queue complete", "success");
                CurrentActionTitle.Text = "Generation queue complete";
                CurrentActionDetail.Text = $"All {count} videos finished in order.";
            }
            else if (cancelled)
            {
                AppendLog($"QUEUE  Cancelled after {completed} of {count} completed");
                SetHeaderState("Queue cancelled", "warning");
                CurrentActionTitle.Text = "Generation queue cancelled";
                CurrentActionDetail.Text = $"{completed} of {count} queued videos completed. No additional queued video will start.";
            }
            else
            {
                AppendLog($"QUEUE  Stopped after {completed} of {count}: {stoppedBecause}");
                SetHeaderState("Queue stopped", "warning");
                CurrentActionTitle.Text = "Generation queue stopped safely";
                CurrentActionDetail.Text = $"Completed {completed} of {count}; {stoppedBecause}.";
            }

        }
    }

    private async Task<bool> CurrentProjectIsCompleteAsync()
    {
        if (_bridge is null || string.IsNullOrWhiteSpace(_currentProject)) return false;
        try
        {
            using var status = await _bridge.QueryStatusAsync(_currentProject);
            var productionComplete = status is not null &&
                                     status.RootElement.TryGetProperty("complete", out var complete) &&
                                     complete.ValueKind is JsonValueKind.True or JsonValueKind.False &&
                                     complete.GetBoolean();
            var projectsRoot = GetProjectsRoot();
            return productionComplete && IsProjectUploadComplete(
                Path.Combine(projectsRoot, _currentProject)
            );
        }
        catch (Exception exc)
        {
            AppendLog($"ERROR  Could not verify queued video completion: {exc.Message}");
            return false;
        }
    }

    private void CancelActiveQueueButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_queueRunning || _queueCancellationRequested) return;
        _queueCancellationRequested = true;
        CancelQueueButton.Visibility = Visibility.Collapsed;
        AppendLog("QUEUE  Cancellation requested; no additional queued videos will start");
        SetHeaderState("Finishing current video", "warning");
        CurrentActionDetail.Text = "The remaining queue is cleared. The current generation will finish; use Stop safely to stop it too.";
    }

    private void UpdateCancelQueueCount(int remaining)
    {
        CancelQueueButton.Content = $"Cancel Queue ({Math.Max(0, remaining)} left)";
    }

    private void RestoreControlsAfterQueue()
    {
        StartButton.Visibility = Visibility.Visible;
        StartButton.IsEnabled = true;
        QuickRunButton.IsEnabled = true;
        SettingsNav.IsEnabled = true;
        StopButton.Visibility = Visibility.Collapsed;
        StopButton.IsEnabled = true;
        CancelQueueButton.Visibility = Visibility.Collapsed;
        CancelQueueButton.IsEnabled = true;
        CancelQueueButton.Content = "Cancel Queue";
    }

    private async Task<ProductionRunOutcome> RunProductionAsync(
        bool createNewVideo = false,
        bool resumeLatest = false,
        bool fastDefaults = false,
        bool refreshProviderQuotas = true,
        int? resumeVideoNumber = null,
        string? resumeStartStage = null)
    {
        if (_bridge is null || _bridge.IsRunning) return ProductionRunOutcome.NotStarted;
        StartButton.IsEnabled = false;
        QuickRunButton.IsEnabled = false;
        StopButton.Visibility = Visibility.Visible;
        SetHeaderState("Running", "accent");
        CurrentActionTitle.Text = "Checking the production environment";
        CurrentActionDetail.Text = "No provider request starts until preflight and the cost decision pass.";
        AppendLog("START  News Studio UI bridge");
        _bridge.EventReceived += Bridge_EventReceived;
        try
        {
            var exitCode = await _bridge.RunInteractiveAsync(
                createNewVideo: createNewVideo,
                resumeLatest: resumeLatest,
                fastDefaults: fastDefaults,
                resumeVideoNumber: resumeVideoNumber,
                resumeStartStage: resumeStartStage);
            AppendLog(exitCode switch
            {
                0 => "DONE   Workflow returned safely",
                130 => "CANCEL Workflow cancelled safely",
                _ => $"FAILED Workflow exited with code {exitCode}",
            });
            return exitCode switch
            {
                0 => ProductionRunOutcome.Completed,
                130 => ProductionRunOutcome.Cancelled,
                _ => ProductionRunOutcome.Failed,
            };
        }
        catch (OperationCanceledException)
        {
            AppendLog("STOP   Workflow cancelled");
            return ProductionRunOutcome.Cancelled;
        }
        catch (Exception exc)
        {
            AppendLog($"ERROR  {exc.Message}");
            SetHeaderState("Failed", "danger");
            CurrentActionTitle.Text = "Production stopped";
            CurrentActionDetail.Text = exc.Message;
            return ProductionRunOutcome.Failed;
        }
        finally
        {
            _bridge.EventReceived -= Bridge_EventReceived;
            StartButton.IsEnabled = !_queueRunning;
            QuickRunButton.IsEnabled = !_queueRunning;
            StopButton.Visibility = _queueRunning ? Visibility.Visible : Visibility.Collapsed;
            SettingsNav.IsEnabled = true;
            HideDecision();
            if (refreshProviderQuotas)
                await Task.WhenAll(RefreshStatusAsync(), RefreshProviderQuotasAsync(true));
            else
                await RefreshStatusAsync();
        }
    }

    private async void ContinueProductionOptions_Click(object sender, RoutedEventArgs e)
    {
        AnimateOverlayOut(ProductionOptionsOverlay, ProductionOptionsCard);
        AppendLog("RUN    Previous video is complete and uploaded; creating a new video");
        await RunProductionAsync(createNewVideo: true);
    }

    private void CloseProductionOptions_Click(object sender, RoutedEventArgs e) =>
        AnimateOverlayOut(ProductionOptionsOverlay, ProductionOptionsCard);

    private void ProductionPreference_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not CheckBox checkBox) return;
        UpdateAllProductionPreferencesCheckBox();
        SaveProductionPreferences([checkBox]);
    }

    private void AllProductionPreferences_PreviewMouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        ToggleAllProductionPreferences();
        e.Handled = true;
    }

    private void AllProductionPreferences_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Space) return;
        ToggleAllProductionPreferences();
        e.Handled = true;
    }

    private void ToggleAllProductionPreferences()
    {
        var enabled = AllProductionPreferencesCheckBox.IsChecked != true;
        var checkBoxes = ProductionPreferenceCheckBoxes();
        foreach (var checkBox in checkBoxes) checkBox.IsChecked = enabled;
        UpdateAllProductionPreferencesCheckBox();
        SaveProductionPreferences(checkBoxes);
    }

    private CheckBox[] ProductionPreferenceCheckBoxes() =>
    [
        ConfirmProductionActionCheckBox,
        ConfirmResumeSectionCheckBox,
        ConfirmProviderRequestsCheckBox,
        UseDefaultLeadStoryCheckBox,
        WaitForSeparatorsCheckBox,
        WaitForAiVideosCheckBox,
        WaitForPreviewCheckBox,
        UseDefaultYouTubeTitleCheckBox,
        ConfirmYouTubeUploadCheckBox,
    ];

    private void UpdateAllProductionPreferencesCheckBox()
    {
        var checkBoxes = ProductionPreferenceCheckBoxes();
        var checkedCount = checkBoxes.Count(checkBox => checkBox.IsChecked == true);
        AllProductionPreferencesCheckBox.IsChecked = checkedCount switch
        {
            0 => false,
            var count when count == checkBoxes.Length => true,
            _ => null,
        };
    }

    private void SaveProductionPreferences(IReadOnlyCollection<CheckBox> checkBoxes)
    {
        if (_envService is null) return;
        var changedSettings = new List<EnvSetting>();
        foreach (var checkBox in checkBoxes)
        {
            if (checkBox.Tag is not string key) continue;
            var setting = _settings.FirstOrDefault(item => item.Key.Equals(key, StringComparison.OrdinalIgnoreCase));
            if (setting is null) continue;
            setting.Value = checkBox.IsChecked == true ? "true" : "false";
            changedSettings.Add(setting);
        }
        try
        {
            var result = _envService.Save(changedSettings);
            if (result.Saved)
                foreach (var setting in changedSettings) setting.OriginalValue = setting.Value;
            ProductionOptionsStatusText.Text = "Saved automatically";
            ProductionOptionsStatusText.SetResourceReference(TextBlock.ForegroundProperty, "SuccessBrush");
        }
        catch (Exception exc)
        {
            foreach (var checkBox in checkBoxes)
            {
                if (checkBox.Tag is not string key) continue;
                var setting = _settings.FirstOrDefault(item => item.Key.Equals(key, StringComparison.OrdinalIgnoreCase));
                if (setting is not null)
                    checkBox.IsChecked = !setting.OriginalValue.Equals("false", StringComparison.OrdinalIgnoreCase);
            }
            UpdateAllProductionPreferencesCheckBox();
            ProductionOptionsStatusText.Text = $"Could not save: {exc.Message}";
            ProductionOptionsStatusText.SetResourceReference(TextBlock.ForegroundProperty, "DangerBrush");
        }
    }

    private void Bridge_EventReceived(JsonElement payload)
    {
        _ = Dispatcher.BeginInvoke(() => HandleBridgeEvent(payload));
    }

    private void HandleBridgeEvent(JsonElement payload)
    {
        if (!payload.TryGetProperty("type", out var typeElement)) return;
        var type = typeElement.GetString() ?? "";
        switch (type)
        {
            case "bridge_ready":
                AppendLog("READY  Pipeline bridge connected");
                break;
            case "log":
                var level = payload.TryGetProperty("level", out var levelElement) ? levelElement.GetString() : "info";
                var message = payload.TryGetProperty("message", out var messageElement) ? messageElement.GetString() : "";
                if (!string.IsNullOrWhiteSpace(message))
                    AppendLog($"{(level == "error" ? "ERROR" : level == "warning" ? "WARN " : "INFO ")}  {message}");
                break;
            case "stage_started":
                UpdateProjectFromEvent(payload);
                var startedStage = StringProperty(payload, "stage");
                var label = StringProperty(payload, "label") ?? startedStage ?? "Production step";
                foreach (var item in _stages)
                    item.StartedAtUtc = null;
                QueueStagesAfter(startedStage);
                SetStageState(startedStage, "RUNNING");
                if (startedStage is not null && _stageLookup.TryGetValue(startedStage, out var startedVm))
                {
                    startedVm.StartedAtUtc = DateTimeOffset.UtcNow;
                    startedVm.ElapsedText = "0s";
                }
                CurrentActionTitle.Text = label;
                CurrentActionDetail.Text = "This step is running. Output is cached as it completes.";
                CurrentProgressStepText.Text = $"Running: {label}";
                SetHeaderState("Running", "accent");
                UpdateOverallProgress();
                AppendLog($"STAGE  {label}");
                if (StageSoundCheckBox.IsChecked == true) SystemSounds.Asterisk.Play();
                break;
            case "stage_completed":
                var completedStage = StringProperty(payload, "stage");
                if (completedStage is not null && _stageLookup.TryGetValue(completedStage, out var completedVm))
                {
                    if (completedVm.StartedAtUtc is { } startedAt)
                        completedVm.ElapsedText = FormatCompactDuration(DateTimeOffset.UtcNow - startedAt);
                    completedVm.StartedAtUtc = null;
                }
                SetStageState(completedStage, "COMPLETE");
                if (!string.IsNullOrWhiteSpace(_currentProjectPath) && Directory.Exists(_currentProjectPath))
                    ApplyStageEventMetrics(_currentProjectPath);
                UpdateOverallProgress();
                if (completedStage is "news" or "tts" or "effects" or "render" or "publish")
                {
                    UpdatePipelineSnapshot();
                    UpdatePreviewButton();
                }
                break;
            case "render_progress":
                SetHeaderState("Rendering", "accent");
                CurrentActionTitle.Text = "Encoding the final video";
                var renderDetail = StringProperty(payload, "detail")
                    ?? "The approved preview is being rendered at full quality.";
                var renderCompleted = IntProperty(payload, "completed");
                var renderTotal = IntProperty(payload, "total");
                CurrentActionDetail.Text = renderTotal > 0 && !renderDetail.Contains(
                    $"{renderCompleted}/{renderTotal}",
                    StringComparison.Ordinal)
                    ? $"{renderDetail}  {renderCompleted}/{renderTotal}"
                    : renderDetail;
                break;
            case "stage_waiting":
                var waitingStage = StringProperty(payload, "stage");
                QueueStagesAfter(waitingStage);
                SetStageState(waitingStage, "WAITING");
                SetHeaderState("Waiting", "warning");
                CurrentActionTitle.Text = "Waiting for your input";
                CurrentActionDetail.Text = "The generated work is preserved. Choose an action or resume when manual files are ready.";
                break;
            case "network_waiting":
                SetHeaderState("Offline — waiting", "warning");
                CurrentActionTitle.Text = "Internet connection interrupted";
                CurrentActionDetail.Text = $"The current stage remains open and will check again in {DoubleProperty(payload, "retry_seconds"):g} seconds.";
                AppendLog($"WAIT   Network check #{IntProperty(payload, "attempt")}");
                break;
            case "network_resumed":
                SetHeaderState("Running", "accent");
                CurrentActionTitle.Text = "Connection restored";
                CurrentActionDetail.Text = "Retrying the same stage from its cached state.";
                AppendLog("ONLINE Network restored");
                break;
            case "stage_failed":
                var failedStage = StringProperty(payload, "stage");
                foreach (var item in _stages)
                    item.StartedAtUtc = null;
                SetStageState(failedStage, "FAILED");
                if (!string.IsNullOrWhiteSpace(_currentProjectPath) && Directory.Exists(_currentProjectPath))
                    ApplyStageEventMetrics(_currentProjectPath);
                SetHeaderState("Failed", "danger");
                CurrentActionTitle.Text = "This stage needs attention";
                CurrentActionDetail.Text = "Review the activity and detailed stage log. Completed stages remain reusable.";
                CurrentProgressStepText.Text = "Needs attention";
                RefreshCurrentProductionHistory();
                break;
            case "choice_requested":
                ShowDecision(payload);
                break;
            case "choice_cancelled":
                AppendLog("CANCEL Current workflow choice cancelled");
                SetHeaderState("Cancelling", "warning");
                break;
            case "attention_required":
                var attentionStage = StringProperty(payload, "stage");
                SetHeaderState("Action needed", "warning");
                CurrentActionTitle.Text = attentionStage == "ai_story_videos"
                    ? "Add the five AI story clips"
                    : "Add the manual separator files";
                CurrentActionDetail.Text = StringProperty(payload, "detail") is { Length: > 0 } detail
                    ? detail
                    : "The generated work is preserved. Continue production after the requested files are ready.";
                if (ChoiceSoundCheckBox.IsChecked == true) SystemSounds.Exclamation.Play();
                FlashTaskbar();
                break;
            case "fatal_error":
                SetHeaderState("Failed", "danger");
                CurrentActionTitle.Text = "Production stopped";
                CurrentActionDetail.Text = StringProperty(payload, "message") ?? "Unexpected pipeline error.";
                AppendLog($"ERROR  {CurrentActionDetail.Text}");
                break;
            case "bridge_finished":
                var status = StringProperty(payload, "status") ?? "COMPLETE";
                SetHeaderState(
                    status == "FAILED" ? "Failed" : status == "CANCELLED" ? "Cancelled" : status == "WAITING" ? "Waiting" : "Ready",
                    status == "FAILED" ? "danger" : status is "CANCELLED" or "WAITING" ? "warning" : "success");
                RefreshCurrentProductionHistory();
                break;
        }
    }

    private void UpdateProjectFromEvent(JsonElement payload)
    {
        var number = IntProperty(payload, "video_number");
        if (number <= 0) return;
        _currentProject = $"Video {number}";
        RecordProductionMode(number);
        var projectsRoot = GetProjectsRoot();
        _currentProjectPath = Path.Combine(projectsRoot, _currentProject);
        ActiveProjectChipText.Text = _currentProject;
        ActiveProjectChip.Visibility = Visibility.Visible;
        RefreshProductionHistory(projectsRoot);
        ProjectSummaryText.Text = $"Current: {_currentProject}";
        UpdatePipelineSnapshot();
        UpdatePreviewButton();
    }

    private void RefreshCurrentProductionHistory()
    {
        var projectsRoot = GetProjectsRoot();
        RefreshProductionHistory(projectsRoot);
    }

    private void ShowDecision(JsonElement payload)
    {
        _decisionTimer?.Stop();
        _decisionKeys.Clear();
        _decisionDefaultOptionTextBlock = null;
        DecisionOptionsPanel.Children.Clear();
        DecisionDetailsPanel.Children.Clear();
        DecisionDetailsCard.Visibility = Visibility.Collapsed;
        DecisionTextInputCard.Visibility = Visibility.Collapsed;
        DecisionTextInputError.Visibility = Visibility.Collapsed;
        DecisionTitle.Text = StringProperty(payload, "prompt") ?? "Choose an option";
        var isPreviewDecision = DecisionTitle.Text.Contains(
            "review preview",
            StringComparison.OrdinalIgnoreCase);
        var isTitleReviewDecision = DecisionTitle.Text.Contains(
            "youtube title",
            StringComparison.OrdinalIgnoreCase) &&
            DecisionTitle.Text.Contains(
                "review",
                StringComparison.OrdinalIgnoreCase);
        _decisionDefault = StringProperty(payload, "default") ?? "";
        _decisionWaitIndefinitely = BoolProperty(payload, "wait_indefinitely");
        _decisionDurationSeconds = _decisionWaitIndefinitely
            ? 0
            : Math.Max(1, DoubleProperty(payload, "timeout_seconds"));
        _decisionDeadline = _decisionWaitIndefinitely
            ? DateTime.MaxValue
            : DateTime.UtcNow.AddSeconds(_decisionDurationSeconds);
        _decisionPending = true;
        _decisionAssetPath = StringProperty(payload, "asset_path");
        DecisionAssetButton.Content = StringProperty(payload, "asset_label") ?? "Open file";
        DecisionAssetButton.Visibility = !string.IsNullOrWhiteSpace(_decisionAssetPath) && File.Exists(_decisionAssetPath)
            ? Visibility.Visible
            : !string.IsNullOrWhiteSpace(_decisionAssetPath) && Directory.Exists(_decisionAssetPath)
                ? Visibility.Visible
                : Visibility.Collapsed;
        FastDefaultsCheckBox.IsChecked = false;
        FastDefaultsCheckBox.Visibility = BoolProperty(payload, "allow_fast_defaults")
            ? Visibility.Visible
            : Visibility.Collapsed;

        DecisionDetailsTitle.Text = DecisionTitle.Text.Contains("provider request", StringComparison.OrdinalIgnoreCase)
            ? "Provider requests for this run"
            : isTitleReviewDecision
                ? "Publishing package details"
                : "Decision details";

        if (payload.TryGetProperty("detail_items", out var detailItems) &&
            detailItems.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in detailItems.EnumerateArray())
            {
                if (item.GetString() is not { Length: > 0 } detail) continue;
                DecisionDetailsPanel.Children.Add(new TextBlock
                {
                    Text = $"•  {detail}",
                    TextWrapping = TextWrapping.Wrap,
                    FontSize = 12,
                    Margin = new Thickness(0, 2, 0, 2),
                });
            }
            DecisionDetailsCard.Visibility = DecisionDetailsPanel.Children.Count > 0
                ? Visibility.Visible
                : Visibility.Collapsed;
        }

        var editableText = StringProperty(payload, "editable_text");
        _decisionEditableActive = !string.IsNullOrWhiteSpace(editableText);
        if (_decisionEditableActive && editableText is not null)
        {
            _decisionInitialEditableText = editableText.Trim();
            _decisionEditableMaxLength = IntProperty(payload, "editable_max_length") is > 0 and var maxLen
                ? maxLen
                : 100;
            DecisionTextInputLabel.Text = StringProperty(payload, "editable_label") ?? "YouTube title";
            DecisionCustomTextBox.MaxLength = _decisionEditableMaxLength;
            _suppressDecisionTextChanged = true;
            DecisionCustomTextBox.Text = _decisionInitialEditableText;
            _suppressDecisionTextChanged = false;
            DecisionTextInputCard.Visibility = Visibility.Visible;
        }
        else
        {
            _decisionInitialEditableText = "";
            _decisionEditableMaxLength = 100;
            _suppressDecisionTextChanged = true;
            DecisionCustomTextBox.Text = "";
            _suppressDecisionTextChanged = false;
        }

        if (payload.TryGetProperty("options", out var options))
        {
            foreach (var option in options.EnumerateObject())
            {
                var key = option.Name;
                var label = option.Value.GetString() ?? key;
                _decisionKeys.Add(key);
                var button = new Button
                {
                    Tag = key,
                    Style = (Style)FindResource(key == _decisionDefault ? "PrimaryButtonStyle" : "SecondaryButtonStyle"),
                    HorizontalContentAlignment = HorizontalAlignment.Stretch,
                    Margin = new Thickness(0, 0, 0, 8),
                    Padding = new Thickness(13, 11, 13, 11),
                };
                var grid = new Grid();
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                grid.ColumnDefinitions.Add(new ColumnDefinition());
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                var keyChip = new Border
                {
                    Width = 25,
                    Height = 25,
                    CornerRadius = new CornerRadius(12.5),
                    Background = new SolidColorBrush(Color.FromArgb(26, 0, 0, 0)),
                    Child = new TextBlock
                    {
                        Text = key.ToUpperInvariant(),
                        FontSize = 11,
                        FontWeight = FontWeights.SemiBold,
                        HorizontalAlignment = HorizontalAlignment.Center,
                        VerticalAlignment = VerticalAlignment.Center,
                    },
                };
                var text = new TextBlock
                {
                    Text = label,
                    TextWrapping = TextWrapping.Wrap,
                    VerticalAlignment = VerticalAlignment.Center,
                    Margin = new Thickness(11, 0, 8, 0),
                };
                Grid.SetColumn(text, 1);
                grid.Children.Add(keyChip);
                grid.Children.Add(text);
                if (key == _decisionDefault)
                {
                    _decisionDefaultOptionTextBlock = text;
                    var defaultText = new TextBlock
                    {
                        Text = "DEFAULT",
                        FontSize = 9,
                        FontWeight = FontWeights.Bold,
                        VerticalAlignment = VerticalAlignment.Center,
                        Opacity = 0.75,
                    };
                    Grid.SetColumn(defaultText, 2);
                    grid.Children.Add(defaultText);
                }
                button.Content = grid;
                button.Click += DecisionOption_Click;
                DecisionOptionsPanel.Children.Add(button);
            }
        }

        if (_decisionEditableActive)
            UpdateDecisionTextInputState();

        DecisionCountdownBorder.Visibility = _decisionWaitIndefinitely
            ? Visibility.Collapsed
            : Visibility.Visible;
        DecisionProgress.Visibility = _decisionWaitIndefinitely
            ? Visibility.Collapsed
            : Visibility.Visible;
        DecisionInstructionText.Text = _decisionWaitIndefinitely
            ? "The pipeline will wait here until you explicitly continue or stop."
            : isTitleReviewDecision
                ? "Review or edit the YouTube title below, or pick a candidate before upload."
                : "Choose now or the safe default will be used when the timer ends.";
        if (!_decisionWaitIndefinitely)
        {
            DecisionProgress.Maximum = _decisionDurationSeconds;
            DecisionProgress.Value = _decisionDurationSeconds;
            UpdateDecisionCountdown();
        }
        AnimateOverlayIn(DecisionOverlay, DecisionCard);
        if (isPreviewDecision)
        {
            SetHeaderState("Review needed", "warning");
            CurrentActionTitle.Text = "Review the generated preview";
            CurrentActionDetail.Text = "The preview is ready. Choose Approve and render or Stop before final render.";
        }
        else if (isTitleReviewDecision)
        {
            SetHeaderState("Review title", "warning");
            CurrentActionTitle.Text = "Review or change the YouTube title";
            CurrentActionDetail.Text = "Confirm the current title, edit it directly, or choose one of the candidate titles before uploading to YouTube.";
        }
        if (ChoiceSoundCheckBox.IsChecked == true)
            SystemSounds.Exclamation.Play();
        FlashTaskbar();
        Activate();

        if (BoolProperty(payload, "auto_open_asset") &&
            !string.IsNullOrWhiteSpace(_decisionAssetPath) &&
            File.Exists(_decisionAssetPath))
        {
            try
            {
                OpenPath(_decisionAssetPath);
                AppendLog($"OPEN   Preview launched: {_decisionAssetPath}");
            }
            catch (Exception exc)
            {
                AppendLog($"WARN   Preview could not open automatically: {exc.Message}");
            }
        }

        if (!_decisionWaitIndefinitely)
        {
            _decisionTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(100) };
            _decisionTimer.Tick += DecisionTimer_Tick;
            _decisionTimer.Start();
        }
    }

    private void DecisionCustomTextBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (_suppressDecisionTextChanged || !_decisionEditableActive) return;
        PauseDecisionTimerForEditing();
        UpdateDecisionTextInputState();
    }

    private void PauseDecisionTimerForEditing()
    {
        if (_decisionWaitIndefinitely) return;
        _decisionWaitIndefinitely = true;
        _decisionTimer?.Stop();
        DecisionCountdownText.Text = "Editing";
        DecisionProgress.Visibility = Visibility.Collapsed;
        DecisionInstructionText.Text = "Timer paused while editing the title. Click option 1 or press Enter when ready.";
    }

    private void UpdateDecisionTextInputState()
    {
        var normalized = Regex.Replace(DecisionCustomTextBox.Text.Trim(), @"\s+", " ");
        var length = normalized.Length;
        DecisionTextInputCounter.Text = $"{length} / {_decisionEditableMaxLength}";
        DecisionTextInputCounter.Foreground = length == 0 || length > _decisionEditableMaxLength
            ? ResourceBrush("DangerBrush")
            : ResourceBrush("TextSecondaryBrush");
        if (length >= 1 && length <= _decisionEditableMaxLength)
            DecisionTextInputError.Visibility = Visibility.Collapsed;
        if (_decisionDefaultOptionTextBlock is not null)
        {
            _decisionDefaultOptionTextBlock.Text = string.Equals(normalized, _decisionInitialEditableText, StringComparison.Ordinal)
                ? $"Keep current title: {_decisionInitialEditableText}"
                : length > 0
                    ? $"Use edited title: {normalized}"
                    : "Use edited title";
        }
    }

    private void DecisionTimer_Tick(object? sender, EventArgs e)
    {
        if (!_decisionPending || _decisionWaitIndefinitely) return;
        var remaining = (_decisionDeadline - DateTime.UtcNow).TotalSeconds;
        if (remaining <= 0)
        {
            _ = SubmitDecisionAsync(_decisionDefault);
            return;
        }
        DecisionProgress.Value = Math.Min(_decisionDurationSeconds, remaining);
        UpdateDecisionCountdown();
    }

    private void UpdateDecisionCountdown()
    {
        var remaining = Math.Max(0, (_decisionDeadline - DateTime.UtcNow).TotalSeconds);
        var span = TimeSpan.FromSeconds(Math.Ceiling(remaining));
        DecisionCountdownText.Text = span.TotalHours >= 1
            ? $"{(int)span.TotalHours}:{span.Minutes:00}:{span.Seconds:00}"
            : $"{span.Minutes}:{span.Seconds:00}";
    }

    private async void DecisionOption_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string key }) return;
        EnableFastDefaultsFromShift();
        await SubmitDecisionAsync(key);
    }

    private void EnableFastDefaultsFromShift()
    {
        if (FastDefaultsCheckBox.Visibility == Visibility.Visible &&
            Keyboard.Modifiers.HasFlag(ModifierKeys.Shift))
            FastDefaultsCheckBox.IsChecked = true;
    }

    private async Task SubmitDecisionAsync(string key)
    {
        if (!_decisionPending || _bridge is null) return;
        string? customText = null;
        if (_decisionEditableActive && key == _decisionDefault)
        {
            var normalized = Regex.Replace(DecisionCustomTextBox.Text.Trim(), @"\s+", " ");
            if (string.IsNullOrWhiteSpace(normalized) || normalized.Length > _decisionEditableMaxLength)
            {
                DecisionTextInputError.Text = $"Enter a YouTube title between 1 and {_decisionEditableMaxLength} characters.";
                DecisionTextInputError.Visibility = Visibility.Visible;
                SystemSounds.Hand.Play();
                DecisionCustomTextBox.Focus();
                return;
            }
            customText = normalized;
        }
        var isPreviewApproval = key == "1" && DecisionTitle.Text.Contains(
            "review preview",
            StringComparison.OrdinalIgnoreCase);
        _decisionPending = false;
        _decisionTimer?.Stop();
        AnimateOverlayOut(DecisionOverlay, DecisionCard);
        var fastDefaults = FastDefaultsCheckBox.IsChecked == true;
        if (customText is not null && !string.Equals(customText, _decisionInitialEditableText, StringComparison.Ordinal))
        {
            AppendLog($"TITLE  YouTube title updated: {customText}" +
                      (fastDefaults ? " · later timed waits will use defaults" : ""));
        }
        else
        {
            AppendLog($"CHOICE {key} selected{(key == _decisionDefault ? " (default)" : "")}" +
                      (fastDefaults ? " · later timed waits will use defaults" : ""));
        }
        if (isPreviewApproval)
        {
            SetHeaderState("Rendering", "accent");
            CurrentActionTitle.Text = "Preparing the final render";
            CurrentActionDetail.Text = "Preview approved. Validating cached outputs and starting the full-quality encode.";
            AppendLog("RENDER Preview approved; preparing the final encode");
        }
        await _bridge.SendChoiceAsync(key, fastDefaults, customText);
    }

    private async void CancelDecisionButton_Click(object sender, RoutedEventArgs e)
    {
        await CancelDecisionAsync();
    }

    private async Task CancelDecisionAsync()
    {
        if (!_decisionPending || _bridge is null) return;
        _decisionPending = false;
        _decisionTimer?.Stop();
        AnimateOverlayOut(DecisionOverlay, DecisionCard);
        AppendLog("CANCEL Workflow cancelled from the choice dialog");
        await _bridge.CancelChoiceAsync();
    }

    private void HideDecision()
    {
        _decisionPending = false;
        _decisionEditableActive = false;
        _decisionTimer?.Stop();
        AnimateOverlayOut(DecisionOverlay, DecisionCard);
    }

    private void Window_KeyDown(object sender, KeyEventArgs e)
    {
        if (QueueOverlay.Visibility == Visibility.Visible)
        {
            if (e.Key == Key.Escape)
            {
                HideQueueDialog();
                e.Handled = true;
            }
            else if (e.Key == Key.Enter)
            {
                ConfirmQueue_Click(QueueCountTextBox, new RoutedEventArgs());
                e.Handled = true;
            }
            return;
        }
        if (ProductionOptionsOverlay.Visibility == Visibility.Visible)
        {
            if (e.Key == Key.Escape)
            {
                AnimateOverlayOut(ProductionOptionsOverlay, ProductionOptionsCard);
                e.Handled = true;
            }
            return;
        }
        if (AddVariableOverlay.Visibility == Visibility.Visible)
        {
            if (e.Key == Key.Escape)
            {
                HideAddVariableDialog();
                e.Handled = true;
            }
            else if (e.Key == Key.Enter)
            {
                ConfirmAddVariable();
                e.Handled = true;
            }
            return;
        }
        if (VoiceoverResetOverlay.Visibility == Visibility.Visible)
        {
            if (e.Key == Key.Escape && !_voiceoverSaving)
            {
                CancelVoiceoverReset_Click(VoiceoverCancelButton, new RoutedEventArgs());
                e.Handled = true;
            }
            return;
        }
        if (!_decisionPending) return;
        if (_decisionEditableActive &&
            (ReferenceEquals(Keyboard.FocusedElement, DecisionCustomTextBox) ||
             ReferenceEquals(e.OriginalSource, DecisionCustomTextBox)))
        {
            if (e.Key == Key.Enter)
            {
                EnableFastDefaultsFromShift();
                _ = SubmitDecisionAsync(_decisionDefault);
                e.Handled = true;
            }
            else if (e.Key == Key.Escape)
            {
                _ = CancelDecisionAsync();
                e.Handled = true;
            }
            return;
        }
        if (e.Key == Key.Enter)
        {
            EnableFastDefaultsFromShift();
            _ = SubmitDecisionAsync(_decisionDefault);
            e.Handled = true;
            return;
        }
        if (e.Key == Key.Escape)
        {
            _ = CancelDecisionAsync();
            e.Handled = true;
            return;
        }
        var key = e.Key switch
        {
            >= Key.D0 and <= Key.D9 => ((int)e.Key - (int)Key.D0).ToString(CultureInfo.InvariantCulture),
            >= Key.NumPad0 and <= Key.NumPad9 => ((int)e.Key - (int)Key.NumPad0).ToString(CultureInfo.InvariantCulture),
            _ => "",
        };
        if (_decisionKeys.Contains(key))
        {
            EnableFastDefaultsFromShift();
            _ = SubmitDecisionAsync(key);
            e.Handled = true;
        }
    }

    private void SetStageState(string? key, string state)
    {
        if (key is null || !_stageLookup.TryGetValue(key, out var stage)) return;
        var normalized = (state ?? "NOT_STARTED").ToUpperInvariant();
        if (normalized == "INVALID") normalized = "FAILED";
        stage.StateKey = normalized;

        switch (normalized)
        {
            case "COMPLETE":
                stage.Status = "Complete";
                stage.StatusBrush = ResourceBrush("SuccessBrush");
                stage.Glyph = "\uE73E";
                stage.RowBackground = Brushes.Transparent;
                stage.RowBorderBrush = Brushes.Transparent;
                stage.BadgeBackground = ResourceBrush("SuccessTintBrush");
                stage.BadgeForeground = ResourceBrush("SuccessBrush");
                stage.StatusPillBackground = ResourceBrush("SuccessTintBrush");
                stage.StatusPillBorderBrush = ResourceBrush("SuccessTintBrush");
                stage.LabelWeight = FontWeights.Medium;
                break;
            case "RUNNING":
                stage.Status = "Running";
                stage.StatusBrush = ResourceBrush("AccentBrush");
                stage.Glyph = "\uE895";
                stage.RowBackground = ResourceBrush("AccentTintBrush");
                stage.RowBorderBrush = ResourceBrush("AccentBrush");
                stage.BadgeBackground = ResourceBrush("AccentBrush");
                stage.BadgeForeground = Brushes.White;
                stage.StatusPillBackground = ResourceBrush("AccentTintBrush");
                stage.StatusPillBorderBrush = ResourceBrush("AccentBrush");
                stage.LabelWeight = FontWeights.SemiBold;
                break;
            case "WAITING":
                stage.Status = "Waiting";
                stage.StatusBrush = ResourceBrush("WarningBrush");
                stage.Glyph = "\uE823";
                stage.RowBackground = ResourceBrush("WarningTintBrush");
                stage.RowBorderBrush = ResourceBrush("WarningBrush");
                stage.BadgeBackground = ResourceBrush("WarningTintBrush");
                stage.BadgeForeground = ResourceBrush("WarningBrush");
                stage.StatusPillBackground = ResourceBrush("WarningTintBrush");
                stage.StatusPillBorderBrush = ResourceBrush("WarningBrush");
                stage.LabelWeight = FontWeights.SemiBold;
                break;
            case "FAILED":
                stage.Status = "Failed";
                stage.StatusBrush = ResourceBrush("DangerBrush");
                stage.Glyph = "\uE783";
                stage.RowBackground = ResourceBrush("DangerTintBrush");
                stage.RowBorderBrush = ResourceBrush("DangerBrush");
                stage.BadgeBackground = ResourceBrush("DangerTintBrush");
                stage.BadgeForeground = ResourceBrush("DangerBrush");
                stage.StatusPillBackground = ResourceBrush("DangerTintBrush");
                stage.StatusPillBorderBrush = ResourceBrush("DangerBrush");
                stage.LabelWeight = FontWeights.SemiBold;
                break;
            case "SKIPPED":
                stage.Status = "Skipped";
                stage.StatusBrush = ResourceBrush("TextTertiaryBrush");
                stage.Glyph = "\uE71A";
                stage.RowBackground = Brushes.Transparent;
                stage.RowBorderBrush = Brushes.Transparent;
                stage.BadgeBackground = ResourceBrush("SurfaceAltBrush");
                stage.BadgeForeground = ResourceBrush("TextTertiaryBrush");
                stage.StatusPillBackground = ResourceBrush("SurfaceAltBrush");
                stage.StatusPillBorderBrush = Brushes.Transparent;
                stage.LabelWeight = FontWeights.Normal;
                break;
            default:
                stage.StateKey = "NOT_STARTED";
                stage.Status = "Not started";
                stage.StatusBrush = ResourceBrush("MutedBrush");
                stage.Glyph = "\uE915";
                stage.RowBackground = Brushes.Transparent;
                stage.RowBorderBrush = Brushes.Transparent;
                stage.BadgeBackground = ResourceBrush("SurfaceAltBrush");
                stage.BadgeForeground = ResourceBrush("TextSecondaryBrush");
                stage.StatusPillBackground = Brushes.Transparent;
                stage.StatusPillBorderBrush = Brushes.Transparent;
                stage.LabelWeight = FontWeights.Normal;
                break;
        }

        UpdateStageTooltip(stage);
    }

    private static bool IsTerminalStageStatus(string state) =>
        state.Equals("COMPLETE", StringComparison.OrdinalIgnoreCase) ||
        state.Equals("SKIPPED", StringComparison.OrdinalIgnoreCase);

    private void QueueStagesAfter(string? key)
    {
        if (string.IsNullOrWhiteSpace(key)) return;
        var found = false;
        foreach (var (stageKey, _) in StageDefinitions)
        {
            if (found)
            {
                if (_stageLookup.TryGetValue(stageKey, out var resetStage))
                {
                    resetStage.ElapsedText = "";
                    resetStage.LogFilePath = null;
                    resetStage.StartedAtUtc = null;
                }
                SetStageState(stageKey, "NOT_STARTED");
            }
            if (stageKey.Equals(key, StringComparison.OrdinalIgnoreCase)) found = true;
        }
    }

    private static string? GetStageLabel(string? key)
    {
        if (string.IsNullOrWhiteSpace(key)) return null;
        var normalized = NormalizeEventStageKey(key);
        foreach (var (stageKey, label) in StageDefinitions)
        {
            if (stageKey.Equals(normalized, StringComparison.OrdinalIgnoreCase))
                return label;
        }
        return null;
    }

    private void UpdateOverallProgress()
    {
        var total = MainStageDefinitions.Length;
        var completed = Math.Clamp(_stages.Count(stage => IsTerminalStageStatus(stage.StateKey)), 0, total);
        OverallProgress.Maximum = total;
        StepsCardProgress.Maximum = total;
        OverallProgress.Value = completed;
        StepsCardProgress.Value = completed;
        StagesCompletedText.Text = $" · {completed} / {total} complete";
        CurrentProgressCountText.Text = $"{completed} of {total} steps";
        PercentText.Text = $"{Math.Round(completed * 100d / total):0}%";
        if (completed >= total)
            CurrentProgressStepText.Text = "All production steps finished";
        else if (completed == 0 && _bridge?.IsRunning != true)
            CurrentProgressStepText.Text = "Ready to start";
    }

    private void SetHeaderState(string text, string kind)
    {
        HeaderStatusText.Text = text;
        HeaderStatusDot.Fill = kind switch
        {
            "accent" => ResourceBrush("AccentBrush"),
            "success" => ResourceBrush("SuccessBrush"),
            "warning" => ResourceBrush("WarningBrush"),
            "danger" => ResourceBrush("DangerBrush"),
            _ => ResourceBrush("MutedBrush"),
        };
    }

    private void ProductionTimer_Tick(object? sender, EventArgs e)
    {
        var now = DateTimeOffset.UtcNow;
        foreach (var production in _productionHistory.Where(item => item.IsRunning))
            production.Duration = FormatDuration(now - production.StartedAtUtc);
        foreach (var stage in _stages)
        {
            if (stage.StartedAtUtc is { } startedAt && stage.StateKey == "RUNNING")
                stage.ElapsedText = FormatCompactDuration(now - startedAt);
        }
        UpdateProductionTimerSummary();
    }

    private void RefreshProductionHistory(string projectsRoot)
    {
        var previousSelectedNumber = (ProductionsMasterList.SelectedItem as ProductionTimerLogViewModel)?.VideoNumber
                                     ?? _selectedHistoryItem?.VideoNumber;
        var allProjects = Directory.Exists(projectsRoot)
            ? Directory.EnumerateDirectories(projectsRoot, "Video *", SearchOption.TopDirectoryOnly)
                .Select(path => new { Path = path, Match = Regex.Match(Path.GetFileName(path), @"^Video (\d+)$", RegexOptions.IgnoreCase) })
                .Where(item => item.Match.Success)
                .Select(item => new { item.Path, Number = int.Parse(item.Match.Groups[1].Value, CultureInfo.InvariantCulture) })
                .OrderByDescending(item => item.Number)
                .ToList()
            : [];

        _productionHistory.Clear();
        var remaining = 0;
        foreach (var project in allProjects)
        {
            if (_productionHistory.Count < _productionHistoryLimit)
            {
                var isActiveProject = _bridge?.IsRunning == true &&
                                      string.Equals(
                                          _currentProject,
                                          $"Video {project.Number}",
                                          StringComparison.OrdinalIgnoreCase);
                var item = CreateProductionTimerLog(project.Path, project.Number, isActiveProject);
                if (item is not null) _productionHistory.Add(item);
            }
            else if (File.Exists(Path.Combine(project.Path, "orchestration", "events.jsonl")))
            {
                remaining++;
            }
        }

        UpdateProductionTimerSummary();

        var totalShown = _productionHistory.Count;
        HistoryCountBadgeText.Text = remaining > 0
            ? $"{totalShown} of {totalShown + remaining} videos"
            : totalShown == 1
                ? "1 video"
                : $"{totalShown} videos";
        HistoryEmptyListPanel.Visibility = totalShown == 0
            ? Visibility.Visible
            : Visibility.Collapsed;

        if (remaining > 0)
        {
            var nextBatch = Math.Min(ProductionHistoryPageSize, remaining);
            HistoryShowMoreLabel.Text = $"Show more... (+{nextBatch})";
            HistoryShowMoreButton.ToolTip = $"Load {nextBatch} older production{(nextBatch == 1 ? "" : "s")} ({remaining} remaining)";
            HistoryShowMoreButton.Visibility = Visibility.Visible;
        }
        else
        {
            HistoryShowMoreButton.Visibility = Visibility.Collapsed;
        }

        var targetItem = previousSelectedNumber is > 0
            ? _productionHistory.FirstOrDefault(item => item.VideoNumber == previousSelectedNumber) ?? _productionHistory.FirstOrDefault()
            : _productionHistory.FirstOrDefault();
        if (targetItem is not null)
        {
            if (ReferenceEquals(ProductionsMasterList.SelectedItem, targetItem))
                LoadHistoryProductionDetail(targetItem);
            else
                ProductionsMasterList.SelectedItem = targetItem;
        }
        else
        {
            LoadHistoryProductionDetail(null);
        }
    }

    private void HistoryShowMoreButton_Click(object sender, RoutedEventArgs e)
    {
        _productionHistoryLimit += ProductionHistoryPageSize;
        var projectsRoot = GetProjectsRoot();
        RefreshProductionHistory(projectsRoot);
    }

    private ProductionTimerLogViewModel? CreateProductionTimerLog(
        string projectPath,
        int videoNumber,
        bool activeOverride = false)
    {
        var eventsPath = Path.Combine(projectPath, "orchestration", "events.jsonl");
        if (!File.Exists(eventsPath)) return null;

        DateTimeOffset? startedAt = null;
        DateTimeOffset? terminalAt = null;
        DateTimeOffset? lastActivityAt = null;
        var status = "Finished";
        try
        {
            foreach (var line in File.ReadLines(eventsPath))
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                using var document = JsonDocument.Parse(line);
                var root = document.RootElement;
                if (!root.TryGetProperty("at_utc", out var atValue) ||
                    !DateTimeOffset.TryParse(atValue.GetString(), CultureInfo.InvariantCulture,
                        DateTimeStyles.AssumeUniversal, out var timestamp))
                    continue;
                lastActivityAt = timestamp;
                var type = StringProperty(root, "type");
                if (type == "flow_started")
                {
                    startedAt ??= timestamp;
                    terminalAt = null;
                    status = "Running";
                }
                else if (type == "stage_started" && startedAt is not null)
                {
                    status = "Running";
                    terminalAt = null;
                }
                else if (type == "flow_completed" && startedAt is not null)
                {
                    var complete = !root.TryGetProperty("complete", out var completeValue) ||
                                   completeValue.ValueKind != JsonValueKind.False;
                    status = complete ? "Finished" : "Failed";
                    terminalAt = timestamp;
                }
                else if (type == "flow_waiting" && startedAt is not null)
                {
                    status = "Finished";
                    terminalAt = timestamp;
                }
                else if ((type is "stage_failed" or "stage_interrupted" or "fatal_error") &&
                         startedAt is not null)
                {
                    status = "Failed";
                    terminalAt = timestamp;
                }
                else if (type == "stage_completed" &&
                         StringProperty(root, "stage") == "youtube_upload" &&
                         startedAt is not null && status == "Running")
                {
                    status = "Finished";
                    terminalAt = timestamp;
                }
            }
        }
        catch (IOException)
        {
            return null;
        }
        catch (JsonException)
        {
            return null;
        }

        if (startedAt is null) return null;
        var videoName = $"Video {videoNumber}";
        var hasProjectLock = HasLiveProjectLock(projectPath);
        var isRunning = activeOverride || (status == "Running" && hasProjectLock);
        if (activeOverride)
        {
            status = "Running";
            terminalAt = null;
        }
        if (status == "Running" && !isRunning)
        {
            status = "Failed";
            terminalAt = lastActivityAt;
        }
        var finishedAt = isRunning
            ? DateTimeOffset.UtcNow
            : terminalAt ?? lastActivityAt ?? startedAt.Value;
        var (statusBackground, statusBorder, statusForeground) = StatusPresentation(status);
        var isUploaded = IsProjectUploadComplete(projectPath);
        var (uploadText, uploadBg, uploadFg) = isUploaded
            ? ("YouTube", ResourceBrush("SuccessTintBrush"), ResourceBrush("SuccessBrush"))
            : !isRunning && string.Equals(status, "Finished", StringComparison.OrdinalIgnoreCase) && ResolveFinalVideoPath(projectPath) is not null
                ? ("Rendered", ResourceBrush("AccentTintBrush"), ResourceBrush("AccentBrush"))
                : ("", Brushes.Transparent, ResourceBrush("TextTertiaryBrush"));
        var headline = ResolveProductionHeadline(projectPath, isRunning, status);

        return new ProductionTimerLogViewModel
        {
            VideoName = videoName,
            VideoNumber = videoNumber,
            ProjectPath = projectPath,
            Headline = headline,
            Date = startedAt.Value.ToLocalTime().ToString("dd MMM yyyy", CultureInfo.CurrentCulture),
            Duration = FormatDuration(finishedAt - startedAt.Value),
            Status = status,
            UploadBadgeText = uploadText,
            UploadBadgeBackground = uploadBg,
            UploadBadgeForeground = uploadFg,
            StatusBackground = statusBackground,
            StatusBorder = statusBorder,
            StatusForeground = statusForeground,
            IsRunning = isRunning,
            StartedAtUtc = startedAt.Value,
        };
    }

    private (Brush Background, Brush Border, Brush Foreground) StatusPresentation(string status) =>
        status switch
        {
            "Running" => (
                ResourceBrush("SurfaceBrush"),
                ResourceBrush("AccentBrush"),
                ResourceBrush("AccentBrush")),
            "Failed" => (
                ResourceBrush("SurfaceBrush"),
                ResourceBrush("DangerBrush"),
                ResourceBrush("DangerBrush")),
            _ => (
                ResourceBrush("SurfaceBrush"),
                ResourceBrush("SuccessBrush"),
                ResourceBrush("SuccessBrush")),
        };

    private static bool HasLiveProjectLock(string projectPath)
    {
        var lockPath = Path.Combine(projectPath, ".news_studio.lock");
        if (!File.Exists(lockPath)) return false;
        try
        {
            using var document = JsonDocument.Parse(File.ReadAllText(lockPath));
            if (!document.RootElement.TryGetProperty("pid", out var pidValue) ||
                !pidValue.TryGetInt32(out var pid) || pid <= 0)
                return false;
            using var process = Process.GetProcessById(pid);
            return !process.HasExited;
        }
        catch (Exception exception) when (
            exception is IOException or JsonException or ArgumentException or
            InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            return false;
        }
    }

    private void RecordProductionMode(int videoNumber)
    {
        var projectsRoot = GetProjectsRoot();
        var metadataPath = Path.Combine(projectsRoot, $"Video {videoNumber}", "orchestration", "production_mode.json");
        if (File.Exists(metadataPath)) return;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(metadataPath)!);
            var metadata = new
            {
                schema_version = 1,
                skeptical = SkepticalModeToggleButton.IsChecked == true,
                recorded_at_utc = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture),
            };
            File.WriteAllText(metadataPath, JsonSerializer.Serialize(metadata, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (IOException)
        {
            // The production itself must not be affected if timing metadata cannot be written.
        }
    }

    private void UpdateProductionTimerSummary()
    {
        var activeProduction = _productionHistory.FirstOrDefault(item => item.IsRunning);
        ProductionTimerText.Text = activeProduction?.Duration ?? "00:00:00";
        ProductionTimerDot.Fill = activeProduction is null
            ? ResourceBrush("MutedBrush")
            : ResourceBrush("AccentBrush");
    }

    private static string FormatDuration(TimeSpan duration)
    {
        if (duration < TimeSpan.Zero) duration = TimeSpan.Zero;
        var hours = (int)duration.TotalHours;
        return $"{hours:00}:{duration.Minutes:00}:{duration.Seconds:00}";
    }

    private void AppendLog(string message)
    {
        var entry = ParseActivityLogEntry(message);
        if (entry is null) return;
        ApplyLogEntryThemeBrushes(entry);
        _allActivityLogs.Add(entry);
        const int maxActivityEntries = 400;
        if (_allActivityLogs.Count > maxActivityEntries)
        {
            var removed = _allActivityLogs[0];
            _allActivityLogs.RemoveAt(0);
            _filteredActivityLogs.Remove(removed);
        }
        if (MatchesActivityFilter(entry))
            _filteredActivityLogs.Add(entry);
        UpdateActivityEmptyState();
        LogScrollViewer.ScrollToEnd();
    }

    private void LoadSettings()
    {
        if (_envService is null) return;
        _settings = _envService.Load().ToList();
        var sound = _settings.First(item => item.Key == "NEWS_STUDIO_GUI_CHOICE_SOUND").Value;
        ChoiceSoundCheckBox.IsChecked = !sound.Equals("false", StringComparison.OrdinalIgnoreCase);
        var stageSound = _settings.First(item => item.Key == "NEWS_STUDIO_GUI_STAGE_SOUND").Value;
        StageSoundCheckBox.IsChecked = !stageSound.Equals("false", StringComparison.OrdinalIgnoreCase);
        var skeptical = _settings.First(item => item.Key == "NEWS_STUDIO_BE_SKEPTICAL").Value;
        SkepticalModeToggleButton.IsChecked = !skeptical.Equals("false", StringComparison.OrdinalIgnoreCase);
        LoadProductionPreference(ConfirmProductionActionCheckBox, "NEWS_STUDIO_GUI_USE_DEFAULT_PRODUCTION_ACTION");
        LoadProductionPreference(ConfirmResumeSectionCheckBox, "NEWS_STUDIO_GUI_USE_DEFAULT_RESUME_SECTION");
        LoadProductionPreference(ConfirmProviderRequestsCheckBox, "NEWS_STUDIO_GUI_USE_DEFAULT_PROVIDER_REQUESTS");
        LoadProductionPreference(UseDefaultLeadStoryCheckBox, "NEWS_STUDIO_GUI_USE_DEFAULT_LEAD_STORY");
        LoadProductionPreference(WaitForSeparatorsCheckBox, "NEWS_STUDIO_GUI_USE_DEFAULT_SEPARATORS");
        LoadProductionPreference(WaitForAiVideosCheckBox, "NEWS_STUDIO_GUI_USE_DEFAULT_AI_VIDEOS");
        LoadProductionPreference(WaitForPreviewCheckBox, "NEWS_STUDIO_GUI_USE_DEFAULT_PREVIEW");
        LoadProductionPreference(UseDefaultYouTubeTitleCheckBox, "NEWS_STUDIO_GUI_USE_DEFAULT_YOUTUBE_TITLE");
        LoadProductionPreference(ConfirmYouTubeUploadCheckBox, "NEWS_STUDIO_GUI_USE_DEFAULT_YOUTUBE_UPLOAD");
        UpdateAllProductionPreferencesCheckBox();
        var theme = _settings.First(item => item.Key == "NEWS_STUDIO_GUI_THEME").Value;
        SelectTheme(theme);
        ApplyTheme(theme);
        BuildSettingsList();
        UpdateUnsavedSettingsIndicators();
        UpdatePipelineSnapshot();
        SettingsStatusText.Text = "Settings stay available during a run. Changes apply to the next pipeline run.";
        SettingsStatusText.SetResourceReference(TextBlock.ForegroundProperty, "TextSecondaryBrush");
    }

    private void LoadProductionPreference(CheckBox checkBox, string key)
    {
        var value = _settings.First(item => item.Key == key).Value;
        checkBox.IsChecked = !value.Equals("false", StringComparison.OrdinalIgnoreCase);
    }

    private void BuildSettingsList()
    {
        CaptureEditorValues();
        _settingEditors.Clear();
        _settingEditedBadges.Clear();
        _settingResetButtons.Clear();
        _categoryModifiedBadges.Clear();
        _settingsCategoryExpanders.Clear();
        SettingsGroupsPanel.Children.Clear();

        var search = SettingsSearchBox.Text.Trim();
        var hasSearch = !string.IsNullOrWhiteSpace(search);
        var sectionFiltered = !string.Equals(_activeSettingsSection, "All", StringComparison.OrdinalIgnoreCase);

        var choiceChanged = _settings.FirstOrDefault(i => i.Key == "NEWS_STUDIO_GUI_CHOICE_SOUND")?.IsChanged == true;
        var stageChanged = _settings.FirstOrDefault(i => i.Key == "NEWS_STUDIO_GUI_STAGE_SOUND")?.IsChanged == true;
        var appMatchesSearch = !hasSearch ||
                               "app experience alerts decision stage sound notification"
                                   .Contains(search, StringComparison.OrdinalIgnoreCase);
        var appMatchesSection = !sectionFiltered ||
                                string.Equals(_activeSettingsSection, "Workspace & AI", StringComparison.OrdinalIgnoreCase);
        var appMatchesModified = !_modifiedOnlySettings || choiceChanged || stageChanged;
        var appMatches = appMatchesSearch && appMatchesSection && appMatchesModified;
        AppExperienceCard.Visibility = appMatches ? Visibility.Visible : Visibility.Collapsed;

        var visible = _settings
            .Where(item => !item.IsSynthetic)
            .Where(item => !sectionFiltered ||
                           string.Equals(item.Section, _activeSettingsSection, StringComparison.OrdinalIgnoreCase))
            .Where(item => !_modifiedOnlySettings || item.IsChanged)
            .Where(item => !hasSearch ||
                           item.Key.Contains(search, StringComparison.OrdinalIgnoreCase) ||
                           item.Title.Contains(search, StringComparison.OrdinalIgnoreCase) ||
                           item.Category.Contains(search, StringComparison.OrdinalIgnoreCase) ||
                           item.Subgroup.Contains(search, StringComparison.OrdinalIgnoreCase) ||
                           item.Description.Contains(search, StringComparison.OrdinalIgnoreCase))
            .GroupBy(item => item.Category)
            .OrderBy(group => group.Min(item => item.CategoryOrder))
            .ThenBy(group => group.Key, StringComparer.OrdinalIgnoreCase)
            .ToList();

        string? lastRenderedSection = null;
        var showSectionHeaders = !sectionFiltered && visible.Count > 1;
        _suppressSettingsExpandTracking = true;
        try
        {
            foreach (var group in visible)
            {
                var settings = group
                    .OrderBy(item => item.SubgroupOrder)
                    .ThenBy(item => item.ItemOrder)
                    .ThenBy(item => item.Key, StringComparer.OrdinalIgnoreCase)
                    .ToList();
                var categoryInfo = EnvFileService.GetCategoryInfo(group.Key);

                if (showSectionHeaders &&
                    !string.Equals(lastRenderedSection, categoryInfo.Section, StringComparison.OrdinalIgnoreCase))
                {
                    lastRenderedSection = categoryInfo.Section;
                    var sectionLabel = new TextBlock
                    {
                        Text = categoryInfo.Section.ToUpperInvariant(),
                        FontSize = 11,
                        FontWeight = FontWeights.Bold,
                        Margin = new Thickness(4, SettingsGroupsPanel.Children.Count == 0 ? 2 : 12, 0, 8),
                    };
                    sectionLabel.SetResourceReference(TextBlock.ForegroundProperty, "TextTertiaryBrush");
                    SettingsGroupsPanel.Children.Add(sectionLabel);
                }

                var panel = new StackPanel();
                var subgroups = settings
                    .GroupBy(setting => setting.Subgroup)
                    .OrderBy(subgroup => subgroup.Min(item => item.SubgroupOrder))
                    .ThenBy(subgroup => subgroup.Key, StringComparer.OrdinalIgnoreCase)
                    .ToList();
                for (var idx = 0; idx < subgroups.Count; idx++)
                {
                    var subgroup = subgroups[idx];
                    panel.Children.Add(CreateSettingsSubcard(
                        subgroup.Key,
                        subgroup.ToList(),
                        isFirstSubgroup: idx == 0));
                }

                var header = BuildCategoryExpanderHeader(group.Key, categoryInfo, settings);
                var shouldExpand = hasSearch || _modifiedOnlySettings || _expandedSettingsCategories.Contains(group.Key);
                var expander = new Expander
                {
                    Tag = group.Key,
                    Header = header,
                    Content = panel,
                    Style = (Style)FindResource("SettingsExpanderStyle"),
                    Margin = new Thickness(0, 0, 0, 10),
                    IsExpanded = shouldExpand,
                };
                expander.Expanded += SettingsCategoryExpander_Expanded;
                expander.Collapsed += SettingsCategoryExpander_Collapsed;
                _settingsCategoryExpanders.Add(expander);
                SettingsGroupsPanel.Children.Add(expander);
            }
        }
        finally
        {
            _suppressSettingsExpandTracking = false;
        }

        if (visible.Count == 0 && !appMatches)
        {
            var emptyMessage = _modifiedOnlySettings
                ? "No unsaved modifications match the current filter."
                : hasSearch
                    ? $"No settings found for “{search}”."
                    : "No settings match the selected section.";
            var emptyText = new TextBlock
            {
                Text = emptyMessage,
                FontSize = 13,
            };
            emptyText.SetResourceReference(TextBlock.ForegroundProperty, "TextSecondaryBrush");
            var empty = new Border
            {
                Style = (Style)FindResource("CardStyle"),
                Padding = new Thickness(20, 18, 20, 18),
                Child = emptyText,
            };
            SettingsGroupsPanel.Children.Add(empty);
        }

        UpdateUnsavedSettingsIndicators();
    }

    private FrameworkElement BuildCategoryExpanderHeader(
        string categoryName,
        SettingsCategoryInfo info,
        IReadOnlyList<EnvSetting> settings)
    {
        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var iconBadge = new Border
        {
            Width = 36,
            Height = 36,
            CornerRadius = new CornerRadius(9),
            BorderThickness = new Thickness(1),
            VerticalAlignment = VerticalAlignment.Center,
        };
        iconBadge.SetResourceReference(Border.BackgroundProperty, "SurfaceAltBrush");
        iconBadge.SetResourceReference(Border.BorderBrushProperty, "StrokeBrush");
        var iconText = new TextBlock
        {
            Text = info.IconGlyph,
            FontFamily = new FontFamily("Segoe Fluent Icons"),
            FontSize = 15,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        };
        iconText.SetResourceReference(TextBlock.ForegroundProperty, "AccentBrush");
        iconBadge.Child = iconText;
        grid.Children.Add(iconBadge);

        var titleStack = new StackPanel
        {
            Margin = new Thickness(12, 0, 12, 0),
            VerticalAlignment = VerticalAlignment.Center,
        };
        titleStack.Children.Add(new TextBlock
        {
            Text = categoryName,
            FontSize = 15.5,
            FontWeight = FontWeights.SemiBold,
        });
        var subtitle = new TextBlock
        {
            Text = info.Subtitle,
            FontSize = 11.5,
            Margin = new Thickness(0, 2, 0, 0),
            TextWrapping = TextWrapping.Wrap,
        };
        subtitle.SetResourceReference(TextBlock.ForegroundProperty, "TextSecondaryBrush");
        titleStack.Children.Add(subtitle);
        Grid.SetColumn(titleStack, 1);
        grid.Children.Add(titleStack);

        var pills = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            VerticalAlignment = VerticalAlignment.Center,
        };

        var modifiedCount = settings.Count(item => item.IsChanged);
        var modifiedLabel = new TextBlock
        {
            Text = $"{modifiedCount} edited",
            FontSize = 10.5,
            FontWeight = FontWeights.Bold,
        };
        modifiedLabel.SetResourceReference(TextBlock.ForegroundProperty, "AccentTextBrush");
        var modifiedBadge = new Border
        {
            CornerRadius = new CornerRadius(10),
            Padding = new Thickness(9, 3, 9, 3),
            Margin = new Thickness(0, 0, 8, 0),
            Visibility = modifiedCount > 0 ? Visibility.Visible : Visibility.Collapsed,
            Child = modifiedLabel,
        };
        modifiedBadge.SetResourceReference(Border.BackgroundProperty, "AccentBrush");
        _categoryModifiedBadges[categoryName] = (modifiedBadge, modifiedLabel);
        pills.Children.Add(modifiedBadge);

        var countLabel = new TextBlock
        {
            Text = $"{settings.Count} setting{(settings.Count == 1 ? "" : "s")}",
            FontSize = 11,
            FontWeight = FontWeights.SemiBold,
        };
        countLabel.SetResourceReference(TextBlock.ForegroundProperty, "TextSecondaryBrush");
        var countPill = new Border
        {
            CornerRadius = new CornerRadius(10),
            Padding = new Thickness(10, 3, 10, 3),
            BorderThickness = new Thickness(1),
            Child = countLabel,
        };
        countPill.SetResourceReference(Border.BackgroundProperty, "SurfaceAltBrush");
        countPill.SetResourceReference(Border.BorderBrushProperty, "StrokeBrush");
        pills.Children.Add(countPill);

        Grid.SetColumn(pills, 2);
        grid.Children.Add(pills);
        return grid;
    }

    private FrameworkElement CreateSettingsSubcard(
        string title,
        IReadOnlyList<EnvSetting> settings,
        bool isFirstSubgroup)
    {
        var container = new StackPanel
        {
            Margin = new Thickness(0, isFirstSubgroup ? 8 : 14, 0, 0),
        };

        var header = new Grid { Margin = new Thickness(2, 0, 2, 6) };
        header.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        header.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var titleText = new TextBlock
        {
            Text = title.ToUpperInvariant(),
            FontSize = 11,
            FontWeight = FontWeights.Bold,
            VerticalAlignment = VerticalAlignment.Center,
        };
        titleText.SetResourceReference(TextBlock.ForegroundProperty, "TextTertiaryBrush");
        header.Children.Add(titleText);

        var count = new TextBlock
        {
            Text = $"{settings.Count} item{(settings.Count == 1 ? "" : "s")}",
            FontSize = 10.5,
            VerticalAlignment = VerticalAlignment.Center,
        };
        count.SetResourceReference(TextBlock.ForegroundProperty, "TextTertiaryBrush");
        Grid.SetColumn(count, 1);
        header.Children.Add(count);
        container.Children.Add(header);

        var rowsStack = new StackPanel();
        for (var index = 0; index < settings.Count; index++)
            rowsStack.Children.Add(CreateSettingRow(settings[index], isLast: index == settings.Count - 1));

        var card = new Border
        {
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(10),
            Padding = new Thickness(14, 2, 14, 2),
            Child = rowsStack,
        };
        card.SetResourceReference(Border.BackgroundProperty, "SurfaceAltBrush");
        card.SetResourceReference(Border.BorderBrushProperty, "StrokeBrush");
        container.Children.Add(card);
        return container;
    }

    private void SettingExpander_Expanded(object sender, RoutedEventArgs e)
    {
        if (sender is not Expander { Content: FrameworkElement content }) return;
        content.BeginAnimation(OpacityProperty, null);
        content.Opacity = 0;
        var translate = new TranslateTransform(0, -7);
        content.RenderTransform = translate;
        var ease = new CubicEase { EasingMode = EasingMode.EaseOut };
        content.BeginAnimation(OpacityProperty,
            new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(175)) { EasingFunction = ease });
        translate.BeginAnimation(TranslateTransform.YProperty,
            new DoubleAnimation(-7, 0, TimeSpan.FromMilliseconds(190)) { EasingFunction = ease });
    }

    private void SettingsCategoryExpander_Expanded(object sender, RoutedEventArgs e)
    {
        if (!_suppressSettingsExpandTracking && sender is Expander { Tag: string category })
            _expandedSettingsCategories.Add(category);
        SettingExpander_Expanded(sender, e);
    }

    private void SettingsCategoryExpander_Collapsed(object sender, RoutedEventArgs e)
    {
        if (!_suppressSettingsExpandTracking && sender is Expander { Tag: string category })
            _expandedSettingsCategories.Remove(category);
    }

    private FrameworkElement CreateSettingRow(EnvSetting setting, bool isLast)
    {
        var row = new Border
        {
            BorderThickness = isLast ? new Thickness(0) : new Thickness(0, 0, 0, 1),
            Padding = new Thickness(0, 12, 0, 12),
        };
        row.SetResourceReference(Border.BorderBrushProperty, "StrokeBrush");

        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(20) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var information = new StackPanel { VerticalAlignment = VerticalAlignment.Center };

        var titleLine = new WrapPanel { Orientation = Orientation.Horizontal };
        titleLine.Children.Add(new TextBlock
        {
            Text = string.IsNullOrWhiteSpace(setting.Title) ? setting.Key : setting.Title,
            FontSize = 13.5,
            FontWeight = FontWeights.SemiBold,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(0, 0, 8, 2),
        });

        var keyChipText = new TextBlock
        {
            Text = setting.Key,
            FontFamily = new FontFamily("Cascadia Mono, Consolas"),
            FontSize = 10.5,
        };
        keyChipText.SetResourceReference(TextBlock.ForegroundProperty, "TextTertiaryBrush");
        var keyChip = new Border
        {
            CornerRadius = new CornerRadius(5),
            Padding = new Thickness(6, 2, 6, 2),
            Margin = new Thickness(0, 0, 6, 2),
            BorderThickness = new Thickness(1),
            VerticalAlignment = VerticalAlignment.Center,
            Cursor = Cursors.Hand,
            ToolTip = $"Click to copy {setting.Key}",
            Child = keyChipText,
        };
        keyChip.SetResourceReference(Border.BackgroundProperty, "SurfaceBrush");
        keyChip.SetResourceReference(Border.BorderBrushProperty, "StrokeBrush");
        keyChip.MouseLeftButtonUp += (_, _) =>
        {
            try
            {
                Clipboard.SetText(setting.Key);
                SettingsStatusText.Text = $"Copied {setting.Key} to clipboard.";
                SettingsStatusText.SetResourceReference(TextBlock.ForegroundProperty, "TextSecondaryBrush");
            }
            catch (ExternalException)
            {
            }
        };
        titleLine.Children.Add(keyChip);

        var editedText = new TextBlock
        {
            Text = setting.IsNew ? "NEW" : "EDITED",
            FontSize = 9.5,
            FontWeight = FontWeights.Bold,
        };
        editedText.SetResourceReference(TextBlock.ForegroundProperty, "AccentTextBrush");
        var editedBadge = new Border
        {
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(7, 2, 7, 2),
            Margin = new Thickness(0, 0, 0, 2),
            VerticalAlignment = VerticalAlignment.Center,
            Visibility = setting.IsChanged ? Visibility.Visible : Visibility.Collapsed,
            Child = editedText,
        };
        editedBadge.SetResourceReference(Border.BackgroundProperty, "AccentBrush");
        _settingEditedBadges[setting.Key] = editedBadge;
        titleLine.Children.Add(editedBadge);

        information.Children.Add(titleLine);

        if (!string.IsNullOrWhiteSpace(setting.Description))
        {
            var description = new TextBlock
            {
                Text = setting.Description,
                FontSize = 12,
                TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 3, 0, 0),
            };
            description.SetResourceReference(TextBlock.ForegroundProperty, "TextSecondaryBrush");
            information.Children.Add(description);
        }

        if (!setting.IsSecret && !string.IsNullOrWhiteSpace(setting.DefaultValue))
        {
            var defaultHint = new TextBlock
            {
                Text = string.IsNullOrWhiteSpace(setting.Unit)
                    ? $"Default: {setting.DefaultValue}"
                    : $"Default: {setting.DefaultValue} {setting.Unit}",
                FontSize = 10.5,
                Margin = new Thickness(0, 3, 0, 0),
            };
            defaultHint.SetResourceReference(TextBlock.ForegroundProperty, "TextTertiaryBrush");
            information.Children.Add(defaultHint);
        }

        grid.Children.Add(information);

        var controlHost = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            VerticalAlignment = VerticalAlignment.Center,
            HorizontalAlignment = HorizontalAlignment.Right,
        };

        var resetButton = new Button
        {
            Content = "\uE777",
            Style = (Style)FindResource("SubtleIconButtonStyle"),
            Margin = new Thickness(0, 0, 8, 0),
            ToolTip = $"Reset to default ({setting.DefaultValue})",
            Visibility = CanResetToDefault(setting) ? Visibility.Visible : Visibility.Collapsed,
        };
        resetButton.Click += (_, _) => ResetSettingToDefault(setting);
        _settingResetButtons[setting.Key] = resetButton;
        controlHost.Children.Add(resetButton);

        FrameworkElement editor;
        if (setting.IsSecret)
        {
            var secretEditor = new SecretValueEditor(setting.Value) { Width = 320 };
            secretEditor.ValueChanged += (_, _) => OnSettingEditorChanged(setting, secretEditor.Value);
            editor = secretEditor;
        }
        else if (setting.IsBoolean)
        {
            var toggle = new ToggleButton
            {
                Style = (Style)FindResource("FluentToggleSwitchStyle"),
                IsChecked = setting.Value.Trim().Equals("true", StringComparison.OrdinalIgnoreCase),
                HorizontalAlignment = HorizontalAlignment.Right,
            };
            AutomationProperties.SetName(toggle, setting.Title);
            toggle.Click += (_, _) => OnSettingEditorChanged(setting, toggle.IsChecked == true ? "true" : "false");
            editor = toggle;
        }
        else if (setting.AllowedValues.Count > 0)
        {
            var combo = new ComboBox
            {
                Width = 220,
                VerticalContentAlignment = VerticalAlignment.Center,
                Style = (Style)FindResource("SettingsComboBoxStyle"),
            };
            foreach (var option in setting.AllowedValues)
                combo.Items.Add(option);

            var normalized = EnvFileService.NormalizeEnumeratedValue(
                setting.Key,
                setting.Value,
                setting.AllowedValues);
            var matched = setting.AllowedValues.FirstOrDefault(
                option => option.Equals(normalized.Trim(), StringComparison.OrdinalIgnoreCase));
            if (matched is not null)
            {
                if (string.Equals(setting.Value, setting.OriginalValue, StringComparison.Ordinal))
                    setting.OriginalValue = matched;
                setting.Value = matched;
                combo.SelectedItem = matched;
            }
            else if (!string.IsNullOrWhiteSpace(setting.Value))
            {
                var current = setting.Value.Trim();
                combo.Items.Add(current);
                combo.SelectedItem = current;
            }
            else
            {
                var defaultMatch = setting.AllowedValues.FirstOrDefault(
                    option => option.Equals(setting.DefaultValue.Trim(), StringComparison.OrdinalIgnoreCase))
                    ?? setting.AllowedValues[0];
                combo.SelectedItem = defaultMatch;
            }
            combo.SelectionChanged += (_, _) =>
            {
                if (combo.SelectedItem is not null)
                    OnSettingEditorChanged(setting, combo.SelectedItem.ToString() ?? setting.Value);
            };
            editor = combo;
        }
        else if (setting.IsNumeric)
        {
            var textBox = new TextBox
            {
                Text = setting.Value,
                Width = 140,
                TextAlignment = TextAlignment.Right,
                Style = (Style)FindResource("InputStyle"),
            };
            textBox.TextChanged += (_, _) => OnSettingEditorChanged(setting, textBox.Text);
            editor = textBox;
        }
        else
        {
            var textBox = new TextBox
            {
                Text = setting.Value,
                Width = 320,
                Style = (Style)FindResource("InputStyle"),
                TextWrapping = TextWrapping.Wrap,
            };
            textBox.TextChanged += (_, _) => OnSettingEditorChanged(setting, textBox.Text);
            editor = textBox;
        }

        editor.VerticalAlignment = VerticalAlignment.Center;
        controlHost.Children.Add(editor);

        if (setting.IsNumeric && !string.IsNullOrWhiteSpace(setting.Unit))
        {
            var unitText = new TextBlock
            {
                Text = setting.Unit,
                FontSize = 11.5,
                Width = 42,
                Margin = new Thickness(8, 0, 0, 0),
                VerticalAlignment = VerticalAlignment.Center,
            };
            unitText.SetResourceReference(TextBlock.ForegroundProperty, "TextTertiaryBrush");
            controlHost.Children.Add(unitText);
        }

        Grid.SetColumn(controlHost, 2);
        grid.Children.Add(controlHost);
        _settingEditors[setting.Key] = editor;
        row.Child = grid;
        return row;
    }

    private static bool CanResetToDefault(EnvSetting setting) =>
        !setting.IsSecret &&
        !string.IsNullOrWhiteSpace(setting.DefaultValue) &&
        !string.Equals(setting.Value.Trim(), setting.DefaultValue.Trim(), StringComparison.OrdinalIgnoreCase);

    private void ResetSettingToDefault(EnvSetting setting)
    {
        if (!_settingEditors.TryGetValue(setting.Key, out var editor)) return;
        var defaultValue = setting.DefaultValue;
        switch (editor)
        {
            case TextBox textBox:
                textBox.Text = defaultValue;
                break;
            case ToggleButton toggle:
                toggle.IsChecked = defaultValue.Trim().Equals("true", StringComparison.OrdinalIgnoreCase);
                OnSettingEditorChanged(setting, toggle.IsChecked == true ? "true" : "false");
                break;
            case ComboBox combo:
                var match = setting.AllowedValues.FirstOrDefault(
                    option => option.Equals(defaultValue.Trim(), StringComparison.OrdinalIgnoreCase));
                if (match is not null) combo.SelectedItem = match;
                break;
            case SecretValueEditor secret:
                secret.Value = defaultValue;
                OnSettingEditorChanged(setting, defaultValue);
                break;
        }
    }

    private void OnSettingEditorChanged(EnvSetting setting, string newValue)
    {
        setting.Value = newValue;
        if (_settingEditedBadges.TryGetValue(setting.Key, out var badge))
            badge.Visibility = setting.IsChanged ? Visibility.Visible : Visibility.Collapsed;
        if (_settingResetButtons.TryGetValue(setting.Key, out var resetButton))
            resetButton.Visibility = CanResetToDefault(setting) ? Visibility.Visible : Visibility.Collapsed;
        UpdateUnsavedSettingsIndicators(setting.Category);
    }

    private void UpdateUnsavedSettingsIndicators(string? updatedCategory = null)
    {
        if (updatedCategory is not null &&
            _categoryModifiedBadges.TryGetValue(updatedCategory, out var categoryBadge))
        {
            var catCount = _settings.Count(item =>
                !item.IsSynthetic &&
                string.Equals(item.Category, updatedCategory, StringComparison.OrdinalIgnoreCase) &&
                item.IsChanged);
            categoryBadge.Badge.Visibility = catCount > 0 ? Visibility.Visible : Visibility.Collapsed;
            categoryBadge.Label.Text = $"{catCount} edited";
        }

        if (UnsavedChangesBadge is null || UnsavedChangesBadgeText is null) return;
        var totalChanged = _settings.Count(item =>
            !string.Equals(item.Key, "NEWS_STUDIO_GUI_THEME", StringComparison.OrdinalIgnoreCase) &&
            !item.Key.StartsWith("NEWS_STUDIO_GUI_USE_DEFAULT_", StringComparison.OrdinalIgnoreCase) &&
            !string.Equals(item.Key, "NEWS_STUDIO_BE_SKEPTICAL", StringComparison.OrdinalIgnoreCase) &&
            item.IsChanged);
        UnsavedChangesBadge.Visibility = totalChanged > 0 ? Visibility.Visible : Visibility.Collapsed;
        UnsavedChangesBadgeText.Text = $"{totalChanged} unsaved";
    }

    private void CaptureEditorValues()
    {
        foreach (var setting in _settings)
        {
            if (!_settingEditors.TryGetValue(setting.Key, out var editor)) continue;
            setting.Value = editor switch
            {
                TextBox textBox => textBox.Text,
                ComboBox comboBox => comboBox.SelectedItem?.ToString() ?? setting.Value,
                ToggleButton toggle => toggle.IsChecked == true ? "true" : "false",
                SecretValueEditor secret => secret.Value,
                _ => setting.Value,
            };
        }
    }

    private async void SaveSettings_Click(object sender, RoutedEventArgs e)
    {
        if (_envService is null) return;
        CaptureEditorValues();
        _settings.First(item => item.Key == "NEWS_STUDIO_GUI_CHOICE_SOUND").Value =
            ChoiceSoundCheckBox.IsChecked == true ? "true" : "false";
        _settings.First(item => item.Key == "NEWS_STUDIO_GUI_STAGE_SOUND").Value =
            StageSoundCheckBox.IsChecked == true ? "true" : "false";
        _settings.First(item => item.Key == "NEWS_STUDIO_GUI_THEME").Value = GetSelectedTheme();
        var errors = _envService.Validate(_settings);
        if (errors.Count > 0)
        {
            SettingsStatusText.Text = string.Join("  ", errors.Take(3));
            SettingsStatusText.Foreground = ResourceBrush("DangerBrush");
            SystemSounds.Hand.Play();
            return;
        }
        try
        {
            var result = _envService.Save(_settings);
            if (result.Saved)
            {
                foreach (var setting in _settings) setting.OriginalValue = setting.Value;
                foreach (var setting in _settings) setting.IsNew = false;
                BuildSettingsList();
                SettingsStatusText.Text = $"Saved {result.ChangedCount} change(s). Backup: {Path.GetFileName(result.BackupPath)}";
                SettingsStatusText.Foreground = ResourceBrush("SuccessBrush");
                SystemSounds.Asterisk.Play();
            }
            else
            {
                SettingsStatusText.Text = "No settings changed.";
                SettingsStatusText.Foreground = ResourceBrush("TextSecondaryBrush");
            }
            ApplyTheme(GetSelectedTheme());
            UpdatePipelineSnapshot();
            if (result.Saved) await RefreshProviderQuotasAsync(true);
        }
        catch (Exception exc)
        {
            SettingsStatusText.Text = $"Could not save: {exc.Message}";
            SettingsStatusText.Foreground = ResourceBrush("DangerBrush");
            SystemSounds.Hand.Play();
        }
    }

    private void ReloadSettings_Click(object sender, RoutedEventArgs e) => LoadSettings();

    private void SettingsSectionFilter_Checked(object sender, RoutedEventArgs e)
    {
        if (sender is not RadioButton { Tag: string section }) return;
        _activeSettingsSection = section;
        if (!_windowLoaded || _envService is null) return;
        BuildSettingsList();
    }

    private void ModifiedOnlyToggle_Click(object sender, RoutedEventArgs e)
    {
        _modifiedOnlySettings = ModifiedOnlyToggle.IsChecked == true;
        if (!_windowLoaded || _envService is null) return;
        BuildSettingsList();
    }

    private void ExpandAllSettings_Click(object sender, RoutedEventArgs e)
    {
        _suppressSettingsExpandTracking = true;
        try
        {
            foreach (var expander in _settingsCategoryExpanders)
            {
                expander.IsExpanded = true;
                if (expander.Tag is string category) _expandedSettingsCategories.Add(category);
            }
        }
        finally
        {
            _suppressSettingsExpandTracking = false;
        }
    }

    private void CollapseAllSettings_Click(object sender, RoutedEventArgs e)
    {
        _suppressSettingsExpandTracking = true;
        try
        {
            foreach (var expander in _settingsCategoryExpanders)
            {
                expander.IsExpanded = false;
                if (expander.Tag is string category) _expandedSettingsCategories.Remove(category);
            }
        }
        finally
        {
            _suppressSettingsExpandTracking = false;
        }
    }

    private void AddVariableButton_Click(object sender, RoutedEventArgs e)
    {
        CaptureEditorValues();
        NewVariableKeyTextBox.Text = "";
        NewVariableValueTextBox.Text = "";
        NewVariableSecretCheckBox.IsChecked = false;
        AddVariableErrorText.Text = "";
        AnimateOverlayIn(AddVariableOverlay, AddVariableCard);
        NewVariableKeyTextBox.Focus();
    }

    private void CancelAddVariable_Click(object sender, RoutedEventArgs e) => HideAddVariableDialog();

    private void ConfirmAddVariable_Click(object sender, RoutedEventArgs e) => ConfirmAddVariable();

    private void ConfirmAddVariable()
    {
        var key = NewVariableKeyTextBox.Text.Trim().ToUpperInvariant();
        if (!Regex.IsMatch(key, @"^[A-Z_][A-Z0-9_]*$"))
        {
            AddVariableErrorText.Text = "Use letters, numbers, and underscores; the first character must be a letter or underscore.";
            SystemSounds.Hand.Play();
            return;
        }
        if (_settings.Any(item => item.Key.Equals(key, StringComparison.OrdinalIgnoreCase)))
        {
            AddVariableErrorText.Text = $"{key} already exists. Edit its current row instead.";
            SystemSounds.Hand.Play();
            return;
        }
        var value = NewVariableValueTextBox.Text;
        if (value.Contains('\n') || value.Contains('\r'))
        {
            AddVariableErrorText.Text = "Environment variable values cannot contain line breaks.";
            SystemSounds.Hand.Play();
            return;
        }
        var newSetting = EnvFileService.CreateSetting(
            key,
            "",
            value,
            "User-defined .env variable.",
            isNew: true,
            forceSecret: NewVariableSecretCheckBox.IsChecked == true ? true : null);
        _settings.Add(newSetting);
        _expandedSettingsCategories.Add(newSetting.Category);
        _activeSettingsSection = "All";
        FilterAllPill.IsChecked = true;
        _modifiedOnlySettings = false;
        ModifiedOnlyToggle.IsChecked = false;
        SettingsSearchBox.Text = "";
        BuildSettingsList();
        SettingsStatusText.Text = $"{key} added to the editor. Press Save changes to write it to .env.";
        SettingsStatusText.Foreground = ResourceBrush("AccentBrush");
        HideAddVariableDialog();
    }

    private void HideAddVariableDialog()
    {
        AnimateOverlayOut(AddVariableOverlay, AddVariableCard);
        SettingsSearchBox.Focus();
    }

    private void SettingsSearchBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        var hasText = !string.IsNullOrEmpty(SettingsSearchBox.Text);
        if (SettingsSearchPlaceholder is not null)
            SettingsSearchPlaceholder.Visibility = hasText ? Visibility.Collapsed : Visibility.Visible;
        if (ClearSettingsSearchButton is not null)
            ClearSettingsSearchButton.Visibility = hasText ? Visibility.Visible : Visibility.Collapsed;
        if (!_windowLoaded || _envService is null) return;
        BuildSettingsList();
    }

    private void SettingsSearchBox_GotFocus(object sender, RoutedEventArgs e)
    {
        SettingsSearchFocusLine.BeginAnimation(OpacityProperty,
            new DoubleAnimation(SettingsSearchFocusLine.Opacity, 1, TimeSpan.FromMilliseconds(120)));
    }

    private void SettingsSearchBox_LostFocus(object sender, RoutedEventArgs e)
    {
        SettingsSearchFocusLine.BeginAnimation(OpacityProperty,
            new DoubleAnimation(SettingsSearchFocusLine.Opacity, 0, TimeSpan.FromMilliseconds(120)));
    }

    private void ClearSettingsSearch_Click(object sender, RoutedEventArgs e)
    {
        SettingsSearchBox.Clear();
        SettingsSearchBox.Focus();
    }

    private void ChoiceSoundCheckBox_Click(object sender, RoutedEventArgs e)
    {
        var setting = _settings.FirstOrDefault(item => item.Key == "NEWS_STUDIO_GUI_CHOICE_SOUND");
        if (setting is not null)
            setting.Value = ChoiceSoundCheckBox.IsChecked == true ? "true" : "false";
        UpdateUnsavedSettingsIndicators();
        if (ChoiceSoundCheckBox.IsChecked == true) SystemSounds.Exclamation.Play();
    }

    private void StageSoundCheckBox_Click(object sender, RoutedEventArgs e)
    {
        var setting = _settings.FirstOrDefault(item => item.Key == "NEWS_STUDIO_GUI_STAGE_SOUND");
        if (setting is not null)
            setting.Value = StageSoundCheckBox.IsChecked == true ? "true" : "false";
        UpdateUnsavedSettingsIndicators();
        if (StageSoundCheckBox.IsChecked == true) SystemSounds.Asterisk.Play();
    }

    private void SkepticalModeToggleButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_windowLoaded || _envService is null) return;
        var setting = _settings.FirstOrDefault(item => item.Key == "NEWS_STUDIO_BE_SKEPTICAL");
        if (setting is null) return;
        var enabled = SkepticalModeToggleButton.IsChecked == true;
        setting.Value = enabled ? "true" : "false";
        try
        {
            var result = _envService.Save([setting]);
            if (result.Saved) setting.OriginalValue = setting.Value;
            AppendLog($"Be Skeptical {(enabled ? "enabled" : "disabled")} for upcoming news summaries.");
        }
        catch (Exception exc)
        {
            setting.Value = setting.OriginalValue;
            SkepticalModeToggleButton.IsChecked = !setting.OriginalValue.Equals("false", StringComparison.OrdinalIgnoreCase);
            AppendLog($"Could not save Be Skeptical: {exc.Message}");
        }
    }

    private void SettingsSearchBox_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Escape || string.IsNullOrEmpty(SettingsSearchBox.Text)) return;
        SettingsSearchBox.Clear();
        e.Handled = true;
    }

    private void ThemeModeToggleButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_windowLoaded) return;
        ApplyThemeChange(ThemeModeToggleButton.IsChecked == true ? "dark" : "light");
    }

    private void CompactThemeButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_windowLoaded) return;
        ApplyThemeChange(ThemeService.IsDark(_activeTheme) ? "light" : "dark");
    }

    private void ApplyThemeChange(string theme)
    {
        SelectTheme(theme);
        ApplyTheme(theme);
        PersistThemeSelection(theme);
    }

    private void PersistThemeSelection(string theme)
    {
        if (_envService is null) return;
        var setting = _settings.FirstOrDefault(item => item.Key == "NEWS_STUDIO_GUI_THEME");
        if (setting is null) return;
        setting.Value = ThemeService.Normalize(theme);
        try
        {
            var result = _envService.Save([setting]);
            if (result.Saved) setting.OriginalValue = setting.Value;
        }
        catch (Exception exc)
        {
            SettingsStatusText.Text = $"Theme changed for this session, but could not be saved: {exc.Message}";
            SettingsStatusText.Foreground = ResourceBrush("DangerBrush");
        }
    }

    private string GetSelectedTheme() => _activeTheme;

    private void SelectTheme(string theme)
    {
        _activeTheme = ThemeService.Normalize(theme);
        SyncThemeModeToggle(_activeTheme);
    }

    private void SyncThemeModeToggle(string theme)
    {
        var dark = ThemeService.IsDark(theme);
        ThemeModeToggleButton.IsChecked = dark;
        CompactThemeGlyph.Text = dark ? "\uE708" : "\uE706";
        CompactThemeButton.ToolTip = dark ? "Switch to Light theme" : "Switch to Dark theme";
    }

    private void ApplyTheme(string theme)
    {
        var normalized = ThemeService.Normalize(theme);
        _activeTheme = normalized;
        if (Application.Current is App app)
            app.SetActiveTheme(normalized);
        else
            ThemeService.ApplyResources(Application.Current.Resources, normalized);
        var dark = ThemeService.IsDark(normalized);
        ApplyBackdrop(dark);
        foreach (var stage in _stages)
            SetStageState(stage.Key, stage.StateKey);
        RefreshActivityLogThemeBrushes();
        RefreshQuotaThemeBrushes();
        RefreshChannelProfiles();
        SyncWebViewColorScheme(normalized);
        if (_selectedHistoryItem is not null)
            LoadHistoryProductionDetail(_selectedHistoryItem);
    }

    private void SidebarToggleButton_Click(object sender, RoutedEventArgs e)
    {
        _sidebarCollapsed = !_sidebarCollapsed;
        ApplySidebarVisualState(animate: true);
    }

    private void ApplySidebarVisualState(bool animate)
    {
        const double expandedWidth = 244;
        const double collapsedWidth = 72;
        var currentWidth = SidebarBorder.ActualWidth > 0 ? SidebarBorder.ActualWidth : SidebarBorder.Width;
        var targetWidth = _sidebarCollapsed ? collapsedWidth : expandedWidth;

        SidebarBorder.Width = targetWidth;
        if (animate)
        {
            var ease = new CubicEase { EasingMode = EasingMode.EaseOut };
            SidebarBorder.BeginAnimation(WidthProperty,
                new DoubleAnimation(currentWidth, targetWidth, TimeSpan.FromMilliseconds(180))
                {
                    EasingFunction = ease,
                    FillBehavior = FillBehavior.Stop,
                });
        }
        else
        {
            SidebarBorder.BeginAnimation(WidthProperty, null);
        }

        var expandedVisibility = _sidebarCollapsed ? Visibility.Collapsed : Visibility.Visible;
        SidebarHeaderTitle.Visibility = expandedVisibility;
        TechBranchHeaderButton.Visibility = expandedVisibility;
        ProductionNavLabel.Visibility = expandedVisibility;
        HistoryNavLabel.Visibility = expandedVisibility;
        QuotaNavLabel.Visibility = expandedVisibility;
        FutureChannelHintTextPanel.Visibility = expandedVisibility;
        ThemeModeToggleButton.Visibility = expandedVisibility;
        CompactThemeButton.Visibility = _sidebarCollapsed ? Visibility.Visible : Visibility.Collapsed;
        ProfileTextPanel.Visibility = expandedVisibility;
        ProfileChevronIcon.Visibility = expandedVisibility;

        if (_sidebarCollapsed)
        {
            SidebarToggleButton.HorizontalAlignment = HorizontalAlignment.Center;
            SidebarToggleButton.ToolTip = "Expand navigation";

            TechBranchContainer.Visibility = Visibility.Visible;
            TechBranchContainer.Margin = new Thickness(0, 0, 0, 8);
            TechBranchContainer.Padding = new Thickness(0);
            TechBranchContainer.BorderThickness = new Thickness(0);

            FutureChannelHintButton.HorizontalAlignment = HorizontalAlignment.Center;
            FutureChannelHintButton.Width = 40;
            FutureChannelHintButton.Padding = new Thickness(8, 7, 8, 7);

            ToolsSpacerColumn.Width = new GridLength(0);
            ToolsGapColumn.Width = new GridLength(0);
            Grid.SetRow(OpenAutomationFolderButton, 1);
            Grid.SetColumn(OpenAutomationFolderButton, 0);
            Grid.SetRow(SettingsNav, 2);
            Grid.SetColumn(SettingsNav, 0);
            CompactThemeButton.Margin = new Thickness(0, 0, 0, 4);
            OpenAutomationFolderButton.Margin = new Thickness(0, 0, 0, 4);
            SidebarToolsBorder.HorizontalAlignment = HorizontalAlignment.Center;
            SidebarToolsBorder.Padding = new Thickness(4, 6, 4, 6);

            ProfileSelectorButton.HorizontalAlignment = HorizontalAlignment.Center;
            ProfileSelectorButton.Padding = new Thickness(6);
        }
        else
        {
            SidebarToggleButton.HorizontalAlignment = HorizontalAlignment.Left;
            SidebarToggleButton.ToolTip = "Collapse navigation";

            TechBranchContainer.Visibility = _techBranchExpanded ? Visibility.Visible : Visibility.Collapsed;
            TechBranchContainer.Margin = new Thickness(13, 0, 0, 8);
            TechBranchContainer.Padding = new Thickness(8, 0, 0, 0);
            TechBranchContainer.BorderThickness = new Thickness(1.5, 0, 0, 0);

            FutureChannelHintButton.HorizontalAlignment = HorizontalAlignment.Stretch;
            FutureChannelHintButton.Width = double.NaN;
            FutureChannelHintButton.Padding = new Thickness(9, 7, 9, 7);

            ToolsSpacerColumn.Width = new GridLength(1, GridUnitType.Star);
            ToolsGapColumn.Width = new GridLength(4);
            Grid.SetRow(OpenAutomationFolderButton, 0);
            Grid.SetColumn(OpenAutomationFolderButton, 2);
            Grid.SetRow(SettingsNav, 0);
            Grid.SetColumn(SettingsNav, 4);
            CompactThemeButton.Margin = new Thickness(0);
            OpenAutomationFolderButton.Margin = new Thickness(0);
            SidebarToolsBorder.HorizontalAlignment = HorizontalAlignment.Stretch;
            SidebarToolsBorder.Padding = new Thickness(7, 6, 7, 6);

            ProfileSelectorButton.HorizontalAlignment = HorizontalAlignment.Stretch;
            ProfileSelectorButton.Padding = new Thickness(8, 7, 8, 7);
        }
    }

    private void TechBranchHeaderButton_Click(object sender, RoutedEventArgs e)
    {
        if (_sidebarCollapsed)
        {
            _sidebarCollapsed = false;
            _techBranchExpanded = true;
            ApplySidebarVisualState(animate: true);
            TechBranchChevron.Text = "\uE70D";
            return;
        }

        _techBranchExpanded = !_techBranchExpanded;
        TechBranchContainer.Visibility = _techBranchExpanded ? Visibility.Visible : Visibility.Collapsed;
        TechBranchChevron.Text = _techBranchExpanded ? "\uE70D" : "\uE76C";
    }

    private void ProfileSelectorButton_Click(object sender, RoutedEventArgs e)
    {
        ChannelPopupStatusText.Text = "Future channel profiles can be selected here cleanly.";
        ChannelProfilePopup.IsOpen = !ChannelProfilePopup.IsOpen;
    }

    private void ChannelProfileOption_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement { DataContext: ChannelProfileOptionViewModel option }) return;
        if (option.IsActive)
        {
            ChannelProfilePopup.IsOpen = false;
            return;
        }

        ChannelPopupStatusText.Text = $"{option.ChannelName} ({option.CategoryLabel}) will be switchable in a future update.";
    }

    private void RefreshChannelProfiles()
    {
        var avatar = LoadChannelAvatarBitmap();
        if (ProfileAvatarImage is not null && avatar is not null)
            ProfileAvatarImage.Source = avatar;

        _channelProfiles.Clear();
        _channelProfiles.Add(new ChannelProfileOptionViewModel
        {
            Key = "tech_news",
            ChannelName = "Tech Scale",
            CategoryLabel = "Tech News",
            Subtitle = "Tech News · feeds.opml",
            AvatarImage = avatar,
            FallbackGlyph = "\uE99A",
            IsActive = true,
            IsAvailable = true,
            BadgeText = "Active",
            BadgeBackground = ResourceBrush("SuccessTintBrush"),
            BadgeForeground = ResourceBrush("SuccessBrush"),
        });

        var hasGamingProfile = !string.IsNullOrWhiteSpace(_automationRoot) &&
                               File.Exists(Path.Combine(_automationRoot, "config", "gaming_channel_profile.txt"));
        _channelProfiles.Add(new ChannelProfileOptionViewModel
        {
            Key = "gaming_news",
            ChannelName = "Gaming News",
            CategoryLabel = "Gaming & Esports",
            Subtitle = hasGamingProfile
                ? "gaming_channel_profile.txt configured"
                : "Future gaming channel workspace",
            AvatarImage = null,
            FallbackGlyph = "\uE7FC",
            IsActive = false,
            IsAvailable = false,
            BadgeText = "Soon",
            BadgeBackground = ResourceBrush("AccentTintBrush"),
            BadgeForeground = ResourceBrush("AccentBrush"),
        });
    }

    private BitmapImage? LoadChannelAvatarBitmap()
    {
        if (!string.IsNullOrWhiteSpace(_automationRoot))
        {
            var customCandidates = new[]
            {
                Path.Combine(_automationRoot, "config", "channel_avatar.png"),
                Path.Combine(_automationRoot, "config", "channel_avatar.jpg"),
                Path.Combine(_automationRoot, "config", "channel_avatar.jpeg"),
            };
            var custom = customCandidates.FirstOrDefault(File.Exists);
            if (custom is not null && LoadBitmapPreview(custom) is { } customBitmap)
                return customBitmap;
        }

        try
        {
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.UriSource = new Uri("pack://application:,,,/Assets/TechLogoIcon.png", UriKind.Absolute);
            bitmap.EndInit();
            bitmap.Freeze();
            return bitmap;
        }
        catch
        {
            return null;
        }
    }

    private void ApplyBackdrop(bool dark)
    {
        var handle = new WindowInteropHelper(this).Handle;
        if (handle == IntPtr.Zero) return;
        var darkValue = dark ? 1 : 0;
        _ = DwmSetWindowAttribute(handle, 20, ref darkValue, sizeof(int));
        var backdrop = 2;
        _ = DwmSetWindowAttribute(handle, 38, ref backdrop, sizeof(int));
        var corners = 2;
        _ = DwmSetWindowAttribute(handle, 33, ref corners, sizeof(int));
    }

    private void UpdatePreviewButton()
    {
        if (string.IsNullOrWhiteSpace(_currentProject))
        {
            OpenPreviewButton.Visibility = Visibility.Collapsed;
            return;
        }
        var path = Path.Combine(GetProjectsRoot(),
            _currentProject, "07_effects", "milestone6_reference_60s.mp4");
        OpenPreviewButton.Visibility = File.Exists(path) ? Visibility.Visible : Visibility.Collapsed;
    }

    private static bool IsProjectUploadComplete(string projectPath)
    {
        var resultPath = Path.Combine(
            projectPath,
            "09_publish",
            "youtube_upload_result.json"
        );
        if (!File.Exists(resultPath)) return false;
        try
        {
            using var document = JsonDocument.Parse(File.ReadAllText(resultPath));
            return string.Equals(
                StringProperty(document.RootElement, "status"),
                "UPLOAD_COMPLETE",
                StringComparison.OrdinalIgnoreCase
            ) && !string.IsNullOrWhiteSpace(
                StringProperty(document.RootElement, "video_id")
            );
        }
        catch (IOException)
        {
            return false;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    private void OpenPreviewButton_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_currentProject)) return;
        OpenPath(Path.Combine(GetProjectsRoot(),
            _currentProject, "07_effects", "milestone6_reference_60s.mp4"));
    }

    private void DecisionAssetButton_Click(object sender, RoutedEventArgs e) => OpenPath(_decisionAssetPath);

    private static void OpenPath(string? path)
    {
        if (string.IsNullOrWhiteSpace(path)) return;
        var isWebAddress = Uri.TryCreate(path, UriKind.Absolute, out var uri) &&
                           (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps);
        if (!isWebAddress && !File.Exists(path) && !Directory.Exists(path)) return;
        Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
    }

    private async void StopButton_Click(object sender, RoutedEventArgs e)
    {
        if (_bridge is null || (!_bridge.IsRunning && !_queueRunning)) return;
        var queueWasRunning = _queueRunning;
        var answer = MessageBox.Show(this,
            _queueRunning
                ? "Stop the active generation and cancel every remaining queued generation? Completed outputs remain cached."
                : "Stop the current process? Completed outputs remain cached and the next run will recover the stale project lock.",
            "Stop production", MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No);
        if (answer != MessageBoxResult.Yes) return;
        if (queueWasRunning)
        {
            _queueCancellationRequested = true;
            CancelQueueButton.Visibility = Visibility.Collapsed;
        }
        AppendLog("STOP   Stop requested by user");
        StopButton.IsEnabled = false;
        SetHeaderState("Stopping safely", "warning");
        CurrentActionTitle.Text = "Finishing the active tool safely";
        CurrentActionDetail.Text = "The app is allowing the current child process to close before production stops.";
        var graceful = !_bridge.IsRunning || await _bridge.StopSafelyAsync();
        AppendLog(graceful
            ? "STOP   Production stopped safely"
            : "STOP   Graceful timeout reached; remaining processes were closed");
        HideDecision();
        if (queueWasRunning)
        {
            for (var attempt = 0; attempt < 50 && _queueRunning && !_bridge.IsRunning; attempt++)
                await Task.Delay(100);
            if (_queueRunning)
            {
                _queueRunning = false;
                RestoreControlsAfterQueue();
                AppendLog("QUEUE  Recovered controls after safe stop");
            }
        }
        else
        {
            StopButton.IsEnabled = true;
            StopButton.Visibility = Visibility.Collapsed;
            StartButton.IsEnabled = true;
            QuickRunButton.IsEnabled = true;
            SettingsNav.IsEnabled = true;
        }
        await RefreshStatusAsync();
    }

    private async void Window_Closing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        if (_bridge is null || (!_bridge.IsRunning && !_queueRunning)) return;
        if (_closingAfterSafeStop) return;
        var answer = MessageBox.Show(this,
            _queueRunning
                ? "A generation queue is active. Close it now and cancel the active and remaining generations? Completed output is preserved."
                : "Production is still running. Close it now? Completed output is preserved.",
            "Close News Studio", MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No);
        if (answer != MessageBoxResult.Yes)
        {
            e.Cancel = true;
            return;
        }
        e.Cancel = true;
        _closingAfterSafeStop = true;
        _queueCancellationRequested = true;
        if (_bridge.IsRunning) await _bridge.StopSafelyAsync();
        Close();
    }

    protected override void OnClosed(EventArgs e)
    {
        _productionTimer.Stop();
        StopAndCloseVoiceoverPlayer();
        if (_quotaWebViewReady)
        {
            try { QuotaWebView.Dispose(); } catch { }
        }
        if (_windowSource is not null) _windowSource.RemoveHook(WindowMessageHook);
        base.OnClosed(e);
    }

    private void HistoryItemMenuButton_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { ContextMenu: { } menu } button) return;
        menu.PlacementTarget = button;
        menu.Placement = PlacementMode.Bottom;
        menu.DataContext = button.DataContext;
        menu.IsOpen = true;
        e.Handled = true;
    }

    private static bool TryGetHistoryItem(object sender, out ProductionTimerLogViewModel item)
    {
        if (sender is FrameworkElement { DataContext: ProductionTimerLogViewModel direct })
        {
            item = direct;
            return item.VideoNumber > 0;
        }
        if (sender is MenuItem menuItem)
        {
            ItemsControl? current = menuItem;
            while (current is not null)
            {
                if (current is ContextMenu { DataContext: ProductionTimerLogViewModel fromMenu })
                {
                    item = fromMenu;
                    return item.VideoNumber > 0;
                }
                if (current is ContextMenu { PlacementTarget: FrameworkElement { DataContext: ProductionTimerLogViewModel fromTarget } })
                {
                    item = fromTarget;
                    return item.VideoNumber > 0;
                }
                current = ItemsControl.ItemsControlFromItemContainer(current);
            }
        }
        item = null!;
        return false;
    }

    private async void UploadToYouTubeAgainMenuItem_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetHistoryItem(sender, out var item)) return;
        if (_bridge is null || _bridge.IsRunning || _queueRunning || _projectActionRunning || _voiceoverBusy)
        {
            MessageBox.Show(this,
                "Another production or project action is currently running. Wait for it to finish first.",
                "Action in progress", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }

        _projectActionRunning = true;
        StartButton.IsEnabled = false;
        QuickRunButton.IsEnabled = false;
        SetHeaderState("Uploading", "accent");
        CurrentActionTitle.Text = $"Uploading {item.VideoName} to YouTube";
        CurrentActionDetail.Text = $"Uploading the latest rendered version of {item.VideoName} from {item.ProjectPath}.";
        AppendLog($"UPLOAD Starting YouTube re-upload for {item.VideoName}");

        try
        {
            using var document = await _bridge.UploadProjectAgainAsync(
                item.VideoNumber,
                progress => Dispatcher.BeginInvoke(() =>
                {
                    CurrentActionDetail.Text = progress;
                }));
            var root = document.RootElement;
            var watchUrl = StringProperty(root, "watch_url") ?? "";
            var scheduledAt = StringProperty(root, "scheduled_publish_at") ?? "";
            AppendLog($"DONE   {item.VideoName} uploaded to YouTube: {watchUrl}" +
                      (!string.IsNullOrWhiteSpace(scheduledAt) ? $" (scheduled: {scheduledAt})" : ""));
            SetHeaderState("Uploaded", "success");
            CurrentActionTitle.Text = $"{item.VideoName} uploaded to YouTube";
            CurrentActionDetail.Text = !string.IsNullOrWhiteSpace(watchUrl)
                ? $"Latest version uploaded ({watchUrl}). Scheduled publish: {scheduledAt}"
                : $"Latest version of {item.VideoName} uploaded to YouTube.";
            if (ChoiceSoundCheckBox.IsChecked == true)
                SystemSounds.Asterisk.Play();
        }
        catch (Exception exc)
        {
            AppendLog($"ERROR  YouTube re-upload failed for {item.VideoName}: {exc.Message}");
            SetHeaderState("Upload failed", "danger");
            CurrentActionTitle.Text = $"YouTube upload failed for {item.VideoName}";
            CurrentActionDetail.Text = exc.Message;
            MessageBox.Show(this,
                $"Could not upload {item.VideoName} to YouTube:\n\n{exc.Message}",
                "YouTube upload failed", MessageBoxButton.OK, MessageBoxImage.Error);
        }
        finally
        {
            _projectActionRunning = false;
            StartButton.IsEnabled = !_queueRunning;
            QuickRunButton.IsEnabled = !_queueRunning;
            await Task.WhenAll(RefreshStatusAsync(), RefreshProviderQuotasAsync(true));
        }
    }

    private async void ResetVoiceoverMenuItem_Click(object sender, RoutedEventArgs e)
    {
        if (!TryGetHistoryItem(sender, out var item)) return;
        if (_bridge is null || _bridge.IsRunning || _queueRunning || _projectActionRunning || _voiceoverBusy)
        {
            MessageBox.Show(this,
                "Another production or project action is currently running. Wait for it to finish first.",
                "Action in progress", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }

        _voiceoverResetVideoNumber = item.VideoNumber;
        VoiceoverResetTitleText.Text = $"{item.VideoName} — Reset voiceover";
        VoiceoverFileNameText.Text = $"{item.VideoName}_voiceover.mp3";
        VoiceoverFileDetailText.Text = "Preparing voiceover synthesis…";
        VoiceoverTimeText.Text = "00:00 / 00:00";
        VoiceoverPlaybackProgress.Value = 0;
        VoiceoverErrorText.Visibility = Visibility.Collapsed;
        VoiceoverErrorText.Text = "";
        AnimateOverlayIn(VoiceoverResetOverlay, VoiceoverResetCard);

        await RunVoiceoverGenerationAsync(item.VideoNumber);
    }

    private async Task RunVoiceoverGenerationAsync(int videoNumber)
    {
        if (_bridge is null || _voiceoverBusy) return;
        _voiceoverBusy = true;
        _voiceoverSaving = false;
        StopAndCloseVoiceoverPlayer();

        VoiceoverBusyBanner.Visibility = Visibility.Visible;
        VoiceoverBusyProgress.IsIndeterminate = true;
        VoiceoverStatusText.Text = $"Regenerating all story voiceovers for Video {videoNumber}…";
        VoiceoverErrorText.Visibility = Visibility.Collapsed;
        VoiceoverPlayButton.IsEnabled = false;
        VoiceoverRegenerateButton.IsEnabled = false;
        VoiceoverSaveButton.IsEnabled = false;
        VoiceoverCancelButton.IsEnabled = true;

        _voiceoverCts?.Cancel();
        _voiceoverCts?.Dispose();
        _voiceoverCts = new CancellationTokenSource();
        var token = _voiceoverCts.Token;

        SetHeaderState("Generating voiceover", "accent");
        AppendLog($"VOICE  Regenerating voiceovers for Video {videoNumber}");

        try
        {
            using var document = await _bridge.GenerateVoiceoverPreviewAsync(
                videoNumber,
                progress => Dispatcher.BeginInvoke(() =>
                {
                    VoiceoverStatusText.Text = progress;
                }),
                token);

            var root = document.RootElement;
            var previewFile = StringProperty(root, "preview_file") ?? "";
            var fileName = StringProperty(root, "file_name") ?? $"Video {videoNumber}_voiceover.mp3";
            var storyCount = IntProperty(root, "story_count");
            var durationSeconds = DoubleProperty(root, "duration_seconds");
            var voiceName = StringProperty(root, "voice_name");

            if (string.IsNullOrWhiteSpace(previewFile) || !File.Exists(previewFile))
                throw new FileNotFoundError($"Generated voiceover preview file was not found: {previewFile}");

            LoadVoiceoverPreviewFile(previewFile, durationSeconds);
            VoiceoverFileNameText.Text = fileName;
            var voiceDetail = !string.IsNullOrWhiteSpace(voiceName) ? $" · {voiceName}" : "";
            VoiceoverFileDetailText.Text = $"{storyCount} stor{(storyCount == 1 ? "y" : "ies")} · {FormatShortAudioTime(TimeSpan.FromSeconds(durationSeconds))}{voiceDetail}";
            VoiceoverBusyBanner.Visibility = Visibility.Collapsed;
            VoiceoverPlayButton.IsEnabled = true;
            VoiceoverRegenerateButton.IsEnabled = true;
            VoiceoverSaveButton.IsEnabled = true;
            AppendLog($"VOICE  New voiceover preview ready for Video {videoNumber} ({FormatShortAudioTime(TimeSpan.FromSeconds(durationSeconds))})");
            SetHeaderState("Voiceover ready", "success");
            if (ChoiceSoundCheckBox.IsChecked == true)
                SystemSounds.Exclamation.Play();
        }
        catch (OperationCanceledException)
        {
            AppendLog($"VOICE  Voiceover regeneration cancelled for Video {videoNumber}");
            SetHeaderState("Ready", "muted");
        }
        catch (Exception exc)
        {
            VoiceoverBusyBanner.Visibility = Visibility.Collapsed;
            VoiceoverErrorText.Text = exc.Message;
            VoiceoverErrorText.Visibility = Visibility.Visible;
            VoiceoverFileDetailText.Text = "Generation failed. Click Regenerate to try again.";
            VoiceoverRegenerateButton.IsEnabled = true;
            AppendLog($"ERROR  Voiceover regeneration failed for Video {videoNumber}: {exc.Message}");
            SetHeaderState("Failed", "danger");
            SystemSounds.Hand.Play();
        }
        finally
        {
            _voiceoverBusy = false;
            _ = RefreshProviderQuotasAsync(true);
        }
    }

    private sealed class FileNotFoundError(string message) : Exception(message);

    private void LoadVoiceoverPreviewFile(string sourcePreviewPath, double durationSeconds)
    {
        StopAndCloseVoiceoverPlayer();
        var tempCopy = Path.Combine(
            Path.GetTempPath(),
            $"news-studio-voiceover-preview-{_voiceoverResetVideoNumber}-{Guid.NewGuid():N}.mp3");
        File.Copy(sourcePreviewPath, tempCopy, overwrite: true);
        _voiceoverPreviewTempFile = tempCopy;
        _voiceoverDurationSeconds = Math.Max(0.1, durationSeconds);
        _voiceoverPlayer.Open(new Uri(tempCopy, UriKind.Absolute));
        VoiceoverPlaybackProgress.Value = 0;
        VoiceoverTimeText.Text = $"00:00 / {FormatShortAudioTime(TimeSpan.FromSeconds(_voiceoverDurationSeconds))}";
    }

    private void VoiceoverPlayButton_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_voiceoverPreviewTempFile) || !File.Exists(_voiceoverPreviewTempFile))
            return;

        if (_voiceoverIsPlaying)
        {
            _voiceoverPlayer.Pause();
            _voiceoverPlayerTimer.Stop();
            _voiceoverIsPlaying = false;
            VoiceoverPlayGlyph.Text = "\uE768";
            VoiceoverPlayLabel.Text = "Play";
        }
        else
        {
            if (_voiceoverPlayer.NaturalDuration.HasTimeSpan &&
                _voiceoverPlayer.Position >= _voiceoverPlayer.NaturalDuration.TimeSpan - TimeSpan.FromMilliseconds(150))
            {
                _voiceoverPlayer.Position = TimeSpan.Zero;
            }
            _voiceoverPlayer.Play();
            _voiceoverPlayerTimer.Start();
            _voiceoverIsPlaying = true;
            VoiceoverPlayGlyph.Text = "\uE769";
            VoiceoverPlayLabel.Text = "Pause";
        }
    }

    private void VoiceoverPlayerTimer_Tick(object? sender, EventArgs e)
    {
        if (!_voiceoverIsPlaying) return;
        var totalSeconds = _voiceoverPlayer.NaturalDuration.HasTimeSpan
            ? _voiceoverPlayer.NaturalDuration.TimeSpan.TotalSeconds
            : _voiceoverDurationSeconds;
        totalSeconds = Math.Max(0.1, totalSeconds);
        var currentSeconds = Math.Clamp(_voiceoverPlayer.Position.TotalSeconds, 0, totalSeconds);
        VoiceoverPlaybackProgress.Value = currentSeconds * 100.0 / totalSeconds;
        VoiceoverTimeText.Text = $"{FormatShortAudioTime(TimeSpan.FromSeconds(currentSeconds))} / {FormatShortAudioTime(TimeSpan.FromSeconds(totalSeconds))}";
    }

    private void VoiceoverPlayer_MediaEnded(object? sender, EventArgs e)
    {
        _voiceoverPlayerTimer.Stop();
        _voiceoverIsPlaying = false;
        _voiceoverPlayer.Position = TimeSpan.Zero;
        VoiceoverPlayGlyph.Text = "\uE768";
        VoiceoverPlayLabel.Text = "Play";
        VoiceoverPlaybackProgress.Value = 0;
        var totalSeconds = _voiceoverPlayer.NaturalDuration.HasTimeSpan
            ? _voiceoverPlayer.NaturalDuration.TimeSpan.TotalSeconds
            : _voiceoverDurationSeconds;
        VoiceoverTimeText.Text = $"00:00 / {FormatShortAudioTime(TimeSpan.FromSeconds(totalSeconds))}";
    }

    private void StopAndCloseVoiceoverPlayer()
    {
        _voiceoverPlayerTimer.Stop();
        _voiceoverIsPlaying = false;
        try
        {
            _voiceoverPlayer.Stop();
            _voiceoverPlayer.Close();
        }
        catch (Exception)
        {
        }
        if (VoiceoverPlayGlyph is not null) VoiceoverPlayGlyph.Text = "\uE768";
        if (VoiceoverPlayLabel is not null) VoiceoverPlayLabel.Text = "Play";
        if (!string.IsNullOrWhiteSpace(_voiceoverPreviewTempFile))
        {
            try { File.Delete(_voiceoverPreviewTempFile); } catch (IOException) { }
            _voiceoverPreviewTempFile = null;
        }
    }

    private static string FormatShortAudioTime(TimeSpan span)
    {
        if (span < TimeSpan.Zero) span = TimeSpan.Zero;
        return span.TotalHours >= 1
            ? $"{(int)span.TotalHours:00}:{span.Minutes:00}:{span.Seconds:00}"
            : $"{span.Minutes:00}:{span.Seconds:00}";
    }

    private async void RegenerateVoiceover_Click(object sender, RoutedEventArgs e)
    {
        if (_voiceoverResetVideoNumber <= 0 || _voiceoverBusy) return;
        await RunVoiceoverGenerationAsync(_voiceoverResetVideoNumber);
    }

    private async void SaveVoiceoverReset_Click(object sender, RoutedEventArgs e)
    {
        if (_bridge is null || _voiceoverResetVideoNumber <= 0 || _voiceoverBusy) return;
        _voiceoverBusy = true;
        _voiceoverSaving = true;
        var videoNumber = _voiceoverResetVideoNumber;
        var savedSuccessfully = false;
        StopAndCloseVoiceoverPlayer();

        VoiceoverBusyBanner.Visibility = Visibility.Visible;
        VoiceoverBusyProgress.IsIndeterminate = true;
        VoiceoverStatusText.Text = $"Saving new voiceovers for Video {videoNumber} and preparing pipeline rebuild…";
        VoiceoverErrorText.Visibility = Visibility.Collapsed;
        VoiceoverPlayButton.IsEnabled = false;
        VoiceoverRegenerateButton.IsEnabled = false;
        VoiceoverSaveButton.IsEnabled = false;
        VoiceoverCancelButton.IsEnabled = false;

        SetHeaderState("Saving voiceover", "accent");
        AppendLog($"VOICE  Saving new voiceover for Video {videoNumber} and restarting from voiceover stage");

        try
        {
            using var document = await _bridge.SaveVoiceoverResetAsync(
                videoNumber,
                progress => Dispatcher.BeginInvoke(() =>
                {
                    VoiceoverStatusText.Text = progress;
                }));

            savedSuccessfully = true;
            AnimateOverlayOut(VoiceoverResetOverlay, VoiceoverResetCard);
            AppendLog($"VOICE  Voiceover saved for Video {videoNumber}; restarting pipeline from Stage 02 (tts)");
            SetHeaderState("Rebuilding from voiceover", "accent");
            CurrentActionTitle.Text = $"Rebuilding Video {videoNumber} from voiceover stage";
            CurrentActionDetail.Text = $"Resyncing timing, animations, visuals, subtitles, effects, and final video for Video {videoNumber}.";
        }
        catch (Exception exc)
        {
            VoiceoverBusyBanner.Visibility = Visibility.Collapsed;
            VoiceoverErrorText.Text = exc.Message;
            VoiceoverErrorText.Visibility = Visibility.Visible;
            VoiceoverRegenerateButton.IsEnabled = true;
            VoiceoverSaveButton.IsEnabled = true;
            VoiceoverCancelButton.IsEnabled = true;
            AppendLog($"ERROR  Could not save voiceover for Video {videoNumber}: {exc.Message}");
            SetHeaderState("Save failed", "danger");
            SystemSounds.Hand.Play();
        }
        finally
        {
            _voiceoverBusy = false;
            _voiceoverSaving = false;
            VoiceoverCancelButton.IsEnabled = true;
            await RefreshStatusAsync();
        }

        if (savedSuccessfully)
        {
            await RunProductionAsync(
                resumeVideoNumber: videoNumber,
                resumeStartStage: "tts");
        }
    }

    private async void CancelVoiceoverReset_Click(object sender, RoutedEventArgs e)
    {
        if (_voiceoverSaving) return;
        var videoNumber = _voiceoverResetVideoNumber;
        _voiceoverCts?.Cancel();
        StopAndCloseVoiceoverPlayer();
        AnimateOverlayOut(VoiceoverResetOverlay, VoiceoverResetCard);
        if (_bridge is not null && videoNumber > 0)
        {
            try
            {
                using var _ = await _bridge.DiscardVoiceoverResetAsync(videoNumber);
            }
            catch (Exception)
            {
            }
        }
        await RefreshStatusAsync();
    }

    private int ResolvePageIndex(FrameworkElement? page) =>
        page == HistoryPage ? 1 : page == QuotaPage ? 2 : page == SettingsPage ? 3 : 0;

    private void SetNavigationControlsEnabled(bool enabled)
    {
        ProductionNav.IsEnabled = enabled;
        HistoryNav.IsEnabled = enabled;
        QuotaNav.IsEnabled = enabled;
        SettingsNav.IsEnabled = enabled;
    }

    private void Navigation_Checked(object sender, RoutedEventArgs e)
    {
        if (ProductionPage is null || HistoryPage is null || QuotaPage is null || SettingsPage is null) return;
        var target = HistoryNav.IsChecked == true
            ? (FrameworkElement)HistoryPage
            : QuotaNav.IsChecked == true
                ? QuotaPage
                : SettingsNav.IsChecked == true
                    ? SettingsPage
                    : ProductionPage;

        if (!_windowLoaded)
        {
            ProductionPage.Visibility = target == ProductionPage ? Visibility.Visible : Visibility.Collapsed;
            HistoryPage.Visibility = target == HistoryPage ? Visibility.Visible : Visibility.Collapsed;
            QuotaPage.Visibility = target == QuotaPage ? Visibility.Visible : Visibility.Collapsed;
            SettingsPage.Visibility = target == SettingsPage ? Visibility.Visible : Visibility.Collapsed;
            _activePage = target;
            return;
        }

        if (target == HistoryPage && ProductionsMasterList.SelectedItem is null && _productionHistory.Count > 0)
            ProductionsMasterList.SelectedIndex = 0;

        var direction = ResolvePageIndex(target) >= ResolvePageIndex(_activePage) ? 1 : -1;
        AnimateNavigationTo(target, direction);
    }

    private void AnimateNavigationTo(FrameworkElement target, int direction)
    {
        if (_activePage == target || _navigationAnimating) return;
        var outgoing = _activePage;
        _activePage = target;
        _navigationAnimating = true;
        SetNavigationControlsEnabled(false);

        var ease = new CubicEase { EasingMode = EasingMode.EaseOut };
        var duration = TimeSpan.FromMilliseconds(230);
        var incomingTransform = new TranslateTransform(direction * 26, 0);
        target.RenderTransform = incomingTransform;
        target.Opacity = 0;
        target.Visibility = Visibility.Visible;
        target.BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, duration) { EasingFunction = ease });
        incomingTransform.BeginAnimation(TranslateTransform.XProperty,
            new DoubleAnimation(direction * 26, 0, duration) { EasingFunction = ease });

        if (outgoing is null)
        {
            FinishNavigationTransition(target, null);
            return;
        }

        var outgoingTransform = new TranslateTransform();
        outgoing.RenderTransform = outgoingTransform;
        outgoing.BeginAnimation(OpacityProperty,
            new DoubleAnimation(1, 0, TimeSpan.FromMilliseconds(145)) { EasingFunction = ease });
        var outgoingAnimation = new DoubleAnimation(0, direction * -14, TimeSpan.FromMilliseconds(175))
        {
            EasingFunction = ease,
        };
        outgoingAnimation.Completed += (_, _) => FinishNavigationTransition(target, outgoing);
        outgoingTransform.BeginAnimation(TranslateTransform.XProperty, outgoingAnimation);
    }

    private void FinishNavigationTransition(FrameworkElement target, FrameworkElement? outgoing)
    {
        if (outgoing is not null)
        {
            outgoing.BeginAnimation(OpacityProperty, null);
            outgoing.Opacity = 1;
            outgoing.RenderTransform = Transform.Identity;
            outgoing.Visibility = Visibility.Collapsed;
        }
        target.Opacity = 1;
        _navigationAnimating = false;
        SetNavigationControlsEnabled(true);
    }

    private static void AnimateOverlayIn(Grid overlay, FrameworkElement card)
    {
        overlay.Visibility = Visibility.Visible;
        overlay.Opacity = 0;
        card.Opacity = 0;
        card.RenderTransformOrigin = new Point(0.5, 0.5);
        var scale = new ScaleTransform(0.975, 0.975);
        var translate = new TranslateTransform(0, 16);
        card.RenderTransform = new TransformGroup { Children = { scale, translate } };
        var ease = new CubicEase { EasingMode = EasingMode.EaseOut };
        overlay.BeginAnimation(OpacityProperty,
            new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(155)) { EasingFunction = ease });
        card.BeginAnimation(OpacityProperty,
            new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(220)) { EasingFunction = ease });
        scale.BeginAnimation(ScaleTransform.ScaleXProperty,
            new DoubleAnimation(0.975, 1, TimeSpan.FromMilliseconds(235)) { EasingFunction = ease });
        scale.BeginAnimation(ScaleTransform.ScaleYProperty,
            new DoubleAnimation(0.975, 1, TimeSpan.FromMilliseconds(235)) { EasingFunction = ease });
        translate.BeginAnimation(TranslateTransform.YProperty,
            new DoubleAnimation(16, 0, TimeSpan.FromMilliseconds(235)) { EasingFunction = ease });
    }

    private static void AnimateOverlayOut(Grid overlay, FrameworkElement card)
    {
        if (overlay.Visibility != Visibility.Visible) return;
        var ease = new CubicEase { EasingMode = EasingMode.EaseIn };
        var fade = new DoubleAnimation(overlay.Opacity, 0, TimeSpan.FromMilliseconds(125))
        {
            EasingFunction = ease,
        };
        fade.Completed += (_, _) =>
        {
            overlay.Visibility = Visibility.Collapsed;
            overlay.BeginAnimation(OpacityProperty, null);
            overlay.Opacity = 1;
            card.BeginAnimation(OpacityProperty, null);
            card.Opacity = 1;
            card.RenderTransform = Transform.Identity;
        };
        overlay.BeginAnimation(OpacityProperty, fade);
        card.BeginAnimation(OpacityProperty,
            new DoubleAnimation(card.Opacity, 0, TimeSpan.FromMilliseconds(105)) { EasingFunction = ease });
    }

    private void ClearLog_Click(object sender, RoutedEventArgs e)
    {
        _allActivityLogs.Clear();
        _filteredActivityLogs.Clear();
        UpdateActivityEmptyState();
    }

    private void StageRow_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (sender is not FrameworkElement { DataContext: StageViewModel stage }) return;
        if (string.IsNullOrWhiteSpace(stage.LogFilePath) || !File.Exists(stage.LogFilePath)) return;
        OpenPath(stage.LogFilePath);
    }

    private void SnapshotOpenFolderButton_Click(object sender, RoutedEventArgs e) => OpenPath(_currentProjectPath);

    private void SnapshotOpenScriptButton_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_currentProjectPath)) return;
        OpenPath(Path.Combine(_currentProjectPath, "video_script.txt"));
    }

    private void SnapshotOpenVideoButton_Click(object sender, RoutedEventArgs e) =>
        OpenPath(ResolveFinalVideoPath(_currentProjectPath));

    private void ActivityFilter_Checked(object sender, RoutedEventArgs e)
    {
        if (ActivityEmptyPanel is null || ActivityCountText is null) return;
        _activeActivityFilter = ActivityFilterStages.IsChecked == true
            ? "Stages"
            : ActivityFilterIssues.IsChecked == true
                ? "Issues"
                : "All";
        _filteredActivityLogs.Clear();
        foreach (var entry in _allActivityLogs)
        {
            if (MatchesActivityFilter(entry))
                _filteredActivityLogs.Add(entry);
        }
        UpdateActivityEmptyState();
        LogScrollViewer.ScrollToEnd();
    }

    private async void CopyLog_Click(object sender, RoutedEventArgs e)
    {
        if (_filteredActivityLogs.Count == 0) return;
        try
        {
            Clipboard.SetText(string.Join(Environment.NewLine, _filteredActivityLogs.Select(item => item.RawLine)));
            CopyLogLabel.Text = "Copied";
            await Task.Delay(1500);
            if (CopyLogLabel.Text == "Copied")
                CopyLogLabel.Text = "Copy";
        }
        catch
        {
            // Clipboard can be temporarily locked by another Windows process.
        }
    }

    private void CopyActivityLine_Click(object sender, RoutedEventArgs e)
    {
        var entry = (sender as MenuItem)?.DataContext as ActivityLogEntry
                    ?? (sender as MenuItem)?.CommandParameter as ActivityLogEntry;
        if (entry is null) return;
        try
        {
            Clipboard.SetText(entry.RawLine);
        }
        catch
        {
            // Clipboard can be temporarily locked by another Windows process.
        }
    }

    private sealed class StageEventMetrics
    {
        public Dictionary<string, double> Durations { get; } = new(StringComparer.OrdinalIgnoreCase);
        public Dictionary<string, string> LogFiles { get; } = new(StringComparer.OrdinalIgnoreCase);
        public Dictionary<string, DateTimeOffset> StartedUtc { get; } = new(StringComparer.OrdinalIgnoreCase);
        public Dictionary<string, string> EventStates { get; } = new(StringComparer.OrdinalIgnoreCase);
        public Dictionary<string, string> EventDetails { get; } = new(StringComparer.OrdinalIgnoreCase);
    }

    private StageEventMetrics ReadStageEventMetrics(string projectPath)
    {
        var metrics = new StageEventMetrics();
        var eventsPath = Path.Combine(projectPath, "orchestration", "events.jsonl");
        if (!File.Exists(eventsPath)) return metrics;

        try
        {
            foreach (var line in File.ReadLines(eventsPath))
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                using var document = JsonDocument.Parse(line);
                var root = document.RootElement;
                var eventType = StringProperty(root, "type") ?? StringProperty(root, "event");
                if (string.IsNullOrWhiteSpace(eventType)) continue;

                if (string.Equals(eventType, "preview_approved", StringComparison.OrdinalIgnoreCase))
                {
                    metrics.EventStates["preview_approval"] = "COMPLETE";
                    continue;
                }

                var stageKey = NormalizeEventStageKey(StringProperty(root, "stage"));
                if (string.IsNullOrWhiteSpace(stageKey)) continue;

                if (ResolveEventLogFile(projectPath, StringProperty(root, "log_file")) is { } resolvedLog)
                    metrics.LogFiles[stageKey] = resolvedLog;

                if (string.Equals(eventType, "stage_started", StringComparison.OrdinalIgnoreCase))
                {
                    metrics.EventStates[stageKey] = "RUNNING";
                    if (DateTimeOffset.TryParse(
                            StringProperty(root, "at_utc") ?? StringProperty(root, "ts"),
                            CultureInfo.InvariantCulture,
                            DateTimeStyles.AssumeUniversal,
                            out var parsedStart))
                    {
                        metrics.StartedUtc[stageKey] = parsedStart;
                    }
                    metrics.Durations.Remove(stageKey);
                }
                else if (string.Equals(eventType, "stage_completed", StringComparison.OrdinalIgnoreCase) ||
                         string.Equals(eventType, "stage_failed", StringComparison.OrdinalIgnoreCase))
                {
                    metrics.EventStates[stageKey] = string.Equals(eventType, "stage_failed", StringComparison.OrdinalIgnoreCase)
                        ? "FAILED"
                        : "COMPLETE";
                    metrics.StartedUtc.Remove(stageKey);
                    if (root.TryGetProperty("elapsed_seconds", out var elapsedValue) &&
                        elapsedValue.TryGetDouble(out var elapsedSeconds) &&
                        elapsedSeconds > 0)
                    {
                        metrics.Durations[stageKey] = elapsedSeconds;
                    }
                }
                else if (string.Equals(eventType, "flow_waiting", StringComparison.OrdinalIgnoreCase))
                {
                    metrics.EventStates[stageKey] = "WAITING";
                    if (StringProperty(root, "detail") is { Length: > 0 } waitingDetail)
                        metrics.EventDetails[stageKey] = waitingDetail;
                }
            }
        }
        catch (Exception exception) when (exception is IOException or JsonException or UnauthorizedAccessException)
        {
            // Ignore transient event log read issues while Python is writing.
        }

        return metrics;
    }

    private void ApplyStageEventMetrics(
        string projectPath,
        IReadOnlyDictionary<string, string>? stageDetails = null,
        bool isActiveRun = false)
    {
        var metrics = ReadStageEventMetrics(projectPath);

        double totalRecordedSeconds = 0;
        foreach (var stage in _stages)
        {
            stage.LogFilePath = metrics.LogFiles.TryGetValue(stage.Key, out var logPath) ? logPath : null;

            if (metrics.Durations.TryGetValue(stage.Key, out var elapsedSeconds) && elapsedSeconds > 0)
            {
                if (IsTerminalStageStatus(stage.StateKey) || string.Equals(stage.StateKey, "FAILED", StringComparison.Ordinal))
                {
                    stage.ElapsedText = FormatCompactDuration(TimeSpan.FromSeconds(elapsedSeconds));
                    totalRecordedSeconds += elapsedSeconds;
                }
            }
            else if (isActiveRun &&
                     string.Equals(stage.StateKey, "RUNNING", StringComparison.Ordinal) &&
                     metrics.StartedUtc.TryGetValue(stage.Key, out var startedAt))
            {
                stage.StartedAtUtc = startedAt;
                stage.ElapsedText = FormatCompactDuration(DateTimeOffset.UtcNow - startedAt);
            }

            var detail = stageDetails is not null && stageDetails.TryGetValue(stage.Key, out var text)
                ? text
                : null;
            UpdateStageTooltip(stage, detail);
        }

        if (totalRecordedSeconds > 0)
        {
            TotalStagesTimeText.Text = FormatCompactDuration(TimeSpan.FromSeconds(totalRecordedSeconds));
            TotalStagesTimeBorder.Visibility = Visibility.Visible;
        }
        else
        {
            TotalStagesTimeBorder.Visibility = Visibility.Collapsed;
        }
    }

    private static string? NormalizeEventStageKey(string? rawStage)
    {
        if (string.IsNullOrWhiteSpace(rawStage)) return null;
        return string.Equals(rawStage, "preview", StringComparison.OrdinalIgnoreCase)
            ? "preview_approval"
            : rawStage;
    }

    private string? ResolveEventLogFile(string projectPath, string? rawPath) =>
        ResolvePortableProjectFile(projectPath, rawPath);

    private static void UpdateStageTooltip(StageViewModel stage, string? detail = null)
    {
        var lines = new List<string> { $"{stage.Number} · {stage.Label} ({stage.Status})" };
        if (!string.IsNullOrWhiteSpace(detail))
            lines.Add(detail.Trim());
        if (!string.IsNullOrWhiteSpace(stage.ElapsedText))
            lines.Add($"Duration: {stage.ElapsedText}");
        if (!string.IsNullOrWhiteSpace(stage.LogFilePath) && File.Exists(stage.LogFilePath))
            lines.Add("Click to open stage log");
        stage.DetailTooltip = string.Join(Environment.NewLine, lines);
    }

    private void UpdatePipelineSnapshot()
    {
        var useProjectMetadata = !string.IsNullOrWhiteSpace(_currentProjectPath) &&
                                 Directory.Exists(_currentProjectPath) &&
                                 (_bridge?.IsRunning == true || !_latestProductionComplete || !_latestUploadComplete);

        string? aiProvider = null;
        string? aiModel = null;
        int storyCount = 0;
        string? ttsProvider = null;
        string? ttsVoice = null;

        if (useProjectMetadata && _currentProjectPath is not null)
        {
            var projectInfoPath = Path.Combine(_currentProjectPath, "project_info.json");
            if (File.Exists(projectInfoPath))
            {
                try
                {
                    using var doc = JsonDocument.Parse(File.ReadAllText(projectInfoPath));
                    var root = doc.RootElement;
                    aiProvider = StringProperty(root, "provider");
                    aiModel = StringProperty(root, "model");
                    storyCount = IntProperty(root, "created_count");
                    if (storyCount <= 0 &&
                        root.TryGetProperty("stories", out var storiesElement) &&
                        storiesElement.ValueKind == JsonValueKind.Array)
                    {
                        storyCount = storiesElement.GetArrayLength();
                    }
                }
                catch
                {
                    // Fall back to configured settings when project metadata is incomplete.
                }
            }

            var ttsInfoPath = Path.Combine(_currentProjectPath, "Sesler", "tts_info.json");
            if (File.Exists(ttsInfoPath))
            {
                try
                {
                    using var doc = JsonDocument.Parse(File.ReadAllText(ttsInfoPath));
                    var root = doc.RootElement;
                    ttsProvider = StringProperty(root, "provider");
                    ttsVoice = StringProperty(root, "voice_name");
                }
                catch
                {
                    // Fall back to configured settings when TTS metadata is incomplete.
                }
            }
        }

        aiProvider ??= GetEnvSettingValue("AI_PROVIDER") ?? "openai";
        var isGemini = string.Equals(aiProvider, "gemini", StringComparison.OrdinalIgnoreCase);
        aiModel ??= GetEnvSettingValue(isGemini ? "GEMINI_MODEL" : "OPENAI_MODEL");
        var aiProviderDisplay = isGemini ? "Gemini" : "OpenAI";
        SnapshotModelText.Text = string.IsNullOrWhiteSpace(aiModel)
            ? $"AI: {aiProviderDisplay}"
            : $"AI: {aiProviderDisplay} · {aiModel}";

        ttsProvider ??= GetEnvSettingValue("TTS_PROVIDER") ?? "qwen";
        var isQwen = string.Equals(ttsProvider, "qwen", StringComparison.OrdinalIgnoreCase);
        ttsVoice ??= isQwen
            ? GetEnvSettingValue("QWEN_TTS_SPEAKER")
            : GetEnvSettingValue("ELEVENLABS_VOICE_ID");
        var ttsProviderDisplay = isQwen ? "Qwen" : string.Equals(ttsProvider, "elevenlabs", StringComparison.OrdinalIgnoreCase) ? "ElevenLabs" : FormatTitleToken(ttsProvider);
        var ttsVoiceDisplay = FormatVoiceDisplay(ttsVoice, isQwen);
        SnapshotTtsText.Text = string.IsNullOrWhiteSpace(ttsVoiceDisplay)
            ? $"TTS: {ttsProviderDisplay}"
            : $"TTS: {ttsProviderDisplay} · {ttsVoiceDisplay}";

        if (storyCount <= 0 && int.TryParse(GetEnvSettingValue("TOP_N"), CultureInfo.InvariantCulture, out var configuredTopN))
            storyCount = configuredTopN;
        SnapshotStoriesText.Text = storyCount > 0
            ? $"{storyCount} {(storyCount == 1 ? "story" : "stories")}"
            : "— stories";

        var hasFolder = !string.IsNullOrWhiteSpace(_currentProjectPath) && Directory.Exists(_currentProjectPath);
        SnapshotOpenFolderButton.IsEnabled = hasFolder;
        SnapshotOpenScriptButton.IsEnabled = hasFolder &&
            File.Exists(Path.Combine(_currentProjectPath!, "video_script.txt"));
        SnapshotOpenVideoButton.IsEnabled = ResolveFinalVideoPath(_currentProjectPath) is not null;
    }

    private string? GetEnvSettingValue(string key) =>
        _settings.FirstOrDefault(setting => string.Equals(setting.Key, key, StringComparison.OrdinalIgnoreCase))?.Value?.Trim();

    private static string FormatTitleToken(string value)
    {
        var trimmed = value.Trim();
        if (trimmed.Length == 0) return trimmed;
        return char.ToUpper(trimmed[0], CultureInfo.CurrentCulture) + trimmed[1..];
    }

    private static string? FormatVoiceDisplay(string? voice, bool isQwen)
    {
        if (string.IsNullOrWhiteSpace(voice)) return null;
        var trimmed = voice.Trim();
        if (isQwen) return FormatTitleToken(trimmed);
        return trimmed.Length > 14 ? $"{trimmed[..10]}…" : trimmed;
    }

    private static string? ResolveFinalVideoPath(string? projectPath)
    {
        if (string.IsNullOrWhiteSpace(projectPath)) return null;
        var renderDir = Path.Combine(projectPath, "08_render");
        if (!Directory.Exists(renderDir)) return null;

        var folderName = Path.GetFileName(projectPath.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
        if (!string.IsNullOrWhiteSpace(folderName))
        {
            var expected = Path.Combine(renderDir, $"{folderName}_final.mp4");
            if (File.Exists(expected)) return expected;
        }

        return new DirectoryInfo(renderDir)
            .GetFiles("*_final.mp4")
            .OrderByDescending(file => file.LastWriteTimeUtc)
            .FirstOrDefault()?.FullName;
    }

    private static ActivityLogEntry? ParseActivityLogEntry(string message)
    {
        var normalized = (message ?? "").Replace("\r\n", " ").Replace('\n', ' ').Trim();
        if (string.IsNullOrWhiteSpace(normalized)) return null;

        var timestamp = DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture);
        var rawLine = $"{timestamp}  {normalized}";
        var tag = "INFO";
        var body = normalized;

        var split = normalized.Split(' ', 2, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (split.Length == 2 &&
            split[0].Length is >= 2 and <= 10 &&
            split[0].All(char.IsAsciiLetterUpper))
        {
            tag = split[0];
            body = split[1];
        }
        else if (normalized.StartsWith("Could not ", StringComparison.OrdinalIgnoreCase))
        {
            tag = "ERROR";
        }

        var category = tag switch
        {
            "ERROR" or "WARN" => "Issue",
            "STAGE" or "START" or "RUN" or "DONE" or "RENDER" or "VOICE" or "UPLOAD" or "CHOICE" or "TITLE" => "Stage",
            _ => "General",
        };

        var tagKind = tag switch
        {
            "ERROR" or "STOP" or "CANCEL" => "error",
            "WARN" or "WAIT" => "warning",
            "DONE" or "ONLINE" or "READY" => "success",
            "STAGE" or "START" or "RUN" or "RENDER" or "UPLOAD" or "VOICE" or "CHOICE" or "TITLE" => "stage",
            _ => "info",
        };

        return new ActivityLogEntry
        {
            Timestamp = timestamp,
            Tag = tag,
            Message = body,
            Category = category,
            TagKind = tagKind,
            RawLine = rawLine,
        };
    }

    private void ApplyLogEntryThemeBrushes(ActivityLogEntry entry)
    {
        switch (entry.TagKind)
        {
            case "error":
                entry.TagBackground = ResourceBrush("DangerTintBrush");
                entry.TagBorderBrush = ResourceBrush("DangerBrush");
                entry.TagForeground = ResourceBrush("DangerBrush");
                entry.MessageForeground = string.Equals(entry.Tag, "ERROR", StringComparison.Ordinal)
                    ? ResourceBrush("DangerBrush")
                    : ResourceBrush("TextPrimaryBrush");
                break;
            case "warning":
                entry.TagBackground = ResourceBrush("WarningTintBrush");
                entry.TagBorderBrush = ResourceBrush("WarningBrush");
                entry.TagForeground = ResourceBrush("WarningBrush");
                entry.MessageForeground = ResourceBrush("TextPrimaryBrush");
                break;
            case "success":
                entry.TagBackground = ResourceBrush("SuccessTintBrush");
                entry.TagBorderBrush = ResourceBrush("SuccessBrush");
                entry.TagForeground = ResourceBrush("SuccessBrush");
                entry.MessageForeground = ResourceBrush("TextPrimaryBrush");
                break;
            case "stage":
                entry.TagBackground = ResourceBrush("AccentTintBrush");
                entry.TagBorderBrush = ResourceBrush("AccentBrush");
                entry.TagForeground = ResourceBrush("AccentBrush");
                entry.MessageForeground = ResourceBrush("TextPrimaryBrush");
                break;
            default:
                entry.TagBackground = ResourceBrush("SurfaceAltBrush");
                entry.TagBorderBrush = ResourceBrush("StrokeBrush");
                entry.TagForeground = ResourceBrush("TextSecondaryBrush");
                entry.MessageForeground = ResourceBrush("TextSecondaryBrush");
                break;
        }
    }

    private void RefreshActivityLogThemeBrushes()
    {
        foreach (var entry in _allActivityLogs)
            ApplyLogEntryThemeBrushes(entry);
    }

    private bool MatchesActivityFilter(ActivityLogEntry entry) => _activeActivityFilter switch
    {
        "Stages" => string.Equals(entry.Category, "Stage", StringComparison.Ordinal),
        "Issues" => string.Equals(entry.Category, "Issue", StringComparison.Ordinal),
        _ => true,
    };

    private void UpdateActivityEmptyState()
    {
        var visibleCount = _filteredActivityLogs.Count;
        var totalCount = _allActivityLogs.Count;
        ActivityCountText.Text = totalCount == 0
            ? "0"
            : string.Equals(_activeActivityFilter, "All", StringComparison.Ordinal)
                ? totalCount.ToString(CultureInfo.InvariantCulture)
                : $"{visibleCount}/{totalCount}";
        CopyLogButton.IsEnabled = visibleCount > 0;

        if (visibleCount == 0)
        {
            ActivityEmptyPanel.Visibility = Visibility.Visible;
            ActivityEmptyText.Text = totalCount == 0
                ? "No activity yet. Start a production to inspect live stage events here."
                : _activeActivityFilter switch
                {
                    "Stages" => "No stage transitions recorded yet.",
                    "Issues" => "No warnings or errors logged.",
                    _ => "No matching activity entries.",
                };
        }
        else
        {
            ActivityEmptyPanel.Visibility = Visibility.Collapsed;
        }
    }

    private static string FormatCompactDuration(TimeSpan duration)
    {
        if (duration < TimeSpan.Zero) duration = TimeSpan.Zero;
        if (duration.TotalHours >= 1)
            return $"{(int)duration.TotalHours}h {duration.Minutes:00}m";
        if (duration.TotalMinutes >= 1)
            return $"{(int)duration.TotalMinutes}m {duration.Seconds:00}s";
        return $"{Math.Max(0, (int)Math.Round(duration.TotalSeconds))}s";
    }

    private static string ResolveProductionHeadline(string projectPath, bool isRunning, string status)
    {
        var titleFile = Path.Combine(projectPath, "09_publish", "title.txt");
        if (File.Exists(titleFile))
        {
            try
            {
                var text = File.ReadLines(titleFile).FirstOrDefault(line => !string.IsNullOrWhiteSpace(line))?.Trim();
                if (!string.IsNullOrWhiteSpace(text)) return text;
            }
            catch
            {
                // Ignore read issues and fall back to metadata.
            }
        }

        var manifestPath = Path.Combine(projectPath, "09_publish", "youtube_upload_manifest.json");
        if (File.Exists(manifestPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(manifestPath));
                if (doc.RootElement.TryGetProperty("metadata", out var meta) &&
                    meta.TryGetProperty("snippet", out var snippet) &&
                    StringProperty(snippet, "title") is { Length: > 0 } manifestTitle)
                {
                    return manifestTitle;
                }
            }
            catch
            {
                // Ignore read issues and fall back to story metadata.
            }
        }

        var projectInfoPath = Path.Combine(projectPath, "project_info.json");
        if (File.Exists(projectInfoPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(projectInfoPath));
                if (doc.RootElement.TryGetProperty("stories", out var stories) &&
                    stories.ValueKind == JsonValueKind.Array)
                {
                    var leadId = doc.RootElement.TryGetProperty("lead_story_selection", out var leadSel)
                        ? StringProperty(leadSel, "selected_story_id")
                        : null;
                    string? firstTitle = null;
                    foreach (var story in stories.EnumerateArray())
                    {
                        var title = StringProperty(story, "title");
                        if (string.IsNullOrWhiteSpace(title)) continue;
                        firstTitle ??= title;
                        if (leadId is not null && string.Equals(StringProperty(story, "story_id"), leadId, StringComparison.Ordinal))
                            return title;
                    }
                    if (!string.IsNullOrWhiteSpace(firstTitle)) return firstTitle;
                }
            }
            catch
            {
                // Fall back to status label.
            }
        }

        return isRunning ? "Production currently running…" : $"Production run · {status}";
    }

    private void ProductionsMasterList_SelectionChanged(object sender, SelectionChangedEventArgs e) =>
        LoadHistoryProductionDetail(ProductionsMasterList.SelectedItem as ProductionTimerLogViewModel);

    private void LoadHistoryProductionDetail(ProductionTimerLogViewModel? item)
    {
        _selectedHistoryStories.Clear();
        _selectedHistoryStages.Clear();
        _selectedHistoryTags.Clear();
        _selectedHistoryWatchUrl = null;
        _selectedHistoryPublishDescription = null;
        _selectedHistoryScriptFullText = null;

        if (item is null || string.IsNullOrWhiteSpace(item.ProjectPath) || !Directory.Exists(item.ProjectPath))
        {
            _selectedHistoryItem = null;
            HistoryDetailEmptyPanel.Visibility = Visibility.Visible;
            HistoryDetailScrollViewer.Visibility = Visibility.Collapsed;
            return;
        }

        _selectedHistoryItem = item;
        HistoryDetailEmptyPanel.Visibility = Visibility.Collapsed;
        HistoryDetailScrollViewer.Visibility = Visibility.Visible;
        HistoryReuploadButton.DataContext = item;
        HistoryReuploadButton.IsEnabled = item.IsFinished;
        HistoryResetVoiceoverButton.DataContext = item;

        var projectPath = item.ProjectPath;
        var publish = ReadHistoryPublishData(projectPath);
        var timing = ReadHistoryTimingData(projectPath);
        var metrics = ReadStageEventMetrics(projectPath);
        var stateInfo = ReadHistoryStateData(projectPath);
        var videoPath = ResolveFinalVideoPath(projectPath);
        var scriptPath = Path.Combine(projectPath, "video_script.txt");

        HistoryDetailVideoChipText.Text = item.VideoName;
        var (statusBg, statusBorder, statusFg) = StatusPresentation(item.Status);
        HistoryDetailStatusBorder.Background = statusBg;
        HistoryDetailStatusBorder.BorderBrush = statusBorder;
        HistoryDetailStatusText.Foreground = statusFg;
        HistoryDetailStatusText.Text = item.Status;

        if (publish.IsUploaded)
        {
            HistoryDetailUploadBadgeBorder.Visibility = Visibility.Visible;
            HistoryDetailUploadBadgeBorder.Background = ResourceBrush("SuccessTintBrush");
            HistoryDetailUploadBadgeText.Foreground = ResourceBrush("SuccessBrush");
            HistoryDetailUploadBadgeText.Text = "Uploaded to YouTube";
        }
        else if (item.IsFinished && videoPath is not null)
        {
            HistoryDetailUploadBadgeBorder.Visibility = Visibility.Visible;
            HistoryDetailUploadBadgeBorder.Background = ResourceBrush("AccentTintBrush");
            HistoryDetailUploadBadgeText.Foreground = ResourceBrush("AccentBrush");
            HistoryDetailUploadBadgeText.Text = "Rendered · Ready to upload";
        }
        else
        {
            HistoryDetailUploadBadgeBorder.Visibility = Visibility.Collapsed;
        }

        var thumbnailBitmap = LoadBitmapPreview(ResolveHistoryThumbnailPath(projectPath, publish.ThumbnailFile));
        if (thumbnailBitmap is not null)
        {
            HistoryDetailThumbnailImage.Source = thumbnailBitmap;
            HistoryDetailThumbnailImage.Visibility = Visibility.Visible;
            HistoryDetailThumbnailFallback.Visibility = Visibility.Collapsed;
        }
        else
        {
            HistoryDetailThumbnailImage.Source = null;
            HistoryDetailThumbnailImage.Visibility = Visibility.Collapsed;
            HistoryDetailThumbnailFallback.Visibility = Visibility.Visible;
        }

        var storiesSummary = PopulateHistoryStories(projectPath, timing);
        HistoryDetailTitleText.Text = !string.IsNullOrWhiteSpace(publish.Title)
            ? publish.Title
            : storiesSummary.FirstStoryTitle ?? item.Headline;

        var metaParts = new List<string>
        {
            item.Date,
            $"Run {item.Duration}",
        };
        if (timing.DurationSeconds > 0)
            metaParts.Add($"Video {FormatShortAudioTime(TimeSpan.FromSeconds(timing.DurationSeconds))}");
        if (videoPath is not null && File.Exists(videoPath))
        {
            var mb = new FileInfo(videoPath).Length / (1024d * 1024d);
            metaParts.Add($"{mb:0.#} MB MP4");
        }
        HistoryDetailMetaText.Text = string.Join("  ·  ", metaParts);

        var scheduleLines = new List<string>();
        if (!string.IsNullOrWhiteSpace(publish.UploadedAtText))
            scheduleLines.Add($"Uploaded {publish.UploadedAtText}");
        if (!string.IsNullOrWhiteSpace(publish.ScheduledAtText))
            scheduleLines.Add($"Scheduled publish {publish.ScheduledAtText}");
        if (scheduleLines.Count > 0)
        {
            HistoryDetailScheduleText.Text = string.Join("  ·  ", scheduleLines);
            HistoryDetailScheduleText.Visibility = Visibility.Visible;
        }
        else
        {
            HistoryDetailScheduleText.Visibility = Visibility.Collapsed;
        }

        _selectedHistoryWatchUrl = publish.WatchUrl;
        HistoryOpenFolderButton.IsEnabled = Directory.Exists(projectPath);
        HistoryOpenVideoButton.IsEnabled = videoPath is not null && File.Exists(videoPath);
        HistoryOpenScriptButton.IsEnabled = File.Exists(scriptPath);
        HistoryOpenYouTubeButton.Visibility = !string.IsNullOrWhiteSpace(publish.WatchUrl)
            ? Visibility.Visible
            : Visibility.Collapsed;

        // 4 Spec tiles
        HistorySpecAiValue.Text = storiesSummary.AiSummary;
        HistorySpecAiSub.Text = storiesSummary.CurationSummary;

        var ttsSummary = ReadHistoryTtsSummary(projectPath);
        HistorySpecTtsValue.Text = ttsSummary.Primary;
        HistorySpecTtsSub.Text = ttsSummary.Detail;

        HistorySpecRenderValue.Text = stateInfo.CueCount > 0 || stateInfo.SfxCount > 0
            ? $"{stateInfo.CueCount} cues · {stateInfo.SfxCount} SFX"
            : string.IsNullOrWhiteSpace(timing.QualityStatus)
                ? "Timeline pending"
                : $"Timing {timing.QualityStatus}";
        HistorySpecRenderSub.Text = !string.IsNullOrWhiteSpace(stateInfo.RenderStatus)
            ? $"Render: {stateInfo.RenderStatus}"
            : videoPath is not null ? "Final video encoded" : "No final video yet";

        HistorySpecYoutubeValue.Text = publish.IsUploaded
            ? (!string.IsNullOrWhiteSpace(publish.VideoId) ? $"ID: {publish.VideoId}" : "Uploaded")
            : !string.IsNullOrWhiteSpace(publish.Title) ? "Package generated" : "Not published";
        HistorySpecYoutubeSub.Text = publish.IsUploaded
            ? $"Privacy: {publish.PrivacyStatus ?? "private"}"
            : !string.IsNullOrWhiteSpace(publish.ScheduledAtText)
                ? $"Target: {publish.ScheduledAtText}"
                : "Upload metadata not created yet";

        PopulateHistoryStages(metrics, stateInfo);

        // Publish metadata card
        _selectedHistoryPublishDescription = publish.Description;
        foreach (var tag in publish.Tags)
            _selectedHistoryTags.Add(tag);

        if (!string.IsNullOrWhiteSpace(publish.Description) || _selectedHistoryTags.Count > 0)
        {
            HistoryPublishCard.Visibility = Visibility.Visible;
            HistoryPublishDescriptionText.Text = string.IsNullOrWhiteSpace(publish.Description)
                ? "(No description text saved)"
                : publish.Description;
            HistoryCopyDescriptionButton.IsEnabled = !string.IsNullOrWhiteSpace(publish.Description);
        }
        else
        {
            HistoryPublishCard.Visibility = Visibility.Collapsed;
        }

        PopulateHistoryScriptCard(scriptPath);
        HistoryDetailScrollViewer.ScrollToTop();
    }

    private sealed record HistoryPublishData(
        bool IsUploaded,
        string? Title,
        string? Description,
        IReadOnlyList<string> Tags,
        string? WatchUrl,
        string? VideoId,
        string? PrivacyStatus,
        string? UploadedAtText,
        string? ScheduledAtText,
        string? ThumbnailFile);

    private static HistoryPublishData ReadHistoryPublishData(string projectPath)
    {
        var publishDir = Path.Combine(projectPath, "09_publish");
        var isUploaded = false;
        string? title = null;
        string? description = null;
        var tags = new List<string>();
        string? watchUrl = null;
        string? videoId = null;
        string? privacy = null;
        string? uploadedAt = null;
        string? scheduledAt = null;
        string? thumbnailFile = null;

        var resultPath = Path.Combine(publishDir, "youtube_upload_result.json");
        if (File.Exists(resultPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(resultPath));
                var root = doc.RootElement;
                isUploaded = string.Equals(StringProperty(root, "status"), "UPLOAD_COMPLETE", StringComparison.OrdinalIgnoreCase);
                videoId = StringProperty(root, "video_id");
                watchUrl = StringProperty(root, "watch_url");
                privacy = StringProperty(root, "privacy_status");
                uploadedAt = FormatIsoTimestampLocal(StringProperty(root, "uploaded_at_utc"));
                scheduledAt = FormatIsoTimestampLocal(StringProperty(root, "scheduled_publish_at") ?? StringProperty(root, "scheduled_publish_at_utc"));
            }
            catch
            {
                // Ignore partial publish result JSON.
            }
        }

        var manifestPath = Path.Combine(publishDir, "youtube_upload_manifest.json");
        if (File.Exists(manifestPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(manifestPath));
                var root = doc.RootElement;
                thumbnailFile = StringProperty(root, "thumbnail_file");
                scheduledAt ??= FormatIsoTimestampLocal(StringProperty(root, "scheduled_publish_at"));
                if (root.TryGetProperty("metadata", out var metadata) &&
                    metadata.TryGetProperty("snippet", out var snippet))
                {
                    title ??= StringProperty(snippet, "title");
                    description ??= StringProperty(snippet, "description")?.Trim();
                    if (snippet.TryGetProperty("tags", out var tagArray) && tagArray.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var element in tagArray.EnumerateArray())
                        {
                            if (element.GetString() is { Length: > 0 } tag && tags.Count < 18)
                                tags.Add(tag);
                        }
                    }
                }
            }
            catch
            {
                // Ignore partial manifest JSON.
            }
        }

        if (string.IsNullOrWhiteSpace(title))
        {
            var titlePath = Path.Combine(publishDir, "title.txt");
            if (File.Exists(titlePath))
            {
                try { title = File.ReadAllText(titlePath).Trim(); } catch { }
            }
        }
        if (string.IsNullOrWhiteSpace(description))
        {
            var descPath = Path.Combine(publishDir, "description.txt");
            if (File.Exists(descPath))
            {
                try { description = File.ReadAllText(descPath).Trim(); } catch { }
            }
        }
        if (tags.Count == 0)
        {
            var tagsPath = Path.Combine(publishDir, "tags.txt");
            if (File.Exists(tagsPath))
            {
                try
                {
                    foreach (var token in File.ReadAllText(tagsPath)
                        .Split([',', '\r', '\n'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
                    {
                        if (tags.Count >= 18) break;
                        tags.Add(token);
                    }
                }
                catch { }
            }
        }

        return new HistoryPublishData(isUploaded, title, description, tags, watchUrl, videoId, privacy, uploadedAt, scheduledAt, thumbnailFile);
    }

    private sealed record HistoryTimingData(
        double DurationSeconds,
        string? QualityStatus,
        Dictionary<string, double> StoryDurationsById,
        Dictionary<int, double> StoryDurationsByOrder);

    private static HistoryTimingData ReadHistoryTimingData(string projectPath)
    {
        var byId = new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);
        var byOrder = new Dictionary<int, double>();
        double totalDuration = 0;
        string? qualityStatus = null;

        var timingPath = Path.Combine(projectPath, "story_timing.json");
        if (!File.Exists(timingPath))
            return new HistoryTimingData(totalDuration, qualityStatus, byId, byOrder);

        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(timingPath));
            var root = doc.RootElement;
            totalDuration = DoubleProperty(root, "duration_seconds");
            if (root.TryGetProperty("quality", out var quality))
                qualityStatus = StringProperty(quality, "status");
            if (root.TryGetProperty("stories", out var stories) && stories.ValueKind == JsonValueKind.Array)
            {
                foreach (var story in stories.EnumerateArray())
                {
                    if (!story.TryGetProperty("narration", out var narration)) continue;
                    var sec = DoubleProperty(narration, "duration");
                    if (sec <= 0) continue;
                    if (StringProperty(story, "story_id") is { Length: > 0 } storyId)
                        byId[storyId] = sec;
                    var order = IntProperty(story, "order");
                    if (order > 0)
                        byOrder[order] = sec;
                }
            }
        }
        catch
        {
            // Ignore partial timing metadata.
        }

        return new HistoryTimingData(totalDuration, qualityStatus, byId, byOrder);
    }

    private sealed record HistoryStateData(
        Dictionary<string, string> States,
        Dictionary<string, string> Details,
        int CueCount,
        int VisualEvents,
        int SfxCount,
        int ArchiveWindows,
        string? RenderStatus);

    private static HistoryStateData ReadHistoryStateData(string projectPath)
    {
        var states = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var details = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        int cues = 0, visualEvents = 0, sfxCount = 0, archiveWindows = 0;
        string? renderStatus = null;

        var statePath = Path.Combine(projectPath, "state.json");
        if (!File.Exists(statePath))
            return new HistoryStateData(states, details, cues, visualEvents, sfxCount, archiveWindows, renderStatus);

        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(statePath));
            if (doc.RootElement.TryGetProperty("stages", out var stages) && stages.ValueKind == JsonValueKind.Object)
            {
                foreach (var prop in stages.EnumerateObject())
                {
                    var key = prop.Name.Equals("storyboard_images", StringComparison.OrdinalIgnoreCase)
                        ? "storyboard"
                        : prop.Name;
                    if (StringProperty(prop.Value, "status") is { Length: > 0 } st)
                        states[key] = st;
                    if (StringProperty(prop.Value, "reason") is { Length: > 0 } reason)
                        details[key] = reason;

                    if (key == "captions")
                        cues = IntProperty(prop.Value, "cue_count");
                    else if (key == "effects")
                    {
                        visualEvents = IntProperty(prop.Value, "visual_event_count");
                        sfxCount = IntProperty(prop.Value, "timeline_sfx_event_count");
                    }
                    else if (key == "archive_visuals")
                        archiveWindows = IntProperty(prop.Value, "archive_window_count");
                    else if (key == "render")
                        renderStatus = StringProperty(prop.Value, "status");
                }
            }
        }
        catch
        {
            // Ignore partial state JSON.
        }

        return new HistoryStateData(states, details, cues, visualEvents, sfxCount, archiveWindows, renderStatus);
    }

    private (string? FirstStoryTitle, string AiSummary, string CurationSummary) PopulateHistoryStories(
        string projectPath,
        HistoryTimingData timing)
    {
        var projectInfoPath = Path.Combine(projectPath, "project_info.json");
        if (!File.Exists(projectInfoPath))
        {
            HistoryStoriesCountText.Text = "0 stories";
            HistoryStoriesEmptyText.Visibility = Visibility.Visible;
            return (null, "—", "No project_info.json");
        }

        string? firstTitle = null;
        var aiSummary = "—";
        var curationSummary = "—";
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(projectInfoPath));
            var root = doc.RootElement;
            var provider = StringProperty(root, "provider");
            var model = StringProperty(root, "model");
            aiSummary = !string.IsNullOrWhiteSpace(provider) && !string.IsNullOrWhiteSpace(model)
                ? $"{FormatTitleToken(provider)} · {model}"
                : model ?? provider ?? "—";

            var candidates = IntProperty(root, "candidate_count");
            var ranked = IntProperty(root, "ranked_count");
            var created = IntProperty(root, "created_count");
            curationSummary = candidates > 0
                ? $"{candidates} scraped · {ranked} ranked · {created} selected"
                : created > 0 ? $"{created} selected stories" : "—";

            var leadStoryId = root.TryGetProperty("lead_story_selection", out var leadSel)
                ? StringProperty(leadSel, "selected_story_id")
                : null;

            if (root.TryGetProperty("stories", out var stories) && stories.ValueKind == JsonValueKind.Array)
            {
                var fallbackOrder = 1;
                foreach (var story in stories.EnumerateArray())
                {
                    var storyId = StringProperty(story, "story_id") ?? "";
                    var order = IntProperty(story, "order");
                    if (order <= 0) order = fallbackOrder;
                    fallbackOrder++;

                    var title = StringProperty(story, "title") ?? $"Story {order}";
                    firstTitle ??= title;
                    var source = StringProperty(story, "source") ?? "Unknown source";
                    var score = IntProperty(story, "score");
                    var words = IntProperty(story, "word_count");
                    var reason = StringProperty(story, "ranking_reason") ?? "";
                    var sourceUrl = StringProperty(story, "source_url") ?? "";
                    var textFile = StringProperty(story, "text") ?? "";
                    var resolvedTextFile = ResolvePortableProjectFile(projectPath, textFile) ?? textFile;

                    double seconds = 0;
                    if (!string.IsNullOrWhiteSpace(storyId))
                        timing.StoryDurationsById.TryGetValue(storyId, out seconds);
                    if (seconds <= 0)
                        timing.StoryDurationsByOrder.TryGetValue(order, out seconds);

                    _selectedHistoryStories.Add(new ProductionStoryDetailViewModel
                    {
                        OrderText = order.ToString("00", CultureInfo.InvariantCulture),
                        Title = title,
                        Source = source,
                        ScoreText = score > 0 ? $"Score {score}" : "Unscored",
                        DurationText = seconds > 0 ? FormatShortAudioTime(TimeSpan.FromSeconds(seconds)) : "—",
                        WordCountText = words > 0 ? $"{words} words" : "—",
                        RankingReason = reason,
                        IsLead = !string.IsNullOrWhiteSpace(leadStoryId) &&
                                 string.Equals(storyId, leadStoryId, StringComparison.Ordinal),
                        SourceUrl = sourceUrl,
                        TextFilePath = resolvedTextFile,
                        HasTextFile = !string.IsNullOrWhiteSpace(resolvedTextFile) && File.Exists(resolvedTextFile),
                    });
                }
            }
        }
        catch
        {
            // Ignore malformed project_info.json.
        }

        HistoryStoriesCountText.Text = _selectedHistoryStories.Count == 1
            ? "1 story"
            : $"{_selectedHistoryStories.Count} stories";
        HistoryStoriesEmptyText.Visibility = _selectedHistoryStories.Count == 0
            ? Visibility.Visible
            : Visibility.Collapsed;

        return (firstTitle, aiSummary, curationSummary);
    }

    private void PopulateHistoryStages(StageEventMetrics metrics, HistoryStateData stateInfo)
    {
        var completedCount = 0;
        double totalSeconds = 0;

        for (var i = 0; i < StageDefinitions.Length; i++)
        {
            var (key, label) = StageDefinitions[i];
            var number = (i + 1).ToString("00", CultureInfo.InvariantCulture);
            metrics.EventStates.TryGetValue(key, out var eventState);
            stateInfo.States.TryGetValue(key, out var rawStateStatus);

            string stateKey;
            if (string.Equals(rawStateStatus, "SKIPPED", StringComparison.OrdinalIgnoreCase))
                stateKey = "SKIPPED";
            else if (!string.IsNullOrWhiteSpace(eventState))
                stateKey = eventState;
            else if (!string.IsNullOrWhiteSpace(rawStateStatus))
                stateKey = "COMPLETE";
            else
                stateKey = "NOT_STARTED";

            if (stateKey is "COMPLETE" or "SKIPPED")
                completedCount++;

            var (statusText, statusBrush, badgeBg) = stateKey switch
            {
                "COMPLETE" => ("Complete", ResourceBrush("SuccessBrush"), ResourceBrush("SuccessTintBrush")),
                "SKIPPED" => ("Skipped", ResourceBrush("TextTertiaryBrush"), ResourceBrush("SurfaceAltBrush")),
                "FAILED" => ("Failed", ResourceBrush("DangerBrush"), ResourceBrush("DangerTintBrush")),
                "WAITING" => ("Waiting", ResourceBrush("WarningBrush"), ResourceBrush("WarningTintBrush")),
                "RUNNING" => ("Running", ResourceBrush("AccentBrush"), ResourceBrush("AccentTintBrush")),
                _ => ("Not run", ResourceBrush("MutedBrush"), ResourceBrush("SurfaceBrush")),
            };

            var elapsedText = "";
            if (metrics.Durations.TryGetValue(key, out var secs) && secs > 0)
            {
                elapsedText = FormatCompactDuration(TimeSpan.FromSeconds(secs));
                totalSeconds += secs;
            }

            metrics.EventDetails.TryGetValue(key, out var detailText);
            if (string.IsNullOrWhiteSpace(detailText))
                stateInfo.Details.TryGetValue(key, out detailText);

            var logPath = metrics.LogFiles.TryGetValue(key, out var foundLog) ? foundLog : null;

            _selectedHistoryStages.Add(new ProductionStageDetailViewModel
            {
                Number = number,
                Label = label,
                StatusText = statusText,
                StatusBrush = statusBrush,
                BadgeBackground = badgeBg,
                ElapsedText = elapsedText,
                DetailText = detailText ?? "",
                LogFilePath = logPath,
                HasLogFile = !string.IsNullOrWhiteSpace(logPath) && File.Exists(logPath),
            });
        }

        HistoryStagesSummaryText.Text = totalSeconds > 0
            ? $"{completedCount} / {StageDefinitions.Length} steps  ·  {FormatCompactDuration(TimeSpan.FromSeconds(totalSeconds))} recorded"
            : $"{completedCount} / {StageDefinitions.Length} steps";
    }

    private (string Primary, string Detail) ReadHistoryTtsSummary(string projectPath)
    {
        var ttsInfoPath = Path.Combine(projectPath, "Sesler", "tts_info.json");
        if (!File.Exists(ttsInfoPath))
            return ("Not synthesized", "No Sesler/tts_info.json found");

        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(ttsInfoPath));
            var root = doc.RootElement;
            var rawProvider = StringProperty(root, "provider") ?? "TTS";
            var provider = rawProvider.StartsWith("qwen", StringComparison.OrdinalIgnoreCase)
                ? "Qwen3-TTS"
                : rawProvider.Contains("eleven", StringComparison.OrdinalIgnoreCase)
                    ? "ElevenLabs"
                    : FormatTitleToken(rawProvider);
            var voice = StringProperty(root, "voice_name") ?? StringProperty(root, "voice_id");
            var isQwen = provider.StartsWith("Qwen", StringComparison.OrdinalIgnoreCase);
            var primary = string.IsNullOrWhiteSpace(voice)
                ? provider
                : $"{provider} · {FormatVoiceDisplay(voice, isQwen)}";

            var totalAudio = DoubleProperty(root, "total_audio_seconds");
            var outputFormat = StringProperty(root, "output_format") ?? "mp3";
            var detailParts = new List<string>();
            if (totalAudio > 0)
                detailParts.Add($"{FormatShortAudioTime(TimeSpan.FromSeconds(totalAudio))} audio");
            if (!string.IsNullOrWhiteSpace(outputFormat))
                detailParts.Add(outputFormat.Replace('_', ' '));
            return (primary, detailParts.Count > 0 ? string.Join(" · ", detailParts) : "Voiceover generated");
        }
        catch
        {
            return ("TTS info unavailable", "Could not read tts_info.json");
        }
    }

    private void PopulateHistoryScriptCard(string scriptPath)
    {
        if (!File.Exists(scriptPath))
        {
            HistoryScriptCard.Visibility = Visibility.Collapsed;
            return;
        }

        try
        {
            var content = File.ReadAllText(scriptPath).Trim();
            if (string.IsNullOrWhiteSpace(content))
            {
                HistoryScriptCard.Visibility = Visibility.Collapsed;
                return;
            }

            _selectedHistoryScriptFullText = content;
            var wordCount = content.Split([' ', '\r', '\n', '\t'], StringSplitOptions.RemoveEmptyEntries).Length;
            const int previewLimit = 2200;
            HistoryScriptMetaText.Text = $"{wordCount} words · {content.Length} characters";
            HistoryScriptPreviewText.Text = content.Length > previewLimit
                ? $"{content[..previewLimit].TrimEnd()}…\n\n[Truncated preview — click Open file for complete script]"
                : content;
            HistoryScriptCard.Visibility = Visibility.Visible;
        }
        catch
        {
            HistoryScriptCard.Visibility = Visibility.Collapsed;
        }
    }

    private string? ResolveHistoryThumbnailPath(string projectPath, string? manifestThumbnailPath)
    {
        var resolvedManifestThumb = ResolvePortableProjectFile(projectPath, manifestThumbnailPath);
        if (!string.IsNullOrWhiteSpace(resolvedManifestThumb) && File.Exists(resolvedManifestThumb))
            return resolvedManifestThumb;

        var publishDir = Path.Combine(projectPath, "09_publish");
        var candidates = new[]
        {
            Path.Combine(publishDir, "thumbnail_final.png"),
            Path.Combine(publishDir, "thumbnail_v1.png"),
            Path.Combine(publishDir, "thumbnail_v2.png"),
            Path.Combine(publishDir, "thumbnail_phone_preview_320x180.png"),
        };
        var match = candidates.FirstOrDefault(File.Exists);
        if (match is not null) return match;

        if (!Directory.Exists(publishDir)) return null;
        return new DirectoryInfo(publishDir)
            .GetFiles("thumbnail_*.png")
            .OrderByDescending(file => file.LastWriteTimeUtc)
            .FirstOrDefault()?.FullName;
    }

    private static BitmapImage? LoadBitmapPreview(string? path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path)) return null;
        try
        {
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.DecodePixelWidth = 640;
            bitmap.UriSource = new Uri(path, UriKind.Absolute);
            bitmap.EndInit();
            bitmap.Freeze();
            return bitmap;
        }
        catch
        {
            return null;
        }
    }

    private static string? FormatIsoTimestampLocal(string? isoTimestamp)
    {
        if (string.IsNullOrWhiteSpace(isoTimestamp)) return null;
        return DateTimeOffset.TryParse(isoTimestamp, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var parsed)
            ? parsed.ToLocalTime().ToString("dd MMM yyyy · HH:mm", CultureInfo.CurrentCulture)
            : null;
    }

    private void HistoryOpenFolder_Click(object sender, RoutedEventArgs e) =>
        OpenPath(_selectedHistoryItem?.ProjectPath);

    private void HistoryOpenVideo_Click(object sender, RoutedEventArgs e) =>
        OpenPath(ResolveFinalVideoPath(_selectedHistoryItem?.ProjectPath));

    private void HistoryOpenScript_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedHistoryItem is null) return;
        OpenPath(Path.Combine(_selectedHistoryItem.ProjectPath, "video_script.txt"));
    }

    private void HistoryOpenYouTube_Click(object sender, RoutedEventArgs e) =>
        OpenPath(_selectedHistoryWatchUrl);

    private void HistoryStorySource_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { DataContext: ProductionStoryDetailViewModel story })
            OpenPath(story.SourceUrl);
    }

    private void HistoryStoryFile_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { DataContext: ProductionStoryDetailViewModel story })
            OpenPath(story.TextFilePath);
    }

    private void HistoryStageLog_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { DataContext: ProductionStageDetailViewModel stage })
            OpenPath(stage.LogFilePath);
    }

    private void HistoryCopyPublishDescription_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_selectedHistoryPublishDescription)) return;
        try
        {
            Clipboard.SetText(_selectedHistoryPublishDescription);
        }
        catch
        {
            // Clipboard may be transiently busy.
        }
    }

    private void HistoryCopyScript_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_selectedHistoryScriptFullText)) return;
        try
        {
            Clipboard.SetText(_selectedHistoryScriptFullText);
        }
        catch
        {
            // Clipboard may be transiently busy.
        }
    }

    private void OpenAutomationFolder_Click(object sender, RoutedEventArgs e) => OpenPath(_automationRoot);

    private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount == 2)
        {
            if (WindowState == WindowState.Maximized)
                SystemCommands.RestoreWindow(this);
            else
                SystemCommands.MaximizeWindow(this);
        }
        else
            DragMove();
    }

    private void MinimizeButton_Click(object sender, RoutedEventArgs e) => SystemCommands.MinimizeWindow(this);
    private void MaximizeButton_Click(object sender, RoutedEventArgs e)
    {
        if (WindowState == WindowState.Maximized)
            SystemCommands.RestoreWindow(this);
        else
            SystemCommands.MaximizeWindow(this);
    }
    private void CloseButton_Click(object sender, RoutedEventArgs e) => Close();

    private void FlashTaskbar()
    {
        var handle = new WindowInteropHelper(this).Handle;
        var info = new FlashInfo
        {
            Size = (uint)Marshal.SizeOf<FlashInfo>(),
            Window = handle,
            Flags = 3 | 12,
            Count = 4,
            Timeout = 0,
        };
        _ = FlashWindowEx(ref info);
    }

    private IntPtr WindowMessageHook(IntPtr window, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (message == WmGetMinMaxInfo)
            handled = ApplyMaximizedBounds(window, lParam);
        return IntPtr.Zero;
    }

    private static bool ApplyMaximizedBounds(IntPtr window, IntPtr minMaxInfoAddress)
    {
        var monitor = MonitorFromWindow(window, MonitorDefaultToNearest);
        if (monitor == IntPtr.Zero) return false;
        var monitorInfo = new MonitorInfo { Size = Marshal.SizeOf<MonitorInfo>() };
        if (!GetMonitorInfo(monitor, ref monitorInfo)) return false;

        var appBar = new AppBarData { Size = (uint)Marshal.SizeOf<AppBarData>() };
        var taskbarAutoHides = (SHAppBarMessage(AbmGetState, ref appBar).ToUInt64() & AbsAutoHide) != 0;
        var target = taskbarAutoHides ? monitorInfo.Monitor : monitorInfo.Work;
        var info = Marshal.PtrToStructure<MinMaxInfo>(minMaxInfoAddress);
        info.MaxPosition.X = target.Left - monitorInfo.Monitor.Left;
        info.MaxPosition.Y = target.Top - monitorInfo.Monitor.Top;
        info.MaxSize.X = target.Right - target.Left;
        info.MaxSize.Y = target.Bottom - target.Top;
        info.MaxTrackSize = info.MaxSize;
        Marshal.StructureToPtr(info, minMaxInfoAddress, false);
        return true;
    }

    private Brush ResourceBrush(string key) => (Brush)Application.Current.Resources[key];
    private static string? StringProperty(JsonElement element, string key) =>
        element.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;
    private static int IntProperty(JsonElement element, string key) =>
        element.TryGetProperty(key, out var value) && value.TryGetInt32(out var result) ? result : 0;
    private static double DoubleProperty(JsonElement element, string key) =>
        element.TryGetProperty(key, out var value) && value.TryGetDouble(out var result) ? result : 0;
    private static bool BoolProperty(JsonElement element, string key) =>
        element.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.True;

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr window, int attribute, ref int value, int size);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool FlashWindowEx(ref FlashInfo info);

    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromWindow(IntPtr window, uint flags);

    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetMonitorInfo(IntPtr monitor, ref MonitorInfo info);

    [DllImport("shell32.dll")]
    private static extern UIntPtr SHAppBarMessage(uint message, ref AppBarData data);

    private const int WmGetMinMaxInfo = 0x0024;
    private const uint MonitorDefaultToNearest = 0x00000002;
    private const uint AbmGetState = 0x00000004;
    private const ulong AbsAutoHide = 0x00000001;

    [StructLayout(LayoutKind.Sequential)]
    private struct NativePoint
    {
        public int X;
        public int Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MinMaxInfo
    {
        public NativePoint Reserved;
        public NativePoint MaxSize;
        public NativePoint MaxPosition;
        public NativePoint MinTrackSize;
        public NativePoint MaxTrackSize;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct NativeRect
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
    private struct MonitorInfo
    {
        public int Size;
        public NativeRect Monitor;
        public NativeRect Work;
        public uint Flags;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct AppBarData
    {
        public uint Size;
        public IntPtr Window;
        public uint CallbackMessage;
        public uint Edge;
        public NativeRect Rect;
        public IntPtr Parameter;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FlashInfo
    {
        public uint Size;
        public IntPtr Window;
        public uint Flags;
        public uint Count;
        public uint Timeout;
    }
}
