using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace OptionHelper.Windows;

internal static class Program
{
    [STAThread]
    private static async Task Main(string[] args)
    {
        if (args.Contains("--check-webview2", StringComparer.Ordinal))
        {
            try
            {
                Console.WriteLine(CoreWebView2Environment.GetAvailableBrowserVersionString());
            }
            catch (Exception error)
            {
                Console.Error.WriteLine($"WebView2 Runtime不可用：{error.Message}");
                Environment.ExitCode = 1;
            }
            return;
        }
        ApplicationConfiguration.Initialize();
        var resources = Path.Combine(AppContext.BaseDirectory, "Resources");
        var backend = Path.Combine(resources, "backend", "OptionHelperBackend", "OptionHelperBackend.exe");
        if (!File.Exists(backend))
        {
            MessageBox.Show("找不到内置App Host。", "OptionHelper", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        var state = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "OptionHelper", "local-state");
        Directory.CreateDirectory(state);
        var initializationToken = Guid.NewGuid().ToString("N");
        using var process = new Process
        {
            StartInfo = new ProcessStartInfo(backend, $"--host 127.0.0.1 --port 0 --data-dir \"{state}\" --resource-dir \"{resources}\" --initialization-token {initializationToken}")
            {
                WorkingDirectory = Path.GetDirectoryName(backend)!,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            }
        };
        var started = false;
        Task<string>? backendErrorTask = null;
        try
        {
            process.Start();
            started = true;
            backendErrorTask = process.StandardError.ReadToEndAsync();
            var url = await WaitForUrl(process, TimeSpan.FromSeconds(45));
            Application.Run(new MainForm(url, state, initializationToken));
        }
        catch (Exception error)
        {
            if (started && !process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                process.WaitForExit(5000);
            }
            var detail = error.Message;
            if (backendErrorTask is not null)
            {
                var backendError = (await backendErrorTask).Trim();
                if (backendError.Length > 4000)
                    backendError = backendError[^4000..];
                if (!string.IsNullOrWhiteSpace(backendError))
                    detail += $"\n\n后端错误：\n{backendError}";
            }
            MessageBox.Show($"App Host未能启动。\n{detail}", "OptionHelper", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        finally
        {
            if (started && !process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                process.WaitForExit(5000);
            }
        }
    }

    private static async Task<string> WaitForUrl(Process process, TimeSpan timeout)
    {
        using var cancellation = new CancellationTokenSource(timeout);
        while (!process.HasExited && !cancellation.Token.IsCancellationRequested)
        {
            var line = await process.StandardOutput.ReadLineAsync(cancellation.Token);
            if (line?.StartsWith("OPTIONHELPER_URL=http://127.0.0.1:") == true)
                return line["OPTIONHELPER_URL=".Length..];
        }
        throw new InvalidOperationException("后端未在45秒内提供本机地址。");
    }
}

internal sealed class MainForm : Form
{
    private const int WmSysCommand = 0x0112;
    private const int ScaleUpCommand = 0x1F10;
    private const int ScaleDownCommand = 0x1F20;
    private const int ScaleResetCommand = 0x1F30;
    private const uint MfString = 0x0000;
    private const uint MfSeparator = 0x0800;
    private const uint MfByCommand = 0x0000;
    private const uint MfEnabled = 0x0000;
    private const uint MfGrayed = 0x0001;
    private const int DwmwaUseImmersiveDarkMode = 20;
    private const int DwmwaSystemBackdropType = 38;
    private const int DwmsbtMainWindow = 2;

    private static readonly double[] ScaleSteps = [0.8, 0.9, 1.0, 1.1, 1.25, 1.4];
    private readonly WebView2 browser = new() { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.Transparent };
    private readonly string uiPreferencesPath;
    private readonly string initializationToken;
    private readonly SemaphoreSlim uiScaleScriptLock = new(1, 1);
    private double uiScale;
    private bool webViewReady;
    private bool systemMenuInstalled;
    private bool nativeBackdropEnabled;
    private string surfaceTheme = "light";
    private string? uiScaleScriptId;

    internal MainForm(string url, string stateDirectory, string initializationToken)
    {
        Text = "OptionHelper";
        Width = 1320;
        Height = 860;
        KeyPreview = true;
        BackColor = Color.FromArgb(247, 247, 247);
        uiPreferencesPath = Path.Combine(stateDirectory, "ui-preferences.json");
        this.initializationToken = initializationToken;
        uiScale = UIScalePreferences.Load(uiPreferencesPath, ScaleSteps);
        Controls.Add(browser);
        Shown += async (_, _) =>
        {
            try
            {
                await browser.EnsureCoreWebView2Async();
                browser.CoreWebView2.Settings.IsZoomControlEnabled = false;
                browser.ZoomFactor = uiScale;
                await UpdateScaleBootstrapScript();
                browser.CoreWebView2.WebMessageReceived += HandleWebMessage;
                browser.CoreWebView2.NavigationCompleted += (_, _) => SyncScaleToPage();
                webViewReady = true;
                browser.CoreWebView2.Navigate(url);
            }
            catch (Exception error)
            {
                MessageBox.Show(
                    $"WebView2 Runtime不可用，OptionHelper无法显示界面。\n请安装Microsoft Edge WebView2 Runtime后重试。\n\n{error.Message}",
                    "OptionHelper",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                Close();
            }
        };
    }

    protected override void OnHandleCreated(EventArgs eventArgs)
    {
        base.OnHandleCreated(eventArgs);
        nativeBackdropEnabled = TryEnableSystemBackdrop();
        ApplySurfaceTheme(surfaceTheme);
        if (systemMenuInstalled) return;
        var menu = GetSystemMenu(Handle, false);
        if (menu == IntPtr.Zero) return;
        AppendMenu(menu, MfSeparator, UIntPtr.Zero, string.Empty);
        AppendMenu(menu, MfString, new UIntPtr((uint)ScaleUpCommand), "放大\tCtrl++");
        AppendMenu(menu, MfString, new UIntPtr((uint)ScaleDownCommand), "缩小\tCtrl+-");
        AppendMenu(menu, MfString, new UIntPtr((uint)ScaleResetCommand), "实际大小\tCtrl+0");
        systemMenuInstalled = true;
        UpdateSystemMenuAvailability();
    }

    protected override void OnHandleDestroyed(EventArgs eventArgs)
    {
        systemMenuInstalled = false;
        nativeBackdropEnabled = false;
        base.OnHandleDestroyed(eventArgs);
    }

    protected override void OnPaintBackground(PaintEventArgs eventArgs)
    {
        if (!nativeBackdropEnabled) base.OnPaintBackground(eventArgs);
    }

    protected override bool ProcessCmdKey(ref Message message, Keys keyData)
    {
        if ((keyData & Keys.Control) == Keys.Control)
        {
            var keyCode = keyData & Keys.KeyCode;
            if (keyCode is Keys.Oemplus or Keys.Add)
            {
                ChangeScale(1);
                return true;
            }
            if (keyCode is Keys.OemMinus or Keys.Subtract)
            {
                ChangeScale(-1);
                return true;
            }
            if (keyCode is Keys.D0 or Keys.NumPad0)
            {
                ApplyScale(1.0, persist: true, notifyPage: true);
                return true;
            }
        }
        return base.ProcessCmdKey(ref message, keyData);
    }

    protected override void WndProc(ref Message message)
    {
        if (message.Msg == WmSysCommand)
        {
            switch (message.WParam.ToInt32() & 0xFFF0)
            {
                case ScaleUpCommand:
                    ChangeScale(1);
                    return;
                case ScaleDownCommand:
                    ChangeScale(-1);
                    return;
                case ScaleResetCommand:
                    ApplyScale(1.0, persist: true, notifyPage: true);
                    return;
            }
        }
        base.WndProc(ref message);
    }

    private void HandleWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs eventArgs)
    {
        try
        {
            using var document = JsonDocument.Parse(eventArgs.WebMessageAsJson);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("type", out var type)) return;
            if (type.GetString() == "set_theme"
                && root.TryGetProperty("theme", out var theme)
                && theme.GetString() is string requestedTheme
                && requestedTheme is "light" or "dark")
            {
                ApplySurfaceTheme(requestedTheme);
                return;
            }
            if (type.GetString() != "set_ui_scale"
                || !root.TryGetProperty("scale", out var scale)
                || !scale.TryGetDouble(out var requested)
                || NormalizeScale(requested) is not double accepted) return;
            ApplyScale(accepted, persist: true, notifyPage: true);
        }
        catch (JsonException)
        {
            // Ignore malformed messages from page scripts.
        }
    }

    private static double? NormalizeScale(double value)
    {
        foreach (var step in ScaleSteps)
        {
            if (Math.Abs(step - value) < 0.0001) return step;
        }
        return null;
    }

    private static string JavascriptNumber(double value) => value.ToString("0.####", CultureInfo.InvariantCulture);

    private void ChangeScale(int offset)
    {
        var current = Array.FindIndex(ScaleSteps, step => Math.Abs(step - uiScale) < 0.0001);
        if (current < 0) current = Array.IndexOf(ScaleSteps, 1.0);
        var next = Math.Clamp(current + offset, 0, ScaleSteps.Length - 1);
        ApplyScale(ScaleSteps[next], persist: true, notifyPage: true);
    }

    private void ApplyScale(double scale, bool persist, bool notifyPage)
    {
        var accepted = NormalizeScale(scale);
        if (accepted is null) return;
        uiScale = accepted.Value;
        if (webViewReady) browser.ZoomFactor = uiScale;
        if (persist) UIScalePreferences.Save(uiPreferencesPath, uiScale);
        UpdateSystemMenuAvailability();
        if (!webViewReady) return;
        _ = UpdateScaleBootstrapScript();
        if (notifyPage) SyncScaleToPage();
    }

    private async Task UpdateScaleBootstrapScript()
    {
        await uiScaleScriptLock.WaitAsync();
        try
        {
            if (browser.CoreWebView2 is null) return;
            if (!string.IsNullOrEmpty(uiScaleScriptId)) browser.CoreWebView2.RemoveScriptToExecuteOnDocumentCreated(uiScaleScriptId);
            var scale = JavascriptNumber(uiScale);
            var material = nativeBackdropEnabled ? "system" : "fallback";
            uiScaleScriptId = await browser.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(
                $"document.documentElement.dataset.nativeShell='windows';document.documentElement.dataset.nativeMaterial='{material}';document.documentElement.dataset.uiScale='{scale}';if(location.pathname==='/')window.__optionhelperInitializationToken='{initializationToken}';"
            );
        }
        finally
        {
            uiScaleScriptLock.Release();
        }
    }

    private void SyncScaleToPage()
    {
        if (!webViewReady) return;
        _ = browser.CoreWebView2.ExecuteScriptAsync($"document.documentElement.dataset.uiScale='{JavascriptNumber(uiScale)}';window.OptionHelperUIScale?.syncFromNative?.({JavascriptNumber(uiScale)});");
    }

    private void UpdateSystemMenuAvailability()
    {
        if (!IsHandleCreated) return;
        var menu = GetSystemMenu(Handle, false);
        if (menu == IntPtr.Zero) return;
        EnableMenuItem(menu, ScaleDownCommand, MfByCommand | (uiScale > ScaleSteps[0] ? MfEnabled : MfGrayed));
        EnableMenuItem(menu, ScaleUpCommand, MfByCommand | (uiScale < ScaleSteps[^1] ? MfEnabled : MfGrayed));
        DrawMenuBar(Handle);
    }

    private bool TryEnableSystemBackdrop()
    {
        if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 22621)) return false;
        try
        {
            var backdrop = DwmsbtMainWindow;
            if (DwmSetWindowAttribute(Handle, DwmwaSystemBackdropType, ref backdrop, sizeof(int)) != 0) return false;
            var margins = new Margins(-1);
            return DwmExtendFrameIntoClientArea(Handle, ref margins) == 0;
        }
        catch (DllNotFoundException)
        {
            return false;
        }
        catch (EntryPointNotFoundException)
        {
            return false;
        }
    }

    private void ApplySurfaceTheme(string theme)
    {
        surfaceTheme = theme == "dark" ? "dark" : "light";
        BackColor = surfaceTheme == "dark" ? Color.FromArgb(31, 31, 31) : Color.FromArgb(247, 247, 247);
        if (IsHandleCreated && OperatingSystem.IsWindowsVersionAtLeast(10, 0, 17763))
        {
            var dark = surfaceTheme == "dark" ? 1 : 0;
            _ = DwmSetWindowAttribute(Handle, DwmwaUseImmersiveDarkMode, ref dark, sizeof(int));
        }
        Invalidate();
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Margins
    {
        internal int Left;
        internal int Right;
        internal int Top;
        internal int Bottom;

        internal Margins(int value)
        {
            Left = value;
            Right = value;
            Top = value;
            Bottom = value;
        }
    }

    [DllImport("user32.dll")]
    private static extern IntPtr GetSystemMenu(IntPtr window, bool revert);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool AppendMenu(IntPtr menu, uint flags, UIntPtr item, string text);

    [DllImport("user32.dll")]
    private static extern bool EnableMenuItem(IntPtr menu, int item, uint enable);

    [DllImport("user32.dll")]
    private static extern bool DrawMenuBar(IntPtr window);

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr window, int attribute, ref int value, int valueSize);

    [DllImport("dwmapi.dll")]
    private static extern int DwmExtendFrameIntoClientArea(IntPtr window, ref Margins margins);
}

internal static class UIScalePreferences
{
    internal static double Load(string path, IReadOnlyList<double> allowed)
    {
        try
        {
            if (!File.Exists(path)) return 1.0;
            using var document = JsonDocument.Parse(File.ReadAllText(path));
            var root = document.RootElement;
            if (!root.TryGetProperty("schema_version", out var version) || version.GetInt32() != 1
                || !root.TryGetProperty("ui_scale", out var scale) || !scale.TryGetDouble(out var value)) return 1.0;
            foreach (var step in allowed)
            {
                if (Math.Abs(step - value) < 0.0001) return step;
            }
            return 1.0;
        }
        catch (Exception)
        {
            return 1.0;
        }
    }

    internal static void Save(string path, double scale)
    {
        var temporary = path + ".tmp";
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(temporary, JsonSerializer.Serialize(new { schema_version = 1, ui_scale = scale }, new JsonSerializerOptions { WriteIndented = true }));
            File.Move(temporary, path, true);
        }
        catch (Exception)
        {
            try { if (File.Exists(temporary)) File.Delete(temporary); }
            catch { /* A later save can replace a stale temporary file. */ }
        }
    }
}
