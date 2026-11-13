using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;

namespace NewsStudio.App;

public sealed class StageViewModel : INotifyPropertyChanged
{
    private string _stateKey = "NOT_STARTED";
    private string _status = "Not started";
    private Brush _statusBrush = Brushes.Gray;
    private string _glyph = "\uE915";
    private string _elapsedText = "";
    private string _detailTooltip = "";
    private string? _logFilePath;
    private Cursor _rowCursor = Cursors.Arrow;
    private Brush _rowBackground = Brushes.Transparent;
    private Brush _rowBorderBrush = Brushes.Transparent;
    private Brush _badgeBackground = Brushes.Transparent;
    private Brush _badgeForeground = Brushes.Gray;
    private Brush _statusPillBackground = Brushes.Transparent;
    private Brush _statusPillBorderBrush = Brushes.Transparent;
    private FontWeight _labelWeight = FontWeights.Normal;

    public required string Key { get; init; }
    public required string Number { get; init; }
    public required string Label { get; init; }
    public string StateKey { get => _stateKey; set => Set(ref _stateKey, value); }
    public string Status { get => _status; set => Set(ref _status, value); }
    public Brush StatusBrush { get => _statusBrush; set => Set(ref _statusBrush, value); }
    public string Glyph { get => _glyph; set => Set(ref _glyph, value); }
    public string ElapsedText
    {
        get => _elapsedText;
        set
        {
            if (Set(ref _elapsedText, value))
                OnPropertyChanged(nameof(HasElapsed));
        }
    }
    public bool HasElapsed => !string.IsNullOrWhiteSpace(_elapsedText);
    public string DetailTooltip { get => _detailTooltip; set => Set(ref _detailTooltip, value); }
    public string? LogFilePath
    {
        get => _logFilePath;
        set
        {
            if (Set(ref _logFilePath, value))
                RowCursor = !string.IsNullOrWhiteSpace(value) ? Cursors.Hand : Cursors.Arrow;
        }
    }
    public Cursor RowCursor { get => _rowCursor; set => Set(ref _rowCursor, value); }
    public Brush RowBackground { get => _rowBackground; set => Set(ref _rowBackground, value); }
    public Brush RowBorderBrush { get => _rowBorderBrush; set => Set(ref _rowBorderBrush, value); }
    public Brush BadgeBackground { get => _badgeBackground; set => Set(ref _badgeBackground, value); }
    public Brush BadgeForeground { get => _badgeForeground; set => Set(ref _badgeForeground, value); }
    public Brush StatusPillBackground { get => _statusPillBackground; set => Set(ref _statusPillBackground, value); }
    public Brush StatusPillBorderBrush { get => _statusPillBorderBrush; set => Set(ref _statusPillBorderBrush, value); }
    public FontWeight LabelWeight { get => _labelWeight; set => Set(ref _labelWeight, value); }
    public DateTimeOffset? StartedAtUtc { get; set; }

    public event PropertyChangedEventHandler? PropertyChanged;

    private bool Set<T>(ref T field, T value, [CallerMemberName] string? propertyName = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return false;
        field = value;
        OnPropertyChanged(propertyName);
        return true;
    }

    private void OnPropertyChanged([CallerMemberName] string? propertyName = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
}

public sealed class ActivityLogEntry : INotifyPropertyChanged
{
    private Brush _tagBackground = Brushes.Transparent;
    private Brush _tagBorderBrush = Brushes.Transparent;
    private Brush _tagForeground = Brushes.Gray;
    private Brush _messageForeground = Brushes.Gray;

    public required string Timestamp { get; init; }
    public required string Tag { get; init; }
    public required string Message { get; init; }
    public required string Category { get; init; }
    public required string TagKind { get; init; }
    public required string RawLine { get; init; }

    public Brush TagBackground { get => _tagBackground; set => Set(ref _tagBackground, value); }
    public Brush TagBorderBrush { get => _tagBorderBrush; set => Set(ref _tagBorderBrush, value); }
    public Brush TagForeground { get => _tagForeground; set => Set(ref _tagForeground, value); }
    public Brush MessageForeground { get => _messageForeground; set => Set(ref _messageForeground, value); }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void Set<T>(ref T field, T value, [CallerMemberName] string? propertyName = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return;
        field = value;
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
    }
}

public sealed class EnvSetting
{
    public required string Key { get; init; }
    public required string Category { get; init; }
    public string Title { get; init; } = "";
    public string Section { get; init; } = "Workspace & AI";
    public string Subgroup { get; init; } = "General";
    public int CategoryOrder { get; init; } = 100;
    public int SubgroupOrder { get; init; } = 100;
    public int ItemOrder { get; init; } = 100;
    public string Unit { get; init; } = "";
    public string Description { get; init; } = "";
    public string DefaultValue { get; init; } = "";
    public string OriginalValue { get; set; } = "";
    public string Value { get; set; } = "";
    public IReadOnlyList<string> AllowedValues { get; init; } = [];
    public bool IsSecret { get; init; }
    public bool IsSynthetic { get; init; }
    public bool IsNumeric { get; init; }
    public bool IsNew { get; set; }
    public bool IsBoolean =>
        AllowedValues.Count == 2 &&
        AllowedValues.Contains("true", StringComparer.OrdinalIgnoreCase) &&
        AllowedValues.Contains("false", StringComparer.OrdinalIgnoreCase);
    public bool IsChanged => IsNew || !string.Equals(Value, OriginalValue, StringComparison.Ordinal);
}

public sealed record EnvSaveResult(bool Saved, string? BackupPath, int ChangedCount);

public sealed class ProviderQuotaViewModel : INotifyPropertyChanged
{
    private Brush _statusBrush = Brushes.Gray;
    private Brush _statusTintBrush = Brushes.Transparent;

    public required string Id { get; init; }
    public required string Name { get; init; }
    public required string Primary { get; init; }
    public required string Detail { get; init; }
    public required string DashboardUrl { get; init; }
    public string StatusKey { get; init; } = "not_configured";
    public string StatusLabel { get; init; } = "Checking";
    public string CategoryLabel { get; init; } = "Provider";
    public string ProviderGlyph { get; init; } = "\uE946";
    public string DashboardDomain { get; init; } = "";
    public bool HasDashboardUrl => !string.IsNullOrWhiteSpace(DashboardUrl);
    public bool IsConfigured => !string.Equals(StatusKey, "not_configured", StringComparison.OrdinalIgnoreCase);
    public Brush StatusBrush { get => _statusBrush; set => Set(ref _statusBrush, value); }
    public Brush StatusTintBrush { get => _statusTintBrush; set => Set(ref _statusTintBrush, value); }
    public required string StatusGlyph { get; init; }
    public double MeterPercent { get; init; }
    public bool HasMeter { get; init; }
    public string MeterText => HasMeter ? $"{Math.Round(MeterPercent):0}%" : "";

    public event PropertyChangedEventHandler? PropertyChanged;

    private void Set<T>(ref T field, T value, [CallerMemberName] string? propertyName = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return;
        field = value;
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
    }
}

public sealed class ProductionTimerLogViewModel : INotifyPropertyChanged
{
    private string _duration = "00:00:00";

    public required string VideoName { get; init; }
    public int VideoNumber { get; init; }
    public string ProjectPath { get; init; } = "";
    public string Headline { get; init; } = "";
    public required string Date { get; init; }
    public required string Status { get; init; }
    public bool IsFinished => string.Equals(Status, "Finished", StringComparison.OrdinalIgnoreCase);
    public string UploadBadgeText { get; init; } = "";
    public bool HasUploadBadge => !string.IsNullOrWhiteSpace(UploadBadgeText);
    public Brush UploadBadgeBackground { get; init; } = Brushes.Transparent;
    public Brush UploadBadgeForeground { get; init; } = Brushes.Gray;
    public required Brush StatusBackground { get; init; }
    public required Brush StatusBorder { get; init; }
    public required Brush StatusForeground { get; init; }
    public bool IsRunning { get; init; }
    public DateTimeOffset StartedAtUtc { get; init; }
    public string Duration { get => _duration; set => Set(ref _duration, value); }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void Set<T>(ref T field, T value, [CallerMemberName] string? propertyName = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return;
        field = value;
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
    }
}

public sealed class ProductionStoryDetailViewModel
{
    public required string OrderText { get; init; }
    public required string Title { get; init; }
    public required string Source { get; init; }
    public required string ScoreText { get; init; }
    public required string DurationText { get; init; }
    public required string WordCountText { get; init; }
    public required string RankingReason { get; init; }
    public bool HasRankingReason => !string.IsNullOrWhiteSpace(RankingReason);
    public bool IsLead { get; init; }
    public string SourceUrl { get; init; } = "";
    public bool HasSourceUrl => !string.IsNullOrWhiteSpace(SourceUrl);
    public string TextFilePath { get; init; } = "";
    public bool HasTextFile { get; init; }
}

public sealed class ProductionStageDetailViewModel
{
    public required string Number { get; init; }
    public required string Label { get; init; }
    public required string StatusText { get; init; }
    public required Brush StatusBrush { get; init; }
    public required Brush BadgeBackground { get; init; }
    public string ElapsedText { get; init; } = "";
    public bool HasElapsed => !string.IsNullOrWhiteSpace(ElapsedText);
    public string DetailText { get; init; } = "";
    public bool HasDetail => !string.IsNullOrWhiteSpace(DetailText);
    public string? LogFilePath { get; init; }
    public bool HasLogFile { get; init; }
}

public sealed class ChannelProfileOptionViewModel
{
    public required string Key { get; init; }
    public required string ChannelName { get; init; }
    public required string CategoryLabel { get; init; }
    public required string Subtitle { get; init; }
    public ImageSource? AvatarImage { get; init; }
    public bool HasAvatarImage => AvatarImage is not null;
    public string FallbackGlyph { get; init; } = "\uE714";
    public bool IsActive { get; init; }
    public bool IsAvailable { get; init; }
    public string BadgeText { get; init; } = "";
    public Brush BadgeBackground { get; init; } = Brushes.Transparent;
    public Brush BadgeForeground { get; init; } = Brushes.Gray;
}
