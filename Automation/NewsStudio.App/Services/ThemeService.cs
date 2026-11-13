using Microsoft.Win32;
using System.IO;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Media;

namespace NewsStudio.App.Services;

public static partial class ThemeService
{
    public static string Normalize(string? theme) => theme?.Trim().ToLowerInvariant() switch
    {
        "dark" => "dark",
        "system" => "system",
        _ => "light",
    };

    public static string LoadPersistedTheme(string startDirectory)
    {
        var cursor = new DirectoryInfo(startDirectory);
        while (cursor is not null)
        {
            if (File.Exists(Path.Combine(cursor.FullName, "create_video.bat")) &&
                File.Exists(Path.Combine(cursor.FullName, "src", "news_studio.py")))
            {
                var envPath = Path.Combine(cursor.FullName, ".env");
                if (!File.Exists(envPath)) return "light";
                foreach (var line in File.ReadLines(envPath))
                {
                    var match = ThemeAssignmentRegex().Match(line);
                    if (match.Success) return Normalize(match.Groups[1].Value);
                }
                return "light";
            }
            cursor = cursor.Parent;
        }
        return "light";
    }

    public static bool IsDark(string theme) => Normalize(theme) switch
    {
        "dark" => true,
        "system" => SystemUsesDarkTheme(),
        _ => false,
    };

    public static void ApplyResources(ResourceDictionary resources, string theme)
    {
        var colors = IsDark(theme)
            ? DarkColors
            : LightColors;
        foreach (var pair in colors)
        {
            var color = (Color)ColorConverter.ConvertFromString(pair.Value);
            if (resources[pair.Key] is SolidColorBrush brush && !brush.IsFrozen)
                brush.Color = color;
            else
                resources[pair.Key] = new SolidColorBrush(color);
        }
    }

    private static bool SystemUsesDarkTheme()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
            return key?.GetValue("AppsUseLightTheme") is int value && value == 0;
        }
        catch
        {
            return false;
        }
    }

    private static readonly IReadOnlyDictionary<string, string> DarkColors = new Dictionary<string, string>
    {
        ["WindowBackgroundBrush"] = "#ED1B1E26", ["NavigationBrush"] = "#DD12141B",
        ["SurfaceBrush"] = "#F2262932", ["SurfaceAltBrush"] = "#EA1E2129",
        ["SurfaceHoverBrush"] = "#FF323640", ["StrokeBrush"] = "#2EFFFFFF",
        ["StrokeStrongBrush"] = "#45FFFFFF", ["TextPrimaryBrush"] = "#FFF7F7F8",
        ["TextSecondaryBrush"] = "#FFB8BAC3", ["TextTertiaryBrush"] = "#FF898C97",
        ["AccentBrush"] = "#FF60A5FA", ["AccentHoverBrush"] = "#FF7CB6FC",
        ["AccentTextBrush"] = "#FF07111F", ["SuccessBrush"] = "#FF4ADE80",
        ["WarningBrush"] = "#FFFBBF24", ["DangerBrush"] = "#FFFB7185", ["MutedBrush"] = "#FF5E6470",
        ["AccentTintBrush"] = "#2260A5FA", ["SuccessTintBrush"] = "#1E4ADE80",
        ["WarningTintBrush"] = "#22FBBF24", ["DangerTintBrush"] = "#22FB7185",
    };

    private static readonly IReadOnlyDictionary<string, string> LightColors = new Dictionary<string, string>
    {
        ["WindowBackgroundBrush"] = "#EAF3F8FB", ["NavigationBrush"] = "#DFF6F9FB",
        ["SurfaceBrush"] = "#F8FFFFFF", ["SurfaceAltBrush"] = "#EAF3F5F7",
        ["SurfaceHoverBrush"] = "#FFE8ECEF", ["StrokeBrush"] = "#1F000000",
        ["StrokeStrongBrush"] = "#36000000", ["TextPrimaryBrush"] = "#FF1B1A19",
        ["TextSecondaryBrush"] = "#FF5D5A58", ["TextTertiaryBrush"] = "#FF7A7774",
        ["AccentBrush"] = "#FF0F6CBD", ["AccentHoverBrush"] = "#FF115EA3",
        ["AccentTextBrush"] = "#FFFFFFFF", ["SuccessBrush"] = "#FF0F7B0F",
        ["WarningBrush"] = "#FF9D5D00", ["DangerBrush"] = "#FFC42B1C", ["MutedBrush"] = "#FF8A8886",
        ["AccentTintBrush"] = "#160F6CBD", ["SuccessTintBrush"] = "#140F7B0F",
        ["WarningTintBrush"] = "#169D5D00", ["DangerTintBrush"] = "#16C42B1C",
    };

    [GeneratedRegex(@"^\s*NEWS_STUDIO_GUI_THEME\s*=\s*([^#\r\n]*?)\s*(?:#.*)?$", RegexOptions.IgnoreCase)]
    private static partial Regex ThemeAssignmentRegex();
}
