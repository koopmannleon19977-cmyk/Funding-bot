Param(
  [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot ".."))
)

$src = Join-Path $RepoRoot "docs\BOT_INSTRUCTION_PLAN.md"
$promptsDir = Join-Path $env:APPDATA "Code\User\prompts"
$dst = Join-Path $promptsDir "funding-bot-constitution.instructions.md"

if (-not (Test-Path $src)) {
  throw "Source file not found: $src"
}

if (-not (Test-Path $promptsDir)) {
  New-Item -ItemType Directory -Path $promptsDir -Force | Out-Null
}

$body = Get-Content -Path $src -Raw
$frontmatter = "---`napplyTo: '**'`n---`n"

Set-Content -Path $dst -Value ($frontmatter + $body) -Encoding UTF8
Write-Output "Synced global VS Code instructions to: $dst"