# Bot panel launcher for "__SERVER_NAME__" (Windows PowerShell 5.1 or PowerShell 7).
#
# Double-click the .cmd file and the bot's web panel opens in your browser. It runs
# on this PC as long as the window is open and talks to the bot through Discord -
# nothing to install or set up, works from anywhere.
#
# This file contains your personal panel key. Don't share it; /panel-launcher revoke
# makes it stop working. Made by /panel-launcher get (cogs/panel_launcher.py).

$ErrorActionPreference = 'Stop'
$Webhook = '__WEBHOOK_URL__'
$KeyId = '__KEY_ID__'
$Secret = [Convert]::FromBase64String('__SECRET__')
$ServerName = '__SERVER_NAME__'

$Version = 1
$Prefix = 'panel-relay:1'
$UserAgent = 'DiscordBot (https://github.com/ErikEdits/discord-bot, 1) PanelLauncher'
$AnswerTimeout = 45
$MaxBody = 8MB
$Gate = ([Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N'))
$script:TimeOffset = 0
$script:StaticCache = @{}
$Latin1 = [Text.Encoding]::GetEncoding(28591)
$Utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false

try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
Add-Type -AssemblyName System.Net.Http
$Http = New-Object System.Net.Http.HttpClient
$Http.Timeout = [TimeSpan]::FromSeconds(30)
$null = $Http.DefaultRequestHeaders.TryAddWithoutValidation('User-Agent', $UserAgent)
$Hmac = New-Object System.Security.Cryptography.HMACSHA256 -ArgumentList (, $Secret)
$Sha = [System.Security.Cryptography.SHA256]::Create()

function Get-Hex([byte[]]$Bytes) {
    return ([BitConverter]::ToString($Bytes) -replace '-', '').ToLowerInvariant()
}

function Get-Header($Response, [string]$Name) {
    $values = $null
    if ($Response.Headers.TryGetValues($Name, [ref]$values)) { return @($values)[0] }
    return $null
}

# One Discord API call with rate-limit and retry handling. $MakeContent builds the body
# again for every attempt (.NET disposes it after sending).
function Invoke-Discord([string]$Method, [string]$Url, [scriptblock]$MakeContent) {
    for ($attempt = 1; $attempt -le 8; $attempt++) {
        $request = New-Object System.Net.Http.HttpRequestMessage -ArgumentList @((New-Object System.Net.Http.HttpMethod -ArgumentList $Method), $Url)
        if ($MakeContent) { $request.Content = & $MakeContent }
        try {
            $response = $Http.SendAsync($request).GetAwaiter().GetResult()
        } catch {
            if ($attempt -ge 3) { throw "Can't reach Discord: $($_.Exception.Message)" }
            Start-Sleep -Milliseconds (500 * $attempt)
            continue
        }
        $status = [int]$response.StatusCode
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        $date = $response.Headers.Date
        if ($date) { $script:TimeOffset = $date.ToUnixTimeSeconds() - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
        if ($status -eq 429) {
            $wait = 1.0
            try { $wait = [double]((ConvertFrom-Json $Utf8.GetString($bytes)).retry_after) } catch {}
            Start-Sleep -Milliseconds ([int]([Math]::Min([Math]::Max($wait, 0.05), 30) * 1000) + 50)
            continue
        }
        if ($status -ge 500) { Start-Sleep -Milliseconds (500 * $attempt); continue }
        if ((Get-Header $response 'X-RateLimit-Remaining') -eq '0') {
            $reset = Get-Header $response 'X-RateLimit-Reset-After'
            if ($reset) { Start-Sleep -Milliseconds ([int]([Math]::Min([double]$reset, 30) * 1000)) }
        }
        return @{ Status = $status; Bytes = $bytes }
    }
    throw 'Discord is busy (rate limited) - try again in a moment.'
}

# Send one HTTP request to the bot through Discord.
function Invoke-Relay([string]$Method, [string]$Path, [string]$ContentType, [byte[]]$Body) {
    if ($null -eq $Body) { $Body = [byte[]]@() }
    $nonce = [Guid]::NewGuid().ToString('N').Substring(0, 24)
    $t = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + $script:TimeOffset
    $bodyHash = Get-Hex ($Sha.ComputeHash($Body))
    $signed = @($KeyId, $nonce, [string]$t, $Method, $Path, $ContentType, $bodyHash) -join "`n"
    $header = [ordered]@{ v = $Version; k = $KeyId; n = $nonce; t = $t; m = $Method; p = $Path; c = $ContentType; b = $bodyHash
                          s = (Get-Hex ($Hmac.ComputeHash($Utf8.GetBytes($signed)))) }
    $content = "$Prefix req " + (ConvertTo-Json $header -Compress)
    if ($content.Length -gt 2000) {
        return @{ Status = 414; Type = 'text/plain'; Location = ''; Body = $Utf8.GetBytes('Address too long for the relay.') }
    }
    $enc = $Utf8  # local copy: GetNewClosure() below only captures local variables
    $payload = [ordered]@{ content = $content; flags = 4; allowed_mentions = @{ parse = @() } }
    if ($Body.Length -gt 0) {
        $payload.attachments = @(@{ id = 0; filename = 'body.bin' })
        $json = ConvertTo-Json $payload -Compress -Depth 5
        $make = {
            $form = New-Object System.Net.Http.MultipartFormDataContent
            $form.Add((New-Object System.Net.Http.StringContent -ArgumentList @($json, $enc, 'application/json')), 'payload_json')
            $file = New-Object System.Net.Http.ByteArrayContent -ArgumentList (, $Body)
            $file.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('application/octet-stream')
            $form.Add($file, 'files[0]', 'body.bin')
            , $form  # the comma keeps PowerShell from unrolling the (enumerable) form into its parts
        }.GetNewClosure()
    } else {
        $json = ConvertTo-Json $payload -Compress -Depth 5
        $make = { New-Object System.Net.Http.StringContent -ArgumentList @($json, $enc, 'application/json') }.GetNewClosure()
    }
    $sent = Invoke-Discord 'POST' "$($Webhook)?wait=true" $make
    if ($sent.Status -eq 401 -or $sent.Status -eq 404) {
        throw 'The relay webhook is gone - get a new launcher file with /panel-launcher get.'
    }
    if ($sent.Status -ne 200) {
        throw "Discord refused the request (HTTP $($sent.Status))."
    }
    $messageId = (ConvertFrom-Json $Utf8.GetString($sent.Bytes)).id
    $deadline = [DateTime]::UtcNow.AddSeconds($AnswerTimeout)
    $delay = 300
    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds $delay
        $delay = [Math]::Min($delay + 100, 1000)
        $got = Invoke-Discord 'GET' "$Webhook/messages/$messageId" $null
        if ($got.Status -eq 404) { throw 'The request disappeared from the relay channel.' }
        if ($got.Status -ne 200) { continue }
        $message = ConvertFrom-Json $Utf8.GetString($got.Bytes)
        $text = [string]$message.content
        if (-not $text.StartsWith("$Prefix res ")) { continue }
        $answer = ConvertFrom-Json $text.Substring($Prefix.Length + 5)
        if ($answer.n -ne $nonce) { continue }
        $data = [byte[]]@()
        if ($message.attachments -and @($message.attachments).Count -gt 0) {
            $file = Invoke-Discord 'GET' (@($message.attachments)[0].url) $null
            if ($file.Status -ne 200) { throw "Couldn't download the answer (HTTP $($file.Status))." }
            $data = $file.Bytes
        }
        $status = 502
        if ($answer.s) { $status = [int]$answer.s }
        return @{ Status = $status; Type = [string]$answer.c; Location = [string]$answer.l; Body = $data }
    }
    throw "The bot didn't answer in time - is it online?"
}

# -------- Local web server (plain sockets: no admin rights, no URL reservations) --------

function New-Page([string]$Title, [string]$Text) {
    $html = "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Bot Panel</title></head>" +
            "<body style='font-family:sans-serif;max-width:640px;margin:60px auto'><h2>$Title</h2><p>$Text</p></body></html>"
    return $Utf8.GetBytes($html)
}

function Send-Response($Stream, [int]$Status, [string]$Type, [byte[]]$Body, [hashtable]$Extra) {
    if ($null -eq $Body) { $Body = [byte[]]@() }
    $reasons = @{ 200 = 'OK'; 204 = 'No Content'; 302 = 'Found'; 303 = 'See Other'; 307 = 'Temporary Redirect'
                  400 = 'Bad Request'; 403 = 'Forbidden'; 404 = 'Not Found'; 413 = 'Payload Too Large'; 502 = 'Bad Gateway' }
    $reason = $reasons[$Status]
    if (-not $reason) { $reason = 'Status' }
    $head = "HTTP/1.1 $Status $reason`r`nContent-Length: $($Body.Length)`r`nCache-Control: no-store`r`nConnection: close`r`n"
    if ($Type) { $head += "Content-Type: $Type`r`n" }
    if ($Extra) { foreach ($key in $Extra.Keys) { $head += "$($key): $($Extra[$key])`r`n" } }
    $head += "`r`n"
    $headBytes = $Latin1.GetBytes($head)
    $Stream.Write($headBytes, 0, $headBytes.Length)
    if ($Body.Length -gt 0) { $Stream.Write($Body, 0, $Body.Length) }
    $Stream.Flush()
}

function Invoke-Client($Client, [int]$Port) {
    $stream = $Client.GetStream()
    $stream.ReadTimeout = 15000
    $buffer = New-Object byte[] 65536
    $received = New-Object System.IO.MemoryStream
    $headerEnd = -1
    while ($headerEnd -lt 0) {
        $n = $stream.Read($buffer, 0, $buffer.Length)
        if ($n -le 0) { return }
        $received.Write($buffer, 0, $n)
        $headerEnd = $Latin1.GetString($received.ToArray()).IndexOf("`r`n`r`n")
        if ($headerEnd -lt 0 -and $received.Length -gt 65536) { return }
    }
    $all = $received.ToArray()
    $lines = $Latin1.GetString($all, 0, $headerEnd) -split "`r`n"
    $parts = $lines[0] -split ' '
    if ($parts.Count -lt 3) { return }
    $method = $parts[0].ToUpperInvariant()
    $path = $parts[1]
    $headers = @{}
    foreach ($line in ($lines | Select-Object -Skip 1)) {
        $i = $line.IndexOf(':')
        if ($i -gt 0) { $headers[$line.Substring(0, $i).Trim().ToLowerInvariant()] = $line.Substring($i + 1).Trim() }
    }
    $length = 0
    if ($headers['content-length']) { $length = [int]$headers['content-length'] }
    if ($length -gt $MaxBody) {
        Send-Response $stream 413 'text/html; charset=utf-8' (New-Page 'Too large' 'Max. 8 MB through the relay.') $null
        return
    }
    $body = New-Object System.IO.MemoryStream
    $bodyStart = $headerEnd + 4
    if ($all.Length -gt $bodyStart) { $body.Write($all, $bodyStart, $all.Length - $bodyStart) }
    while ($body.Length -lt $length) {
        $n = $stream.Read($buffer, 0, [Math]::Min($buffer.Length, $length - $body.Length))
        if ($n -le 0) { return }
        $body.Write($buffer, 0, $n)
    }
    $bodyBytes = $body.ToArray()

    if (@("localhost:$Port", "127.0.0.1:$Port") -notcontains $headers['host']) {
        Send-Response $stream 403 'text/plain' ($Utf8.GetBytes('Forbidden')) $null
        return
    }
    if ($path.StartsWith('/__launcher/start')) {
        if ($path -ceq "/__launcher/start?t=$Gate") {
            Send-Response $stream 303 '' $null @{ 'Location' = '/'; 'Set-Cookie' = "panel_gate=$Gate; Path=/; HttpOnly; SameSite=Strict" }
        } else {
            Send-Response $stream 403 'text/html; charset=utf-8' (New-Page 'Old link' 'Use the link shown in the launcher window.') $null
        }
        return
    }
    $cookies = @()
    if ($headers['cookie']) { $cookies = @($headers['cookie'] -split ';' | ForEach-Object { $_.Trim() }) }
    if ($cookies -cnotcontains "panel_gate=$Gate") {
        Send-Response $stream 403 'text/html; charset=utf-8' (New-Page 'Open the panel from the launcher' 'Use the link shown in the launcher window (or restart the launcher).') $null
        return
    }
    if ($path -eq '/favicon.ico') { Send-Response $stream 204 '' $null $null; return }
    $cacheable = ($method -eq 'GET' -and $path.StartsWith('/static/'))
    if ($cacheable -and $script:StaticCache.ContainsKey($path)) {
        $hit = $script:StaticCache[$path]
        Send-Response $stream 200 $hit.Type $hit.Body $null
        return
    }
    $contentType = ''
    if ($headers['content-type']) { $contentType = $headers['content-type'] }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $answer = Invoke-Relay $method $path $contentType $bodyBytes
    } catch {
        Write-Host "  ! $method $path : $($_.Exception.Message)" -ForegroundColor Yellow
        Send-Response $stream 502 'text/html; charset=utf-8' (New-Page "Can't reach the bot" $_.Exception.Message) $null
        return
    }
    $shown = $path
    if ($shown.Length -gt 70) { $shown = $shown.Substring(0, 70) }
    Write-Host ("  {0} {1} -> {2} ({3:0.0}s)" -f $method, $shown, $answer.Status, $watch.Elapsed.TotalSeconds)
    if ($cacheable -and $answer.Status -eq 200) { $script:StaticCache[$path] = @{ Type = $answer.Type; Body = $answer.Body } }
    $extra = $null
    if ($answer.Location) { $extra = @{ 'Location' = $answer.Location } }
    if ($method -eq 'HEAD') { $answer.Body = [byte[]]@() }
    Send-Response $stream $answer.Status $answer.Type $answer.Body $extra
}

function Stop-WithMessage([string]$Text) {
    Write-Host ''
    Write-Host "  $Text" -ForegroundColor Red
    Write-Host ''
    Read-Host '  Press Enter to close' | Out-Null
    exit 0  # already paused here; the .cmd only pauses on unexpected errors (exit code 1)
}

# -------- Start --------------------------------------------------------------

try { $Host.UI.RawUI.WindowTitle = "Bot Panel - $ServerName" } catch {}
Write-Host ''
Write-Host "  Bot panel launcher - $ServerName" -ForegroundColor Cyan
Write-Host '  Connecting to the bot through Discord...'
try {
    $check = Invoke-Discord 'GET' $Webhook $null
    if ($check.Status -eq 401 -or $check.Status -eq 404) {
        Stop-WithMessage "This launcher file no longer works (the relay was reset).`n  Get a new one in Discord with /panel-launcher get."
    }
    $ping = Invoke-Relay 'GET' '/__relay/ping' '' $null
} catch {
    Stop-WithMessage $_.Exception.Message
}
if ($ping.Status -ne 200) {
    $text = $Utf8.GetString($ping.Body)
    $match = [regex]::Match($text, '<p>(.*?)</p>')
    if ($match.Success) { $text = $match.Groups[1].Value -replace '</?code>', '' }
    Stop-WithMessage "The bot refused the launcher: $text"
}
$info = ConvertFrom-Json $Utf8.GetString($ping.Body)

$listener = $null
$port = 0
foreach ($candidate in 8765..8799) {
    try {
        $listener = New-Object System.Net.Sockets.TcpListener -ArgumentList @([System.Net.IPAddress]::Loopback, $candidate)
        $listener.Start()
        $port = $candidate
        break
    } catch { $listener = $null }
}
if (-not $listener) { Stop-WithMessage 'No free port between 8765 and 8799.' }

$url = "http://127.0.0.1:$port/__launcher/start?t=$Gate"
Write-Host "  Connected as $($info.user) ($($info.server))." -ForegroundColor Green
Write-Host ''
Write-Host "  Panel: $url"
Write-Host '  The panel runs as long as this window is open. Close it (or press Ctrl+C) to stop.'
Write-Host ''
if (-not $env:PANEL_LAUNCHER_NO_BROWSER) { Start-Process $url }

# Serve connections one by one, but only those that have sent something - browsers open
# spare connections they may never use, and those must not block the others.
$clients = New-Object System.Collections.ArrayList
try {
    while ($true) {
        while ($listener.Pending()) {
            [void]$clients.Add(@{ Client = $listener.AcceptTcpClient(); Since = [DateTime]::UtcNow })
        }
        $worked = $false
        foreach ($entry in @($clients)) {
            $client = $entry.Client
            if ($client.Available -gt 0) {
                $clients.Remove($entry)
                $worked = $true
                try { Invoke-Client $client $port }
                catch { Write-Host "  ! $($_.Exception.Message)" -ForegroundColor Yellow }
                finally { $client.Close() }
            } elseif (([DateTime]::UtcNow - $entry.Since).TotalSeconds -gt 60 -or
                      ($client.Client.Poll(0, [System.Net.Sockets.SelectMode]::SelectRead) -and $client.Available -eq 0)) {
                $clients.Remove($entry)
                $client.Close()
            }
        }
        if (-not $worked) { Start-Sleep -Milliseconds 20 }
    }
} finally {
    $listener.Stop()
}
