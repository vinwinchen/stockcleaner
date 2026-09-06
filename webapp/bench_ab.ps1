# Excel 引擎 A/B: 同量级大文件, 两个服务实例分别钉住不同引擎。
# 关键: SC_EXCEL_ENGINE 必须设在服务端进程里, 设在调用方没用。
# 用法: powershell -File bench_ab.ps1 [行数]
param([int]$Rows = 200000)

$here = $PSScriptRoot
$py = Join-Path $here '.venv\Scripts\python.exe'
$codeA = "import sys, os; sys.path[:0]=[os.getcwd(), os.path.dirname(os.getcwd())]; from backend.app import app, mount_static; mount_static(app); import uvicorn; uvicorn.run(app, host='127.0.0.1', port=8791, log_level='critical')"
$codeB = $codeA -replace '8791', '8792'

$env:PYTHONIOENCODING = 'utf-8'

$jobA = Start-Job -ScriptBlock {
    Set-Location $using:here
    Remove-Item Env:SC_EXCEL_ENGINE -ErrorAction SilentlyContinue
    & "$using:here\.venv\Scripts\python.exe" -c $using:codeA
}
$jobB = Start-Job -ScriptBlock {
    Set-Location $using:here
    $env:SC_EXCEL_ENGINE = 'openpyxl'
    & "$using:here\.venv\Scripts\python.exe" -c $using:codeB
}
Start-Sleep -Seconds 6

foreach ($case in @(@(8791, '默认 (calamine 优先)'), @(8792, '钉住 openpyxl'))) {
    $port, $label = $case
    Write-Host "--- $label : http://127.0.0.1:$port ---"
    & $py (Join-Path $here 'bench_e2e.py') "http://127.0.0.1:$port" $Rows 'quick'
}

Stop-Job $jobA, $jobB
Remove-Job $jobA, $jobB -Force
