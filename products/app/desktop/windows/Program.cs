using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace OptionHelper.Windows;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
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
            MessageBox.Show("找不到内置应用服务。", "OptionHelper", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        var state = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "OptionHelper", "local-state");
        Directory.CreateDirectory(state);
        using var process = new Process
        {
            StartInfo = new ProcessStartInfo(backend, $"--host 127.0.0.1 --port 0 --data-dir \"{state}\" --resource-dir \"{resources}\"")
            {
                WorkingDirectory = Path.GetDirectoryName(backend)!,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = System.Text.Encoding.UTF8,
                StandardErrorEncoding = System.Text.Encoding.UTF8,
                CreateNoWindow = true,
            }
        };
        var started = false;
        Task<string>? backendErrorTask = null;
        try
        {
            process.StartInfo.Environment["PYTHONUTF8"] = "1";
            process.StartInfo.Environment["PYTHONIOENCODING"] = "utf-8";
            process.Start();
            started = true;
            backendErrorTask = process.StandardError.ReadToEndAsync();
            // Before Application.Run there is no WinForms synchronization
            // context. An awaited Main would resume on an MTA worker thread.
            var url = WaitForUrl(process, TimeSpan.FromSeconds(45)).GetAwaiter().GetResult();
            _ = DrainOutput(process.StandardOutput);
            Application.Run(new MainForm(url, state));
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
                var backendError = backendErrorTask.GetAwaiter().GetResult().Trim();
                if (backendError.Length > 4000)
                    backendError = backendError[^4000..];
                if (!string.IsNullOrWhiteSpace(backendError))
                    detail += $"\n\n后端错误：\n{backendError}";
            }
            MessageBox.Show($"应用服务未能启动。\n{detail}", "OptionHelper", MessageBoxButtons.OK, MessageBoxIcon.Error);
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

    private static async Task DrainOutput(StreamReader output)
    {
        try
        {
            while (await output.ReadLineAsync().ConfigureAwait(false) is not null) { }
        }
        catch (IOException) { }
        catch (ObjectDisposedException) { }
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
    private readonly Uri appOrigin;
    private readonly HashSet<Form> reportWindows = [];
    private readonly SemaphoreSlim uiScaleScriptLock = new(1, 1);
    private double uiScale;
    private bool webViewReady;
    private bool systemMenuInstalled;
    private bool nativeBackdropEnabled;
    private string surfaceTheme = "light";
    private string? uiScaleScriptId;

    internal MainForm(string url, string stateDirectory)
    {
        appOrigin = new Uri(url);
        Text = "OptionHelper";
        Width = 1320;
        Height = 860;
        KeyPreview = true;
        BackColor = Color.FromArgb(247, 247, 247);
        uiPreferencesPath = Path.Combine(stateDirectory, "ui-preferences.json");
        uiScale = UIScalePreferences.Load(uiPreferencesPath, ScaleSteps);
        Controls.Add(browser);
        Shown += async (_, _) =>
        {
            try
            {
                var webViewData = Path.Combine(stateDirectory, "webview2");
                Directory.CreateDirectory(webViewData);
                var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: webViewData);
                await browser.EnsureCoreWebView2Async(environment);
                browser.CoreWebView2.Settings.IsZoomControlEnabled = false;
                browser.ZoomFactor = uiScale;
                await UpdateScaleBootstrapScript();
                browser.CoreWebView2.WebMessageReceived += HandleWebMessage;
                browser.CoreWebView2.NewWindowRequested += OpenReportWindow;
                browser.CoreWebView2.NavigationCompleted += (_, _) => SyncScaleToPage();
                webViewReady = true;
                var startupUrl = new UriBuilder(url);
                var query = startupUrl.Query.TrimStart('?');
                startupUrl.Query = (string.IsNullOrEmpty(query) ? "" : query + "&")
                    + "app_startup=" + Guid.NewGuid().ToString("N");
                browser.CoreWebView2.Navigate(startupUrl.Uri.AbsoluteUri);
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

    private bool IsSameOrigin(Uri url) => url.Scheme == appOrigin.Scheme
        && url.Host == appOrigin.Host && url.Port == appOrigin.Port && string.IsNullOrEmpty(url.UserInfo);

    private bool IsReportEditor(Uri url) => IsSameOrigin(url) && string.IsNullOrEmpty(url.Query)
        && Regex.IsMatch(url.AbsolutePath, @"^/reports/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/edit$");

    private bool IsReportArtifact(Uri url) => IsSameOrigin(url)
        && Regex.IsMatch(url.AbsolutePath, @"^/api/reports/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/(?:document-artifacts|artifacts)/[^/]+$")
        && (string.IsNullOrEmpty(url.Query) || url.Query == "?download=1");

    private async void OpenReportWindow(object? sender, CoreWebView2NewWindowRequestedEventArgs eventArgs)
    {
        eventArgs.Handled = true;
        if (!Uri.TryCreate(eventArgs.Uri, UriKind.Absolute, out var url)) return;
        var editor = ReferenceEquals(sender, browser.CoreWebView2) && IsReportEditor(url);
        if (!editor && !IsReportArtifact(url)) return;
        using var deferral = eventArgs.GetDeferral();
        var child = new Form { Text = editor ? "OptionHelper报告编辑" : "OptionHelper报告预览", Width = 1000, Height = 800 };
        var view = new WebView2 { Dock = DockStyle.Fill };
        child.Controls.Add(view);
        reportWindows.Add(child);
        child.FormClosed += (_, _) => reportWindows.Remove(child);
        try
        {
            child.Show(this);
            // Share the authenticated profile without installing the main window's native bridge.
            await view.EnsureCoreWebView2Async(browser.CoreWebView2.Environment);
            if (child.IsDisposed) return;
            view.ZoomFactor = uiScale;
            view.CoreWebView2.Settings.IsWebMessageEnabled = false;
            view.CoreWebView2.NewWindowRequested += OpenReportWindow;
            view.CoreWebView2.NavigationStarting += (_, navigation) =>
            {
                if (!Uri.TryCreate(navigation.Uri, UriKind.Absolute, out var target))
                {
                    navigation.Cancel = true;
                    return;
                }
                if (editor && IsSameOrigin(target) && target.AbsolutePath == "/optchat")
                {
                    navigation.Cancel = true;
                    child.Close();
                    return;
                }
                navigation.Cancel = target.Scheme != "about" && !IsReportArtifact(target)
                    && !(editor && IsReportEditor(target));
            };
            eventArgs.NewWindow = view.CoreWebView2;
        }
        catch (Exception)
        {
            child.Close();
            MessageBox.Show(this, "报告窗口未能打开，请重试。", "OptionHelper", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
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
            if (type.GetString() == "signed_out")
            {
                foreach (var reportWindow in reportWindows.ToArray()) reportWindow.Close();
                return;
            }
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
                $"document.documentElement.dataset.nativeShell='windows';document.documentElement.dataset.nativeMaterial='{material}';document.documentElement.dataset.uiScale='{scale}';if(location.pathname==='/'&&new URL(location.href).searchParams.has('app_startup')){{document.documentElement.classList.add('login-boot');}}"
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
