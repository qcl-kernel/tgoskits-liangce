param(
    [string]$Root = $PSScriptRoot
)

$resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
$manifestPath = Join-Path $resolvedRoot "SHA256SUMS.txt"
$rootPrefix = $resolvedRoot.TrimEnd('\') + '\'

$lines = Get-ChildItem -LiteralPath $resolvedRoot -File -Recurse -Force |
    Where-Object { $_.FullName -ne $manifestPath } |
    Sort-Object FullName |
    ForEach-Object {
        $relativePath = $_.FullName.Substring($rootPrefix.Length)
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
        "$hash *$($relativePath.Replace('\', '/'))"
    }

[System.IO.File]::WriteAllLines($manifestPath, $lines, [System.Text.UTF8Encoding]::new($false))
Write-Output "Wrote $($lines.Count) entries to $manifestPath"
