<#
.SYNOPSIS
mgy (multigravity) - Profile manager for Antigravity CLI (agy) on Windows
Manages isolated credentials, sessions, and configurations per profile.
#>
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$Command = if ($args.Count -gt 0) { [string]$args[0] } else { $null }
[string[]]$RemainingArgs = @($args | Select-Object -Skip 1)


# Resolve Home and Profiles Directory
$RealHome = if ($env:REAL_HOME) { 
    $env:REAL_HOME 
} elseif ($env:USERPROFILE -and $env:USERPROFILE -like "*\.config\multigravity-profiles*") {
    $env:USERPROFILE.Substring(0, $env:USERPROFILE.IndexOf("\.config\multigravity-profiles"))
} elseif ($env:USERPROFILE) { 
    $env:USERPROFILE 
} else { 
    $env:HOME 
}

$ProfilesDir = if ($env:MULTIGRAVITY_PROFILES_DIR) { 
    $env:MULTIGRAVITY_PROFILES_DIR 
} else { 
    Join-Path $RealHome ".config\multigravity-profiles" 
}

# Windows Credential Manager P/Invoke definition for isolating Go-Keyring tokens
$WinCredSource = @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public class WinCredManager {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct CREDENTIAL {
        public uint Flags;
        public uint Type;
        public string TargetName;
        public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredRead(string target, uint type, int reservedFlag, out IntPtr credentialPtr);

    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredWrite([In] ref CREDENTIAL userCredential, uint flags);

    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredDelete(string target, uint type, int flags);

    [DllImport("advapi32.dll", SetLastError = true)]
    public static extern void CredFree([In] IntPtr buffer);

    public static string ReadCredential(string target) {
        string res = ReadSingleCredential(target);
        if (res == null && target.Contains(":")) {
            res = ReadSingleCredential(target.Replace(':', '/'));
        } else if (res == null && target.Contains("/")) {
            res = ReadSingleCredential(target.Replace('/', ':'));
        }
        return res;
    }

    private static string ReadSingleCredential(string target) {
        IntPtr credPtr;
        if (CredRead(target, 1, 0, out credPtr)) {
            try {
                CREDENTIAL cred = (CREDENTIAL)Marshal.PtrToStructure(credPtr, typeof(CREDENTIAL));
                if (cred.CredentialBlobSize > 0 && cred.CredentialBlob != IntPtr.Zero) {
                    byte[] bytes = new byte[cred.CredentialBlobSize];
                    Marshal.Copy(cred.CredentialBlob, bytes, 0, (int)cred.CredentialBlobSize);
                    return Encoding.UTF8.GetString(bytes);
                }
            } finally {
                CredFree(credPtr);
            }
        }
        return null;
    }

    public static bool WriteCredential(string target, string data) {
        byte[] bytes = Encoding.UTF8.GetBytes(data);
        IntPtr blobPtr = Marshal.AllocHGlobal(bytes.Length);
        try {
            Marshal.Copy(bytes, 0, blobPtr, bytes.Length);
            CREDENTIAL cred = new CREDENTIAL();
            cred.Type = 1; // CRED_TYPE_GENERIC
            cred.TargetName = target;
            cred.CredentialBlobSize = (uint)bytes.Length;
            cred.CredentialBlob = blobPtr;
            cred.Persist = 2; // CRED_PERSIST_LOCAL_MACHINE
            cred.UserName = "antigravity";
            return CredWrite(ref cred, 0);
        } finally {
            Marshal.FreeHGlobal(blobPtr);
        }
    }

    public static bool DeleteCredential(string target) {
        bool d1 = CredDelete(target, 1, 0);
        if (target.Contains(":")) {
            CredDelete(target.Replace(':', '/'), 1, 0);
        } else if (target.Contains("/")) {
            CredDelete(target.Replace('/', ':'), 1, 0);
        }
        return d1;
    }
}
"@

if (-not ([System.Management.Automation.PSTypeName]'WinCredManager').Type) {
    try {
        Add-Type -TypeDefinition $WinCredSource -ErrorAction SilentlyContinue
    } catch {}
}

function Show-Help {
    @"
Usage: mgy [command] [options]

Commands:
  new <profile>          Create a new isolated profile and authenticate
  import <profile|tar.gz> Import host session into profile OR import conversation archive
  list                   List all existing profiles
  quotas, quota, quotes  Check model quota usage across all profiles
  conversations          List recent conversations and their IDs
  fork --conversation=<id> Fork an existing conversation across all other profiles
  export <conv_id> [tar] Export a conversation into a portable tar.gz archive
  import-conversation <tar> Alias for import conversation archive
  delete <profile>       Delete an existing profile
  help, --help, -h       Show this help message
  <profile> [agy args]   Start agy session using the specified profile

Resume & Conversation Flags (same as agy):
  mgy <profile> --conversation=<id>   Resume a specific conversation by ID
  mgy <profile> -c, --continue        Continue the most recent conversation

Examples:
  mgy new work                 # Set up a new profile with Google login
  mgy list                     # List all registered profiles
  mgy fork --conversation=<id> # Fork conversation across all profiles
  mgy export <id> [path.tar.gz] # Export conversation to a portable archive
  mgy import <path.tar.gz>      # Import conversation archive into agy
  mgy import-conversation <archive> # (Alias) Import conversation archive
  mgy work                     # Start interactive agy session with profile
  mgy work -c                  # Continue latest chat under profile
  mgy work --conversation=<id> # Resume specific conversation by ID
  mgy conversations            # View list of recent conversation IDs
  mgy quotas                   # Inspect quotas across all profiles
"@
}

function Setup-ProfileEnvironment([string]$TargetDir) {
    $geminiDir = Join-Path $TargetDir ".gemini"
    $targetAgyDir = Join-Path $geminiDir "antigravity-cli"
    $sourceAgyDir = Join-Path $RealHome ".gemini\antigravity-cli"

    if (-not (Test-Path $geminiDir)) {
        New-Item -ItemType Directory -Path $geminiDir -Force | Out-Null
    }
    if (-not (Test-Path $sourceAgyDir)) {
        New-Item -ItemType Directory -Path $sourceAgyDir -Force | Out-Null
    }

    # Create NTFS Junction (shared conversations, history, MCP tools)
    $needsJunction = $true
    if (Test-Path $targetAgyDir) {
        try {
            $item = Get-Item -LiteralPath $targetAgyDir -Force
            if ($item.Attributes.HasFlag([System.IO.FileAttributes]::ReparsePoint)) {
                $needsJunction = $false
            } else {
                $children = Get-ChildItem -LiteralPath $targetAgyDir -Force -ErrorAction SilentlyContinue
                if (-not $children -or $children.Count -eq 0) {
                    Remove-Item -LiteralPath $targetAgyDir -Force -Recurse -ErrorAction SilentlyContinue
                } else {
                    $needsJunction = $false
                }
            }
        } catch {
            $needsJunction = $false
        }
    }

    if ($needsJunction -and -not (Test-Path $targetAgyDir)) {
        try {
            New-Item -ItemType Junction -Path $targetAgyDir -Target $sourceAgyDir -ErrorAction Stop | Out-Null
        } catch {
            cmd /c mklink /J "$targetAgyDir" "$sourceAgyDir" 2>$null | Out-Null
        }
    }

    # Link host .gitconfig (git identity preservation)
    $sourceGit = Join-Path $RealHome ".gitconfig"
    $targetGit = Join-Path $TargetDir ".gitconfig"
    if ((Test-Path $sourceGit) -and -not (Test-Path $targetGit)) {
        try {
            New-Item -ItemType HardLink -Path $targetGit -Target $sourceGit -ErrorAction Stop | Out-Null
        } catch {
            Copy-Item -Path $sourceGit -Destination $targetGit -Force
        }
    }

    # Link host .ssh (keys preservation)
    $sourceSsh = Join-Path $RealHome ".ssh"
    $targetSsh = Join-Path $TargetDir ".ssh"
    if ((Test-Path $sourceSsh) -and -not (Test-Path $targetSsh)) {
        try {
            New-Item -ItemType Junction -Path $targetSsh -Target $sourceSsh -ErrorAction Stop | Out-Null
        } catch {
            cmd /c mklink /J "$targetSsh" "$sourceSsh" 2>$null | Out-Null
        }
    }
}

function Import-ConversationArchive([string]$ArchivePath) {
    if (-not $ArchivePath) {
        Write-Error "Error: Missing archive path. Usage: mgy import <path_to_tar_gz>"
        exit 1
    }

    if (-not [System.IO.Path]::IsPathRooted($ArchivePath)) {
        $absArchive = Join-Path (Get-Location).Path $ArchivePath
    } else {
        $absArchive = $ArchivePath
    }

    if (-not (Test-Path $absArchive)) {
        Write-Error "Error: Archive file not found: $absArchive"
        exit 1
    }

    $targetDir = Join-Path $RealHome ".gemini\antigravity-cli"
    New-Item -ItemType Directory -Path (Join-Path $targetDir "conversations") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $targetDir "brain") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $targetDir "annotations") -Force | Out-Null

    Write-Host "Importing conversation archive: $absArchive..." -ForegroundColor Cyan

    $pythonCmd = $null
    if (Get-Command "python3" -ErrorAction SilentlyContinue) {
        $pythonCmd = "python3"
    } elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
        $pythonCmd = "python"
    }

    if ($pythonCmd) {
        $pyScript = @"
import sqlite3, json, os, tarfile, zipfile, re

target_dir = r'$targetDir'
abs_tar = r'$absArchive'

imported_id = None
try:
    if zipfile.is_zipfile(abs_tar):
        with zipfile.ZipFile(abs_tar, 'r') as zf:
            zf.extractall(target_dir)
            for name in zf.namelist():
                mat = re.search(r'(?:database/|conversations/)?([a-zA-Z0-9-]{36})\.db$', name)
                if mat:
                    imported_id = mat.group(1)
                    break
    else:
        with tarfile.open(abs_tar, 'r:*') as tar:
            tar.extractall(target_dir)
            for m in tar.getmembers():
                mat = re.search(r'(?:database/|conversations/)?([a-zA-Z0-9-]{36})\.db$', m.name)
                if mat:
                    imported_id = mat.group(1)
                    break
except Exception as e:
    pass

if imported_id:
    sum_db = os.path.join(target_dir, 'conversation_summaries.db')
    meta_file = os.path.join(target_dir, 'brain', imported_id, '.conversation_summary.json')
    if os.path.exists(sum_db) and os.path.exists(meta_file):
        try:
            with open(meta_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            conn = sqlite3.connect(sum_db, timeout=5.0)
            cur = conn.cursor()
            cols = []
            vals = []
            for k, v in data.items():
                cols.append(k)
                if isinstance(v, dict) and "__bytes__" in v:
                    vals.append(bytes.fromhex(v["__bytes__"]))
                else:
                    vals.append(v)
            placeholders = ",".join(["?"] * len(cols))
            sql = f"INSERT OR REPLACE INTO conversation_summaries ({','.join(cols)}) VALUES ({placeholders})"
            cur.execute(sql, vals)
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.close()
        except Exception:
            pass
    print(imported_id)
"@
        $importedId = ($pyScript | & $pythonCmd - | Out-String).Trim()
    } elseif (Get-Command "tar" -ErrorAction SilentlyContinue) {
        tar.exe -xzf "$absArchive" -C "$targetDir"
        $importedId = (tar.exe -tf "$absArchive" | Select-String -Pattern 'conversations/([a-zA-Z0-9-]+)\.db$' | ForEach-Object { $_.Matches.Groups[1].Value } | Select-Object -First 1)
    } else {
        Write-Error "Error: Neither Python nor tar.exe found to extract tar.gz archive."
        exit 1
    }

    Write-Host "==> Successfully extracted conversation files to $targetDir" -ForegroundColor Green
    if ($importedId) {
        Write-Host "Imported conversation ID: $importedId" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "You can resume it using:"
        Write-Host "  mgy <profile> --conversation $importedId"
        Write-Host "  agy --conversation $importedId"
    }
}

function Get-ProfileTokenAccount([string]$TokenJson) {
    if (-not $TokenJson) { return $null }
    try {
        $clean = $TokenJson.Trim().TrimStart([char]0xFEFF)
        if ($clean.StartsWith("go-keyring-base64:")) {
            $b64 = $clean.Substring("go-keyring-base64:".Length)
            $bytes = [System.Convert]::FromBase64String($b64)
            $clean = [System.Text.Encoding]::UTF8.GetString($bytes)
        }
        $obj = $clean | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($obj.id_token_email) { return $obj.id_token_email }
        if ($obj.email) { return $obj.email }
        if ($obj.token) {
            if ($obj.token.id_token_email) { return $obj.token.id_token_email }
            if ($obj.token.email) { return $obj.token.email }
        }
        $idToken = $obj.id_token
        if (-not $idToken -and $obj.token -and $obj.token.id_token) {
            $idToken = $obj.token.id_token
        }
        if ($idToken -and $idToken.Contains(".")) {
            $parts = $idToken.Split(".")
            if ($parts.Length -gt 1) {
                $payload = $parts[1]
                $mod = $payload.Length % 4
                if ($mod -gt 0) { $payload += "=" * (4 - $mod) }
                $bytes = [System.Convert]::FromBase64String($payload.Replace("-", "+").Replace("_", "/"))
                $claims = [System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json -ErrorAction SilentlyContinue
                if ($claims.email) { return $claims.email }
                if ($claims.sub) { return $claims.sub }
            }
        }
    } catch {}
    return $null
}

function Get-ProfileAccount([string]$ProfilePath) {
    $tokenCandidates = @(
        (Join-Path $ProfilePath ".tokens\credential.txt"),
        (Join-Path $ProfilePath ".tokens\token.json"),
        (Join-Path $ProfilePath "antigravity-oauth-token"),
        (Join-Path $ProfilePath ".gemini\antigravity-cli\antigravity-oauth-token")
    )
    foreach ($cand in $tokenCandidates) {
        if (Test-Path $cand) {
            try {
                $raw = [System.IO.File]::ReadAllText($cand, [System.Text.Encoding]::UTF8)
                $account = Get-ProfileTokenAccount $raw
                if ($account) { return $account }
            } catch {}
        }
    }
    return $null
}

function Show-ProfilesList {
    Write-Host "Available profiles:" -ForegroundColor Cyan
    if ((Test-Path $ProfilesDir) -and (Get-ChildItem -Path $ProfilesDir -Directory -ErrorAction SilentlyContinue)) {
        $dirs = @(Get-ChildItem -Path $ProfilesDir -Directory | Where-Object { -not $_.Name.StartsWith(".") })
        if ($dirs.Count -gt 0) {
            $maxLen = ($dirs | ForEach-Object { $_.Name.Length } | Measure-Object -Maximum).Maximum
            $bullet = [char]0x2022
            foreach ($d in $dirs) {
                $account = Get-ProfileAccount $d.FullName
                $paddedName = $d.Name.PadRight($maxLen)
                Write-Host "  $bullet " -NoNewline
                Write-Host $paddedName -NoNewline -ForegroundColor White
                if ($account) {
                    Write-Host "  ($account)" -ForegroundColor Gray
                } else {
                    Write-Host "  (not authenticated)" -ForegroundColor Yellow
                }
            }
        } else {
            Write-Host "  (no profiles found)" -ForegroundColor DarkGray
        }
    } else {
        Write-Host "  (no profiles found)" -ForegroundColor DarkGray
    }
}

function Start-ProfileSession([string]$ProfileName, [string[]]$AgyArgs) {
    $targetDir = Join-Path $ProfilesDir $ProfileName
    if (-not (Test-Path $targetDir)) {
        Write-Error "Error: profile '$ProfileName' does not exist. Create it first using: mgy new $ProfileName"
        exit 1
    }

    Setup-ProfileEnvironment -TargetDir $targetDir

    $hasWinCred = ([System.Management.Automation.PSTypeName]'WinCredManager').Type -ne $null
    $tokensDir = Join-Path $targetDir ".tokens"
    $tokenFile = Join-Path $tokensDir "credential.txt"
    $bootLockFile = Join-Path $ProfilesDir ".wincred_boot.lock"

    # Startup synchronization: Ensure only one agy instance reads WinCred at a time
    if ($hasWinCred) {
        $waitRetries = 0
        while ($true) {
            try {
                $lockStream = [System.IO.File]::Open($bootLockFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
                $lockBytes = [System.Text.Encoding]::UTF8.GetBytes("$PID - $ProfileName")
                $lockStream.Write($lockBytes, 0, $lockBytes.Length)
                $lockStream.Close()
                break
            } catch [System.IO.IOException] {
                if (Test-Path $bootLockFile) {
                    $age = (Get-Date) - (Get-Item $bootLockFile).LastWriteTime
                    if ($age.TotalSeconds -gt 8) {
                        Remove-Item $bootLockFile -Force -ErrorAction SilentlyContinue
                        continue
                    }
                }
                Write-Host "[*] Multigravity: Waiting for worker initialization slot ($ProfileName)..." -ForegroundColor DarkGray
                Start-Sleep -Milliseconds 600
                $waitRetries++
                if ($waitRetries -gt 35) {
                    Remove-Item $bootLockFile -Force -ErrorAction SilentlyContinue
                    break
                }
            } catch {
                break
            }
        }

        # Background cleaner to release boot lock after agy reads the credential into memory (4 seconds)
        try {
            $cleaner = [PowerShell]::Create().AddScript({
                param($path)
                Start-Sleep -Seconds 4
                if (Test-Path $path) { Remove-Item $path -Force -ErrorAction SilentlyContinue }
            }).AddArgument($bootLockFile)
            $cleaner.BeginInvoke() | Out-Null
        } catch {}

        # Swap in profile credential before launching agy
        if (Test-Path $tokenFile) {
            $tokenData = [System.IO.File]::ReadAllText($tokenFile, [System.Text.Encoding]::UTF8)
            [WinCredManager]::WriteCredential("gemini:antigravity", $tokenData) | Out-Null
        } else {
            [WinCredManager]::DeleteCredential("gemini:antigravity") | Out-Null
        }
    }

    $prevUserProfile = $env:USERPROFILE
    $prevHome = $env:HOME
    try {
        $env:USERPROFILE = $targetDir
        $env:HOME = $targetDir
        Write-Host "Starting agy with profile '$ProfileName'..." -ForegroundColor Cyan
        & agy @AgyArgs
    } finally {
        # Ensure boot lock is cleaned up if session exited early
        if (Test-Path $bootLockFile) {
            try {
                $content = [System.IO.File]::ReadAllText($bootLockFile)
                if ($content -like "*$PID*") {
                    Remove-Item $bootLockFile -Force -ErrorAction SilentlyContinue
                }
            } catch {}
        }

        # Save updated credential back to profile ONLY if it belongs to this profile
        if ($hasWinCred) {
            $savedCred = [WinCredManager]::ReadCredential("gemini:antigravity")
            if ($savedCred) {
                $savedAccount = Get-ProfileTokenAccount $savedCred
                $initialAccount = Get-ProfileTokenAccount $tokenData

                $shouldSave = $false
                if (-not $tokenData -or -not (Test-Path $tokenFile)) {
                    $shouldSave = $true
                } elseif ($initialAccount -and $savedAccount -and ($initialAccount -eq $savedAccount)) {
                    $shouldSave = $true
                } elseif (-not $initialAccount -and -not $savedAccount) {
                    $shouldSave = $true
                } else {
                    Write-Host "[!] mgy: Skipped saving credential for '$ProfileName' ($initialAccount) because Windows Credential Manager holds a different account ($savedAccount)." -ForegroundColor DarkGray
                }

                if ($shouldSave) {
                    if (-not (Test-Path $tokensDir)) { New-Item -ItemType Directory -Path $tokensDir -Force | Out-Null }
                    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
                    [System.IO.File]::WriteAllText($tokenFile, $savedCred, $utf8NoBom)
                }
            }
        }
        $env:USERPROFILE = $prevUserProfile
        $env:HOME = $prevHome
    }
}

switch -Wildcard ($Command) {
    "help" { Show-Help; exit 0 }
    "-h"   { Show-Help; exit 0 }
    "--help" { Show-Help; exit 0 }
    ""     { Show-Help; exit 1 }

    "list" {
        Show-ProfilesList
    }

    "fork" {
        $convId = $null
        for ($i = 0; $i -lt $RemainingArgs.Count; $i++) {
            if ($RemainingArgs[$i] -match "^--conversation=(.+)$") {
                $convId = $matches[1]
                break
            } elseif ($RemainingArgs[$i] -eq "--conversation" -and ($i + 1) -lt $RemainingArgs.Count) {
                $convId = $RemainingArgs[$i + 1]
                break
            } elseif (-not $convId -and $RemainingArgs[$i] -notmatch "^-") {
                $convId = $RemainingArgs[$i]
            }
        }

        if (-not $convId) {
            Write-Error "Error: Missing conversation ID to fork. Usage: mgy fork --conversation=<id>"
            exit 1
        }

        $sourceAgyDir = Join-Path $RealHome ".gemini\antigravity-cli"
        $srcDb = Join-Path $sourceAgyDir "conversations\$convId.db"
        $srcBrain = Join-Path $sourceAgyDir "brain\$convId"

        if (-not (Test-Path $srcDb) -and -not (Test-Path $srcBrain)) {
            Write-Error "Error: Conversation '$convId' not found at $sourceAgyDir"
            exit 1
        }

        if (-not (Test-Path $ProfilesDir)) {
            Write-Error "Error: Profiles directory not found at $ProfilesDir"
            exit 1
        }

        $profiles = @(Get-ChildItem -Path $ProfilesDir -Directory -ErrorAction SilentlyContinue | Sort-Object Name)
        if ($profiles.Count -eq 0) {
            Write-Error "Error: No profiles found in $ProfilesDir. Create profiles first with: mgy new <profile_name>"
            exit 1
        }

        $firstProfile = $profiles[0].Name
        $otherProfiles = if ($profiles.Count -gt 1) { $profiles[1..($profiles.Count - 1)] } else { @() }

        Write-Host "Forking conversation '$convId' across $($profiles.Count) profiles..." -ForegroundColor Cyan

        $results = @()
        # First profile retains original conversation
        $results += "mgy $firstProfile --conversation $convId"

        # Subsequent profiles each get a new forked conversation
        foreach ($p in $otherProfiles) {
            $pName = $p.Name
            $newId = [guid]::NewGuid().ToString()

            # 1. Copy SQLite database files (.db, .db-wal, .db-shm)
            $dstDb = Join-Path $sourceAgyDir "conversations\$newId.db"
            $srcWal = Join-Path $sourceAgyDir "conversations\$convId.db-wal"
            $dstWal = Join-Path $sourceAgyDir "conversations\$newId.db-wal"
            $srcShm = Join-Path $sourceAgyDir "conversations\$convId.db-shm"
            $dstShm = Join-Path $sourceAgyDir "conversations\$newId.db-shm"

            if (Test-Path $srcDb) {
                Copy-Item -Path $srcDb -Destination $dstDb -Force
                if (Test-Path $srcWal) { Copy-Item -Path $srcWal -Destination $dstWal -Force }
                if (Test-Path $srcShm) { Copy-Item -Path $srcShm -Destination $dstShm -Force }

                # Rebind trajectory identity in SQLite to prevent "trajectory not found" / duplicate-load collisions
                try {
                    $pythonCmd = if (Get-Command "python" -ErrorAction SilentlyContinue) { "python" } elseif (Get-Command "python3" -ErrorAction SilentlyContinue) { "python3" } else { $null }
                    if ($pythonCmd) {
                        $sumDb = Join-Path $sourceAgyDir "conversation_summaries.db"
                        $pyScript = @"
import sqlite3, uuid, datetime

src_db = r'$srcDb'
dst_db = r'$dstDb'
sum_db = r'$sumDb'
conv_id = '$convId'
new_id = '$newId'

old_traj = None
try:
    c_src = sqlite3.connect(f'file:{src_db}?mode=ro', uri=True)
    row_meta = c_src.execute('SELECT trajectory_id FROM trajectory_meta').fetchone()
    if row_meta:
        old_traj = row_meta[0]
    c_src.close()
except Exception:
    pass

c = sqlite3.connect(dst_db, timeout=10.0)
new_traj = str(uuid.uuid4())
c.execute('UPDATE trajectory_meta SET cascade_id = ?, trajectory_id = ?', (new_id, new_traj))
rows = c.execute('SELECT id, data FROM trajectory_metadata_blob').fetchall()
for b_id, b_data in rows:
    if b_data and conv_id.encode('utf-8') in b_data:
        patched = b_data.replace(conv_id.encode('utf-8'), new_id.encode('utf-8'))
        c.execute('UPDATE trajectory_metadata_blob SET data = ? WHERE id = ?', (patched, b_id))
c.commit()
c.execute('PRAGMA wal_checkpoint(TRUNCATE);')
c.close()

try:
    c_sum = sqlite3.connect(sum_db, timeout=10.0)
    row = c_sum.execute('SELECT * FROM conversation_summaries WHERE conversation_id = ?', (conv_id,)).fetchone()
    if row:
        cols = [d[1] for d in c_sum.execute('PRAGMA table_info(conversation_summaries)').fetchall()]
        row_dict = dict(zip(cols, row))
        raw = row_dict.get('raw_summary')
        if raw:
            raw = raw.replace(conv_id.encode('utf-8'), new_id.encode('utf-8'))
            if old_traj:
                raw = raw.replace(old_traj.encode('utf-8'), new_traj.encode('utf-8'))
            row_dict['raw_summary'] = raw
        row_dict['conversation_id'] = new_id
        row_dict['last_modified_time'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        placeholders = ','.join(['?'] * len(cols))
        values = [row_dict[k] for k in cols]
        c_sum.execute(f"INSERT OR REPLACE INTO conversation_summaries ({','.join(cols)}) VALUES ({placeholders})", values)
        c_sum.commit()
        c_sum.execute('PRAGMA wal_checkpoint(TRUNCATE);')
    c_sum.close()
except Exception:
    pass
"@
                        $pyScript | & $pythonCmd -
                    }
                } catch {}
            }

            # 2. Copy brain directory using robocopy for high-speed multi-file cloning
            $dstBrain = Join-Path $sourceAgyDir "brain\$newId"
            if (Test-Path $srcBrain) {
                if (Test-Path $dstBrain) { Remove-Item -Path $dstBrain -Recurse -Force }
                New-Item -ItemType Directory -Path $dstBrain -Force | Out-Null
                robocopy "$srcBrain" "$dstBrain" /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
            }

            # 3. Copy annotations if present
            $srcAnno = Join-Path $sourceAgyDir "annotations\$convId.pbtxt"
            $dstAnno = Join-Path $sourceAgyDir "annotations\$newId.pbtxt"
            if (Test-Path $srcAnno) {
                Copy-Item -Path $srcAnno -Destination $dstAnno -Force
            }

            # 4. Remove presence lock if exists
            $dstLock = Join-Path $sourceAgyDir "presence\$newId.lock"
            if (Test-Path $dstLock) {
                Remove-Item -Path $dstLock -Force -ErrorAction SilentlyContinue
            }

            $results += "mgy $pName --conversation $newId"
        }

        Write-Host ""
        foreach ($r in $results) {
            Write-Host $r
        }
    }

    { $_ -in "export", "export-conversation", "export-conv" } {
        $convId = $null
        $outputTar = $null

        for ($i = 0; $i -lt $RemainingArgs.Count; $i++) {
            if ($RemainingArgs[$i] -match "^--conversation=(.+)$") {
                $convId = $matches[1]
            } elseif ($RemainingArgs[$i] -eq "--conversation" -and ($i + 1) -lt $RemainingArgs.Count) {
                $convId = $RemainingArgs[$i + 1]
                $i++
            } elseif (-not $convId -and $RemainingArgs[$i] -notmatch "^-") {
                $convId = $RemainingArgs[$i]
            } elseif (-not $outputTar -and $RemainingArgs[$i] -notmatch "^-") {
                $outputTar = $RemainingArgs[$i]
            }
        }

        if (-not $convId) {
            Write-Error "Error: Missing conversation ID to export. Usage: mgy export <conversation_id> [tar_gz_path]"
            exit 1
        }

        $sourceAgyDir = Join-Path $RealHome ".gemini\antigravity-cli"
        $srcDb = Join-Path $sourceAgyDir "conversations\$convId.db"
        $srcBrain = Join-Path $sourceAgyDir "brain\$convId"
        $srcAnno = Join-Path $sourceAgyDir "annotations\$convId.pbtxt"

        if (-not (Test-Path $srcDb) -and -not (Test-Path $srcBrain)) {
            Write-Error "Error: Conversation '$convId' not found at $sourceAgyDir"
            exit 1
        }

        if (-not $outputTar) {
            $outputTar = "conversation_${convId}.tar.gz"
        }

        if (-not [System.IO.Path]::IsPathRooted($outputTar)) {
            $absOutputTar = Join-Path (Get-Location).Path $outputTar
        } else {
            $absOutputTar = $outputTar
        }

        if (Test-Path $absOutputTar -PathType Container) {
            $absOutputTar = Join-Path $absOutputTar "conversation_${convId}.tar.gz"
        }

        $outParent = Split-Path -Parent $absOutputTar
        if ($outParent -and -not (Test-Path $outParent)) {
            New-Item -ItemType Directory -Path $outParent -Force | Out-Null
        }

        $pythonCmd = $null
        if (Get-Command "python3" -ErrorAction SilentlyContinue) {
            $pythonCmd = "python3"
        } elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
            $pythonCmd = "python"
        }

        if ($pythonCmd) {
            $pyScript = @"
import sqlite3, json, os, tarfile

conv_id = '$convId'
source_dir = r'$sourceAgyDir'
abs_tar = r'$absOutputTar'
src_db = os.path.join(source_dir, 'conversations', f'{conv_id}.db')
brain_dir = os.path.join(source_dir, 'brain', conv_id)
sum_db = os.path.join(source_dir, 'conversation_summaries.db')

if os.path.exists(src_db):
    try:
        con = sqlite3.connect(src_db)
        con.execute('PRAGMA wal_checkpoint(TRUNCATE);')
        con.close()
    except Exception:
        pass

if os.path.exists(sum_db) and os.path.isdir(brain_dir):
    try:
        conn = sqlite3.connect(sum_db, timeout=5.0)
        cur = conn.cursor()
        cur.execute("SELECT * FROM conversation_summaries WHERE conversation_id = ?", (conv_id,))
        row = cur.fetchone()
        if row:
            cols = [col[0] for col in cur.description]
            data = {}
            for c, v in zip(cols, row):
                if isinstance(v, bytes):
                    data[c] = {"__bytes__": v.hex()}
                else:
                    data[c] = v
            with open(os.path.join(brain_dir, '.conversation_summary.json'), 'w') as f:
                json.dump(data, f)
        conn.close()
    except Exception:
        pass

items = [
    (os.path.join(source_dir, 'conversations', f'{conv_id}.db'), f'conversations/{conv_id}.db'),
    (os.path.join(source_dir, 'conversations', f'{conv_id}.db-wal'), f'conversations/{conv_id}.db-wal'),
    (os.path.join(source_dir, 'conversations', f'{conv_id}.db-shm'), f'conversations/{conv_id}.db-shm'),
    (os.path.join(source_dir, 'annotations', f'{conv_id}.pbtxt'), f'annotations/{conv_id}.pbtxt'),
    (os.path.join(source_dir, 'brain', conv_id), f'brain/{conv_id}'),
]

with tarfile.open(abs_tar, 'w:gz', compresslevel=1) as tar:
    for src, arc in items:
        if os.path.exists(src):
            tar.add(src, arcname=arc)
"@
            Write-Host "Exporting conversation '$convId' to $absOutputTar..." -ForegroundColor Cyan
            $pyScript | & $pythonCmd -
        } elseif (Get-Command "tar" -ErrorAction SilentlyContinue) {
            Write-Host "Exporting conversation '$convId' to $absOutputTar..." -ForegroundColor Cyan
            $items = @()
            if (Test-Path "$sourceAgyDir\conversations\$convId.db") { $items += "conversations/$convId.db" }
            if (Test-Path "$sourceAgyDir\conversations\$convId.db-wal") { $items += "conversations/$convId.db-wal" }
            if (Test-Path "$sourceAgyDir\conversations\$convId.db-shm") { $items += "conversations/$convId.db-shm" }
            if (Test-Path "$sourceAgyDir\annotations\$convId.pbtxt") { $items += "annotations/$convId.pbtxt" }
            if (Test-Path "$sourceAgyDir\brain\$convId") { $items += "brain/$convId" }
            tar.exe -czf "$absOutputTar" -C "$sourceAgyDir" $items
        } else {
            Write-Error "Error: Neither Python nor tar.exe found to create tar.gz archive."
            exit 1
        }

        if (Test-Path $absOutputTar) {
            $size = (Get-Item $absOutputTar).Length / 1MB
            Write-Host ("==> Successfully exported conversation archive ({0:N1} MB)" -f $size) -ForegroundColor Green
            Write-Host "Archive: $absOutputTar"
            Write-Host ""
            Write-Host "To import and resume on another machine:"
            Write-Host "  mgy import `"$absOutputTar`""
            Write-Host "Or manually:"
            Write-Host "  tar -xzf `"$absOutputTar`" -C `"`$HOME\.gemini\antigravity-cli`""
            Write-Host "  agy --conversation $convId"
        } else {
            Write-Error "Error: Failed to create tar archive."
            exit 1
        }
    }

        { $_ -in "import-conversation", "import-conv" } {
        $archivePath = $RemainingArgs[0]
        Import-ConversationArchive -ArchivePath $archivePath
        exit 0
    }

    "new" {
        $profileName = $RemainingArgs[0]
        if (-not $profileName) { Show-Help; exit 1 }
        $targetDir = Join-Path $ProfilesDir $profileName

        if (Test-Path $targetDir) {
            Write-Host "Profile '$profileName' already exists at: $targetDir"
            $resp = Read-Host "Re-authenticate profile '$profileName'? [y/N]"
            if ($resp -notmatch "^[yY]") {
                Write-Host "Aborted."
                exit 0
            }
        }

        $tokensDir = Join-Path $targetDir ".tokens"
        New-Item -ItemType Directory -Path $tokensDir -Force | Out-Null
        Setup-ProfileEnvironment -TargetDir $targetDir

        Write-Host "Setting up profile '$profileName'..." -ForegroundColor Cyan

        $hasWinCred = ([System.Management.Automation.PSTypeName]'WinCredManager').Type -ne $null
        $prevCred = if ($hasWinCred) { [WinCredManager]::ReadCredential("gemini:antigravity") } else { $null }
        if ($hasWinCred) {
            [WinCredManager]::DeleteCredential("gemini:antigravity") | Out-Null
        }

        $prevUserProfile = $env:USERPROFILE
        $prevHome = $env:HOME
        try {
            $env:USERPROFILE = $targetDir
            $env:HOME = $targetDir
            # Launch agy in interactive mode to authenticate without the 60s headless timeout
            & agy
        } finally {
            if ($hasWinCred) {
                $savedCred = [WinCredManager]::ReadCredential("gemini:antigravity")
                if ($savedCred) {
                    $tokenFile = Join-Path $tokensDir "credential.txt"
                    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
                    [System.IO.File]::WriteAllText($tokenFile, $savedCred, $utf8NoBom)
                    Write-Host "Profile '$profileName' is ready!" -ForegroundColor Green
                } else {
                    Write-Host "Profile '$profileName' was created, but not yet authenticated." -ForegroundColor Yellow
                    Write-Host "Run 'mgy $profileName' whenever you're ready to sign in." -ForegroundColor Cyan
                    if ($prevCred) {
                        [WinCredManager]::WriteCredential("gemini:antigravity", $prevCred) | Out-Null
                    }
                }
            } else {
                Write-Host "Profile '$profileName' is ready!" -ForegroundColor Green
            }
            $env:USERPROFILE = $prevUserProfile
            $env:HOME = $prevHome
        }
    }

    "import" {
        $firstArg = $RemainingArgs[0]
        if (-not $firstArg) { Show-Help; exit 1 }

        # Check if argument is an archive path (.tar.gz, .tgz, .tar, .zip) or an existing file
        $isArchivePattern = $firstArg -match '\.(tar\.gz|tgz|tar|zip)$'
        $isFile = Test-Path $firstArg -PathType Leaf

        if ($isArchivePattern -or $isFile) {
            Import-ConversationArchive -ArchivePath $firstArg
            exit 0
        }

        # Otherwise treat as profile name for host session import
        $profileName = $firstArg
        $targetDir = Join-Path $ProfilesDir $profileName
        $sourceAgyDir = Join-Path $RealHome ".gemini\antigravity-cli"

        if (-not (Test-Path $sourceAgyDir)) {
            Write-Error "Error: Default antigravity configuration not found at $sourceAgyDir"
            exit 1
        }

        if (Test-Path $targetDir) {
            Write-Host "Profile '$profileName' already exists at $targetDir."
            $resp = Read-Host "Overwrite profile '$profileName' with host session? [y/N]"
            if ($resp -match "^[yY]") {
                Remove-Item -Path $targetDir -Recurse -Force
            } else {
                Write-Host "Aborted."
                exit 0
            }
        }

        $tokensDir = Join-Path $targetDir ".tokens"
        New-Item -ItemType Directory -Path $tokensDir -Force | Out-Null
        Setup-ProfileEnvironment -TargetDir $targetDir

        $hasWinCred = ([System.Management.Automation.PSTypeName]'WinCredManager').Type -ne $null
        if ($hasWinCred) {
            $hostCred = [WinCredManager]::ReadCredential("gemini:antigravity")
            if ($hostCred) {
                $tokenFile = Join-Path $tokensDir "credential.txt"
                $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
                [System.IO.File]::WriteAllText($tokenFile, $hostCred, $utf8NoBom)
            }
        }

        Write-Host "Profile '$profileName' imported from global host config!" -ForegroundColor Green
    }

    { $_ -in @("quotas", "quota", "quotes", "quote") } {
        $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
        $quotaScript = Join-Path $scriptDir "mgy-quota"
        if (-not (Test-Path $quotaScript)) {
            $quotaScript = Join-Path $scriptDir "mgy-quota.py"
        }
        if (-not (Test-Path $quotaScript)) {
            $quotaCmd = Get-Command "mgy-quota" -ErrorAction SilentlyContinue
            if ($quotaCmd) { $quotaScript = $quotaCmd.Source }
        }
        if (-not (Test-Path $quotaScript)) {
            $quotaCmd = Get-Command "mgy-quota.py" -ErrorAction SilentlyContinue
            if ($quotaCmd) { $quotaScript = $quotaCmd.Source }
        }

        if (Test-Path $quotaScript) {
            if (-not $env:COLUMNS) {
                try {
                    $cols = $Host.UI.RawUI.WindowSize.Width
                    if ($cols -and $cols -gt 0) {
                        $env:COLUMNS = "$cols"
                    }
                } catch {}
            }
            python $quotaScript @RemainingArgs
            exit $LASTEXITCODE
        } else {
            # Fallback
            $profiles = Get-ChildItem -Path $ProfilesDir -Directory -ErrorAction SilentlyContinue
            if ($profiles) {
                foreach ($p in $profiles) {
                    Write-Host "`n=== Quota for profile: $($p.Name) ===" -ForegroundColor Cyan
                    Start-ProfileSession -ProfileName $p.Name -AgyArgs @("-p", "/quota")
                }
            } else {
                Write-Host "(no profiles found in $ProfilesDir)" -ForegroundColor DarkGray
            }
        }
    }

    { $_ -in @("delete", "rm", "remove") } {
        $profileName = $RemainingArgs[0]
        if (-not $profileName) { Show-Help; exit 1 }
        $targetDir = Join-Path $ProfilesDir $profileName
        if (-not (Test-Path $targetDir)) {
            Write-Error "Error: profile '$profileName' does not exist."
            exit 1
        }
        $resp = Read-Host "Are you sure you want to delete profile '$profileName'? [y/N]"
        if ($resp -match "^[yY]") {
            Remove-Item -Path $targetDir -Recurse -Force
            Write-Host "Profile '$profileName' deleted." -ForegroundColor Green
        } else {
            Write-Host "Aborted."
        }
    }

    { $_ -in @("conversations", "history", "chats") } {
        Write-Host "Recent conversations:" -ForegroundColor Cyan
        $db = Join-Path $RealHome ".gemini\antigravity-cli\conversation_summaries.db"
        $sqliteCmd = Get-Command "sqlite3" -ErrorAction SilentlyContinue
        if ((Test-Path $db) -and $sqliteCmd) {
            & sqlite3 -header -column $db "SELECT conversation_id AS ID, title AS Title, datetime(last_modified_time) AS Last_Active FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT 10;"
        } elseif (Test-Path $db) {
            Write-Host "Found conversation database at: $db (install sqlite3 to query table directly)" -ForegroundColor Yellow
        } else {
            Write-Host "(no conversation history database found)" -ForegroundColor DarkGray
        }
    }

    "-*" {
        # Flags passed without explicit profile name (e.g. mgy -c, mgy --conversation=<id>)
        $selectedProfile = $env:MULTIGRAVITY_DEFAULT_PROFILE
        if (-not $selectedProfile -or -not (Test-Path (Join-Path $ProfilesDir $selectedProfile))) {
            $profiles = @(Get-ChildItem -Path $ProfilesDir -Directory -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
            if ($profiles.Count -eq 1 -and $profiles[0]) {
                $selectedProfile = $profiles[0]
            } elseif ($profiles.Count -eq 0) {
                Write-Error "Error: No profiles found. Create one first using: mgy new <profile_name>"
                exit 1
            } else {
                Write-Error "Multiple profiles found. Please specify which profile to run: mgy <profile> $Command $($RemainingArgs -join ' ')"
                Show-ProfilesList
                exit 1
            }
        }

        Start-ProfileSession -ProfileName $selectedProfile -AgyArgs (@($Command) + $RemainingArgs)
    }

    default {
        Start-ProfileSession -ProfileName $Command -AgyArgs $RemainingArgs
    }
}
