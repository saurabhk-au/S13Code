$PYTHON = "C:\Users\bhamb\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
$env:PYTHONPATH = "$PSScriptRoot;$PSScriptRoot\.venv\Lib\site-packages"
& $PYTHON -m s13code.cli serve
