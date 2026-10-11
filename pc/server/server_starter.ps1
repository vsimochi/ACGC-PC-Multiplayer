# server_starter.ps1 - the helper behind "Start Server.bat" (Animal Crossing PC Multiplayer, Windows dedicated server).
#
# It reads server_config.ini, VALIDATES every value (and the combinations of values) BEFORE anything is started, writes the two settings that have no
# command-line switch (plus the "auto" values) into the game's real settings.ini, and then starts ACLauncher.exe with the verified arguments.
# The game itself is a windowed (GUI) program, so a bad command line would otherwise fail where nobody can see it: this script is the place where mistakes are reported.
#
#   -CheckOnly          validate and print what would be done (the arguments, the settings.ini changes); change nothing, start nothing. Exit code 0 = valid, 2 = invalid.
#   -WriteSettingsOnly  validate, apply the settings.ini changes, start nothing (used to test the settings.ini updater).
#
# Exit codes: 0 ok, 2 invalid configuration / refused to start, 3 could not start or the server did not stay running.
# PowerShell 5.1 compatible (the one that ships with Windows 10/11).
[CmdletBinding()]
param(
    [string]$Root = $PSScriptRoot,
    [string]$ConfigPath = '',
    [switch]$CheckOnly,
    [switch]$WriteSettingsOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

# --- the settings: name, default, and a help line used in messages. Everything the config may contain is listed here; any other key is refused (typo protection). ---
$Keys = @('port', 'max_peers', 'max_guests', 'allow_new_guests', 'resident_tokens', 'town_serve', 'personal_sync', 'interest_management', 'guest_memory_mb', 'debug_categories', 'check_updates')
$Defaults = @{
    port                = '7777'
    max_peers           = '8'
    max_guests          = '4'
    allow_new_guests    = '1'
    resident_tokens     = 'off'
    town_serve          = 'auto'
    personal_sync       = 'auto'
    interest_management = '1'
    guest_memory_mb     = '256'
    debug_categories    = ''
    check_updates       = '0'
}
# the game's -debug categories (names and aliases, from pc_log.c; a test keeps this list in step with the source) plus 'all'
$DebugNames = @('general', 'network', 'net', 'players', 'player', 'villagers', 'villager', 'npc', 'buildings', 'building', 'houses', 'items', 'item', 'wildlife', 'fish', 'bugs',
    'save', 'saves', 'events', 'event', 'transactions', 'transaction', 'txn', 'records', 'record', 'rec', 'guests', 'guest', 'townsvc', 'townservices', 'townservice',
    'mail', 'letters', 'shop', 'shops', 'all')

function Fail([string]$msg) {
    throw [System.InvalidOperationException]::new($msg)
}

function Read-WholeNumber([string]$name, [string]$value, [int]$min, [int]$max, [string]$what) {
    if ($value -notmatch '^[0-9]{1,9}$') {
        Fail "$name = '$value' is not valid: $what must be a whole number from $min to $max (digits only)."
    }
    $n = [int]$value
    if ($n -lt $min -or $n -gt $max) {
        Fail "$name = $value is out of range: $what must be a whole number from $min to $max."
    }
    return $n
}

function Read-Choice([string]$name, [string]$value, [string[]]$allowed) {
    $v = $value.ToLowerInvariant()
    if ($allowed -notcontains $v) {
        Fail "$name = '$value' is not valid: use one of  $($allowed -join ' | ')."
    }
    return $v
}

# --- server_config.ini parser: "key = value" lines, '#' or ';' at the start of a line = comment, blank lines ignored, a [section] header is ignored. ---
function Read-ServerConfig([string]$path) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Fail "server_config.ini was not found at:`n  $path`nIt must be in the same folder as Start Server.bat."
    }
    $text = [System.IO.File]::ReadAllText($path)
    $text = $text.TrimStart([char]0xFEFF)
    $found = @{}
    $n = 0
    foreach ($raw in ($text -split "`r?`n")) {
        $n++
        $line = $raw.Trim()
        if ($line -eq '' -or $line.StartsWith('#') -or $line.StartsWith(';') -or ($line.StartsWith('[') -and $line.EndsWith(']'))) { continue }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { Fail "server_config.ini line ${n}: '$line' is not a  name = value  line (put a # in front of notes)." }
        $k = $line.Substring(0, $eq).Trim().ToLowerInvariant()
        $v = $line.Substring($eq + 1).Trim()
        if ($Keys -notcontains $k) { Fail "server_config.ini line ${n}: unknown setting '$k'. Known settings: $($Keys -join ', ')." }
        if ($found.ContainsKey($k)) { Fail "server_config.ini line ${n}: '$k' appears twice (first on line $($found[$k].Line)). Keep ONE line per setting." }
        if ($v -match '[#;]') { Fail "server_config.ini line ${n}: '$k = $v' - put notes on their own line starting with #, not after the value." }
        if ($v -eq '' -and $k -ne 'debug_categories') { Fail "server_config.ini line ${n}: '$k' has no value. Give it a value, or delete the line to use the default ($($Defaults[$k]))." }
        $found[$k] = @{ Value = $v; Line = $n }
    }
    $cfg = @{}
    foreach ($k in $Keys) {
        if ($found.ContainsKey($k)) { $cfg[$k] = $found[$k].Value } else { $cfg[$k] = $Defaults[$k] }
    }
    return $cfg
}

# --- validates the whole configuration and returns the plan: the launcher arguments, the settings.ini changes, a summary ---
function New-ServerPlan($cfg) {
    $port = Read-WholeNumber 'port' $cfg.port 1 65535 'the UDP port'
    $peers = Read-WholeNumber 'max_peers' $cfg.max_peers 1 254 'the number of simultaneous connections'
    $guests = Read-WholeNumber 'max_guests' $cfg.max_guests 1 254 'the number of guests'
    $newg = Read-WholeNumber 'allow_new_guests' $cfg.allow_new_guests 0 1 'allow_new_guests'
    if ($cfg.allow_new_guests -notmatch '^[01]$') { Fail "allow_new_guests = '$($cfg.allow_new_guests)' is not valid: use 0 or 1." }
    $tokens = Read-Choice 'resident_tokens' $cfg.resident_tokens @('off', 'tofu', 'required')
    if ($cfg.town_serve.ToLowerInvariant() -eq 'full') { Fail "town_serve = full is not available in the server starter (it would hand every resident's private data to every client)." }
    $serve = Read-Choice 'town_serve' $cfg.town_serve @('auto', 'off', 'on')
    $psync = Read-Choice 'personal_sync' $cfg.personal_sync @('auto', 'on', 'off')
    $interest = Read-WholeNumber 'interest_management' $cfg.interest_management 0 1 'interest_management'
    if ($cfg.interest_management -notmatch '^[01]$') { Fail "interest_management = '$($cfg.interest_management)' is not valid: use 0 or 1." }
    $mem = Read-WholeNumber 'guest_memory_mb' $cfg.guest_memory_mb 1 1048576 'the guest memory budget in megabytes'
    $upd = Read-WholeNumber 'check_updates' $cfg.check_updates 0 1 'check_updates'
    if ($cfg.check_updates -notmatch '^[01]$') { Fail "check_updates = '$($cfg.check_updates)' is not valid: use 0 or 1." }

    # combinations
    if ($guests -gt $peers) {
        Fail "max_guests = $guests is larger than max_peers = $peers. Every guest needs one connection, so the guest limit can never exceed max_peers. Lower max_guests or raise max_peers."
    }
    if ($psync -eq 'off' -and $serve -ne 'off') {
        Fail "personal_sync = off together with town_serve = ${serve}: players who download the town get blank diaries/mail that only personal sync can give back. Set personal_sync = auto (or on), or set town_serve = off."
    }

    $dbg = @()
    $dbgText = $cfg.debug_categories.Trim()
    if ($dbgText -ne '' -and $dbgText.ToLowerInvariant() -ne 'none') {
        if ($dbgText -notmatch '^[A-Za-z]+(,[A-Za-z]+)*$') { Fail "debug_categories = '$dbgText' is not valid: use names separated by commas, no spaces (for example  network,guests ), or all, or leave it empty." }
        foreach ($t in $dbgText.Split(',')) {
            if ($DebugNames -notcontains $t.ToLowerInvariant()) { Fail "debug_categories: unknown category '$t'. Known: $(($DebugNames | Sort-Object -Unique) -join ', ')." }
            $dbg += $t.ToLowerInvariant()
        }
    }

    $launchArgs = @('--host', "$port", '--dedicated', '--max-peers', "$peers", '--max-guests', "$guests", '--allow-new-guests', "$newg", '--resident-tokens', $tokens)
    if ($serve -ne 'auto') { $launchArgs += @('--town-serve', $serve) }
    if ($psync -ne 'auto') { $launchArgs += @('--personal-sync', $psync) }
    if ($dbg.Count -gt 0) { $launchArgs += "--debug=$($dbg -join ',')" }
    if ($upd -eq 0) { $launchArgs += '--updater-skip' }

    # settings that live only in settings.ini: no command-line switch exists. "auto" has no switch either: it is written so a stale value (for example an old 'full') cannot apply.
    $ini = [ordered]@{ interest_management = "$interest"; guest_memory_mb = "$mem" }
    if ($serve -eq 'auto') { $ini['town_serve'] = 'auto' }
    if ($psync -eq 'auto') { $ini['personal_sync'] = 'auto' }

    return @{ Port = $port; Args = $launchArgs; Ini = $ini; CheckUpdates = $upd; Cfg = $cfg }
}

# --- settings.ini updater: changes ONLY the listed keys (value in place; a missing key is added to the [Network] section), keeps every other line, the line endings and the
#     file's encoding marker; writes nothing when nothing changes; replaces the file atomically. Returns the list of changes. ---
function Update-SettingsIni([string]$path, $wanted, [bool]$apply) {
    $changes = @()
    $exists = Test-Path -LiteralPath $path -PathType Leaf
    $bytes = @()
    if ($exists) { $bytes = [System.IO.File]::ReadAllBytes($path) }
    $bom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    # ISO-8859-1 maps every byte to one character and back, so bytes the game never wrote (a note typed in another code page) survive the rewrite unchanged
    $latin1 = [System.Text.Encoding]::GetEncoding(28591)
    $text = ''
    if ($exists) { $text = $latin1.GetString($bytes, $(if ($bom) { 3 } else { 0 }), $bytes.Length - $(if ($bom) { 3 } else { 0 })) }
    $nl = "`r`n"
    if ($text -match "[^\r]\n" -and $text -notmatch "\r\n") { $nl = "`n" }
    $endsNl = ($text -eq '' -or $text.EndsWith("`n"))
    $lines = New-Object System.Collections.Generic.List[string]
    if ($text -ne '') {
        $body = $text
        if ($endsNl) { $body = $body.Substring(0, $body.Length - 1); if ($body.EndsWith("`r")) { $body = $body.Substring(0, $body.Length - 1) } }
        foreach ($l in ($body -split "`r?`n")) { $lines.Add($l) }
    }
    $dirty = $false
    foreach ($k in $wanted.Keys) {
        $v = [string]$wanted[$k]
        $hit = $false
        for ($i = 0; $i -lt $lines.Count; $i++) {
            $m = [regex]::Match($lines[$i], '^(\s*)(' + [regex]::Escape($k) + ')(\s*=\s*)(.*?)\s*$', 'IgnoreCase')
            if ($m.Success) {
                $hit = $true
                if ($m.Groups[4].Value -ne $v) {
                    $changes += "$k : '$($m.Groups[4].Value)' -> '$v'"
                    $lines[$i] = $m.Groups[1].Value + $m.Groups[2].Value + $m.Groups[3].Value + $v
                    $dirty = $true
                }
            }
        }
        if (-not $hit) {
            $changes += "$k : (not present) -> '$v'"
            $dirty = $true
            $net = -1
            for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].Trim() -ieq '[Network]') { $net = $i } }
            if ($net -lt 0) {
                if ($lines.Count -gt 0 -and $lines[$lines.Count - 1].Trim() -ne '') { $lines.Add('') }
                $lines.Add('[Network]')
                $lines.Add("$k = $v")
            } else {
                $at = $net + 1
                while ($at -lt $lines.Count -and -not $lines[$at].Trim().StartsWith('[')) { $at++ }
                while ($at -gt $net + 1 -and $lines[$at - 1].Trim() -eq '') { $at-- }
                $lines.Insert($at, "$k = $v")
            }
        }
    }
    if ($dirty -and $apply) {
        $out = ($lines -join $nl) + $nl
        $pre = [byte[]]@()
        if ($bom) { $pre = [byte[]]@(0xEF, 0xBB, 0xBF) }
        $tmp = "$path.tmp-starter"
        [System.IO.File]::WriteAllBytes($tmp, [byte[]]($pre + $latin1.GetBytes($out)))
        if ($exists) { $old = "$path.starter-old"; [System.IO.File]::Replace($tmp, $path, $old); Remove-Item -LiteralPath $old -Force } else { [System.IO.File]::Move($tmp, $path) }
    }
    return , $changes
}

function Find-RunningServer([string]$exePath) {
    $found = @()
    foreach ($p in (Get-Process -Name 'AnimalCrossing' -ErrorAction SilentlyContinue)) {
        $pp = $null
        try { $pp = $p.Path } catch { $pp = $null }
        if (-not $pp) {
            # an elevated (administrator) process hides its path from a normal window: say so instead of silently assuming it is not ours
            Write-Host "Note: could not read the location of AnimalCrossing.exe (process $($p.Id)). If that is your server, close it before starting another." -ForegroundColor Yellow
        }
        if ($pp -and ([System.IO.Path]::GetFullPath($pp) -ieq [System.IO.Path]::GetFullPath($exePath))) { $found += $p }
    }
    return , $found
}

# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------
$code = 0
try {
    $Root = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($ConfigPath -eq '') { $ConfigPath = Join-Path $Root 'server_config.ini' }
    $cfg = Read-ServerConfig $ConfigPath
    $plan = New-ServerPlan $cfg
    $launcher = Join-Path $Root 'ACLauncher.exe'
    $game = Join-Path $Root 'AnimalCrossing.exe'
    $settingsPath = Join-Path $Root 'settings.ini'

    Write-Host ''
    Write-Host 'Animal Crossing multiplayer server - settings read from server_config.ini:'
    foreach ($k in $Keys) { Write-Host ('  {0,-20} {1}' -f $k, $(if ($cfg[$k] -eq '') { '(none)' } else { $cfg[$k] })) }
    Write-Host ''

    if ($CheckOnly) {
        Write-Host 'CHECK ONLY: nothing is changed or started.'
        Write-Host "LAUNCHER: $launcher"
        foreach ($a in $plan.Args) { Write-Host "ARG: $a" }
        $ch = Update-SettingsIni $settingsPath $plan.Ini $false
        foreach ($c in $ch) { Write-Host "INI: $c" }
        Write-Host 'The configuration is valid.'
        exit 0
    }

    if ($WriteSettingsOnly) {
        foreach ($c in (Update-SettingsIni $settingsPath $plan.Ini $true)) { Write-Host "INI: $c" }
        Write-Host 'settings.ini written; nothing started.'
        exit 0
    }

    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { Fail "ACLauncher.exe was not found in:`n  $Root`nPut Start Server.bat and server_config.ini in the game folder, next to ACLauncher.exe." }
    if (-not (Test-Path -LiteralPath $game -PathType Leaf)) { Fail "AnimalCrossing.exe was not found in:`n  $Root`nThe game files are incomplete." }
    $running = Find-RunningServer $game
    if ($running.Count -gt 0) {
        Fail "The game from this folder is already running (process $(($running | ForEach-Object { $_.Id }) -join ', ')). Close it first; if it is the server, type  stop  in the server console window. Two programs must never use the same save."
    }

    $changes = Update-SettingsIni $settingsPath $plan.Ini $true
    if ($changes.Count -gt 0) {
        Write-Host 'settings.ini was updated (the game reads it once at start; every other line was left alone):'
        foreach ($c in $changes) { Write-Host "  $c" }
        Write-Host ''
    }
    Write-Host "Starting the server on UDP port $($plan.Port) ..."
    Write-Host "  ACLauncher.exe $($plan.Args -join ' ')"
    $p = Start-Process -FilePath $launcher -ArgumentList ($plan.Args -join ' ') -WorkingDirectory $Root -PassThru
    $null = $p.Handle # keeps the process handle so ExitCode can be read after it ends
    $up = $false
    $deadline = (Get-Date).AddSeconds($(if ($plan.CheckUpdates -eq 0) { 25 } else { 60 }))
    $seenAt = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if ((Find-RunningServer $game).Count -gt 0) {
            if ($null -eq $seenAt) { $seenAt = Get-Date }
            if (((Get-Date) - $seenAt).TotalSeconds -ge 6) { $up = $true; break }
        } elseif ($null -ne $seenAt) {
            break # it started and ended again
        }
    }
    if ($up) {
        Write-Host ''
        Write-Host 'The server is running in its own window ("Animal Crossing dedicated server console").'
        Write-Host '  - If this is the first start it may ask questions there (use an existing town? what is the town name?). Answer them in THAT window.'
        Write-Host '  - To stop the server: type  stop  in that window and press Enter (it saves the town first). Do not just close the window.'
        Write-Host "  - Friends connect to your address and UDP port $($plan.Port) (see SERVER_SETUP.txt for the firewall and router steps)."
    } else {
        $code = 3
        Write-Host ''
        Write-Host 'The server did not start or did not stay running.' -ForegroundColor Red
        $lrc = $null
        try { if ($p.HasExited) { $lrc = $p.ExitCode } } catch { $lrc = $null }
        if ($null -ne $lrc -and $lrc -ne 0) { Write-Host "  - ACLauncher.exe itself ended with error code $lrc (it could not start the game)." -ForegroundColor Red }
        Write-Host '  - Look at updater.log and servers\default\logs\server.log in this folder.'
        if ($plan.CheckUpdates -ne 0) { Write-Host '  - check_updates = 1: the launcher may be waiting for you to answer an update window.' }
        Write-Host '  - Another program may already use the port, or the game files may be damaged.'
    }
} catch [System.InvalidOperationException] {
    Write-Host ''
    Write-Host 'The server was NOT started.' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ''
    Write-Host 'Fix server_config.ini (open it with Notepad) and run Start Server.bat again.'
    $code = 2
} catch {
    Write-Host ''
    Write-Host 'The server was NOT started: unexpected error.' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    $code = 3
}
exit $code
