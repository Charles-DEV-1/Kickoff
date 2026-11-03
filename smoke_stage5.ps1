$ErrorActionPreference = "Stop"

function Get-DotEnvValue([string]$name) {
    $line = Get-Content -LiteralPath ".env" | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if (-not $line) { return "" }
    return (($line -replace "^$name=", "").Trim().Trim('"').TrimEnd('/'))
}

function Invoke-Api([string]$method, [string]$path, [hashtable]$headers = @{}, $body = $null) {
    try {
        if ($null -eq $body) {
            $response = Invoke-WebRequest -UseBasicParsing -Method $method -Uri "$script:apiBase$path" -Headers $headers
        } else {
            $response = Invoke-WebRequest -UseBasicParsing -Method $method -Uri "$script:apiBase$path" `
                -Headers $headers -ContentType "application/json" `
                -Body (ConvertTo-Json -InputObject $body -Depth 10 -Compress)
        }
        $status = [int]$response.StatusCode
        $responseHeaders = $response.Headers
        $content = $response.Content
    } catch {
        $response = $_.Exception.Response
        if ($null -eq $response) { throw "API connection failed: $($_.Exception.GetType().Name)" }
        $status = [int]$response.StatusCode
        $responseHeaders = $response.Headers
        $content = ""
        $stream = $response.GetResponseStream()
        if ($stream) {
            $reader = [System.IO.StreamReader]::new($stream)
            $content = $reader.ReadToEnd()
            $reader.Dispose()
        }
    }
    $json = if ($content) { try { $content | ConvertFrom-Json } catch { $null } } else { $null }
    return [pscustomobject]@{ Status = $status; Headers = $responseHeaders; Json = $json }
}

function Assert-Status($response, [int]$expected, [string]$label) {
    if ($response.Status -ne $expected) {
        $code = if ($response.Json -and $response.Json.error) { $response.Json.error.code } else { "no_error_code" }
        throw "$label returned HTTP $($response.Status) ($code), expected $expected."
    }
    Write-Host "PASS $label (HTTP $expected)"
}

$supabaseUrl = Get-DotEnvValue "SUPABASE_URL"
$publishableKey = Get-DotEnvValue "SUPABASE_PUBLISHABLE_KEY"
if (-not $supabaseUrl -or -not $publishableKey) { throw "SUPABASE_URL or SUPABASE_PUBLISHABLE_KEY is missing from .env." }
$script:apiBase = "http://127.0.0.1:5000"
$script:accessToken = $null

$manifest = Invoke-Api "GET" "/v1/templates/manifest"
Assert-Status $manifest 200 "Public template manifest"
if (-not $manifest.Headers["ETag"]) { throw "Manifest response did not include an ETag." }
$template = $manifest.Json.items | Where-Object { $_.slug -eq "generic_task_en" -and $_.language -eq "en" } | Select-Object -First 1
if (-not $template) { throw "The provisional English generic template is not in the manifest. Apply the Stage 5 seed SQL." }
Write-Host "PASS Manifest includes the provisional English generic template"

$unchanged = Invoke-Api "GET" "/v1/templates/manifest" @{ "If-None-Match" = $manifest.Headers["ETag"] }
Assert-Status $unchanged 304 "Manifest ETag response"

$listed = Invoke-Api "GET" "/v1/templates?task_type=generic&language=en&limit=10"
Assert-Status $listed 200 "Public template list"
$listedTemplate = $listed.Json.items | Where-Object { $_.id -eq $template.id } | Select-Object -First 1
if (-not $listedTemplate) { throw "Filtered template list omitted the generic template." }
Write-Host "PASS Filtered list includes the generic template"

$detail = Invoke-Api "GET" "/v1/templates/$($template.id)"
Assert-Status $detail 200 "Public template detail"
if ($detail.Json.variants.short.Count -ne 5 -or $detail.Json.variants.standard.Count -ne 8 -or $detail.Json.variants.detailed.Count -ne 18) {
    throw "Template variants do not contain 5 short, 8 standard, and 18 detailed steps."
}
Write-Host "PASS Template detail has all three configured step-count variants"

$email = Read-Host "Supabase test-user email"
$securePassword = Read-Host "Supabase test-user password" -AsSecureString
$password = [System.Net.NetworkCredential]::new("", $securePassword).Password
try {
    $login = Invoke-RestMethod -Method Post -Uri "$supabaseUrl/auth/v1/token?grant_type=password" `
        -Headers @{ apikey = $publishableKey } -ContentType "application/json" `
        -Body (@{ email = $email; password = $password } | ConvertTo-Json -Compress)
    $script:accessToken = $login.access_token
    if (-not $script:accessToken) { throw "Supabase sign-in did not return an access token." }
} finally {
    Remove-Variable password, securePassword -ErrorAction SilentlyContinue
    Remove-Variable login -ErrorAction SilentlyContinue
}

try {
    $breakdown = Invoke-Api "POST" "/v1/ai/breakdown" `
        @{ Authorization = "Bearer $script:accessToken" } `
        @{ title = "Organize my study plan"; task_type = "generic"; language = "en"; detail_level = 2 }
    Assert-Status $breakdown 200 "Template-backed breakdown"
    $result = $breakdown.Json
    if ($result.source -ne "template" -or $result.template_id -ne $template.id -or $result.steps.Count -ne 5) {
        throw "Breakdown did not return five suggestions from the seeded generic template."
    }
    if ($result.steps[0].planned_minutes -gt 10 -or $result.usage.used -ne 0 -or $result.usage.limit -ne 10) {
        throw "Breakdown response did not meet the first-step or usage contract."
    }
    foreach ($step in $result.steps) {
        if (-not $step.text -or $step.text.Length -gt 280 -or $step.planned_minutes -lt 1 -or $step.planned_minutes -gt 120) {
            throw "Breakdown returned a step outside the text or minutes limits."
        }
    }
    Write-Host "PASS Breakdown returned five validated template steps and usage fields"
    Write-Host "Stage 5 live API smoke test passed."
} finally {
    $script:accessToken = $null
}
