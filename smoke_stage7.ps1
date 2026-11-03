$ErrorActionPreference = "Stop"

function Get-DotEnvValue([string]$name) {
    $line = Get-Content -LiteralPath ".env" | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if (-not $line) { return "" }
    return (($line -replace "^$name=", "").Trim().Trim('"'))
}

function Invoke-Api([string]$method, [string]$path, [hashtable]$headers = @{}, $body = $null) {
    $params = @{
        UseBasicParsing = $true
        Method = $method
        Uri = "$script:apiBase$path"
        Headers = $headers
        TimeoutSec = 30
    }
    if ($null -ne $body) {
        $params.ContentType = "application/json"
        $params.Body = ConvertTo-Json -InputObject $body -Depth 10 -Compress
    }
    try {
        $response = Invoke-WebRequest @params
        $status = [int]$response.StatusCode
        $content = $response.Content
        $responseHeaders = $response.Headers
    } catch {
        $raw = $_.Exception.Response
        if ($null -eq $raw) { throw "API request failed ($($_.Exception.GetType().Name)); no response body or credentials were printed." }
        $status = [int]$raw.StatusCode
        $responseHeaders = $raw.Headers
        $content = ""
        if ($raw.PSObject.Methods.Name -contains "GetResponseStream") {
            $stream = $raw.GetResponseStream()
            if ($stream) {
                $reader = [System.IO.StreamReader]::new($stream)
                $content = $reader.ReadToEnd()
                $reader.Dispose()
            }
        } elseif ($raw.Content -and $raw.Content.PSObject.Methods.Name -contains "ReadAsStringAsync") {
            $content = $raw.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        }
    }
    $json = if ($content) { try { $content | ConvertFrom-Json } catch { $null } } else { $null }
    return [pscustomobject]@{ Status = $status; Json = $json; Headers = $responseHeaders }
}

function Assert-Pass([bool]$condition, [string]$message) {
    if (-not $condition) { throw $message }
    Write-Host "PASS $message"
}

$supabaseUrl = (Get-DotEnvValue "SUPABASE_URL").TrimEnd('/')
$publishableKey = Get-DotEnvValue "SUPABASE_PUBLISHABLE_KEY"
if (-not $supabaseUrl -or -not $publishableKey) {
    throw "SUPABASE_URL or SUPABASE_PUBLISHABLE_KEY is missing from .env. Their values will not be printed."
}
$script:apiBase = "http://127.0.0.1:5000"
$email = Read-Host "Supabase test-user email"
$securePassword = Read-Host "Supabase test-user password" -AsSecureString
$password = [System.Net.NetworkCredential]::new("", $securePassword).Password
$script:accessToken = $null
$taskId = $null
$taskVersion = $null
$taskDeleted = $false
try {
    try {
        $login = Invoke-RestMethod -Method Post -Uri "$supabaseUrl/auth/v1/token?grant_type=password" `
            -Headers @{ apikey = $publishableKey } -ContentType "application/json" `
            -Body (@{ email = $email; password = $password } | ConvertTo-Json -Compress) -TimeoutSec 30
        $script:accessToken = $login.access_token
        if (-not $script:accessToken) { throw "No access token was returned." }
    } catch {
        throw "Supabase sign-in failed. Check the test-user credentials and project configuration; secret values were not logged."
    }
} finally {
    Remove-Variable password, securePassword, login -ErrorAction SilentlyContinue
}

$headers = @{ Authorization = "Bearer $script:accessToken" }
try {
    $bootstrap = Invoke-Api "GET" "/v1/bootstrap" $headers
    Assert-Pass ($bootstrap.Status -eq 200) "Bootstrap returns HTTP 200"
    Assert-Pass ($bootstrap.Json.profile.id -and $bootstrap.Json.reward_state -and $bootstrap.Json.template_manifest) `
        "Bootstrap includes profile, reward state, and template manifest"
    Assert-Pass ($bootstrap.Json.tasks.Count -le 50 -and $bootstrap.Json.active_task_count -ge $bootstrap.Json.tasks.Count) `
        "Bootstrap task count is bounded and active count is present"
    Assert-Pass ($bootstrap.Json.server_time -and $bootstrap.Json.api_version -eq "v1") `
        "Bootstrap includes server time and API version"
    Assert-Pass ($bootstrap.Headers["Cache-Control"] -match "private" -and $bootstrap.Headers["Cache-Control"] -match "must-revalidate") `
        "Bootstrap has private revalidation cache policy"

    $etag = [string]$bootstrap.Headers["ETag"]
    $conditional = Invoke-Api "GET" "/v1/bootstrap" (@{ Authorization = $headers.Authorization; "If-None-Match" = $etag })
    Assert-Pass ($conditional.Status -eq 304) "Unchanged bootstrap revalidates with HTTP 304"

    $since = [System.Uri]::EscapeDataString([string]$bootstrap.Json.server_time)
    $baseline = Invoke-Api "GET" "/v1/tasks?updated_since=$since&status=all&limit=50" $headers
    Assert-Pass ($baseline.Status -eq 200 -and $baseline.Json.next_since) "Initial task delta returns a server cursor"

    $createHeaders = @{ Authorization = $headers.Authorization; "Idempotency-Key" = "stage7-create-$([guid]::NewGuid().ToString('N'))" }
    $created = Invoke-Api "POST" "/v1/tasks" $createHeaders @{
        title = "Stage 7 read verification"
        notes = "Temporary task created and soft-deleted by the Stage 7 smoke test."
        steps = @(@{ text = "Verify delta tombstone"; planned_minutes = 5 })
    }
    Assert-Pass ($created.Status -eq 201) "Temporary task creation returns HTTP 201"
    $taskId = [string]$created.Json.id
    $taskVersion = [int]$created.Json.version

    $deltaSince = [System.Uri]::EscapeDataString([string]$baseline.Json.next_since)
    $delta = Invoke-Api "GET" "/v1/tasks?updated_since=$deltaSince&status=all&limit=50" $headers
    Assert-Pass ($delta.Status -eq 200 -and @($delta.Json.items | Where-Object { $_.id -eq $taskId }).Count -eq 1) `
        "Delta includes a newly created task"
    Assert-Pass ($delta.Json.next_since -and $delta.Json.snapshot_at) "Delta returns stable server snapshot cursors"

    $deleteHeaders = @{ Authorization = $headers.Authorization; "Idempotency-Key" = "stage7-delete-$([guid]::NewGuid().ToString('N'))" }
    $deleted = Invoke-Api "DELETE" "/v1/tasks/$taskId" $deleteHeaders @{ version = $taskVersion }
    Assert-Pass ($deleted.Status -eq 200) "Temporary task soft-delete returns HTTP 200"
    $taskDeleted = $true

    $deleteSince = [System.Uri]::EscapeDataString([string]$delta.Json.next_since)
    $tombstones = Invoke-Api "GET" "/v1/tasks?updated_since=$deleteSince&status=all&limit=50" $headers
    $tombstone = @($tombstones.Json.items | Where-Object { $_.id -eq $taskId }) | Select-Object -First 1
    Assert-Pass ($tombstones.Status -eq 200 -and $tombstone.deleted_at -and $tombstone.matches_filter -eq $false) `
        "Task delta includes the soft-delete tombstone"

    $oldSince = [System.Uri]::EscapeDataString((Get-Date).ToUniversalTime().AddDays(-31).ToString("o"))
    $expired = Invoke-Api "GET" "/v1/tasks?updated_since=$oldSince&status=all" $headers
    Assert-Pass ($expired.Status -eq 200 -and $expired.Json.full_refresh_required -eq $true) `
        "Expired delta cursor requests a full refresh"

    Write-Host "Stage 7 live API smoke test passed."
} finally {
    if ($taskId -and -not $taskDeleted) {
        try {
            $cleanupHeaders = @{ Authorization = $headers.Authorization; "Idempotency-Key" = "stage7-cleanup-$([guid]::NewGuid().ToString('N'))" }
            $null = Invoke-Api "DELETE" "/v1/tasks/$taskId" $cleanupHeaders @{ version = $taskVersion }
            Write-Host "Cleanup soft-deleted the temporary task."
        } catch {
            Write-Warning "Automatic cleanup failed. Delete temporary task $taskId manually if it remains."
        }
    }
    $script:accessToken = $null
    Remove-Variable email -ErrorAction SilentlyContinue
}
