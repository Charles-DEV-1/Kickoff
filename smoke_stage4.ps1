$ErrorActionPreference = "Stop"

function Get-DotEnvValue([string]$name) {
    $line = Get-Content -LiteralPath ".env" | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if (-not $line) { return "" }
    return (($line -replace "^$name=", "").Trim().Trim('"').TrimEnd('/'))
}

function Invoke-Api([string]$method, [string]$path, $body = $null, [string]$key = "") {
    $headers = @{ Authorization = "Bearer $script:accessToken" }
    if ($key) { $headers["Idempotency-Key"] = $key }
    try {
        if ($null -eq $body) {
            $response = Invoke-WebRequest -UseBasicParsing -Method $method -Uri "$script:apiBase$path" -Headers $headers
        } else {
            $response = Invoke-WebRequest -UseBasicParsing -Method $method -Uri "$script:apiBase$path" `
                -Headers $headers -ContentType "application/json" `
                -Body (ConvertTo-Json -InputObject $body -Depth 10 -Compress)
        }
        $status = [int]$response.StatusCode
        $headersOut = $response.Headers
        $content = $response.Content
    } catch {
        $response = $_.Exception.Response
        if ($null -eq $response) { throw "API connection failed: $($_.Exception.GetType().Name)" }
        $status = [int]$response.StatusCode
        $headersOut = $response.Headers
        $stream = $response.GetResponseStream()
        $content = if ($stream) { $reader = [System.IO.StreamReader]::new($stream); $reader.ReadToEnd(); $reader.Dispose() } else { "" }
    }
    $json = if ($content) { try { $content | ConvertFrom-Json } catch { $null } } else { $null }
    return [pscustomobject]@{ Status = $status; Headers = $headersOut; Json = $json }
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
$taskId = $null
$taskDeleted = $false
try {
    $email = Read-Host "Supabase test-user email"
    $securePassword = Read-Host "Supabase test-user password" -AsSecureString
    $password = [System.Net.NetworkCredential]::new("", $securePassword).Password
    $login = Invoke-RestMethod -Method Post -Uri "$supabaseUrl/auth/v1/token?grant_type=password" `
        -Headers @{ apikey = $publishableKey } -ContentType "application/json" `
        -Body (@{ email = $email; password = $password } | ConvertTo-Json -Compress)
    $script:accessToken = $login.access_token
    if (-not $script:accessToken) { throw "Supabase sign-in did not return an access token." }
    Remove-Variable password, securePassword, login

    $runId = [guid]::NewGuid().ToString()
    $created = Invoke-Api "POST" "/v1/tasks" @{
        title = "Stage 4 temporary $runId"
        notes = "temporary smoke test"
        steps = @(@{ text = "Complete this temporary step"; planned_minutes = 5 })
    } "stage4-create-$runId"
    Assert-Status $created 201 "Create temporary task and step"
    $taskId = $created.Json.id
    $step = $created.Json.steps[0]
    if (-not $taskId -or -not $step.id) { throw "Create response is missing task or step IDs." }

    $completeKey = "stage4-complete-$runId"
    $completed = Invoke-Api "POST" "/v1/steps/$($step.id)/complete" `
        @{ version = $step.version } $completeKey
    Assert-Status $completed 200 "Complete never-started step"
    $step = $completed.Json.step
    if ($step.started_at -ne $step.completed_at) { throw "Never-started completion did not set matching start and completion times." }
    if ($null -eq $completed.Json.reward_delta -or $completed.Json.reward_delta.points_awarded -lt 0) {
        throw "Completion response did not include a nonnegative reward_delta."
    }
    Write-Host "PASS Reward delta returned with step action"

    $replay = Invoke-Api "POST" "/v1/steps/$($created.Json.steps[0].id)/complete" `
        @{ version = $created.Json.steps[0].version } $completeKey
    Assert-Status $replay 200 "Retry completion with same key"
    if ($replay.Headers["Idempotent-Replay"] -ne "true") { throw "Retry did not return the idempotency replay header." }
    Write-Host "PASS Completion retry replayed the original response"

    $reopened = Invoke-Api "POST" "/v1/steps/$($step.id)/reopen" `
        @{ version = $step.version } "stage4-reopen-$runId"
    Assert-Status $reopened 200 "Reopen completed step"
    if ($reopened.Json.reward_delta.points_awarded -ne 0) { throw "Reopen incorrectly awarded points." }

    $step = $reopened.Json.step
    $redone = Invoke-Api "POST" "/v1/steps/$($step.id)/complete" `
        @{ version = $step.version } "stage4-redo-$runId"
    Assert-Status $redone 200 "Complete reopened step again"
    if ($redone.Json.reward_delta.points_awarded -ne 0) { throw "Repeating completion incorrectly awarded points." }

    $currentTask = Invoke-Api "GET" "/v1/tasks/$taskId"
    Assert-Status $currentTask 200 "Get task for cleanup"
    $deleted = Invoke-Api "DELETE" "/v1/tasks/$taskId" `
        @{ version = $currentTask.Json.version } "stage4-cleanup-$runId"
    Assert-Status $deleted 200 "Soft-delete temporary task"
    $taskDeleted = $true
    Write-Host "Stage 4 live API smoke test passed."
} finally {
    if ($taskId -and -not $taskDeleted -and $script:accessToken) {
        try {
            $current = Invoke-Api "GET" "/v1/tasks/$taskId"
            if ($current.Status -eq 200) {
                Invoke-Api "DELETE" "/v1/tasks/$taskId" `
                    @{ version = $current.Json.version } "stage4-cleanup-$([guid]::NewGuid().ToString())" | Out-Null
                Write-Host "Cleanup removed temporary task."
            }
        } catch { Write-Warning "Cleanup failed; delete temporary task $taskId manually if it remains." }
    }
    $script:accessToken = $null
}
