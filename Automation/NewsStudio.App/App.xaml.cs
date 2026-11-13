using NewsStudio.App.Services;
using System.Windows;

namespace NewsStudio.App;

public partial class App : Application
{
    public string ActiveTheme { get; private set; } = "light";

    protected override void OnStartup(StartupEventArgs e)
    {
        ActiveTheme = ThemeService.LoadPersistedTheme(AppContext.BaseDirectory);
        ThemeService.ApplyResources(Resources, ActiveTheme);

        base.OnStartup(e);
        MainWindow = new MainWindow();
        MainWindow.Show();
    }

    public void SetActiveTheme(string theme)
    {
        ActiveTheme = ThemeService.Normalize(theme);
        ThemeService.ApplyResources(Resources, ActiveTheme);
    }
}
