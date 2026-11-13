using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace NewsStudio.App.Controls;

public sealed class SecretValueEditor : Grid
{
    private readonly PasswordBox _password = new();
    private readonly TextBox _text = new();
    private readonly Button _toggle = new();
    private readonly TextBlock _toggleIcon = new();
    private readonly TextBlock _toggleLabel = new();
    private bool _revealed;
    private bool _suppressEvents;

    public event EventHandler? ValueChanged;

    public SecretValueEditor(string value)
    {
        ColumnDefinitions.Add(new ColumnDefinition());
        ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        _password.Password = value;
        _password.Height = 36;
        _password.PasswordChanged += (_, _) =>
        {
            if (!_suppressEvents && !_revealed)
                ValueChanged?.Invoke(this, EventArgs.Empty);
        };

        _text.Text = value;
        _text.Style = (Style)Application.Current.Resources["InputStyle"];
        _text.Height = 36;
        _text.Visibility = Visibility.Collapsed;
        _text.TextChanged += (_, _) =>
        {
            if (!_suppressEvents && _revealed)
                ValueChanged?.Invoke(this, EventArgs.Empty);
        };

        _toggleIcon.Text = "\uE7B3";
        _toggleIcon.FontFamily = new FontFamily("Segoe Fluent Icons");
        _toggleIcon.FontSize = 12;
        _toggleIcon.VerticalAlignment = VerticalAlignment.Center;
        _toggleIcon.Margin = new Thickness(0, 0, 6, 0);

        _toggleLabel.Text = "Show";
        _toggleLabel.FontSize = 12;
        _toggleLabel.FontWeight = FontWeights.SemiBold;
        _toggleLabel.VerticalAlignment = VerticalAlignment.Center;

        var toggleContent = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            VerticalAlignment = VerticalAlignment.Center,
        };
        toggleContent.Children.Add(_toggleIcon);
        toggleContent.Children.Add(_toggleLabel);

        _toggle.Content = toggleContent;
        _toggle.Style = (Style)Application.Current.Resources["SecondaryButtonStyle"];
        _toggle.Height = 36;
        _toggle.Padding = new Thickness(11, 0, 11, 0);
        _toggle.Margin = new Thickness(8, 0, 0, 0);
        _toggle.ToolTip = "Reveal or hide secret value";
        _toggle.Click += (_, _) => Toggle();

        SetColumn(_password, 0);
        SetColumn(_text, 0);
        SetColumn(_toggle, 1);
        Children.Add(_password);
        Children.Add(_text);
        Children.Add(_toggle);
    }

    public string Value
    {
        get => _revealed ? _text.Text : _password.Password;
        set
        {
            _suppressEvents = true;
            try
            {
                _password.Password = value;
                _text.Text = value;
            }
            finally
            {
                _suppressEvents = false;
            }
        }
    }

    private void Toggle()
    {
        _suppressEvents = true;
        try
        {
            if (_revealed)
            {
                _password.Password = _text.Text;
                _text.Visibility = Visibility.Collapsed;
                _password.Visibility = Visibility.Visible;
                _toggleIcon.Text = "\uE7B3";
                _toggleLabel.Text = "Show";
            }
            else
            {
                _text.Text = _password.Password;
                _password.Visibility = Visibility.Collapsed;
                _text.Visibility = Visibility.Visible;
                _toggleIcon.Text = "\uED1A";
                _toggleLabel.Text = "Hide";
            }
            _revealed = !_revealed;
        }
        finally
        {
            _suppressEvents = false;
        }
    }
}
