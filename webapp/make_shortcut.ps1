# 生成 StockCleaner 的任务栏固定项 (Start Menu 快捷方式 + AppID 注册)。
#
# 背景: 任务栏按钮的图标按 AppUserModelID 取。run.py 把进程 AUMID 设成
# StockCleaner.Desktop, 标题栏/运行中的按钮才会用我们自己的图标; 固定项要显示同一个
# 图标, 就得有一个 IconLocation 指向 stockcleaner.ico 的 .lnk。
#
# 为什么 .lnk 上没有写 AUMID (三条路在本机都堵死, 别再试):
#   1. 激活 CLSID_ShellLink {0002157F-...} -> 80040154 REGDB_E_CLASSNOTREG
#      (同一进程里 New-Object -ComObject WScript.Shell 却能用, 是这里的注册表视图问题)
#   2. WScript.Shell 建的快捷方式对象 QI IPropertyStore -> E_NOINTERFACE, WSH 不转发
#   3. propsys!SHGetPropertyStoreFromParsingName -> propsys.dll 的 SH* 系列只按序号导出
#      (命名导出 223 个, 里面没有它), 序号未知, 蒙一个等于随便调地址
#   参照物也没有: 本机 331 个 .lnk 里带 System.AppUserModel_ID 的是 0 个。
# 所以走本机其他桌面应用在用的路子: HKCU\Software\Classes\AppUserModelId\<AUMID> 注册
# DisplayName / IconUri, .lnk 只负责图标和启动参数。
#
# 固定这一步必须手动: Win10/11 出于安全把 taskbarpin 谓词收走了, 任何脚本都固定不了。
#
# 用法:  pwsh -File webapp/make_shortcut.ps1
#        换安装位置后重跑一次即可 (注册表和 .lnk 里都是绝对路径)。

[CmdletBinding()]
param(
    [string]$Aumid = 'StockCleaner.Desktop'
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$entry = Join-Path $root 'run.py'
$icon = Join-Path $root 'assets\stockcleaner.ico'

foreach ($f in @($pythonw, $entry, $icon)) {
    if (-not (Test-Path -LiteralPath $f)) { throw "缺少文件: $f" }
}

# ---------------------------------------------------------------- 1. 快捷方式
$lnkDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$lnkPath = Join-Path $lnkDir 'StockCleaner.lnk'

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "`"$entry`""                     # run.py 用绝对路径, 不依赖工作目录
$lnk.WorkingDirectory = $root
$lnk.IconLocation = "$icon,0"
$lnk.Description = 'StockCleaner 金融数据清洗'
$lnk.Save()

# 回读一遍, 确认落盘而不是留在内存里
$check = $shell.CreateShortcut($lnkPath)
Write-Host "快捷方式 $lnkPath"
Write-Host "  target    = $($check.TargetPath)"
Write-Host "  args      = $($check.Arguments)"
Write-Host "  workdir   = $($check.WorkingDirectory)"
Write-Host "  icon      = $($check.IconLocation)"

# ---------------------------------------------------------------- 2. AppID 注册
$appIdPath = "HKCU:\Software\Classes\AppUserModelId\$Aumid"
if (-not (Test-Path -LiteralPath $appIdPath)) {
    $null = New-Item -Path $appIdPath -Force
}
Set-ItemProperty -LiteralPath $appIdPath -Name DisplayName -Value 'StockCleaner 金融数据清洗' -Type String
Set-ItemProperty -LiteralPath $appIdPath -Name IconUri -Value $icon -Type String
Write-Host "AppID 注册 $appIdPath"
Get-ItemProperty -LiteralPath $appIdPath |
    Select-Object DisplayName, IconUri |
    Format-List | Out-String | ForEach-Object { Write-Host $_.TrimEnd() }

# ---------------------------------------------------------------- 3. 修复任务栏固定项
# 在任务栏"运行中"的按钮上直接固定, Explorer 会按**进程镜像路径**凭空造一条链。实测得到
# User Pinned\TaskBar\Python.lnk: target=C:\Python314\pythonw.exe (venv 跳板背后的基础
# 解释器)、args 空、icon 空 —— 图标是 Python, 点它还会因为没参数而直接退出。
# 这里按指纹精确认领这种链并改写回正确字段 (只认 pythonw + 无参数 + 无图标, 不会碰别的)。
# 另外: 不要给这条链改名。Explorer 的固定记录按路径记, 改名后按钮会立刻变成空白文档图标。
$homeDir = $null
$cfg = Join-Path $root '.venv\pyvenv.cfg'
if (Test-Path -LiteralPath $cfg) {
    $line = Select-String -LiteralPath $cfg -Pattern '^\s*home\s*=\s*(.+)$' | Select-Object -First 1
    if ($line) { $homeDir = $line.Matches[0].Groups[1].Value.Trim() }
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class ShellNotify
{
    [DllImport("shell32.dll")]
    static extern void SHChangeNotify(uint eventId, uint flags, IntPtr i1, IntPtr i2);

    public static void Item(string path)
    {
        IntPtr p = Marshal.StringToHGlobalUni(path);
        try { SHChangeNotify(0x00002000, 0x0005, p, IntPtr.Zero); }   // SHCNE_UPDATEITEM | SHCNF_PATHW
        finally { Marshal.FreeHGlobal(p); }
    }

    // 只改 .lnk 字段不够: 任务栏画的是图标缓存里的旧图。这条让 shell 丢掉图标图像缓存,
    // 比重启 explorer 温和得多 (实测有效: 改完立刻从 Python 图标换成我们的)。
    public static void Images()
    {
        SHChangeNotify(0x00008000, 0x0003, IntPtr.Zero, IntPtr.Zero); // SHCNE_UPDATEIMAGE | SHCNF_DWORD
    }
}
'@

$pinDir = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'
$repaired = 0
if (Test-Path -LiteralPath $pinDir) {
    foreach ($file in (Get-ChildItem -LiteralPath $pinDir -Filter *.lnk)) {
        $p = $file.FullName
        $pl = $shell.CreateShortcut($p)
        if ($pl.TargetPath -ieq $pythonw) {
            Write-Host "任务栏固定项已就绪: $($file.Name)"
            continue
        }
        $isOurHost = $pl.TargetPath -match '\\python(w)?\.exe$' -and (
            ($homeDir -and $pl.TargetPath -ieq (Join-Path $homeDir 'pythonw.exe')) -or
            ($pl.TargetPath -ieq $pythonw))
        if (-not ($isOurHost -and -not $pl.Arguments -and $pl.IconLocation -notmatch '\S')) { continue }

        $pl.TargetPath = $pythonw
        $pl.Arguments = "`"$entry`""
        $pl.WorkingDirectory = $root
        $pl.IconLocation = "$icon,0"
        $pl.Description = 'StockCleaner 金融数据清洗'
        $pl.Save()
        [ShellNotify]::Item($p)
        $repaired++
        Write-Host "已修复 Explorer 自动生成的固定项: $($file.Name)"
    }
}
if ($repaired) {
    [ShellNotify]::Images()
    Write-Host "已刷新 shell 图标缓存 ($repaired 项)"
}

Write-Host ''
Write-Host '固定这一步只能手动 (系统不允许脚本固定):'
Write-Host '  想要按钮显示 "StockCleaner": 开始菜单 -> 所有应用 -> StockCleaner -> 右键 -> 固定到任务栏'
Write-Host '  在运行中的按钮上固定也行, 但 Explorer 会先造出一条 Python.lnk (名字改不掉,'
Write-Host '  改名会让按钮变空白), 再跑一次本脚本即可把它的图标和启动参数纠正回来。'
