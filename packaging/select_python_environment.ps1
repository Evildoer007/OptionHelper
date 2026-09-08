param(
    [Parameter(Mandatory = $true)]
    [string] $ListScript,
    [Parameter(Mandatory = $true)]
    [string] $OutputFile
)

$ErrorActionPreference = "Stop"
$paths = @(& $ListScript -PathsOnly)
if ($paths.Count -eq 0) {
    [Console]::Error.WriteLine("未找到可选择的Python解释器。请安装64位Python3.12或3.13后重试。")
    exit 1
}

for ($index = 0; $index -lt $paths.Count; $index += 1) {
    [Console]::Error.WriteLine("  [$($index + 1)] 解释器绝对路径=$($paths[$index])")
}

while ($true) {
    $choice = Read-Host "请输入序号后按回车（直接回车取消）"
    if ([string]::IsNullOrWhiteSpace($choice)) {
        [Console]::Error.WriteLine("未选择Python解释器，构建已取消。")
        exit 1
    }
    $number = 0
    if ([int]::TryParse($choice, [ref] $number) -and $number -ge 1 -and $number -le $paths.Count) {
        [System.IO.File]::WriteAllText($OutputFile, [string] $paths[$number - 1], [System.Text.UTF8Encoding]::new($false))
        exit 0
    }
    [Console]::Error.WriteLine("请输入1到$($paths.Count)之间的序号。")
}
