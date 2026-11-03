param([string]$CleanupTaskId)

$ErrorActionPreference = "Stop"

function Get-DotEnvValue([string]$name) {
    $line = Get-Content -LiteralPath ".env" | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if (-not $line) { return "" }
    return (($line -replace "^$name=", "").Trim().Trim('"').TrimEnd('/'))
}

function Invoke-Api([string]$method, [string]$path, $body = $null, [string]$key = "") {
    $headers = @{ Authorization = "Bearer $script:accessToken" }
    if ($key) { $headers["Idempotency-Key"] = $key }
    $responseContent = ""
    try {
        if ($null -eq $body) {
            $response = Invoke-WebRequest -Method $method -Uri "$script:apiBase$path" -Headers $headers
        } else {
            $json = ConvertTo-Json -InputObject $body -Depth 10 -Compress
            $response = Invoke-WebRequest -Method $method -Uri "$script:apiBase$path" `
                -Headers $headers -ContentType "application/json" -Body $json
        }
        $statusCode = [int]$response.StatusCode
        $responseHeaders = $response.Headers
        $responseContent = $response.Content
    } catch {
        $response = $_.Exception.Response
        if ($null -eq $response) {
            return [pscustomobject]@{ StatusCode = 0; Headers = $null; Json = $null; ErrorCode = "transport_error" }
        }
        $statusCode = [int]$response.StatusCode
        $responseHeaders = $response.Headers
        if ($response -is [System.Net.Http.HttpResponseMessage]) {
            $responseContent = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        } else {
            $stream = $response.GetResponseStream()
            if ($stream) {
                $reader = [System.IO.StreamReader]::new($stream)
                $responseContent = $reader.ReadToEnd()
                $reader.Dispose()
            }
        }
    }
    $parsed = $null
    if ($responseContent) { try { $parsed = $responseContent | ConvertFrom-Json } catch { } }
    $errorCode = if ($parsed -and $parsed.error) { $parsed.error.code } else { "" }
    return [pscustomobject]@{ StatusCode = $statusCode; Headers = $responseHeaders; Json = $parsed; ErrorCode = $errorCode }
}

function Expect-Status($response, [int]$expected, [string]$label) {
    if ($response.StatusCode -ne $expected) {
        $code = if ($response.ErrorCode) { $response.ErrorCode } else { "no_error_code" }
        throw "$label returned HTTP $($response.StatusCode) ($code); expected HTTP $expected."
    }
    Write-Host "PASS $label (HTTP $expected)"
}

$supabaseUrl = Get-DotEnvValue "SUPABASE_URL"
$publishableKey = Get-DotEnvValue "SUPABASE_PUBLISHABLE_KEY"
if (-not $supabaseUrl -or -not $publishableKey) {
    throw "SUPABASE_URL or SUPABASE_PUBLISHABLE_KEY is missing from .env."
}

$script:apiBase = "http://127.0.0.1:5000"
$script:accessToken = $null
$taskId = $null
$taskDeleted = $false
try {
    $email = Read-Host "Supabase test-user email"
    $securePassword = Read-Host "Supabase test-user password" -AsSecureString
    $password = [System.Net.NetworkCredential]::new("", $securePassword).Password
    $login = Invoke-RestMethod -Method Post `
        -Uri "$supabaseUrl/auth/v1/token?grant_type=password" `
        -Headers @{ apikey = $publishableKey } -ContentType "application/json" `
        -Body (@{ email = $email; password = $password } | ConvertTo-Json -Compress)
    $script:accessToken = $login.access_token
    if (-not $script:accessToken) { throw "Supabase sign-in did not return an access token." }
    Remove-Variable password, securePassword, login

    $health = Invoke-WebRequest -Uri "$script:apiBase/v1/health" -TimeoutSec 5
    Expect-Status $health 200 "Local API health"

    if ($CleanupTaskId) {
        $taskId = $CleanupTaskId
        $existing = Invoke-Api "GET" "/v1/tasks/$taskId"
        if ($existing.StatusCode -eq 404) {
            Write-Host "Task was already deleted or is not visible to this test user."
            $taskDeleted = $true
        } else {
            Expect-Status $existing 200 "Read task for cleanup"
            $cleanupKey = "stage3-cleanup-$([guid]::NewGuid().ToString())"
            $removed = Invoke-Api "DELETE" "/v1/tasks/$taskId" `
                @{ version = $existing.Json.version } $cleanupKey
            Expect-Status $removed 200 "Soft-delete task and its steps"
            $taskDeleted = $true
            Write-Host "Cleanup complete."
        }
    } else {
    $runId = [guid]::NewGuid().ToString()
    $dueDate = [DateTime]::UtcNow.AddDays(1).ToString("yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
    $since = [DateTime]::UtcNow.AddMinutes(-1).ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
    $createKey = "stage3-create-$runId"
    $createBody = @{
        title = "Stage 3 smoke $runId"
        notes = "temporary smoke-test task"
        due_at = "${dueDate}T12:00:00Z"
        steps = @(
            @{ text = "Temporary step A"; planned_minutes = 5 },
            @{ text = "Temporary step B"; planned_minutes = 10 }
        )
    }

    $created = Invoke-Api "POST" "/v1/tasks" $createBody $createKey
    Expect-Status $created 201 "Create task with steps"
    $task = $created.Json
    $taskId = $task.id
    if (-not $taskId -or $task.steps.Count -ne 2) { throw "Create response did not contain both steps." }

    $replay = Invoke-Api "POST" "/v1/tasks" $createBody $createKey
    Expect-Status $replay 201 "Retry create with same key"
    if ($replay.Json.id -ne $taskId -or $replay.Headers["Idempotent-Replay"] -ne "true") {
        throw "The repeated create did not replay the original response."
    }
    Write-Host "PASS idempotency replay returned the same task"

    $read = Invoke-Api "GET" "/v1/tasks/$taskId"
    Expect-Status $read 200 "Get task"
    $query = "/v1/tasks?status=active&due_date=$dueDate&updated_since=$([uri]::EscapeDataString($since))&limit=10"
    $listed = Invoke-Api "GET" $query
    Expect-Status $listed 200 "List task with filters"
    if (-not ($listed.Json.items | Where-Object { $_.id -eq $taskId })) { throw "Filtered list missed the created task." }

    $updated = Invoke-Api "PATCH" "/v1/tasks/$taskId" `
        @{ version = $task.version; title = "Updated Stage 3 smoke $runId" } "stage3-patch-$runId"
    Expect-Status $updated 200 "Versioned task update"
    $task = $updated.Json

    $reversedIds = @($task.steps | Sort-Object position -Descending | ForEach-Object { $_.id })
    $reordered = Invoke-Api "PUT" "/v1/tasks/$taskId/steps/order" `
        @{ version = $task.version; step_ids = $reversedIds } "stage3-order-$runId"
    Expect-Status $reordered 200 "Atomic step reorder"
    $task = $reordered.Json
    $storedIds = @($task.steps | Sort-Object position | ForEach-Object { $_.id })
    if (($storedIds -join ",") -ne ($reversedIds -join ",")) { throw "The stored order differs from the requested order." }

    $added = Invoke-Api "POST" "/v1/tasks/$taskId/steps" `
        @{ text = "Temporary step C"; planned_minutes = 15 } "stage3-add-step-$runId"
    Expect-Status $added 201 "Add step"
    $step = $added.Json.step
    $edited = Invoke-Api "PATCH" "/v1/steps/$($step.id)" `
        @{ version = $step.version; text = "Edited temporary step C" } "stage3-edit-step-$runId"
    Expect-Status $edited 200 "Versioned step edit"
    $step = $edited.Json.step
    $removed = Invoke-Api "DELETE" "/v1/steps/$($step.id)" `
        @{ version = $step.version } "stage3-delete-step-$runId"
    Expect-Status $removed 200 "Soft-delete step"

    $task = (Invoke-Api "GET" "/v1/tasks/$taskId").Json
    $removedTask = Invoke-Api "DELETE" "/v1/tasks/$taskId" `
        @{ version = $task.version } "stage3-delete-task-$runId"
    Expect-Status $removedTask 200 "Soft-delete task and remaining steps"
    $taskDeleted = $true
    Write-Host "Stage 3 live API smoke test passed; temporary task was deleted."
    }
} finally {
    if ($taskId -and -not $taskDeleted -and $script:accessToken) {
        try {
            $latest = Invoke-Api "GET" "/v1/tasks/$taskId"
            if ($latest.StatusCode -eq 200) {
                $cleanupKey = "stage3-cleanup-$([guid]::NewGuid().ToString())"
                $null = Invoke-Api "DELETE" "/v1/tasks/$taskId" `
                    @{ version = $latest.Json.version } $cleanupKey
                Write-Host "Cleanup attempted for temporary task $taskId."
            }
        } catch { Write-Host "Automatic cleanup failed; delete temporary task $taskId manually if it remains." }
    }
    Remove-Variable accessToken -Scope Script -ErrorAction SilentlyContinue
}
