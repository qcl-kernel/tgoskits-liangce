[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')]
    [string] $RunId = ((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '-host'),

    [switch] $IncludeDocker,

    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,255}$')]
    [string] $ContainerImage = 'ghcr.io/rcore-os/tgoskits-container:latest',

    [switch] $DryRun,

    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-ExternalCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Command,

        [string[]] $Arguments = @()
    )

    if ($null -eq (Get-Command $Command -ErrorAction SilentlyContinue)) {
        return [ordered]@{
            available = $false
            exitCode = $null
            output = @()
            error = 'command not found'
        }
    }

    try {
        $output = @(& $Command @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $exitCode = $LASTEXITCODE
        return [ordered]@{
            available = $true
            exitCode = $exitCode
            output = $output
            error = $null
        }
    }
    catch {
        return [ordered]@{
            available = $true
            exitCode = $null
            output = @()
            error = $_.Exception.Message
        }
    }
}

function Get-CimValue {
    param(
        [Parameter(Mandatory = $true)]
        [string] $ClassName
    )

    if ($null -eq (Get-Command Get-CimInstance -ErrorAction SilentlyContinue)) {
        return @()
    }

    try {
        return @(Get-CimInstance -ClassName $ClassName -ErrorAction Stop)
    }
    catch {
        return @()
    }
}

$scriptDirectory = Split-Path -Parent $PSCommandPath
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptDirectory '..\..'))
$runsRoot = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot 'results\baseline\runs'))
$outputDirectory = [System.IO.Path]::GetFullPath((Join-Path $runsRoot $RunId))
$expectedPrefix = $runsRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar

if (-not $outputDirectory.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing output path outside results/baseline/runs: $outputDirectory"
}

$outputPath = Join-Path $outputDirectory 'host-environment.json'

if ($DryRun) {
    Write-Host "[dry-run] repository: $repositoryRoot"
    Write-Host "[dry-run] output:     $outputPath"
    Write-Host '[dry-run] collect: Windows version, CPU, memory, WSL, and Git metadata'
    if ($IncludeDocker) {
        Write-Host "[dry-run] collect: Docker version and local image identity for $ContainerImage"
    }
    else {
        Write-Host '[dry-run] Docker collection disabled; pass -IncludeDocker to enable it'
    }
    exit 0
}

if ((Test-Path -LiteralPath $outputPath) -and -not $Force) {
    throw "Output already exists: $outputPath. Choose another -RunId or pass -Force."
}

$operatingSystems = @(Get-CimValue -ClassName 'Win32_OperatingSystem')
$processors = @(Get-CimValue -ClassName 'Win32_Processor')
$computerSystems = @(Get-CimValue -ClassName 'Win32_ComputerSystem')

$windows = [ordered]@{
    caption = $null
    version = [System.Environment]::OSVersion.VersionString
    buildNumber = $null
}
if ($operatingSystems.Count -gt 0) {
    $windows.caption = $operatingSystems[0].Caption
    $windows.version = $operatingSystems[0].Version
    $windows.buildNumber = $operatingSystems[0].BuildNumber
}

$cpu = [ordered]@{
    models = @($processors | ForEach-Object { $_.Name } | Sort-Object -Unique)
    physicalCores = $null
    logicalProcessors = $null
}
if ($processors.Count -gt 0) {
    $cpu.physicalCores = ($processors | Measure-Object -Property NumberOfCores -Sum).Sum
    $cpu.logicalProcessors = ($processors | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
}

$memoryBytes = $null
if ($computerSystems.Count -gt 0) {
    $memoryBytes = [UInt64] $computerSystems[0].TotalPhysicalMemory
}

$gitCommit = Invoke-ExternalCapture -Command 'git' -Arguments @('-C', $repositoryRoot, 'rev-parse', 'HEAD')
$gitBranch = Invoke-ExternalCapture -Command 'git' -Arguments @('-C', $repositoryRoot, 'branch', '--show-current')
$gitStatus = Invoke-ExternalCapture -Command 'git' -Arguments @('-C', $repositoryRoot, 'status', '--porcelain')

$docker = [ordered]@{
    requested = [bool] $IncludeDocker
    version = $null
    image = $null
}
if ($IncludeDocker) {
    $docker.version = Invoke-ExternalCapture -Command 'docker' -Arguments @(
        'version',
        '--format',
        '{{json .}}'
    )
    $docker.image = Invoke-ExternalCapture -Command 'docker' -Arguments @(
        'image',
        'inspect',
        $ContainerImage,
        '--format',
        '{{json .Id}} {{json .RepoDigests}}'
    )
}

$document = [ordered]@{
    schemaVersion = 1
    capturedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    runId = $RunId
    host = [ordered]@{
        operatingSystem = $windows
        architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        cpu = $cpu
        physicalMemoryBytes = $memoryBytes
    }
    wsl = [ordered]@{
        version = Invoke-ExternalCapture -Command 'wsl.exe' -Arguments @('--version')
        status = Invoke-ExternalCapture -Command 'wsl.exe' -Arguments @('--status')
        distributions = Invoke-ExternalCapture -Command 'wsl.exe' -Arguments @('--list', '--verbose')
    }
    docker = $docker
    repository = [ordered]@{
        commit = $gitCommit
        branch = $gitBranch
        status = $gitStatus
        dirty = ($gitStatus.available -and $gitStatus.exitCode -eq 0 -and $gitStatus.output.Count -gt 0)
    }
}

New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$temporaryPath = $outputPath + '.tmp'
$document | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
Move-Item -LiteralPath $temporaryPath -Destination $outputPath -Force

Write-Host "Host environment written to $outputPath"
