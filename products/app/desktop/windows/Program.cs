using System.Diagnostics;
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
        using var process = new Process
        {
            StartInfo = new ProcessStartInfo(backend, $"--host 127.0.0.1 --port 0 --data-dir \"{state}\" --resource-dir \"{resources}\"")
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
            Application.Run(new MainForm(url));
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
    private readonly WebView2 browser = new() { Dock = DockStyle.Fill };

    internal MainForm(string url)
    {
        Text = "OptionHelper";
        Width = 1320;
        Height = 860;
        Controls.Add(browser);
        Shown += async (_, _) =>
        {
            try
            {
                await browser.EnsureCoreWebView2Async();
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
}
