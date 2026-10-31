param(
    [switch]$Rotate
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$envPath = Join-Path $repoRoot '.env'
$secretDir = Join-Path $repoRoot 'infra\secrets'
$aclPath = Join-Path $secretDir 'redis-users.acl'

$existingLines = @()
if (Test-Path -LiteralPath $envPath) {
    $existingLines = @(Get-Content -LiteralPath $envPath)
}
$hasUrl = $existingLines | Where-Object { $_ -match '^FINGUARD_REDIS_URL=' }
if (($hasUrl -or (Test-Path -LiteralPath $aclPath)) -and -not $Rotate) {
    throw 'Redis credentials already exist. Use -Rotate to replace them deliberately.'
}

New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
$randomBytes = [byte[]]::new(48)
$generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$generator.GetBytes($randomBytes)
$generator.Dispose()
$password = [Convert]::ToBase64String($randomBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
$passwordHash = [Convert]::ToHexString(
    [System.Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($password))
).ToLowerInvariant()

$permissions = '+ping +client|setinfo +type +hget +hincrby +zadd +hset +sadd +time +expire '
$permissions += '+zrangebyscore +zrem +hdel +get +set +srem +smembers +exists +hgetall +hlen '
$permissions += '+multi +exec +evalsha +script|load'
$acl = 'user default off resetpass resetkeys resetchannels -@all' + [Environment]::NewLine
$acl += 'user finguard_app reset on #' + $passwordHash
$acl += ' ~finguard:features:* -@all ' + $permissions + [Environment]::NewLine

$preservedLines = @($existingLines | Where-Object { $_ -notmatch '^FINGUARD_REDIS_URL=' })
$preservedLines += 'FINGUARD_REDIS_URL=redis://finguard_app:' + $password + '@127.0.0.1:6379/0'
[IO.File]::WriteAllText(
    $aclPath,
    $acl,
    [Text.UTF8Encoding]::new($false)
)
[IO.File]::WriteAllText(
    $envPath,
    (($preservedLines -join [Environment]::NewLine) + [Environment]::NewLine),
    [Text.UTF8Encoding]::new($false)
)

$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $secretDir /inheritance:r /grant:r `
    ('*' + $currentSid + ':(OI)(CI)(F)') `
    '*S-1-5-18:(OI)(CI)(F)' `
    '*S-1-5-32-544:(OI)(CI)(F)' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Could not restrict Redis secret-directory permissions.'
}
foreach ($path in @($envPath, $aclPath)) {
    & icacls.exe $path /inheritance:r /grant:r `
        ('*' + $currentSid + ':(F)') `
        '*S-1-5-18:(F)' `
        '*S-1-5-32-544:(F)' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not restrict a Redis secret-file permission.'
    }
}

Remove-Variable password, passwordHash, randomBytes
Write-Output 'Redis application credentials were generated and restricted to local administrators and the current user.'
