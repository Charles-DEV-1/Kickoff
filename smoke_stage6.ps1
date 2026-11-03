$ErrorActionPreference = "Stop"

function Get-DotEnvValue([string]$name) {
    $line = Get-Content -LiteralPath ".env" | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if (-not $line) { return "" }
    return (($line -replace "^$name=", "").Trim().Trim('"'))
}

function Invoke-Api([string]$method, [string]$path, [hashtable]$headers = @{}, $body = $null) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Method $method -Uri "$script:apiBase$path" `
            -Headers $headers -ContentType "application/json" `
            -Body (ConvertTo-Json -InputObject $body -Depth 10 -Compress)
        $status = [int]$response.StatusCode
        $content = $response.Content
        $requestId = $response.Headers["X-Request-Id"]
    } catch {
        $response = $_.Exception.Response
        if ($null -eq $response) { throw "API connection failed: $($_.Exception.GetType().Name)" }
        $status = [int]$response.StatusCode
        $requestId = $response.Headers["X-Request-Id"]
        $content = ""
        $stream = $response.GetResponseStream()
        if ($stream) {
            $reader = [System.IO.StreamReader]::new($stream)
            $content = $reader.ReadToEnd()
            $reader.Dispose()
        }
    }
    $json = if ($content) { try { $content | ConvertFrom-Json } catch { $null } } else { $null }
    return [pscustomobject]@{ Status = $status; Json = $json; RequestId = $requestId }
}

$provider = Get-DotEnvValue "AI_PROVIDER"
$groqApiKey = Get-DotEnvValue "GROQ_API_KEY"
$supabaseUrl = (Get-DotEnvValue "SUPABASE_URL").TrimEnd('/')
$publishableKey = Get-DotEnvValue "SUPABASE_PUBLISHABLE_KEY"
if ($provider -ne "groq") { throw "Set AI_PROVIDER=groq in .env, then restart Flask." }
if (-not $groqApiKey) { throw "Add GROQ_API_KEY to .env, then restart Flask. The key is never displayed by this script." }
if (-not $supabaseUrl -or -not $publishableKey) { throw "SUPABASE_URL or SUPABASE_PUBLISHABLE_KEY is missing from .env." }
$groqApiKey = $null

$script:apiBase = "http://127.0.0.1:5000"
$email = Read-Host "Supabase test-user email"
$securePassword = Read-Host "Supabase test-user password" -AsSecureString
$password = [System.Net.NetworkCredential]::new("", $securePassword).Password
$script:accessToken = $null
try {
    try {
        $login = Invoke-RestMethod -Method Post -Uri "$supabaseUrl/auth/v1/token?grant_type=password" `
            -Headers @{ apikey = $publishableKey } -ContentType "application/json" `
            -Body (@{ email = $email; password = $password } | ConvertTo-Json -Compress)
        $script:accessToken = $login.access_token
        if (-not $script:accessToken) { throw "No access token was returned." }
    } catch {
        throw "Supabase sign-in failed. Check the test-user credentials and Supabase configuration; secret values were not logged."
    }
} finally {
    Remove-Variable password, securePassword, login -ErrorAction SilentlyContinue
}

try {
    $payload = @{
        title = "Stage 6 Groq integration check"
        description = "Create a short practical plan to review one lesson and answer practice questions."
        task_type = "exam_revision"
        language = "en"
        detail_level = 2
    }
    $headers = @{ Authorization = "Bearer $script:accessToken" }
    $first = Invoke-Api "POST" "/v1/ai/breakdown" $headers $payload
    if ($first.Status -ne 200) {
        $code = if ($first.Json -and $first.Json.error) { $first.Json.error.code } else { "no_error_code" }
        throw "Groq breakdown returned HTTP $($first.Status) ($code)."
    }
    if ($first.Json.source -ne "ai" -or $first.Json.steps.Count -ne 5) {
        throw "The API returned the template fallback (request_id=$($first.RequestId)). Check the Flask console for its safe ai_fallback_reason. Also confirm migration 007, SUPABASE_SECRET_KEY, AI_CACHE_HMAC_KEY, GROQ_API_KEY, and that Flask restarted. No key or task text was printed."
    }
    foreach ($step in $first.Json.steps) {
        if (-not $step.text -or $step.text.Length -gt 280 -or $step.planned_minutes -lt 1 -or $step.planned_minutes -gt 120) {
            throw "Groq response contained a step outside the API validation limits."
        }
    }
    if ($first.Json.usage.used -lt 1 -or $first.Json.usage.limit -lt $first.Json.usage.used) {
        throw "AI daily usage was not committed as expected."
    }
    Write-Host "PASS Groq-generated breakdown (HTTP 200, five validated steps)"

    $retry = Invoke-Api "POST" "/v1/ai/breakdown" $headers $payload
    if ($retry.Status -ne 200 -or $retry.Json.source -ne "ai") {
        throw "Repeated breakdown did not return the cached AI result."
    }
    if ($retry.Json.usage.used -ne $first.Json.usage.used) {
        throw "Cache replay unexpectedly incremented AI daily usage."
    }
    Write-Host "PASS Cached AI response did not increment daily usage"
    Write-Host "Stage 6 Groq live API smoke test passed."
} finally {
    $script:accessToken = $null
    Remove-Variable email, groqApiKey -ErrorAction SilentlyContinue
}
