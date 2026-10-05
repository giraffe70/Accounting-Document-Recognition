param(
    [ValidateSet('check', 'contracts', 'database', 'validation', 'scoring', 'all')]
    [string]$Scope = 'contracts',
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

if (-not $Python) {
    $Python = Join-Path $env:USERPROFILE 'Miniconda3\envs\pdfscan312\python.exe'
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python not found: $Python. Install/use the pdfscan312 environment or pass -Python explicitly. System Python fallback is disabled."
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

Write-Host "Python: $Python"
& $Python -B --version
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Python -B (Join-Path $PSScriptRoot 'verify_agent_config.py')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($Scope -eq 'check') {
    exit 0
}

$Pattern = switch ($Scope) {
    'contracts' { 'test_doc_contracts.py' }
    'database' { 'test_database_view.py' }
    'validation' { 'test_validation.py' }
    'scoring' { 'test_evaluation.py' }
    'all' { 'test*.py' }
}
if ($Pattern -ne 'test*.py' -and -not (Test-Path -LiteralPath (Join-Path $Root "tests\$Pattern") -PathType Leaf)) {
    throw "No tests/$Pattern exists for scope '$Scope'."
}

# Disable outbound TCP from tests while preserving in-process ASGI TestClient behavior.
$Runner = @'
import socket
import sys
import unittest
import inspect
from unittest.mock import patch

original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex

def is_internal_socketpair():
    # Windows asyncio emulates socket.socketpair() with a loopback connection.
    # Permit that internal transport setup while rejecting test-initiated TCP.
    frame = inspect.currentframe().f_back
    while frame:
        if frame.f_code.co_name == "_fallback_socketpair" and frame.f_code.co_filename.endswith("socket.py"):
            return True
        frame = frame.f_back
    return False

def blocked_connect(sock, address):
    if is_internal_socketpair():
        return original_connect(sock, address)
    raise RuntimeError("network disabled in verification tests")

def blocked_connect_ex(sock, address):
    if is_internal_socketpair():
        return original_connect_ex(sock, address)
    raise RuntimeError("network disabled in verification tests")

pattern = sys.argv[1]
loader = unittest.TestLoader()
runner = unittest.TextTestRunner(verbosity=2)
with patch.object(socket.socket, "connect", blocked_connect), \
     patch.object(socket.socket, "connect_ex", blocked_connect_ex), \
     patch.object(socket, "create_connection", side_effect=RuntimeError("network disabled in verification tests")):
    suite = loader.discover("tests", pattern=pattern)
    if suite.countTestCases() == 0:
        raise SystemExit("No tests found for verification scope")
    result = runner.run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
'@
$EncodedRunner = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Runner))
& $Python -B -c "import base64; exec(base64.b64decode('$EncodedRunner'))" $Pattern
exit $LASTEXITCODE
