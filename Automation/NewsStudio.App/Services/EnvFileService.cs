using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace NewsStudio.App.Services;

public sealed record SettingsCategoryInfo(
    string IconGlyph,
    string Subtitle,
    string Section,
    int Order);

public sealed partial class EnvFileService
{
    private sealed record SettingMetadata(
        string Title,
        string Description,
        string Category,
        string Subgroup,
        int SubgroupOrder,
        int ItemOrder,
        string Unit = "");

    private static readonly HashSet<string> DeprecatedAppPreferenceKeys = new(StringComparer.OrdinalIgnoreCase)
    {
        "NEWS_STUDIO_GUI_CONFIRM_PRODUCTION_ACTION",
        "NEWS_STUDIO_GUI_CONFIRM_RESUME_SECTION",
        "NEWS_STUDIO_GUI_CONFIRM_PROVIDER_REQUESTS",
        "NEWS_STUDIO_GUI_WAIT_FOR_SEPARATORS",
        "NEWS_STUDIO_GUI_WAIT_FOR_AI_VIDEOS",
        "NEWS_STUDIO_GUI_WAIT_FOR_PREVIEW",
        "NEWS_STUDIO_GUI_CONFIRM_YOUTUBE_UPLOAD",
    };

    private static readonly IReadOnlyDictionary<string, SettingsCategoryInfo> CategoryCatalog =
        new Dictionary<string, SettingsCategoryInfo>(StringComparer.OrdinalIgnoreCase)
        {
            ["AI providers"] = new(
                "\uE99A",
                "Primary LLM provider, API keys, models, and reasoning effort.",
                "Workspace & AI",
                10),
            ["Workspace, network & approvals"] = new(
                "\uE774",
                "Project directories, checkpoint timeouts, and network retry behavior.",
                "Workspace & AI",
                20),
            ["News selection & archive"] = new(
                "\uF57E",
                "RSS story filtering, ranking thresholds, script length, and duplicate detection.",
                "News & Script",
                30),
            ["Voiceover & TTS"] = new(
                "\uE720",
                "ElevenLabs and local Qwen3-TTS voices, credentials, and delivery tuning.",
                "Voice & Audio",
                40),
            ["Audio mix & background music"] = new(
                "\uE8D6",
                "Master LUFS loudness, SFX gain, music tracks, and section fades.",
                "Voice & Audio",
                50),
            ["Storyboards & AI images"] = new(
                "\uEB9F",
                "Hero image generation count, storyboard planner, and OpenAI image settings.",
                "Visuals & Media",
                60),
            ["Stock video & scene mix"] = new(
                "\uE714",
                "Motion-graphic ratio, Pexels/Pixabay video retrieval, and AI clip review.",
                "Visuals & Media",
                70),
            ["Media library & Unsplash"] = new(
                "\uE8B9",
                "Local media index rules, window durations, and Unsplash image fallback.",
                "Visuals & Media",
                80),
            ["Manual separators"] = new(
                "\uE8A9",
                "Chapter separator duration, folder opening, and video format validation.",
                "Visuals & Media",
                90),
            ["Effects, captions & overlays"] = new(
                "\uE790",
                "Motion zoom, TV overlay, glitch, sound effect files, and subtitle layout.",
                "Render & Effects",
                100),
            ["Rendering & layout"] = new(
                "\uE7F4",
                "1080p final & preview encoding, worker threads, and on-screen text margins.",
                "Render & Effects",
                110),
            ["Publishing & thumbnails"] = new(
                "\uE8A1",
                "YouTube metadata model and AI thumbnail generation dimensions and style.",
                "Publishing",
                120),
            ["YouTube upload"] = new(
                "\uE898",
                "Publish schedule slots, privacy defaults, OAuth files, and upload retries.",
                "Publishing",
                130),
            ["Custom variables"] = new(
                "\uE74C",
                "User-defined environment variables not listed in .env.example.",
                "Workspace & AI",
                140),
        };

    private static readonly IReadOnlyDictionary<string, SettingMetadata> KnownMetadata = BuildKnownMetadata();

    private readonly string _envPath;
    private readonly string _examplePath;

    public EnvFileService(string automationRoot)
    {
        _envPath = Path.Combine(automationRoot, ".env");
        _examplePath = Path.Combine(automationRoot, ".env.example");
    }

    public static SettingsCategoryInfo GetCategoryInfo(string category)
    {
        if (CategoryCatalog.TryGetValue(category, out var info))
            return info;
        return new SettingsCategoryInfo(
            "\uE713",
            "Additional pipeline environment settings.",
            "Workspace & AI",
            150);
    }

    public IReadOnlyList<EnvSetting> Load()
    {
        var current = ReadAssignments(_envPath);
        var result = new List<EnvSetting>
        {
            new()
            {
                Key = "NEWS_STUDIO_GUI_CHOICE_SOUND",
                Category = "News Studio app",
                Title = "Decision alert sound",
                Description = "Play a Windows alert sound whenever the workflow needs a decision.",
                DefaultValue = "true",
                OriginalValue = current.GetValueOrDefault("NEWS_STUDIO_GUI_CHOICE_SOUND", "true"),
                Value = current.GetValueOrDefault("NEWS_STUDIO_GUI_CHOICE_SOUND", "true"),
                AllowedValues = ["true", "false"],
                IsSynthetic = true,
            },
            new()
            {
                Key = "NEWS_STUDIO_GUI_STAGE_SOUND",
                Category = "News Studio app",
                Title = "Stage transition sound",
                Description = "Play a Windows notification sound when production moves to another stage.",
                DefaultValue = "true",
                OriginalValue = current.GetValueOrDefault("NEWS_STUDIO_GUI_STAGE_SOUND", "true"),
                Value = current.GetValueOrDefault("NEWS_STUDIO_GUI_STAGE_SOUND", "true"),
                AllowedValues = ["true", "false"],
                IsSynthetic = true,
            },
            new()
            {
                Key = "NEWS_STUDIO_GUI_THEME",
                Category = "News Studio app",
                Title = "App color theme",
                Description = "App color mode: system, dark, or light.",
                DefaultValue = "light",
                OriginalValue = current.GetValueOrDefault("NEWS_STUDIO_GUI_THEME", "light"),
                Value = current.GetValueOrDefault("NEWS_STUDIO_GUI_THEME", "light"),
                AllowedValues = ["system", "light", "dark"],
                IsSynthetic = true,
            },
            AppPreference(current, "NEWS_STUDIO_BE_SKEPTICAL", false,
                "Use the stronger skeptical editorial prompt for technology-news summaries."),
            AppPreference(current, "NEWS_STUDIO_GUI_USE_DEFAULT_PRODUCTION_ACTION", false,
                "Create a new Tech News video instead of asking whether to continue the latest video."),
            AppPreference(current, "NEWS_STUDIO_GUI_USE_DEFAULT_RESUME_SECTION", false,
                "Resume from the recommended first unfinished production stage without asking."),
            AppPreference(current, "NEWS_STUDIO_GUI_USE_DEFAULT_PROVIDER_REQUESTS", false,
                "Allow every provider request shown in the cost list without asking."),
            AppPreference(current, "NEWS_STUDIO_GUI_USE_DEFAULT_LEAD_STORY", false,
                "Choose the first, highest-ranked displayed news story as the editorial lead without asking."),
            AppPreference(current, "NEWS_STUDIO_GUI_USE_DEFAULT_SEPARATORS", false,
                "Skip manually created separator videos and use storyboard images instead."),
            AppPreference(current, "NEWS_STUDIO_GUI_USE_DEFAULT_AI_VIDEOS", false,
                "Keep storyboard images instead of waiting for manually supplied AI story videos."),
            AppPreference(current, "NEWS_STUDIO_GUI_USE_DEFAULT_PREVIEW", false,
                "Approve the review preview and start the final render without asking."),
            AppPreference(current, "NEWS_STUDIO_GUI_USE_DEFAULT_YOUTUBE_TITLE", false,
                "Keep the recommended YouTube title without waiting to review or edit it before upload."),
            AppPreference(current, "NEWS_STUDIO_GUI_USE_DEFAULT_YOUTUBE_UPLOAD", false,
                "Upload the completed video package to YouTube without asking."),
        };

        if (!File.Exists(_examplePath)) return result;

        var pendingComments = new List<string>();
        foreach (var rawLine in File.ReadAllLines(_examplePath, Encoding.UTF8))
        {
            var trimmed = rawLine.Trim();
            if (trimmed.StartsWith('#'))
            {
                var comment = trimmed.TrimStart('#').Trim();
                if (string.IsNullOrWhiteSpace(comment) || comment.All(ch => ch == '='))
                    continue;
                if (LooksLikeSection(comment))
                    pendingComments.Clear();
                else
                    pendingComments.Add(comment);
                continue;
            }

            var match = AssignmentRegex().Match(rawLine);
            if (!match.Success)
            {
                if (string.IsNullOrWhiteSpace(trimmed)) pendingComments.Clear();
                continue;
            }

            var key = match.Groups[1].Value;
            var fallback = match.Groups[2].Value;
            var value = current.GetValueOrDefault(key, fallback);
            result.Add(CreateSetting(key, fallback, value, string.Join(" ", pendingComments)));
            pendingComments.Clear();
        }

        foreach (var pair in current)
        {
            if (result.Any(item => item.Key.Equals(pair.Key, StringComparison.OrdinalIgnoreCase)))
                continue;
            if (DeprecatedAppPreferenceKeys.Contains(pair.Key)) continue;
            result.Add(CreateSetting(pair.Key, "", pair.Value, "Present in .env but not listed in .env.example."));
        }
        return result;
    }

    public static EnvSetting CreateSetting(
        string key,
        string defaultValue,
        string value,
        string rawComment = "",
        bool isNew = false,
        bool? forceSecret = null)
    {
        var allowedValues = AllowedValuesForSetting(key, defaultValue, value);
        var isSecret = forceSecret ?? SecretRegex().IsMatch(key);
        var isNumeric = IsNumericSetting(key, defaultValue, value, allowedValues);

        if (KnownMetadata.TryGetValue(key, out var known))
        {
            var categoryInfo = GetCategoryInfo(known.Category);
            return new EnvSetting
            {
                Key = key,
                Category = known.Category,
                Title = known.Title,
                Section = categoryInfo.Section,
                Subgroup = known.Subgroup,
                CategoryOrder = categoryInfo.Order,
                SubgroupOrder = known.SubgroupOrder,
                ItemOrder = known.ItemOrder,
                Unit = known.Unit,
                Description = known.Description,
                DefaultValue = defaultValue,
                OriginalValue = isNew ? "" : value,
                Value = value,
                AllowedValues = allowedValues,
                IsSecret = isSecret,
                IsNumeric = isNumeric,
                IsNew = isNew,
            };
        }

        var fallbackCategory = isNew ? "Custom variables" : FallbackCategoryForKey(key);
        var fallbackInfo = GetCategoryInfo(fallbackCategory);
        var fallbackSubgroup = FallbackSubgroupForKey(fallbackCategory, key);
        return new EnvSetting
        {
            Key = key,
            Category = fallbackCategory,
            Title = TitleCaseSettingName(key),
            Section = fallbackInfo.Section,
            Subgroup = fallbackSubgroup,
            CategoryOrder = fallbackInfo.Order,
            SubgroupOrder = 90,
            ItemOrder = 100,
            Unit = InferUnitFromKey(key),
            Description = EnglishDescription(key, fallbackCategory, rawComment),
            DefaultValue = defaultValue,
            OriginalValue = isNew ? "" : value,
            Value = value,
            AllowedValues = allowedValues,
            IsSecret = isSecret,
            IsNumeric = isNumeric,
            IsNew = isNew,
        };
    }

    public IReadOnlyList<string> Validate(IEnumerable<EnvSetting> settings)
    {
        var errors = new List<string>();
        foreach (var setting in settings)
        {
            if (setting.Value.Contains('\n') || setting.Value.Contains('\r'))
            {
                errors.Add($"{setting.Key}: values cannot contain line breaks.");
                continue;
            }

            var sample = setting.DefaultValue.Trim();
            var value = setting.Value.Trim();
            if (setting.Key.Equals("MOTION_GRAPHIC_TARGET_PERCENT", StringComparison.OrdinalIgnoreCase))
            {
                if (!int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var percent))
                    errors.Add($"{setting.Key}: enter a whole percentage from 0 to 100.");
                else if (percent is < 0 or > 100)
                    errors.Add($"{setting.Key}: enter a percentage from 0 to 100.");
                continue;
            }

            if (setting.AllowedValues.Count > 0)
            {
                if (!setting.AllowedValues.Any(option => option.Equals(value, StringComparison.OrdinalIgnoreCase)))
                    errors.Add($"{setting.Key}: choose one of {string.Join(", ", setting.AllowedValues)}.");
                continue;
            }

            if (sample.Equals("true", StringComparison.OrdinalIgnoreCase) ||
                sample.Equals("false", StringComparison.OrdinalIgnoreCase))
            {
                if (!value.Equals("true", StringComparison.OrdinalIgnoreCase) &&
                    !value.Equals("false", StringComparison.OrdinalIgnoreCase))
                    errors.Add($"{setting.Key}: use true or false.");
            }
            else if (int.TryParse(sample, NumberStyles.Integer, CultureInfo.InvariantCulture, out _) &&
                     !string.IsNullOrEmpty(value) &&
                     !int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out _))
            {
                errors.Add($"{setting.Key}: enter a whole number.");
            }
            else if (double.TryParse(sample, NumberStyles.Float, CultureInfo.InvariantCulture, out _) &&
                     !string.IsNullOrEmpty(value) &&
                     !double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out _))
            {
                errors.Add($"{setting.Key}: enter a number using a dot for decimals.");
            }
        }
        return errors;
    }

    public EnvSaveResult Save(IReadOnlyCollection<EnvSetting> settings)
    {
        var changed = settings.Where(item => item.IsChanged).ToDictionary(
            item => item.Key,
            item => item.Value,
            StringComparer.OrdinalIgnoreCase);
        if (changed.Count == 0) return new EnvSaveResult(false, null, 0);

        var originalText = File.Exists(_envPath)
            ? File.ReadAllText(_envPath, Encoding.UTF8)
            : "";
        var newline = originalText.Contains("\r\n", StringComparison.Ordinal) ? "\r\n" : "\n";
        var hadTrailingNewline = originalText.EndsWith("\n", StringComparison.Ordinal);
        var lines = originalText.Replace("\r\n", "\n", StringComparison.Ordinal).Split('\n').ToList();
        if (hadTrailingNewline && lines.Count > 0 && lines[^1] == "") lines.RemoveAt(lines.Count - 1);

        var updatedKeys = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        for (var index = 0; index < lines.Count; index++)
        {
            var match = AssignmentRegex().Match(lines[index]);
            if (!match.Success) continue;
            var key = match.Groups[1].Value;
            if (!changed.TryGetValue(key, out var value)) continue;
            lines[index] = $"{key}={value}";
            updatedKeys.Add(key);
        }

        var missing = changed.Keys.Where(key => !updatedKeys.Contains(key)).ToList();
        if (missing.Count > 0)
        {
            if (lines.Count > 0 && !string.IsNullOrWhiteSpace(lines[^1])) lines.Add("");
            lines.Add("# News Studio app settings");
            lines.AddRange(missing.Select(key => $"{key}={changed[key]}"));
        }

        var directory = Path.GetDirectoryName(_envPath)!;
        Directory.CreateDirectory(directory);
        string? backup = null;
        if (File.Exists(_envPath))
        {
            var backupDirectory = Path.Combine(directory, "data", "env_backups");
            Directory.CreateDirectory(backupDirectory);
            backup = Path.Combine(
                backupDirectory,
                $".env.backup-{DateTime.Now:yyyyMMdd-HHmmss-fff}");
            File.Copy(_envPath, backup, overwrite: false);
        }

        var temporary = Path.Combine(directory, $".env.tmp-{Guid.NewGuid():N}");
        var output = string.Join(newline, lines) + newline;
        File.WriteAllText(temporary, output, new UTF8Encoding(false));
        File.Move(temporary, _envPath, overwrite: true);
        return new EnvSaveResult(true, backup, changed.Count);
    }

    private static EnvSetting AppPreference(
        IReadOnlyDictionary<string, string> current,
        string key,
        bool defaultValue,
        string description)
    {
        var fallback = defaultValue ? "true" : "false";
        var value = current.GetValueOrDefault(key, fallback);
        return new EnvSetting
        {
            Key = key,
            Category = "News Studio app",
            Title = TitleCaseSettingName(key),
            Description = description,
            DefaultValue = fallback,
            OriginalValue = value,
            Value = value,
            AllowedValues = ["true", "false"],
            IsSynthetic = true,
        };
    }

    public static IReadOnlyList<string> AllowedValuesForSetting(
        string key,
        string defaultValue,
        string currentValue)
    {
        var upperKey = key.ToUpperInvariant();
        IReadOnlyList<string>? explicitOptions = upperKey switch
        {
            "TTS_PROVIDER" => ["elevenlabs", "qwen"],
            "AI_PROVIDER" => ["gemini", "openai"],
            "QWEN_TTS_SPEAKER" => ["ryan", "aiden", "serena", "vivian", "uncle_fu", "ono_anna", "sohee", "eric", "dylan"],
            "QWEN_TTS_LANGUAGE" => ["English", "Chinese", "German", "Italian", "Portuguese", "Spanish", "Japanese", "Korean", "French", "Russian", "Auto"],
            "OPENAI_IMAGE_QUALITY" or "THUMBNAIL_IMAGE_QUALITY" => ["low", "medium", "high"],
            "OPENAI_IMAGE_FORMAT" or "THUMBNAIL_IMAGE_FORMAT" => ["png", "jpeg", "webp"],
            "OPENAI_IMAGE_SIZE" => ["1280x720", "1536x864", "1792x1008", "2048x1152", "2560x1440", "3840x2160"],
            "THUMBNAIL_IMAGE_SIZE" => ["1280x720"],
            "AI_GENERATED_IMAGES_PER_STORY" => ["0", "1", "2", "3", "4"],
            "UNSPLASH_CONTENT_FILTER" => ["low", "high"],
            "RENDER_PRESET" => ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
            "CAPTION_MAX_LINES" => ["1", "2"],
            "YOUTUBE_PRIVACY_STATUS" => ["private", "unlisted", "public"],
            "ELEVENLABS_OUTPUT_FORMAT" => ["mp3_22050_32", "mp3_44100_32", "mp3_44100_64", "mp3_44100_96", "mp3_44100_128", "mp3_44100_192"],
            "SEPARATOR_AUDIO_POLICY" => ["consistent", "require", "forbid", "allow_mixed"],
            _ => null,
        };
        if (explicitOptions is not null)
            return explicitOptions;

        if (upperKey.EndsWith("_REASONING_EFFORT", StringComparison.Ordinal))
            return ["none", "low", "medium", "high", "xhigh", "max"];

        var trimmedDefault = defaultValue.Trim();
        var trimmedCurrent = currentValue.Trim();
        if (trimmedDefault.Equals("true", StringComparison.OrdinalIgnoreCase) ||
            trimmedDefault.Equals("false", StringComparison.OrdinalIgnoreCase) ||
            trimmedCurrent.Equals("true", StringComparison.OrdinalIgnoreCase) ||
            trimmedCurrent.Equals("false", StringComparison.OrdinalIgnoreCase) ||
            upperKey.EndsWith("_ENABLED", StringComparison.Ordinal) ||
            upperKey.EndsWith("_IS_OVERRIDE", StringComparison.Ordinal))
        {
            return ["true", "false"];
        }

        return [];
    }

    public static string NormalizeEnumeratedValue(
        string key,
        string value,
        IReadOnlyList<string> allowedValues)
    {
        if (allowedValues.Count == 0) return value;

        var trimmed = value.Trim();
        if (key.Equals("TTS_PROVIDER", StringComparison.OrdinalIgnoreCase) &&
            trimmed.ToLowerInvariant() is "qwen3" or "qwen_tts" or "local")
        {
            trimmed = "qwen";
        }

        var match = allowedValues.FirstOrDefault(
            option => option.Equals(trimmed, StringComparison.OrdinalIgnoreCase));
        if (match is not null) return match;
        return string.IsNullOrEmpty(trimmed) ? value : value;
    }

    private static bool IsNumericSetting(
        string key,
        string defaultValue,
        string currentValue,
        IReadOnlyList<string> allowedValues)
    {
        if (allowedValues.Count > 0) return false;
        var sample = string.IsNullOrWhiteSpace(defaultValue) ? currentValue.Trim() : defaultValue.Trim();
        if (double.TryParse(sample, NumberStyles.Float, CultureInfo.InvariantCulture, out _))
            return true;
        var upper = key.ToUpperInvariant();
        return upper.EndsWith("_SECONDS", StringComparison.Ordinal) ||
               upper.EndsWith("_HOURS", StringComparison.Ordinal) ||
               upper.EndsWith("_DAYS", StringComparison.Ordinal) ||
               upper.EndsWith("_DB", StringComparison.Ordinal) ||
               upper.EndsWith("_LUFS", StringComparison.Ordinal) ||
               upper.EndsWith("_PERCENT", StringComparison.Ordinal) ||
               upper.EndsWith("_TIMEOUT", StringComparison.Ordinal) ||
               upper.EndsWith("_RETRIES", StringComparison.Ordinal) ||
               upper.EndsWith("_WIDTH", StringComparison.Ordinal) ||
               upper.EndsWith("_HEIGHT", StringComparison.Ordinal) ||
               upper.EndsWith("_FPS", StringComparison.Ordinal) ||
               upper.EndsWith("_CRF", StringComparison.Ordinal) ||
               upper.EndsWith("_WORDS", StringComparison.Ordinal) ||
               upper.EndsWith("_LIMIT", StringComparison.Ordinal) ||
               upper.EndsWith("_WORKERS", StringComparison.Ordinal) ||
               upper.EndsWith("_THREADS", StringComparison.Ordinal) ||
               upper.EndsWith("_BYTES", StringComparison.Ordinal) ||
               upper.EndsWith("_SIZE", StringComparison.Ordinal) && !upper.Contains("IMAGE_SIZE", StringComparison.Ordinal);
    }

    private static Dictionary<string, string> ReadAssignments(string path)
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (!File.Exists(path)) return result;
        foreach (var line in File.ReadAllLines(path, Encoding.UTF8))
        {
            var match = AssignmentRegex().Match(line);
            if (match.Success) result[match.Groups[1].Value] = match.Groups[2].Value;
        }
        return result;
    }

    private static bool LooksLikeSection(string value)
    {
        if (value.Length > 72 || value.EndsWith('.') || value.Contains(':')) return false;
        var letters = value.Where(char.IsLetter).ToArray();
        return letters.Length >= 3 && letters.All(char.IsUpper);
    }

    private static string FallbackCategoryForKey(string key)
    {
        var value = key.ToUpperInvariant();

        if (value is "AI_PROVIDER" or "GEMINI_API_KEY" or "GEMINI_MODEL" or "GEMINI_TTS_MODEL" or
            "OPENAI_API_KEY" or "OPENAI_MODEL" or "OPENAI_NEWS_SELECTION_MODEL" or
            "OPENAI_NEWS_SELECTION_REASONING_EFFORT" or "OPENAI_REASONING_EFFORT")
            return "AI providers";

        if (value is "AUTOMATION_ROOT" or "PROJECTS_ROOT" or "REQUIRE_PREVIEW_APPROVAL" or
            "REQUEST_TIMEOUT" or "API_RETRIES" or "RSS_RETRIES" or "USER_AGENT" ||
            value.StartsWith("AUTOMATION_", StringComparison.Ordinal) ||
            value.StartsWith("NETWORK_", StringComparison.Ordinal))
            return "Workspace, network & approvals";

        if (value == "NEWS_ARCHIVE_PATH" ||
            value.StartsWith("NEWS_DUPLICATE_", StringComparison.Ordinal) ||
            value.StartsWith("RSS_FEED_", StringComparison.Ordinal) ||
            value.StartsWith("NEWS_SELECTION_", StringComparison.Ordinal) ||
            value.StartsWith("NARRATION_", StringComparison.Ordinal) ||
            value is "MAX_CANDIDATES" or "MAX_FEED_ITEMS" or "TOP_N" or "RANKING_RESERVE" or
            "REQUIRE_FULL_TOP_N" or "MIN_SCORE" or "MIN_ARTICLE_CHARS")
            return "News selection & archive";

        if (value.StartsWith("ELEVENLABS_", StringComparison.Ordinal) ||
            value.StartsWith("QWEN_TTS_", StringComparison.Ordinal) ||
            value == "TTS_PROVIDER")
            return "Voiceover & TTS";

        if (value is "VOICEOVER_GAIN_DB" or "SEPARATOR_AUDIO_GAIN_DB" or
            "GLITCH_SFX_GAIN_DB" or "PAPER_SFX_GAIN_DB" or "SHUTTER_SFX_GAIN_DB" or
            "FINAL_TARGET_LUFS" or "FINAL_TRUE_PEAK_DB" or "FIRST_BACKGROUND_MUSIC" ||
            value.StartsWith("BACKGROUND_MUSIC_", StringComparison.Ordinal))
            return "Audio mix & background music";

        if (value.StartsWith("STORYBOARD_", StringComparison.Ordinal) ||
            value.StartsWith("OPENAI_IMAGE_", StringComparison.Ordinal) ||
            value.StartsWith("AI_IMAGE_", StringComparison.Ordinal) ||
            value.StartsWith("MAX_IMAGE_", StringComparison.Ordinal) ||
            value.StartsWith("IMAGE_DUPLICATE_", StringComparison.Ordinal) ||
            value == "AI_GENERATED_IMAGES_PER_STORY")
            return "Storyboards & AI images";

        if (value == "MOTION_GRAPHIC_TARGET_PERCENT" ||
            value.StartsWith("STOCK_VIDEO_", StringComparison.Ordinal) ||
            value.StartsWith("PEXELS_", StringComparison.Ordinal) ||
            value.StartsWith("PIXABAY_", StringComparison.Ordinal))
            return "Stock video & scene mix";

        if (value.StartsWith("MEDIA_", StringComparison.Ordinal) ||
            value.StartsWith("UNSPLASH_", StringComparison.Ordinal))
            return "Media library & Unsplash";

        if (value.StartsWith("SEPARATOR_", StringComparison.Ordinal) ||
            value is "INTER_STORY_GAP_SECONDS" or "AUTO_OPEN_SEPARATOR_FOLDERS")
            return "Manual separators";

        if (value.StartsWith("EFFECTS_PROOF_", StringComparison.Ordinal) ||
            value.StartsWith("RENDER_", StringComparison.Ordinal) ||
            value.StartsWith("PREVIEW_", StringComparison.Ordinal) ||
            value.StartsWith("TITLE_", StringComparison.Ordinal) ||
            value.StartsWith("ATTRIBUTION_", StringComparison.Ordinal))
            return "Rendering & layout";

        if (value.StartsWith("EFFECTS_", StringComparison.Ordinal) ||
            value.StartsWith("TV_FRAME_", StringComparison.Ordinal) ||
            value.StartsWith("IMAGE_TV_EFFECT_", StringComparison.Ordinal) ||
            value.StartsWith("GLITCH_", StringComparison.Ordinal) ||
            value.StartsWith("PAPER_SFX_", StringComparison.Ordinal) ||
            value.StartsWith("SHUTTER_SFX_", StringComparison.Ordinal) ||
            value.StartsWith("CAPTION_", StringComparison.Ordinal) ||
            value.StartsWith("PAGE_FLIP_", StringComparison.Ordinal) ||
            value == "EDITING_REFERENCE_FILE")
            return "Effects, captions & overlays";

        if (value.StartsWith("PUBLISH_", StringComparison.Ordinal) ||
            value.StartsWith("THUMBNAIL_", StringComparison.Ordinal) ||
            value == "AI_GENERATED_THUMBNAIL_ENABLED")
            return "Publishing & thumbnails";

        if (value.StartsWith("YOUTUBE_", StringComparison.Ordinal))
            return "YouTube upload";

        return "Workspace, network & approvals";
    }

    private static string FallbackSubgroupForKey(string category, string key)
    {
        var value = key.ToUpperInvariant();
        if (category == "Stock video & scene mix" && value.StartsWith("PEXELS_", StringComparison.Ordinal))
            return "Pexels overrides";
        if (category == "Custom variables")
            return "User-defined";
        return "Additional settings";
    }

    private static string EnglishDescription(string key, string category, string description)
    {
        if (string.IsNullOrWhiteSpace(description) || LooksTurkish(description))
        {
            var friendly = FriendlySettingName(key);
            if (SecretRegex().IsMatch(key)) return $"Credential used by {category}.";
            if (key.Contains("MODEL", StringComparison.OrdinalIgnoreCase))
                return $"Model used by {category}.";
            if (key.EndsWith("_FILE", StringComparison.OrdinalIgnoreCase) ||
                key.EndsWith("_PATH", StringComparison.OrdinalIgnoreCase) ||
                key.EndsWith("_DIRECTORY", StringComparison.OrdinalIgnoreCase) ||
                key.EndsWith("_ROOT", StringComparison.OrdinalIgnoreCase))
                return $"Path used by {category}.";
            if (key.EndsWith("_ENABLED", StringComparison.OrdinalIgnoreCase))
                return $"Enables or disables {friendly[..^8].TrimEnd()}.";
            if (key.Contains("TIMEOUT", StringComparison.OrdinalIgnoreCase))
                return $"Timeout setting for {category}.";
            return $"Controls {friendly}.";
        }
        return description;
    }

    private static string TitleCaseSettingName(string key)
    {
        var words = key.Split('_', StringSplitOptions.RemoveEmptyEntries);
        var formatted = new List<string>(words.Length);
        for (var i = 0; i < words.Length; i++)
        {
            var word = words[i];
            formatted.Add(word switch
            {
                "AI" or "API" or "ID" or "URL" or "RSS" or "LUFS" or "FPS" or "CRF" or "SFX" or "TV" or "TTS" => word,
                "DB" => "dB",
                "OPENAI" => "OpenAI",
                "ELEVENLABS" => "ElevenLabs",
                "QWEN" => "Qwen",
                "YOUTUBE" => "YouTube",
                "UNSPLASH" => "Unsplash",
                "PEXELS" => "Pexels",
                "PIXABAY" => "Pixabay",
                "OAUTH" => "OAuth",
                _ when i == 0 => char.ToUpperInvariant(word[0]) + word[1..].ToLowerInvariant(),
                _ => word.ToLowerInvariant(),
            });
        }
        return string.Join(" ", formatted);
    }

    private static string FriendlySettingName(string key) => string.Join(
        " ",
        key.Split('_', StringSplitOptions.RemoveEmptyEntries).Select(word => word switch
        {
            "AI" or "API" or "ID" or "URL" or "RSS" or "LUFS" or "FPS" or "CRF" or "SFX" or "TV" or "TTS" => word,
            "DB" => "dB",
            "OPENAI" => "OpenAI",
            "ELEVENLABS" => "ElevenLabs",
            "QWEN" => "Qwen",
            "YOUTUBE" => "YouTube",
            "UNSPLASH" => "Unsplash",
            "PEXELS" => "Pexels",
            "PIXABAY" => "Pixabay",
            _ => word.ToLowerInvariant(),
        }));

    private static string InferUnitFromKey(string key)
    {
        var upper = key.ToUpperInvariant();
        if (upper.EndsWith("_SECONDS", StringComparison.Ordinal) || upper.EndsWith("_TIMEOUT", StringComparison.Ordinal))
            return "sec";
        if (upper.EndsWith("_HOURS", StringComparison.Ordinal)) return "hrs";
        if (upper.EndsWith("_DAYS", StringComparison.Ordinal)) return "days";
        if (upper.EndsWith("_DB", StringComparison.Ordinal)) return "dB";
        if (upper.EndsWith("_LUFS", StringComparison.Ordinal)) return "LUFS";
        if (upper.EndsWith("_PERCENT", StringComparison.Ordinal)) return "%";
        if (upper.EndsWith("_WIDTH", StringComparison.Ordinal) || upper.EndsWith("_HEIGHT", StringComparison.Ordinal))
            return "px";
        if (upper.EndsWith("_FPS", StringComparison.Ordinal)) return "fps";
        if (upper.EndsWith("_BYTES", StringComparison.Ordinal)) return "bytes";
        if (upper.EndsWith("_WORDS", StringComparison.Ordinal)) return "words";
        return "";
    }

    private static bool LooksTurkish(string value)
    {
        if (value.IndexOfAny(['ç', 'Ç', 'ğ', 'Ğ', 'ı', 'İ', 'ö', 'Ö', 'ş', 'Ş', 'ü', 'Ü']) >= 0)
            return true;
        return Regex.IsMatch(
            value,
            @"\b(?:veya|göre|icin|için|kullan|kullanıyorsan|yalnizca|yalnızca|gorsel|görsel|uretim|üretim|ayar|dosya|klasör|haber|ses|süre|geçerli|deger|değer|seçilen|seçilen|daha|olmalı|olduğunda|iken|birinci|ikinci|üçüncü)\b",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
    }

    private static Dictionary<string, SettingMetadata> BuildKnownMetadata()
    {
        var map = new Dictionary<string, SettingMetadata>(StringComparer.OrdinalIgnoreCase);

        void Add(string category, string subgroup, int subgroupOrder, string key, string title, string description, int itemOrder, string unit = "")
            => map[key] = new SettingMetadata(title, description, category, subgroup, subgroupOrder, itemOrder, unit);

        // 1. AI providers
        Add("AI providers", "Provider selection", 1, "AI_PROVIDER",
            "Primary AI provider", "Choose whether Gemini or OpenAI handles primary text generation.", 1);
        Add("AI providers", "Gemini", 2, "GEMINI_API_KEY",
            "Gemini API key", "Secret API key for Google Gemini requests.", 1);
        Add("AI providers", "Gemini", 2, "GEMINI_MODEL",
            "Gemini model", "Default Gemini model ID used for text and reasoning tasks.", 2);
        Add("AI providers", "Gemini", 2, "GEMINI_TTS_MODEL",
            "Gemini TTS model", "Optional Gemini model ID if Gemini speech synthesis is used.", 3);
        Add("AI providers", "OpenAI", 3, "OPENAI_API_KEY",
            "OpenAI API key", "Secret API key for OpenAI chat, reasoning, and image generation.", 1);
        Add("AI providers", "OpenAI", 3, "OPENAI_MODEL",
            "OpenAI primary model", "Default OpenAI model used for summarization and general tasks.", 2);
        Add("AI providers", "OpenAI", 3, "OPENAI_REASONING_EFFORT",
            "OpenAI reasoning effort", "Reasoning depth applied to general OpenAI generation requests.", 3);
        Add("AI providers", "OpenAI", 3, "OPENAI_NEWS_SELECTION_MODEL",
            "News selection model", "OpenAI model dedicated to ranking and selecting candidate news stories.", 4);
        Add("AI providers", "OpenAI", 3, "OPENAI_NEWS_SELECTION_REASONING_EFFORT",
            "News selection reasoning effort", "Reasoning depth used when ranking news candidates.", 5);

        // 2. Workspace, network & approvals
        Add("Workspace, network & approvals", "Paths & storage", 1, "AUTOMATION_ROOT",
            "Automation root directory", "Base folder for code, config, and data; relative paths resolve from here.", 1);
        Add("Workspace, network & approvals", "Paths & storage", 1, "PROJECTS_ROOT",
            "Projects root directory", "Parent folder where Video 1, Video 2, and new project folders are created.", 2);
        Add("Workspace, network & approvals", "Workflow approvals & timeouts", 2, "REQUIRE_PREVIEW_APPROVAL",
            "Require preview approval", "Pause before the final encode so you can review the 60-second proof clip.", 1);
        Add("Workspace, network & approvals", "Workflow approvals & timeouts", 2, "AUTOMATION_DECISION_TIMEOUT_SECONDS",
            "Decision dialog timeout", "Seconds to wait for manual input before choosing the safe default option.", 2, "sec");
        Add("Workspace, network & approvals", "Workflow approvals & timeouts", 2, "AUTOMATION_UPLOAD_TIMEOUT_SECONDS",
            "Upload decision timeout", "Seconds to wait at the YouTube upload prompt before continuing automatically.", 3, "sec");
        Add("Workspace, network & approvals", "Network & retries", 3, "REQUEST_TIMEOUT",
            "HTTP request timeout", "Maximum time to wait for a single web or RSS HTTP response.", 1, "sec");
        Add("Workspace, network & approvals", "Network & retries", 3, "API_RETRIES",
            "General API retries", "Retry attempts for transient API request failures.", 2);
        Add("Workspace, network & approvals", "Network & retries", 3, "RSS_RETRIES",
            "RSS feed retries", "Retry attempts when fetching an RSS feed fails temporarily.", 3);
        Add("Workspace, network & approvals", "Network & retries", 3, "NETWORK_RETRY_INTERVAL_SECONDS",
            "Offline retry interval", "Seconds between connectivity checks when waiting for internet access to return.", 4, "sec");
        Add("Workspace, network & approvals", "Network & retries", 3, "NETWORK_PROBE_TARGETS",
            "Connectivity probe hosts", "Comma-separated host:port endpoints tested after a detected network outage.", 5);
        Add("Workspace, network & approvals", "Network & retries", 3, "USER_AGENT",
            "HTTP User-Agent", "User-Agent header sent when fetching RSS feeds and news articles.", 6);

        // 3. News selection & archive
        Add("News selection & archive", "Editorial ranking & story count", 1, "TOP_N",
            "Stories per episode", "Number of selected news stories included in each video.", 1);
        Add("News selection & archive", "Editorial ranking & story count", 1, "REQUIRE_FULL_TOP_N",
            "Require full story count", "Only mark the news stage complete and save URL history when all TOP_N stories pass.", 2);
        Add("News selection & archive", "Editorial ranking & story count", 1, "MIN_SCORE",
            "Minimum editorial score", "Preferred quality threshold; lower-ranked stories are only used if needed to reach TOP_N.", 3);
        Add("News selection & archive", "Editorial ranking & story count", 1, "MIN_ARTICLE_CHARS",
            "Minimum article length", "Minimum extracted source article length required before summarizing.", 4, "chars");
        Add("News selection & archive", "Editorial ranking & story count", 1, "MAX_CANDIDATES",
            "Maximum RSS candidates", "Upper limit on recent RSS items collected before balancing and AI ranking.", 5);
        Add("News selection & archive", "Editorial ranking & story count", 1, "NEWS_SELECTION_CANDIDATE_COUNT",
            "AI ranking pool size", "Number of publisher-balanced, diverse headlines sent in a single AI ranking call.", 6);
        Add("News selection & archive", "Editorial ranking & story count", 1, "RANKING_RESERVE",
            "Backup candidate reserve", "Extra ranked fallback stories kept in case a top choice fails validation.", 7);
        Add("News selection & archive", "Editorial ranking & story count", 1, "NEWS_SELECTION_TITLE_SIMILARITY_THRESHOLD",
            "Headline similarity cap", "Defer near-duplicate headlines at or above this containment similarity score.", 8);
        Add("News selection & archive", "RSS feeds", 2, "RSS_FEED_TIME_INTERVAL_HOURS",
            "Feed lookback window", "Only RSS items published within this many hours are eligible for selection.", 1, "hrs");
        Add("News selection & archive", "RSS feeds", 2, "MAX_FEED_ITEMS",
            "Max items per feed", "Maximum recent entries read from each individual RSS feed.", 2);
        Add("News selection & archive", "Narration length", 3, "NARRATION_MIN_WORDS",
            "Minimum narration words", "Lower word-count floor enforced for each story narration script.", 1, "words");
        Add("News selection & archive", "Narration length", 3, "NARRATION_TARGET_WORDS",
            "Target narration words", "Ideal word count target given to the script writer per story.", 2, "words");
        Add("News selection & archive", "Narration length", 3, "NARRATION_MAX_WORDS",
            "Maximum narration words", "Upper word-count ceiling enforced by validation and local trimming.", 3, "words");
        Add("News selection & archive", "News archive & duplicate filtering", 4, "NEWS_ARCHIVE_PATH",
            "News archive folder", "Directory storing previously covered stories for cross-episode deduplication.", 1);
        Add("News selection & archive", "News archive & duplicate filtering", 4, "NEWS_DUPLICATE_CANDIDATE_LIMIT",
            "Duplicate AI check limit", "Maximum plausible archived matches forwarded to AI per candidate story.", 2);
        Add("News selection & archive", "News archive & duplicate filtering", 4, "NEWS_DUPLICATE_LOCAL_THRESHOLD",
            "Local similarity threshold", "Minimum free local text similarity score before invoking AI duplicate verification.", 3);
        Add("News selection & archive", "News archive & duplicate filtering", 4, "NEWS_DUPLICATE_AI_MIN_CONFIDENCE",
            "AI duplicate confidence", "Minimum AI confidence required to reject a story as already covered.", 4);
        Add("News selection & archive", "News archive & duplicate filtering", 4, "NEWS_DUPLICATE_AI_TEXT_LIMIT",
            "Duplicate check character cap", "Maximum article characters sent during AI duplicate verification.", 5, "chars");

        // 4. Voiceover & TTS
        Add("Voiceover & TTS", "TTS provider", 1, "TTS_PROVIDER",
            "Voiceover provider", "Choose cloud ElevenLabs API (elevenlabs) or local GPU Qwen3-TTS (qwen).", 1);
        Add("Voiceover & TTS", "ElevenLabs credentials", 2, "ELEVENLABS_API_KEY_1",
            "ElevenLabs API key 1", "Primary ElevenLabs key tried first during voiceover synthesis.", 1);
        Add("Voiceover & TTS", "ElevenLabs credentials", 2, "ELEVENLABS_API_KEY_2",
            "ElevenLabs API key 2", "Second fallback key used when key 1 hits quota or access limits.", 2);
        Add("Voiceover & TTS", "ElevenLabs credentials", 2, "ELEVENLABS_API_KEY_3",
            "ElevenLabs API key 3", "Third fallback key used when earlier keys lack remaining credits.", 3);
        Add("Voiceover & TTS", "ElevenLabs credentials", 2, "ELEVENLABS_ALLOW_CREDIT_POOLING",
            "Pool credits across keys", "Split long stories at sentence boundaries to combine remaining credits across keys.", 4);
        Add("Voiceover & TTS", "ElevenLabs voice & model", 3, "ELEVENLABS_VOICE_ID",
            "ElevenLabs voice ID", "Voice identifier used for ElevenLabs narration synthesis.", 1);
        Add("Voiceover & TTS", "ElevenLabs voice & model", 3, "ELEVENLABS_MODEL_ID",
            "ElevenLabs model ID", "Synthesis model (e.g. eleven_multilingual_v2 or eleven_v3).", 2);
        Add("Voiceover & TTS", "ElevenLabs voice & model", 3, "ELEVENLABS_EMOTIONS_ENABLED",
            "Inline emotion tags", "Allow AI-driven expression tags such as [serious] or [skeptical] on v3/v4 models.", 3);
        Add("Voiceover & TTS", "ElevenLabs voice & model", 3, "ELEVENLABS_OUTPUT_FORMAT",
            "Audio output format", "Encoded MP3 sample rate and bitrate returned by ElevenLabs.", 4);
        Add("Voiceover & TTS", "ElevenLabs voice & model", 3, "ELEVENLABS_IS_OVERRIDE",
            "Force spoken language", "When enabled, sends ELEVENLABS_LANGUAGE_CODE instead of auto-detecting.", 5);
        Add("Voiceover & TTS", "ElevenLabs voice & model", 3, "ELEVENLABS_LANGUAGE_CODE",
            "Language code", "ISO language code (e.g. en) sent when language override is enabled.", 6);
        Add("Voiceover & TTS", "ElevenLabs voice tuning & retries", 4, "ELEVENLABS_SPEED",
            "Speaking speed", "Speech pace multiplier (valid range 0.7 to 1.2; 1.0 is normal speed).", 1);
        Add("Voiceover & TTS", "ElevenLabs voice tuning & retries", 4, "ELEVENLABS_STABILITY",
            "Voice stability", "Lower values (0.0) are more expressive; higher values (1.0) are steadier.", 2);
        Add("Voiceover & TTS", "ElevenLabs voice tuning & retries", 4, "ELEVENLABS_SIMILARITY_BOOST",
            "Similarity boost", "How closely output matches the selected voice profile (0.0 to 1.0).", 3);
        Add("Voiceover & TTS", "ElevenLabs voice tuning & retries", 4, "ELEVENLABS_STYLE",
            "Style exaggeration", "Delivery stylization intensity from 0.0 (natural) to 1.0 (strong).", 4);
        Add("Voiceover & TTS", "ElevenLabs voice tuning & retries", 4, "ELEVENLABS_SPEAKER_BOOST",
            "Speaker boost", "Enhances clarity and vocal presence for the selected ElevenLabs voice.", 5);
        Add("Voiceover & TTS", "ElevenLabs voice tuning & retries", 4, "ELEVENLABS_TIMEOUT",
            "Request timeout", "Maximum seconds to wait for a single ElevenLabs synthesis request.", 6, "sec");
        Add("Voiceover & TTS", "ElevenLabs voice tuning & retries", 4, "ELEVENLABS_RETRIES",
            "API retries per key", "Retry attempts on transient network or server errors before switching keys.", 7);
        Add("Voiceover & TTS", "Qwen local TTS", 5, "QWEN_TTS_SPEAKER",
            "Qwen speaker voice", "Speaker preset used for local Qwen3-TTS synthesis.", 1);
        Add("Voiceover & TTS", "Qwen local TTS", 5, "QWEN_TTS_LANGUAGE",
            "Qwen spoken language", "Target language for local Qwen3-TTS generation.", 2);
        Add("Voiceover & TTS", "Qwen local TTS", 5, "QWEN_TTS_INSTRUCTION",
            "Qwen delivery instruction", "Tone and pacing prompt passed to the local Qwen3-TTS model.", 3);

        // 5. Audio mix & background music
        Add("Audio mix & background music", "Master & voice loudness", 1, "FINAL_TARGET_LUFS",
            "Final master loudness", "Integrated loudness target applied to the complete final mix.", 1, "LUFS");
        Add("Audio mix & background music", "Master & voice loudness", 1, "FINAL_TRUE_PEAK_DB",
            "Final true peak ceiling", "Keep at or below -1.0 dB to avoid digital clipping after encoding.", 2, "dB");
        Add("Audio mix & background music", "Master & voice loudness", 1, "VOICEOVER_GAIN_DB",
            "Voiceover gain", "Relative narration volume adjustment (+/- 1 to 3 dB recommended).", 3, "dB");
        Add("Audio mix & background music", "Master & voice loudness", 1, "SEPARATOR_AUDIO_GAIN_DB",
            "Separator audio gain", "Volume adjustment applied to audio embedded in manual separator clips.", 4, "dB");
        Add("Audio mix & background music", "Sound effects levels", 2, "GLITCH_SFX_GAIN_DB",
            "Glitch SFX gain", "Volume adjustment for glitch sound effects (more negative is quieter).", 1, "dB");
        Add("Audio mix & background music", "Sound effects levels", 2, "PAPER_SFX_GAIN_DB",
            "Paper SFX gain", "Volume adjustment for paper transition sound effects.", 2, "dB");
        Add("Audio mix & background music", "Sound effects levels", 2, "SHUTTER_SFX_GAIN_DB",
            "Shutter SFX gain", "Volume adjustment for camera shutter sound effects.", 3, "dB");
        Add("Audio mix & background music", "Background music mix", 3, "BACKGROUND_MUSIC_ENABLED",
            "Enable background music", "Mix background music beds underneath story narration and separators.", 1);
        Add("Audio mix & background music", "Background music mix", 3, "BACKGROUND_MUSIC_TARGET_LUFS",
            "Music normalization target", "Base loudness tracks are normalized to before ducking gains apply.", 2, "LUFS");
        Add("Audio mix & background music", "Background music mix", 3, "BACKGROUND_MUSIC_SPEECH_GAIN_DB",
            "Music gain during speech", "Reduction applied while narration is playing (more negative is quieter).", 3, "dB");
        Add("Audio mix & background music", "Background music mix", 3, "BACKGROUND_MUSIC_SEPARATOR_GAIN_DB",
            "Music gain during separators", "Reduction applied while a manual separator clip is visible.", 4, "dB");
        Add("Audio mix & background music", "Music library & tracks", 4, "BACKGROUND_MUSIC_DIRECTORY",
            "Music library folder", "Folder containing background music MP3 tracks.", 1);
        Add("Audio mix & background music", "Music library & tracks", 4, "FIRST_BACKGROUND_MUSIC",
            "Story 01 opening track", "Specific music file always assigned to the opening story.", 2);
        Add("Audio mix & background music", "Music library & tracks", 4, "BACKGROUND_MUSIC_FILE",
            "Fixed single music track", "Optional override track; leave blank to pick tracks deterministically.", 3);
        Add("Audio mix & background music", "Music fades & transitions", 5, "BACKGROUND_MUSIC_FADE_SECONDS",
            "Track edge fade", "Fade-in and fade-out duration at music start and end.", 1, "sec");
        Add("Audio mix & background music", "Music fades & transitions", 5, "BACKGROUND_MUSIC_SECTION_FADE_SECONDS",
            "Story transition fade", "Fade-out/in duration when changing tracks between adjacent stories.", 2, "sec");
        Add("Audio mix & background music", "Music fades & transitions", 5, "BACKGROUND_MUSIC_SECTION_SILENCE_SECONDS",
            "Inter-track silence", "Quiet gap between fade-out and the next story's music fade-in.", 3, "sec");
        Add("Audio mix & background music", "Music fades & transitions", 5, "BACKGROUND_MUSIC_LOOP_CROSSFADE_SECONDS",
            "Loop crossfade", "Crossfade duration when a track loops within a longer story.", 4, "sec");

        // 6. Storyboards & AI images
        Add("Storyboards & AI images", "Generation & archive", 1, "AI_GENERATED_IMAGES_PER_STORY",
            "AI images per story", "Number of paid hero images generated for each news story (0 to 4).", 1);
        Add("Storyboards & AI images", "Generation & archive", 1, "MAX_IMAGE_REQUESTS_PER_RUN",
            "Max story image requests", "Safety cap per run; must be at least TOP_N × AI_GENERATED_IMAGES_PER_STORY.", 2);
        Add("Storyboards & AI images", "Generation & archive", 1, "AI_IMAGE_ARCHIVE_DIRECTORY",
            "AI image archive folder", "Directory where generated story hero images are saved and indexed.", 3);
        Add("Storyboards & AI images", "Generation & archive", 1, "IMAGE_DUPLICATE_HAMMING_THRESHOLD",
            "Image duplicate threshold", "Perceptual hash distance below which two images are treated as duplicates.", 4);
        Add("Storyboards & AI images", "Storyboard planning", 2, "STORYBOARD_MODEL",
            "Storyboard planning model", "Model that selects visual moments and writes AI image prompts.", 1);
        Add("Storyboards & AI images", "Storyboard planning", 2, "STORYBOARD_REASONING_EFFORT",
            "Storyboard reasoning effort", "Reasoning depth used when planning visual scenes and prompts.", 2);
        Add("Storyboards & AI images", "Storyboard planning", 2, "STORYBOARD_MAX_SOURCE_CHARACTERS",
            "Max source characters", "Maximum article characters provided to the storyboard planner.", 3, "chars");
        Add("Storyboards & AI images", "Storyboard planning", 2, "STORYBOARD_API_RETRIES",
            "Storyboard API retries", "Retry attempts if storyboard planning fails temporarily.", 4);
        Add("Storyboards & AI images", "OpenAI image model", 3, "OPENAI_IMAGE_MODEL",
            "Image generation model", "OpenAI image model used to render story hero visuals.", 1);
        Add("Storyboards & AI images", "OpenAI image model", 3, "OPENAI_IMAGE_QUALITY",
            "Image quality", "Rendering quality tier (low, medium, or high) for generated story images.", 2);
        Add("Storyboards & AI images", "OpenAI image model", 3, "OPENAI_IMAGE_SIZE",
            "Image resolution", "Target pixel dimensions for generated 16:9 story images.", 3);
        Add("Storyboards & AI images", "OpenAI image model", 3, "OPENAI_IMAGE_FORMAT",
            "Image file format", "Output image encoding format (png, jpeg, or webp).", 4);
        Add("Storyboards & AI images", "OpenAI image model", 3, "OPENAI_IMAGE_TIMEOUT",
            "Image generation timeout", "Maximum seconds to wait for a single image generation response.", 5, "sec");

        // 7. Stock video & scene mix
        Add("Stock video & scene mix", "Providers & scene mix", 1, "STOCK_VIDEO_ENABLED",
            "Enable stock video search", "Use Pexels and Pixabay stock video clips during visual assembly.", 1);
        Add("Stock video & scene mix", "Providers & scene mix", 1, "PEXELS_VIDEO_ENABLED",
            "Use Pexels Videos", "Include Pexels as a primary stock video source.", 2);
        Add("Stock video & scene mix", "Providers & scene mix", 1, "PIXABAY_VIDEO_ENABLED",
            "Use Pixabay Videos", "Include Pixabay as a secondary stock video source.", 3);
        Add("Stock video & scene mix", "Providers & scene mix", 1, "MOTION_GRAPHIC_TARGET_PERCENT",
            "Motion graphics share", "Target percentage of visual scenes rendered as animations instead of video.", 4, "%");
        Add("Stock video & scene mix", "API credentials", 2, "PEXELS_API_KEY",
            "Pexels API key", "Secret API key for Pexels stock video searches.", 1);
        Add("Stock video & scene mix", "API credentials", 2, "PIXABAY_API_KEY",
            "Pixabay API key", "Secret API key for Pixabay stock video searches.", 2);
        Add("Stock video & scene mix", "AI query & preview verification", 3, "STOCK_VIDEO_QUERY_MODEL",
            "Search query model", "AI model that converts narration clauses into concise 2–6 word search phrases.", 1);
        Add("Stock video & scene mix", "AI query & preview verification", 3, "STOCK_VIDEO_QUERY_REASONING_EFFORT",
            "Query reasoning effort", "Reasoning depth for generating stock video search queries.", 2);
        Add("Stock video & scene mix", "AI query & preview verification", 3, "STOCK_VIDEO_REVIEW_MODEL",
            "Preview review model", "Vision model that inspects candidate video thumbnails before download.", 3);
        Add("Stock video & scene mix", "AI query & preview verification", 3, "STOCK_VIDEO_REVIEW_REASONING_EFFORT",
            "Review reasoning effort", "Reasoning depth used when scoring candidate video previews.", 4);
        Add("Stock video & scene mix", "AI query & preview verification", 3, "STOCK_VIDEO_REVIEW_CANDIDATE_LIMIT",
            "Candidates reviewed per slot", "Maximum preview candidates visually verified per timed scene.", 5);
        Add("Stock video & scene mix", "AI query & preview verification", 3, "STOCK_VIDEO_MIN_REVIEW_CONFIDENCE",
            "Minimum review confidence", "Confidence threshold required before downloading a stock video clip.", 6);
        Add("Stock video & scene mix", "Search & download limits", 4, "STOCK_VIDEO_MAX_DOWNLOADS_PER_VIDEO",
            "Max downloads per episode", "Ceiling on downloaded stock clips per provider for one video.", 1);
        Add("Stock video & scene mix", "Search & download limits", 4, "STOCK_VIDEO_MAX_SEARCHES_PER_PROVIDER",
            "Max searches per provider", "Ceiling on search API calls per provider for one video.", 2);
        Add("Stock video & scene mix", "Search & download limits", 4, "STOCK_VIDEO_CANDIDATE_LIMIT",
            "Candidate pool limit", "Maximum filtered candidates retained from a search response.", 3);
        Add("Stock video & scene mix", "Search & download limits", 4, "STOCK_VIDEO_REQUEST_RESULT_LIMIT",
            "Results per API page", "Number of results requested in each stock video search call.", 4);
        Add("Stock video & scene mix", "Search & download limits", 4, "STOCK_VIDEO_REQUEST_INTERVAL",
            "Request pacing interval", "Minimum delay between consecutive stock video API requests.", 5, "sec");
        Add("Stock video & scene mix", "Search & download limits", 4, "STOCK_VIDEO_TIMEOUT",
            "Request timeout", "Timeout for stock video search and file download requests.", 6, "sec");
        Add("Stock video & scene mix", "Search & download limits", 4, "STOCK_VIDEO_LOCK_TIMEOUT",
            "Provider lock timeout", "Wait limit for cross-process rate-limit coordination.", 7, "sec");
        Add("Stock video & scene mix", "Quality filters & storage", 5, "STOCK_VIDEO_MIN_WIDTH",
            "Minimum clip width", "Reject stock videos narrower than this pixel width.", 1, "px");
        Add("Stock video & scene mix", "Quality filters & storage", 5, "STOCK_VIDEO_MIN_HEIGHT",
            "Minimum clip height", "Reject stock videos shorter than this pixel height.", 2, "px");
        Add("Stock video & scene mix", "Quality filters & storage", 5, "STOCK_VIDEO_MAX_SOURCE_DURATION",
            "Max source clip duration", "Skip source clips longer than this duration to avoid huge downloads.", 3, "sec");
        Add("Stock video & scene mix", "Quality filters & storage", 5, "STOCK_VIDEO_MAX_DOWNLOAD_BYTES",
            "Max clip file size", "Maximum allowed file size when downloading a stock video.", 4, "bytes");
        Add("Stock video & scene mix", "Quality filters & storage", 5, "PEXELS_VIDEO_DESTINATION",
            "Pexels save folder", "Optional folder override; blank uses Assets/Media/Online Retrieved.", 5);
        Add("Stock video & scene mix", "Quality filters & storage", 5, "PIXABAY_VIDEO_DESTINATION",
            "Pixabay save folder", "Optional folder override; blank uses Assets/Media/Online Retrieved.", 6);

        // 8. Media library & Unsplash
        Add("Media library & Unsplash", "Local media selection", 1, "MEDIA_SELECTION_MODEL",
            "Archive selection model", "AI model that matches timed story segments to local archive images.", 1);
        Add("Media library & Unsplash", "Local media selection", 1, "MEDIA_SELECTION_REASONING_EFFORT",
            "Selection reasoning effort", "Reasoning depth for picking the best local image from the shortlist.", 2);
        Add("Media library & Unsplash", "Local media selection", 1, "MEDIA_MIN_AI_CONFIDENCE",
            "Minimum AI confidence", "Reject low-confidence local matches and fall back to online providers.", 3);
        Add("Media library & Unsplash", "Local media selection", 1, "MEDIA_MIN_LOCAL_SCORE",
            "Minimum local lexical score", "Minimum keyword score required for an image to enter the shortlist.", 4);
        Add("Media library & Unsplash", "Local media selection", 1, "MEDIA_SHORTLIST_SIZE",
            "Shortlist size", "Number of local candidate images sent to the AI selector per window.", 5);
        Add("Media library & Unsplash", "Local media selection", 1, "MEDIA_MAX_USES_PER_VIDEO",
            "Max reuses per video", "Maximum number of visual windows a single media file can appear in.", 6);
        Add("Media library & Unsplash", "Local media selection", 1, "MEDIA_MIN_WINDOW_SECONDS",
            "Minimum visual window", "Shortest allowed duration for a single visual segment.", 7, "sec");
        Add("Media library & Unsplash", "Local media selection", 1, "MEDIA_TARGET_WINDOW_SECONDS",
            "Target visual window", "Ideal duration aimed for when splitting narration into visual beats.", 8, "sec");
        Add("Media library & Unsplash", "Local media selection", 1, "MEDIA_MAX_WINDOW_SECONDS",
            "Maximum visual window", "Longest duration allowed before splitting into a new visual segment.", 9, "sec");
        Add("Media library & Unsplash", "Local index & dimensions", 2, "MEDIA_INDEX_PATH",
            "Media index file", "Path to the JSON index built from Assets/Media folders.", 1);
        Add("Media library & Unsplash", "Local index & dimensions", 2, "MEDIA_INDEX_WORKERS",
            "Indexing worker threads", "Concurrent workers used while scanning and hashing local media files.", 2);
        Add("Media library & Unsplash", "Local index & dimensions", 2, "MEDIA_MIN_WIDTH",
            "Minimum image width", "Ignore local archive images narrower than this pixel width.", 3, "px");
        Add("Media library & Unsplash", "Local index & dimensions", 2, "MEDIA_MIN_HEIGHT",
            "Minimum image height", "Ignore local archive images shorter than this pixel height.", 4, "px");
        Add("Media library & Unsplash", "Local index & dimensions", 2, "MEDIA_CROP_RETENTION_THRESHOLD",
            "Aspect-ratio crop threshold", "Minimum retained area ratio allowed when fitting an image to 16:9.", 5);
        Add("Media library & Unsplash", "Local index & dimensions", 2, "MEDIA_COPY_SELECTED",
            "Copy selected images", "False keeps immutable source path + hash instead of copying archive images.", 6);
        Add("Media library & Unsplash", "Local index & dimensions", 2, "MEDIA_SELECTION_TIMEOUT",
            "Selection timeout", "Maximum seconds to wait for an AI media selection request.", 7, "sec");
        Add("Media library & Unsplash", "Local index & dimensions", 2, "MEDIA_SELECTION_API_RETRIES",
            "Selection API retries", "Retry attempts when media selection requests fail temporarily.", 8);
        Add("Media library & Unsplash", "Unsplash fallback & search", 3, "UNSPLASH_FALLBACK_ENABLED",
            "Enable Unsplash fallback", "Search Unsplash photos when both video providers and local media miss.", 1);
        Add("Media library & Unsplash", "Unsplash fallback & search", 3, "UNSPLASH_API_KEY",
            "Unsplash API key", "Secret Access Key for Unsplash photo search fallback.", 2);
        Add("Media library & Unsplash", "Unsplash fallback & search", 3, "UNSPLASH_QUERY_MODEL",
            "Unsplash query model", "AI model that crafts short photo search queries for Unsplash.", 3);
        Add("Media library & Unsplash", "Unsplash fallback & search", 3, "UNSPLASH_QUERY_REASONING_EFFORT",
            "Query reasoning effort", "Reasoning depth when generating Unsplash search queries.", 4);
        Add("Media library & Unsplash", "Unsplash fallback & search", 3, "UNSPLASH_CONTENT_FILTER",
            "Content safety filter", "Unsplash content filter strictness (low or high).", 5);
        Add("Media library & Unsplash", "Unsplash fallback & search", 3, "UNSPLASH_MIN_RELEVANCE_SCORE",
            "Minimum relevance score", "Minimum ranking score required before accepting an Unsplash photo.", 6);
        Add("Media library & Unsplash", "Unsplash limits & dimensions", 4, "UNSPLASH_MAX_DOWNLOADS_PER_VIDEO",
            "Max downloads per episode", "Maximum Unsplash photos downloaded during a single episode.", 1);
        Add("Media library & Unsplash", "Unsplash limits & dimensions", 4, "UNSPLASH_MAX_SEARCHES_PER_VIDEO",
            "Max searches per episode", "Maximum Unsplash search requests allowed during a single episode.", 2);
        Add("Media library & Unsplash", "Unsplash limits & dimensions", 4, "UNSPLASH_SEARCH_LIMIT",
            "Search candidates limit", "Number of candidate photos evaluated from an Unsplash search.", 3);
        Add("Media library & Unsplash", "Unsplash limits & dimensions", 4, "UNSPLASH_REQUEST_RESULT_LIMIT",
            "Results per API call", "Page size requested from the Unsplash search endpoint.", 4);
        Add("Media library & Unsplash", "Unsplash limits & dimensions", 4, "UNSPLASH_MIN_WIDTH",
            "Minimum photo width", "Reject Unsplash photos narrower than this width.", 5, "px");
        Add("Media library & Unsplash", "Unsplash limits & dimensions", 4, "UNSPLASH_MIN_HEIGHT",
            "Minimum photo height", "Reject Unsplash photos shorter than this height.", 6, "px");
        Add("Media library & Unsplash", "Unsplash limits & dimensions", 4, "UNSPLASH_MAX_DOWNLOAD_BYTES",
            "Max photo file size", "Maximum allowed file size when downloading an Unsplash photo.", 7, "bytes");
        Add("Media library & Unsplash", "Unsplash limits & dimensions", 4, "UNSPLASH_REQUEST_INTERVAL",
            "Request pacing interval", "Minimum delay between consecutive Unsplash API requests.", 8, "sec");
        Add("Media library & Unsplash", "Unsplash limits & dimensions", 4, "UNSPLASH_TIMEOUT",
            "Request timeout", "Maximum seconds to wait for an Unsplash API or download response.", 9, "sec");
        Add("Media library & Unsplash", "Unsplash limits & dimensions", 4, "UNSPLASH_LOCK_TIMEOUT",
            "Provider lock timeout", "Wait limit for cross-process Unsplash rate-limit coordination.", 10, "sec");

        // 9. Manual separators
        Add("Manual separators", "Workflow & timing", 1, "AUTO_OPEN_SEPARATOR_FOLDERS",
            "Auto-open separator folders", "Open Haberler and 03_manual_separators automatically when pausing for separators.", 1);
        Add("Manual separators", "Workflow & timing", 1, "SEPARATOR_DURATION_SECONDS",
            "Reserved separator duration", "Planned intro separator duration reserved before reading actual clip lengths.", 2, "sec");
        Add("Manual separators", "Workflow & timing", 1, "INTER_STORY_GAP_SECONDS",
            "Inter-story gap", "Pause duration inserted between consecutive news chapters.", 3, "sec");
        Add("Manual separators", "Workflow & timing", 1, "SEPARATOR_MIN_DURATION_SECONDS",
            "Minimum clip duration", "Shortest valid duration accepted for a manual separator video.", 4, "sec");
        Add("Manual separators", "Format & quality rules", 2, "SEPARATOR_AUDIO_POLICY",
            "Separator audio policy", "Audio stream requirement across manual separator clips.", 1);
        Add("Manual separators", "Format & quality rules", 2, "SEPARATOR_WIDTH",
            "Required video width", "Expected horizontal resolution of manual separator exports.", 2, "px");
        Add("Manual separators", "Format & quality rules", 2, "SEPARATOR_HEIGHT",
            "Required video height", "Expected vertical resolution of manual separator exports.", 3, "px");
        Add("Manual separators", "Format & quality rules", 2, "SEPARATOR_FPS",
            "Required frame rate", "Expected frames per second for manual separator exports.", 4, "fps");
        Add("Manual separators", "Format & quality rules", 2, "SEPARATOR_FPS_TOLERANCE",
            "Frame rate tolerance", "Allowed FPS deviation from SEPARATOR_FPS during validation.", 5, "fps");
        Add("Manual separators", "Format & quality rules", 2, "SEPARATOR_ALLOWED_CODECS",
            "Allowed video codecs", "Comma-separated list of accepted codecs (e.g. h264,hevc).", 6);
        Add("Manual separators", "Format & quality rules", 2, "SEPARATOR_ALLOWED_PIXEL_FORMATS",
            "Allowed pixel formats", "Comma-separated list of accepted pixel formats (e.g. yuv420p).", 7);

        // 10. Effects, captions & overlays
        Add("Effects, captions & overlays", "Motion & composition", 1, "EFFECTS_WIDTH",
            "Effects canvas width", "Working canvas width used by the effects planner.", 1, "px");
        Add("Effects, captions & overlays", "Motion & composition", 1, "EFFECTS_HEIGHT",
            "Effects canvas height", "Working canvas height used by the effects planner.", 2, "px");
        Add("Effects, captions & overlays", "Motion & composition", 1, "EFFECTS_FPS",
            "Effects frame rate", "Frame rate used for motion animation timing.", 3, "fps");
        Add("Effects, captions & overlays", "Motion & composition", 1, "EFFECTS_MOTION_SCALE",
            "Standard zoom scale", "Smooth center push-in scale applied to standard visual clips.", 4);
        Add("Effects, captions & overlays", "Motion & composition", 1, "EFFECTS_HERO_MOTION_SCALE",
            "Hero image zoom scale", "Subtle push-in scale applied to AI hero storyboard images.", 5);
        Add("Effects, captions & overlays", "Motion & composition", 1, "EFFECTS_MAX_VISUAL_HOLD_SECONDS",
            "Max visual hold", "Maximum time a single still visual stays on screen without changing.", 6, "sec");
        Add("Effects, captions & overlays", "Motion & composition", 1, "EDITING_REFERENCE_FILE",
            "Editing reference screenshot", "Reference image used to verify layer placement and composition.", 7);
        Add("Effects, captions & overlays", "Overlays, page flip & glitch", 2, "IMAGE_TV_EFFECT_ENABLED",
            "Enable TV overlay on images", "Composite the subtle alpha TV scanline overlay onto still images.", 1);
        Add("Effects, captions & overlays", "Overlays, page flip & glitch", 2, "IMAGE_TV_EFFECT_FILE",
            "TV overlay MOV file", "Alpha-channel overlay video applied over still images.", 2);
        Add("Effects, captions & overlays", "Overlays, page flip & glitch", 2, "IMAGE_TV_EFFECT_OPACITY",
            "TV overlay opacity", "Opacity of the TV overlay effect (0.0 transparent to 1.0 opaque).", 3);
        Add("Effects, captions & overlays", "Overlays, page flip & glitch", 2, "PAGE_FLIP_ENABLED",
            "Enable page-flip transition", "Play a page-flip transition at chapter starts when manual separators are skipped.", 4);
        Add("Effects, captions & overlays", "Overlays, page flip & glitch", 2, "PAGE_FLIP_DURATION_SECONDS",
            "Page-flip duration", "Duration of the chapter-start page-flip transition.", 5, "sec");
        Add("Effects, captions & overlays", "Overlays, page flip & glitch", 2, "GLITCH_ENABLED",
            "Enable procedural glitch", "Adds brief procedural noise/dropout glitches between scenes.", 6);
        Add("Effects, captions & overlays", "Overlays, page flip & glitch", 2, "GLITCH_DURATION_SECONDS",
            "Glitch duration", "Length of each procedural glitch burst.", 7, "sec");
        Add("Effects, captions & overlays", "Overlays, page flip & glitch", 2, "GLITCH_MAX_PER_MINUTE",
            "Max glitches per minute", "Maximum frequency of procedural glitch effects.", 8);
        Add("Effects, captions & overlays", "Overlays, page flip & glitch", 2, "GLITCH_MINIMUM_SPACING_SECONDS",
            "Minimum glitch spacing", "Minimum quiet interval required between two glitches.", 9, "sec");
        Add("Effects, captions & overlays", "Overlays, page flip & glitch", 2, "GLITCH_NOISE_STRENGTH",
            "Glitch noise strength", "Visual intensity of the procedural noise distortion.", 10);
        Add("Effects, captions & overlays", "Sound effect files", 3, "PAPER_SFX_ENABLED",
            "Enable paper SFX", "Play the paper transition sound at chapter boundaries.", 1);
        Add("Effects, captions & overlays", "Sound effect files", 3, "PAPER_SFX_FILE",
            "Paper SFX audio file", "Path to the paper transition sound effect file.", 2);
        Add("Effects, captions & overlays", "Sound effect files", 3, "SHUTTER_SFX_ENABLED",
            "Enable shutter SFX", "Play the camera shutter sound on visual transitions.", 3);
        Add("Effects, captions & overlays", "Sound effect files", 3, "SHUTTER_SFX_FILE",
            "Shutter SFX audio file", "Path to the camera shutter sound effect file.", 4);
        Add("Effects, captions & overlays", "Captions", 4, "CAPTION_MAX_LINES",
            "Max lines per subtitle", "Maximum stacked lines displayed at once in burned-in captions.", 1);
        Add("Effects, captions & overlays", "Captions", 4, "CAPTION_MAX_WORDS",
            "Max words per caption", "Maximum number of words grouped into a single caption event.", 2, "words");
        Add("Effects, captions & overlays", "Captions", 4, "CAPTION_MAX_CHARACTERS_PER_LINE",
            "Max characters per line", "Line-wrap character limit for ASS/SRT subtitles.", 3, "chars");
        Add("Effects, captions & overlays", "Captions", 4, "CAPTION_MIN_DURATION_SECONDS",
            "Minimum caption duration", "Shortest display duration enforced for a single subtitle block.", 4, "sec");
        Add("Effects, captions & overlays", "Captions", 4, "CAPTION_MARGIN_VERTICAL",
            "Caption bottom margin", "Vertical distance from the bottom edge of the frame to subtitles.", 5, "px");

        // 11. Rendering & layout
        Add("Rendering & layout", "Final video encoding", 1, "RENDER_WIDTH",
            "Final video width", "Horizontal output resolution of the final rendered MP4.", 1, "px");
        Add("Rendering & layout", "Final video encoding", 1, "RENDER_HEIGHT",
            "Final video height", "Vertical output resolution of the final rendered MP4.", 2, "px");
        Add("Rendering & layout", "Final video encoding", 1, "RENDER_FPS",
            "Final frame rate", "Target output frames per second for FFmpeg encoding.", 3, "fps");
        Add("Rendering & layout", "Final video encoding", 1, "RENDER_PRESET",
            "x264 encoder preset", "Speed vs. compression efficiency preset used by FFmpeg.", 4);
        Add("Rendering & layout", "Final video encoding", 1, "RENDER_FINAL_CRF",
            "Final video CRF", "Constant Rate Factor for the final master (lower is higher quality).", 5);
        Add("Rendering & layout", "Final video encoding", 1, "RENDER_SEGMENT_CRF",
            "Segment intermediate CRF", "Constant Rate Factor used for pre-rendered visual segments.", 6);
        Add("Rendering & layout", "Preview & proof encoding", 2, "PREVIEW_WIDTH",
            "Preview video width", "Horizontal resolution for the 60-second review preview.", 1, "px");
        Add("Rendering & layout", "Preview & proof encoding", 2, "PREVIEW_HEIGHT",
            "Preview video height", "Vertical resolution for the 60-second review preview.", 2, "px");
        Add("Rendering & layout", "Preview & proof encoding", 2, "RENDER_PREVIEW_CRF",
            "Preview video CRF", "Constant Rate Factor for the fast review preview clip.", 3);
        Add("Rendering & layout", "Preview & proof encoding", 2, "EFFECTS_PROOF_WIDTH",
            "Effects proof width", "Resolution width for low-cost effects proof renders.", 4, "px");
        Add("Rendering & layout", "Preview & proof encoding", 2, "EFFECTS_PROOF_HEIGHT",
            "Effects proof height", "Resolution height for low-cost effects proof renders.", 5, "px");
        Add("Rendering & layout", "Performance & cleanup", 3, "RENDER_SEGMENT_WORKERS",
            "Concurrent segment workers", "Number of visual segments encoded in parallel.", 1);
        Add("Rendering & layout", "Performance & cleanup", 3, "RENDER_VIDEO_FILTER_THREADS",
            "FFmpeg filter threads", "Thread count passed to FFmpeg video filter graphs.", 2);
        Add("Rendering & layout", "Performance & cleanup", 3, "RENDER_CLEANUP_INTERMEDIATES",
            "Clean up intermediate clips", "Delete temporary segment files after the final video and preview pass QA.", 3);
        Add("Rendering & layout", "Story titles & attribution", 4, "TITLE_FONT_SIZE",
            "Story title font size", "Font size of the upper-left story headline overlay.", 1, "px");
        Add("Rendering & layout", "Story titles & attribution", 4, "TITLE_MARGIN_LEFT",
            "Story title left margin", "Horizontal offset for the on-screen story headline.", 2, "px");
        Add("Rendering & layout", "Story titles & attribution", 4, "TITLE_MARGIN_TOP",
            "Story title top margin", "Vertical offset from the top edge for the story headline.", 3, "px");
        Add("Rendering & layout", "Story titles & attribution", 4, "ATTRIBUTION_FONT_SIZE",
            "Source credit font size", "Font size of the bottom-right media attribution text.", 4, "px");
        Add("Rendering & layout", "Story titles & attribution", 4, "ATTRIBUTION_MARGIN_RIGHT",
            "Source credit right margin", "Horizontal distance from the right edge for media credits.", 5, "px");
        Add("Rendering & layout", "Story titles & attribution", 4, "ATTRIBUTION_MARGIN_BOTTOM",
            "Source credit bottom margin", "Vertical distance from the bottom edge for media credits.", 6, "px");

        // 12. Publishing & thumbnails
        Add("Publishing & thumbnails", "Metadata generation", 1, "PUBLISH_MODEL",
            "Publishing metadata model", "AI model that writes YouTube titles, descriptions, tags, and thumbnail text.", 1);
        Add("Publishing & thumbnails", "Metadata generation", 1, "PUBLISH_REASONING_EFFORT",
            "Publishing reasoning effort", "Reasoning depth used when generating publishing copy.", 2);
        Add("Publishing & thumbnails", "Metadata generation", 1, "PUBLISH_TIMEOUT",
            "Publishing API timeout", "Maximum seconds to wait for a metadata generation call.", 3, "sec");
        Add("Publishing & thumbnails", "Metadata generation", 1, "PUBLISH_API_RETRIES",
            "Publishing API retries", "Retry attempts if metadata generation fails temporarily.", 4);
        Add("Publishing & thumbnails", "AI thumbnail generation", 2, "AI_GENERATED_THUMBNAIL_ENABLED",
            "Generate AI thumbnail", "Create a dedicated YouTube thumbnail image independently of story images.", 1);
        Add("Publishing & thumbnails", "AI thumbnail generation", 2, "THUMBNAIL_IMAGE_MODEL",
            "Thumbnail image model", "OpenAI image model used to generate the YouTube thumbnail.", 2);
        Add("Publishing & thumbnails", "AI thumbnail generation", 2, "THUMBNAIL_IMAGE_QUALITY",
            "Thumbnail image quality", "Quality tier (low, medium, or high) for the generated thumbnail.", 3);
        Add("Publishing & thumbnails", "AI thumbnail generation", 2, "THUMBNAIL_IMAGE_SIZE",
            "Thumbnail image size", "Requested pixel dimensions for the generated thumbnail.", 4);
        Add("Publishing & thumbnails", "AI thumbnail generation", 2, "THUMBNAIL_IMAGE_FORMAT",
            "Thumbnail file format", "Image format returned for the generated thumbnail.", 5);
        Add("Publishing & thumbnails", "Thumbnail dimensions & font", 3, "THUMBNAIL_WIDTH",
            "Final thumbnail width", "Output pixel width of the composited YouTube thumbnail.", 1, "px");
        Add("Publishing & thumbnails", "Thumbnail dimensions & font", 3, "THUMBNAIL_HEIGHT",
            "Final thumbnail height", "Output pixel height of the composited YouTube thumbnail.", 2, "px");
        Add("Publishing & thumbnails", "Thumbnail dimensions & font", 3, "THUMBNAIL_PHONE_WIDTH",
            "Phone preview width", "Downscaled preview width used to check mobile thumbnail readability.", 3, "px");
        Add("Publishing & thumbnails", "Thumbnail dimensions & font", 3, "THUMBNAIL_PHONE_HEIGHT",
            "Phone preview height", "Downscaled preview height used to check mobile thumbnail readability.", 4, "px");
        Add("Publishing & thumbnails", "Thumbnail dimensions & font", 3, "THUMBNAIL_FONT_FILE",
            "Thumbnail headline font", "TrueType font file used to render headline text on the thumbnail.", 5);

        // 13. YouTube upload
        Add("YouTube upload", "Schedule & privacy", 1, "YOUTUBE_PRIVACY_STATUS",
            "Default privacy status", "YouTube visibility applied when uploading or scheduling a video.", 1);
        Add("YouTube upload", "Schedule & privacy", 1, "YOUTUBE_PUBLISH_HOUR",
            "Scheduled publish hour", "Target local hour (GMT+3) for the next open YouTube publish slot.", 2);
        Add("YouTube upload", "Schedule & privacy", 1, "YOUTUBE_PUBLISH_INTERVAL_DAYS",
            "Schedule spacing", "Days to advance when a scheduled slot is already occupied on YouTube.", 3, "days");
        Add("YouTube upload", "Schedule & privacy", 1, "YOUTUBE_CATEGORY_ID",
            "YouTube category ID", "Numeric YouTube video category ID (28 = Science & Technology).", 4);
        Add("YouTube upload", "Schedule & privacy", 1, "YOUTUBE_DEFAULT_LANGUAGE",
            "Default video language", "ISO language code attached to uploaded YouTube video metadata.", 5);
        Add("YouTube upload", "Schedule & privacy", 1, "YOUTUBE_NOTIFY_SUBSCRIBERS",
            "Notify subscribers", "Send subscriber notifications when the uploaded video becomes public.", 6);
        Add("YouTube upload", "OAuth & state files", 2, "YOUTUBE_CLIENT_SECRETS_FILE",
            "OAuth client secrets JSON", "Google Cloud Desktop OAuth client credentials file path.", 1);
        Add("YouTube upload", "OAuth & state files", 2, "YOUTUBE_TOKEN_FILE",
            "OAuth refresh token file", "Cached YouTube authorization token path created by youtube_authorize.bat.", 2);
        Add("YouTube upload", "OAuth & state files", 2, "YOUTUBE_SCHEDULE_STATE_FILE",
            "Schedule state file", "JSON file tracking the local YouTube upload schedule state.", 3);
        Add("YouTube upload", "Upload reliability", 3, "YOUTUBE_UPLOAD_CHUNK_SIZE",
            "Resumable chunk size", "Upload chunk size in bytes for resumable YouTube video transfers.", 1, "bytes");
        Add("YouTube upload", "Upload reliability", 3, "YOUTUBE_UPLOAD_MAX_RETRIES",
            "Upload max retries", "Maximum retry attempts for resumable YouTube upload chunks.", 2);

        return map;
    }

    [GeneratedRegex(@"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")]
    private static partial Regex AssignmentRegex();

    [GeneratedRegex(@"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", RegexOptions.IgnoreCase)]
    private static partial Regex SecretRegex();
}
