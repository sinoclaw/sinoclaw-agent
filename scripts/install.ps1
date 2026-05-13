# ============================================================================
# Sinoclaw Agent Installer for Windows
# ============================================================================
# Installation script for Windows (PowerShell).
# Uses uv for fast Python provisioning and package management.
#
# Usage:
#   irm https://raw.githubusercontent.com/sinoclaw/sinoclaw-agent/main/scripts/install.ps1 | iex
#
# Or download and run with options:
#   .\install.ps1 -NoVenv -SkipSetup
#
# ============================================================================

param(
    [switch]$NoVenv,
    [switch]$SkipSetup,
    [string]$Branch = "main",
    [string]$SinoclawHome = "$env:LOCALAPPDATA\sinoclaw",
    [string]$InstallDir = "$env:LOCALAPPDATA\sinoclaw\sinoclaw-agent"
)

$ErrorActionPreference = "Stop"

# ============================================================================
# Configuration
# ============================================================================

$RepoUrlSsh = "git@github.com:sinoclaw/sinoclaw-agent.git"
$RepoUrlHttps = "https://github.com/sinoclaw/sinoclaw-agent.git"
$PythonVersion = "3.11"
$NodeVersion = "22"

# ============================================================================
# Helper functions
# ============================================================================

function Write-Banner {
    Write-Host ""
    Write-Host "┌─────────────────────────────────────────────────────────┐" -ForegroundColor Magenta
    Write-Host "│             ⚕ Sinoclaw Agent Installer                  │" -ForegroundColor Magenta
    Write-Host "├─────────────────────────────────────────────────────────┤" -ForegroundColor Magenta
    Write-Host "│  An open source AI agent by Sinoclaw Team.              │" -ForegroundColor Magenta
    Write-Host "└─────────────────────────────────────────────────────────┘" -ForegroundColor Magenta
    Write-Host ""
}

function Write-Info {
    param([string]$Message)
    Write-Host "→ $Message" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host "✓ $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "⚠ $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Message)
    Write-Host "✗ $Message" -ForegroundColor Red
}

# ============================================================================
# Dependency checks
# ============================================================================

function Install-Uv {
    Write-Info "Checking for uv package manager..."
    
    # Check if uv is already available
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        $version = uv --version
        $script:UvCmd = "uv"
        Write-Success "uv found ($version)"
        return $true
    }
    
    # Check common install locations
    $uvPaths = @(
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:USERPROFILE\.cargo\bin\uv.exe"
    )
    foreach ($uvPath in $uvPaths) {
        if (Test-Path $uvPath) {
            $script:UvCmd = $uvPath
            $version = & $uvPath --version
            Write-Success "uv found at $uvPath ($version)"
            return $true
        }
    }
    
    # Install uv
    Write-Info "Installing uv (fast Python package manager)..."
    try {
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" 2>&1 | Out-Null
        
        # Find the installed binary
        $uvExe = "$env:USERPROFILE\.local\bin\uv.exe"
        if (-not (Test-Path $uvExe)) {
            $uvExe = "$env:USERPROFILE\.cargo\bin\uv.exe"
        }
        if (-not (Test-Path $uvExe)) {
            # Refresh PATH and try again
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
            if (Get-Command uv -ErrorAction SilentlyContinue) {
                $uvExe = (Get-Command uv).Source
            }
        }
        
        if (Test-Path $uvExe) {
            $script:UvCmd = $uvExe
            $version = & $uvExe --version
            Write-Success "uv installed ($version)"
            return $true
        }
        
        Write-Err "uv installed but not found on PATH"
        Write-Info "Try restarting your terminal and re-running"
        return $false
    } catch {
        Write-Err "Failed to install uv"
        Write-Info "Install manually: https://docs.astral.sh/uv/getting-started/installation/"
        return $false
    }
}

function Test-Python {
    Write-Info "Checking Python $PythonVersion..."
    
    # Let uv find or install Python
    try {
        $pythonPath = & $UvCmd python find $PythonVersion 2>$null
        if ($pythonPath) {
            $ver = & $pythonPath --version 2>$null
            Write-Success "Python found: $ver"
            return $true
        }
    } catch { }
    
    # Python not found — use uv to install it (no admin needed!)
    Write-Info "Python $PythonVersion not found, installing via uv..."
    try {
        $uvOutput = & $UvCmd python install $PythonVersion 2>&1
        if ($LASTEXITCODE -eq 0) {
            $pythonPath = & $UvCmd python find $PythonVersion 2>$null
            if ($pythonPath) {
                $ver = & $pythonPath --version 2>$null
                Write-Success "Python installed: $ver"
                return $true
            }
        } else {
            Write-Warn "uv python install output:"
            Write-Host $uvOutput -ForegroundColor DarkGray
        }
    } catch {
        Write-Warn "uv python install error: $_"
    }

    # Fallback: check if ANY Python 3.10+ is already available on the system
    Write-Info "Trying to find any existing Python 3.10+..."
    foreach ($fallbackVer in @("3.12", "3.13", "3.10")) {
        try {
            $pythonPath = & $UvCmd python find $fallbackVer 2>$null
            if ($pythonPath) {
                $ver = & $pythonPath --version 2>$null
                Write-Success "Found fallback: $ver"
                $script:PythonVersion = $fallbackVer
                return $true
            }
        } catch { }
    }

    # Fallback: try system python
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $sysVer = python --version 2>$null
        if ($sysVer -match "3\.(1[0-9]|[1-9][0-9])") {
            Write-Success "Using system Python: $sysVer"
            return $true
        }
    }
    
    Write-Err "Failed to install Python $PythonVersion"
    Write-Info "Install Python 3.11 manually, then re-run this script:"
    Write-Info "  https://www.python.org/downloads/"
    Write-Info "  Or: winget install Python.Python.3.11"
    return $false
}

function Install-Git {
    <#
    .SYNOPSIS
    Ensure Git (and Git Bash) are installed.  Git for Windows bundles bash.exe
    which Sinoclaw uses to run shell commands.

    Priority order (deliberately simple — no winget, no registry, no system
    package manager):
      1. Existing ``git`` on PATH — use it as-is (the common fast path).
      2. Download **PortableGit** from the official git-for-windows GitHub
         release (self-extracting 7z.exe) and unpack it to
         ``%LOCALAPPDATA%\sinoclaw\git`` — never touches system Git, never
         requires admin, works even on locked-down machines and machines
         with a broken system Git install.

    **Why PortableGit, not MinGit:**  MinGit is the minimal-automation
    distribution and ships ONLY ``git.exe`` — no bash, no POSIX utilities.
    Sinoclaw needs ``bash.exe`` to run shell commands.  PortableGit is the
    full Git for Windows distribution without the installer UI; it ships
    ``git.exe`` + ``bash.exe`` + ``sh``, ``awk``, ``sed``, ``grep``, ``curl``,
    ``ssh``, etc. in ``usr\bin\``.

    We deliberately skip winget because it fails badly when the system Git
    install is in a half-installed state (partially registered, or uninstall-
    blocked).  Owning the Sinoclaw copy of Git ourselves is predictable and
    recoverable: if it ever breaks, ``Remove-Item %LOCALAPPDATA%\sinoclaw\git``
    and re-running this installer fully recovers.

    After install we locate ``bash.exe`` and persist the path in
    ``SINOCLAW_GIT_BASH_PATH`` (User scope) so Sinoclaw can find it in a fresh
    shell without a second PATH refresh.
    #>
    Write-Info "Checking Git..."

    if (Get-Command git -ErrorAction SilentlyContinue) {
        $version = git --version
        Write-Success "Git found ($version)"
        Set-GitBashEnvVar
        return $true
    }

    # Download PortableGit into $SinoclawHome\git.  Always works as long as
    # we can reach github.com — no admin, no winget, no reliance on the
    # user's possibly-broken system Git install.
    Write-Info "Git not found — downloading PortableGit to $SinoclawHome\git\ ..."
    Write-Info "(no admin rights required; isolated from any system Git install)"

    try {
        $arch = if ([Environment]::Is64BitOperatingSystem) {
            # Detect ARM64 vs x64 explicitly; PortableGit ships separate assets.
            if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") {
                "arm64"
            } else {
                "64-bit"
            }
        } else {
            # PortableGit does not ship a 32-bit build — fall back to MinGit 32-bit
            # with a warning that bash-based features will be unavailable.
            "32-bit-mingit"
        }

        $releaseApi = "https://api.github.com/repos/git-for-windows/git/releases/latest"
        $release = Invoke-RestMethod -Uri $releaseApi -UseBasicParsing -Headers @{ "User-Agent" = "sinoclaw-installer" }

        if ($arch -eq "32-bit-mingit") {
            Write-Warn "32-bit Windows detected — PortableGit is 64-bit only.  Installing MinGit 32-bit as a last resort; bash-dependent Sinoclaw features (terminal tool, agent-browser) will not work on this machine."
            $assetPattern = "MinGit-*-32-bit.zip"
            $downloadIsZip = $true
        } elseif ($arch -eq "arm64") {
            $assetPattern = "PortableGit-*-arm64.7z.exe"
            $downloadIsZip = $false
        } else {
            $assetPattern = "PortableGit-*-64-bit.7z.exe"
            $downloadIsZip = $false
        }

        $asset = $release.assets | Where-Object { $_.name -like $assetPattern } | Select-Object -First 1

        if (-not $asset) {
            throw "Could not find $assetPattern in latest git-for-windows release"
        }

        $downloadUrl = $asset.browser_download_url
        $downloadExt = if ($downloadIsZip) { "zip" } else { "7z.exe" }
        $tmpFile = "$env:TEMP\$($asset.name)"
        $gitDir = "$SinoclawHome\git"

        Write-Info "Downloading $($asset.name) ($([math]::Round($asset.size / 1MB, 1)) MB)..."
        Invoke-WebRequest -Uri $downloadUrl -OutFile $tmpFile -UseBasicParsing

        if (Test-Path $gitDir) {
            Write-Info "Removing previous Git install at $gitDir ..."
            Remove-Item -Recurse -Force $gitDir
        }
        New-Item -ItemType Directory -Path $gitDir -Force | Out-Null

        if ($downloadIsZip) {
            Expand-Archive -Path $tmpFile -DestinationPath $gitDir -Force
        } else {
            # PortableGit is a self-extracting 7z archive.  Invoke it with
            # `-o<target> -y` (silent) to extract to $gitDir.  No 7z install
            # required; it's fully self-contained.
            Write-Info "Extracting PortableGit to $gitDir ..."
            $extractProc = Start-Process -FilePath $tmpFile `
                -ArgumentList "-o`"$gitDir`"", "-y" `
                -NoNewWindow -Wait -PassThru
            if ($extractProc.ExitCode -ne 0) {
                throw "PortableGit extraction failed (exit code $($extractProc.ExitCode))"
            }
        }
        Remove-Item -Force $tmpFile -ErrorAction SilentlyContinue

        # PortableGit layout: cmd\git.exe + bin\bash.exe + usr\bin\ (coreutils)
        # MinGit layout:      cmd\git.exe + usr\bin\bash.exe (if present)
        $gitExe = "$gitDir\cmd\git.exe"
        if (-not (Test-Path $gitExe)) {
            throw "Git extraction did not produce git.exe at $gitExe"
        }

        # Add to session PATH so the rest of this install run can use git.
        $env:Path = "$gitDir\cmd;$env:Path"

        # Persist to User PATH so fresh shells see it.  PortableGit needs
        # cmd\ (for git.exe), bin\ (for bash.exe + core tools), and
        # usr\bin\ (for perl, ssh, curl, and other POSIX coreutils).
        $newPathEntries = @(
            "$gitDir\cmd",
            "$gitDir\bin",
            "$gitDir\usr\bin"
        )
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $userPathItems = if ($userPath) { $userPath -split ";" } else { @() }
        $changed = $false
        foreach ($entry in $newPathEntries) {
            if ($userPathItems -notcontains $entry) {
                $userPathItems += $entry
                $changed = $true
            }
        }
        if ($changed) {
            [Environment]::SetEnvironmentVariable("Path", ($userPathItems -join ";"), "User")
        }

        $version = & $gitExe --version
        Write-Success "Git $version installed to $gitDir (portable, user-scoped)"
        Set-GitBashEnvVar
        return $true
    } catch {
        Write-Err "Could not install portable Git: $_"
        Write-Info ""
        Write-Info "Fallback: install Git manually from https://git-scm.com/download/win"
        Write-Info "then re-run this installer.  Sinoclaw needs Git Bash on Windows to run"
        Write-Info "shell commands (same as Claude Code and other coding agents)."
        return $false
    }
}

function Set-GitBashEnvVar {
    <#
    .SYNOPSIS
    Locate ``bash.exe`` from an already-installed Git and persist the path in
    ``SINOCLAW_GIT_BASH_PATH`` (User env scope) so Sinoclaw can find it even before
    PATH propagation completes in a newly-spawned shell.
    #>
    $candidates = @()

    # Our own portable Git install is ALWAYS checked first, so a broken
    # system Git doesn't hijack us.  If the user had a working system Git
    # we'd have returned early from Install-Git's fast path and never called
    # this with a system-Git-only installation anyway.
    #
    # Layouts:
    #   PortableGit (our default): $SinoclawHome\git\bin\bash.exe
    #   MinGit (32-bit fallback):  $SinoclawHome\git\usr\bin\bash.exe
    $candidates += "$SinoclawHome\git\bin\bash.exe"       # PortableGit layout (primary)
    $candidates += "$SinoclawHome\git\usr\bin\bash.exe"   # MinGit / PortableGit usr\bin fallback

    # git.exe on PATH can tell us where the install root is
    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    if ($gitCmd) {
        $gitExe = $gitCmd.Source
        # Git for Windows (full installer): <root>\cmd\git.exe + <root>\bin\bash.exe
        # MinGit:                           <root>\cmd\git.exe + <root>\usr\bin\bash.exe
        $gitRoot = Split-Path (Split-Path $gitExe -Parent) -Parent
        $candidates += "$gitRoot\bin\bash.exe"
        $candidates += "$gitRoot\usr\bin\bash.exe"
    }

    # Standard system install locations as a final fallback.  Note:
    # ProgramFiles(x86) can't be referenced via ${env:...} string interpolation
    # because of the parens — use [Environment]::GetEnvironmentVariable().
    $candidates += "${env:ProgramFiles}\Git\bin\bash.exe"
    $pf86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    if ($pf86) { $candidates += "$pf86\Git\bin\bash.exe" }
    $candidates += "${env:LocalAppData}\Programs\Git\bin\bash.exe"

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            [Environment]::SetEnvironmentVariable("SINOCLAW_GIT_BASH_PATH", $candidate, "User")
            $env:SINOCLAW_GIT_BASH_PATH = $candidate
            Write-Info "Set SINOCLAW_GIT_BASH_PATH=$candidate"
            return
        }
    }

    Write-Warn "Could not locate bash.exe — Sinoclaw may not find Git Bash."
    Write-Info "If needed, set SINOCLAW_GIT_BASH_PATH manually to your bash.exe path."
}

function Test-Node {
    Write-Info "Checking Node.js (for browser tools)..."

    if (Get-Command node -ErrorAction SilentlyContinue) {
        $version = node --version
        Write-Success "Node.js $version found"
        $script:HasNode = $true
        return $true
    }

    # Check our own managed install from a previous run
    $managedNode = "$SinoclawHome\node\node.exe"
    if (Test-Path $managedNode) {
        $version = & $managedNode --version
        $env:Path = "$SinoclawHome\node;$env:Path"
        Write-Success "Node.js $version found (Sinoclaw-managed)"
        $script:HasNode = $true
        return $true
    }

    Write-Info "Node.js not found — installing Node.js $NodeVersion LTS..."

    # Try winget first (cleanest on modern Windows)
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Info "Installing via winget..."
        try {
            winget install OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
            # Refresh PATH
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
            if (Get-Command node -ErrorAction SilentlyContinue) {
                $version = node --version
                Write-Success "Node.js $version installed via winget"
                $script:HasNode = $true
                return $true
            }
        } catch { }
    }

    # Fallback: download binary zip to ~/.sinoclaw/node/
    Write-Info "Downloading Node.js $NodeVersion binary..."
    try {
        $arch = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
        $indexUrl = "https://nodejs.org/dist/latest-v${NodeVersion}.x/"
        $indexPage = Invoke-WebRequest -Uri $indexUrl -UseBasicParsing
        $zipName = ($indexPage.Content | Select-String -Pattern "node-v${NodeVersion}\.\d+\.\d+-win-${arch}\.zip" -AllMatches).Matches[0].Value

        if ($zipName) {
            $downloadUrl = "${indexUrl}${zipName}"
            $tmpZip = "$env:TEMP\$zipName"
            $tmpDir = "$env:TEMP\sinoclaw-node-extract"

            Invoke-WebRequest -Uri $downloadUrl -OutFile $tmpZip -UseBasicParsing
            if (Test-Path $tmpDir) { Remove-Item -Recurse -Force $tmpDir }
            Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force

            $extractedDir = Get-ChildItem $tmpDir -Directory | Select-Object -First 1
            if ($extractedDir) {
                if (Test-Path "$SinoclawHome\node") { Remove-Item -Recurse -Force "$SinoclawHome\node" }
                Move-Item $extractedDir.FullName "$SinoclawHome\node"
                $env:Path = "$SinoclawHome\node;$env:Path"

                $version = & "$SinoclawHome\node\node.exe" --version
                Write-Success "Node.js $version installed to ~/.sinoclaw/node/"
                $script:HasNode = $true

                Remove-Item -Force $tmpZip -ErrorAction SilentlyContinue
                Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
                return $true
            }
        }
    } catch {
        Write-Warn "Download failed: $_"
    }

    Write-Warn "Could not auto-install Node.js"
    Write-Info "Install manually: https://nodejs.org/en/download/"
    $script:HasNode = $false
    return $true
}

function Install-SystemPackages {
    $script:HasRipgrep = $false
    $script:HasFfmpeg = $false
    $needRipgrep = $false
    $needFfmpeg = $false

    Write-Info "Checking ripgrep (fast file search)..."
    if (Get-Command rg -ErrorAction SilentlyContinue) {
        $version = rg --version | Select-Object -First 1
        Write-Success "$version found"
        $script:HasRipgrep = $true
    } else {
        $needRipgrep = $true
    }

    Write-Info "Checking ffmpeg (TTS voice messages)..."
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
        Write-Success "ffmpeg found"
        $script:HasFfmpeg = $true
    } else {
        $needFfmpeg = $true
    }

    if (-not $needRipgrep -and -not $needFfmpeg) { return }

    # Build description and package lists for each package manager
    $descParts = @()
    $wingetPkgs = @()
    $chocoPkgs = @()
    $scoopPkgs = @()

    if ($needRipgrep) {
        $descParts += "ripgrep for faster file search"
        $wingetPkgs += "BurntSushi.ripgrep.MSVC"
        $chocoPkgs += "ripgrep"
        $scoopPkgs += "ripgrep"
    }
    if ($needFfmpeg) {
        $descParts += "ffmpeg for TTS voice messages"
        $wingetPkgs += "Gyan.FFmpeg"
        $chocoPkgs += "ffmpeg"
        $scoopPkgs += "ffmpeg"
    }

    $description = $descParts -join " and "
    $hasWinget = Get-Command winget -ErrorAction SilentlyContinue
    $hasChoco = Get-Command choco -ErrorAction SilentlyContinue
    $hasScoop = Get-Command scoop -ErrorAction SilentlyContinue

    # Try winget first (most common on modern Windows)
    if ($hasWinget) {
        Write-Info "Installing $description via winget..."
        foreach ($pkg in $wingetPkgs) {
            try {
                winget install $pkg --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
            } catch { }
        }
        # Refresh PATH and recheck
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
        if ($needRipgrep -and (Get-Command rg -ErrorAction SilentlyContinue)) {
            Write-Success "ripgrep installed"
            $script:HasRipgrep = $true
            $needRipgrep = $false
        }
        if ($needFfmpeg -and (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
            Write-Success "ffmpeg installed"
            $script:HasFfmpeg = $true
            $needFfmpeg = $false
        }
        if (-not $needRipgrep -and -not $needFfmpeg) { return }
    }

    # Fallback: choco
    if ($hasChoco -and ($needRipgrep -or $needFfmpeg)) {
        Write-Info "Trying Chocolatey..."
        foreach ($pkg in $chocoPkgs) {
            try { choco install $pkg -y 2>&1 | Out-Null } catch { }
        }
        if ($needRipgrep -and (Get-Command rg -ErrorAction SilentlyContinue)) {
            Write-Success "ripgrep installed via chocolatey"
            $script:HasRipgrep = $true
            $needRipgrep = $false
        }
        if ($needFfmpeg -and (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
            Write-Success "ffmpeg installed via chocolatey"
            $script:HasFfmpeg = $true
            $needFfmpeg = $false
        }
    }

    # Fallback: scoop
    if ($hasScoop -and ($needRipgrep -or $needFfmpeg)) {
        Write-Info "Trying Scoop..."
        foreach ($pkg in $scoopPkgs) {
            try { scoop install $pkg 2>&1 | Out-Null } catch { }
        }
        if ($needRipgrep -and (Get-Command rg -ErrorAction SilentlyContinue)) {
            Write-Success "ripgrep installed via scoop"
            $script:HasRipgrep = $true
            $needRipgrep = $false
        }
        if ($needFfmpeg -and (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
            Write-Success "ffmpeg installed via scoop"
            $script:HasFfmpeg = $true
            $needFfmpeg = $false
        }
    }

    # Show manual instructions for anything still missing
    if ($needRipgrep) {
        Write-Warn "ripgrep not installed (file search will use findstr fallback)"
        Write-Info "  winget install BurntSushi.ripgrep.MSVC"
    }
    if ($needFfmpeg) {
        Write-Warn "ffmpeg not installed (TTS voice messages will be limited)"
        Write-Info "  winget install Gyan.FFmpeg"
    }
}

# ============================================================================
# Installation
# ============================================================================

function Install-Repository {
    Write-Info "Installing to $InstallDir..."

    $didUpdate = $false

    if (Test-Path $InstallDir) {
        # Test-Path "$InstallDir\.git" returns True when .git is a file OR a
        # directory OR a symlink OR a submodule-style gitfile — and also when
        # it's a broken stub left over from a failed previous install (e.g.
        # a partial Remove-Item that couldn't delete a locked index.lock).
        # Validate the repo properly by asking git itself.  Two checks
        # belt-and-braces: rev-parse AND git status.  If either fails the
        # repo is broken and we fall through to a fresh clone.
        $repoValid = $false
        if (Test-Path "$InstallDir\.git") {
            Push-Location $InstallDir
            try {
                # Reset $LASTEXITCODE before the probe so we don't pick up
                # a stale 0 from an earlier git call in this session.
                $global:LASTEXITCODE = 0
                $revParseOut = & git -c windows.appendAtomically=false rev-parse --is-inside-work-tree 2>&1
                $revParseOk = ($LASTEXITCODE -eq 0) -and ($revParseOut -match "true")

                $global:LASTEXITCODE = 0
                $null = & git -c windows.appendAtomically=false status --short 2>&1
                $statusOk = ($LASTEXITCODE -eq 0)

                if ($revParseOk -and $statusOk) {
                    $repoValid = $true
                }
            } catch {}
            Pop-Location
        }

        if ($repoValid) {
            Write-Info "Existing installation found, updating..."
            Push-Location $InstallDir
            try {
                git -c windows.appendAtomically=false fetch origin
                if ($LASTEXITCODE -ne 0) { throw "git fetch failed (exit $LASTEXITCODE)" }
                git -c windows.appendAtomically=false checkout $Branch
                if ($LASTEXITCODE -ne 0) { throw "git checkout $Branch failed (exit $LASTEXITCODE)" }
                git -c windows.appendAtomically=false pull origin $Branch
                if ($LASTEXITCODE -ne 0) { throw "git pull failed (exit $LASTEXITCODE)" }
            } finally {
                Pop-Location
            }
            $didUpdate = $true
        } else {
            # Directory exists but isn't a usable git repo.  Wipe it and
            # fall through to a fresh clone.  A leftover ``.git`` stub from
            # a partial uninstall used to lock the installer into the
            # "update" branch forever, emitting three ``fatal: not a git
            # repository`` errors and failing with "not in a git directory".
            Write-Warn "Existing directory at $InstallDir is not a valid git repo — replacing it."
            try {
                Remove-Item -Recurse -Force $InstallDir -ErrorAction Stop
            } catch {
                Write-Err "Could not remove $InstallDir : $_"
                Write-Info "Close any programs that might be using files in $InstallDir (editors,"
                Write-Info "terminals, running sinoclaw processes) and try again."
                throw
            }
        }
    }

    if (-not $didUpdate) {
        $cloneSuccess = $false

        # Fix Windows git "copy-fd: write returned: Invalid argument" error.
        # Git for Windows can fail on atomic file operations (hook templates,
        # config lock files) due to antivirus, OneDrive, or NTFS filter drivers.
        # The -c flag injects config before any file I/O occurs.
        Write-Info "Configuring git for Windows compatibility..."
        $env:GIT_CONFIG_COUNT = "1"
        $env:GIT_CONFIG_KEY_0 = "windows.appendAtomically"
        $env:GIT_CONFIG_VALUE_0 = "false"
        git config --global windows.appendAtomically false 2>$null

        # Try SSH first, then HTTPS, with -c flag for atomic write fix
        Write-Info "Trying SSH clone..."
        $env:GIT_SSH_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=5"
        try {
            git -c windows.appendAtomically=false clone --branch $Branch --recurse-submodules $RepoUrlSsh $InstallDir
            if ($LASTEXITCODE -eq 0) { $cloneSuccess = $true }
        } catch { }
        $env:GIT_SSH_COMMAND = $null

        if (-not $cloneSuccess) {
            if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue }
            Write-Info "SSH failed, trying HTTPS..."
            try {
                git -c windows.appendAtomically=false clone --branch $Branch --recurse-submodules $RepoUrlHttps $InstallDir
                if ($LASTEXITCODE -eq 0) { $cloneSuccess = $true }
            } catch { }
        }

        # Fallback: download ZIP archive (bypasses git file I/O issues entirely)
        if (-not $cloneSuccess) {
            if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue }
            Write-Warn "Git clone failed — downloading ZIP archive instead..."
            try {
                $zipUrl = "https://github.com/sinoclaw/sinoclaw-agent/archive/refs/heads/$Branch.zip"
                $zipPath = "$env:TEMP\sinoclaw-agent-$Branch.zip"
                $extractPath = "$env:TEMP\sinoclaw-agent-extract"

                Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
                if (Test-Path $extractPath) { Remove-Item -Recurse -Force $extractPath }
                Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force

                # GitHub ZIPs extract to repo-branch/ subdirectory
                $extractedDir = Get-ChildItem $extractPath -Directory | Select-Object -First 1
                if ($extractedDir) {
                    New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir) -ErrorAction SilentlyContinue | Out-Null
                    Move-Item $extractedDir.FullName $InstallDir -Force
                    Write-Success "Downloaded and extracted"

                    # Initialize git repo so updates work later
                    Push-Location $InstallDir
                    git -c windows.appendAtomically=false init 2>$null
                    git -c windows.appendAtomically=false config windows.appendAtomically false 2>$null
                    git remote add origin $RepoUrlHttps 2>$null
                    Pop-Location
                    Write-Success "Git repo initialized for future updates"

                    $cloneSuccess = $true
                }

                # Cleanup temp files
                Remove-Item -Force $zipPath -ErrorAction SilentlyContinue
                Remove-Item -Recurse -Force $extractPath -ErrorAction SilentlyContinue
            } catch {
                Write-Err "ZIP download also failed: $_"
            }
        }

        if (-not $cloneSuccess) {
            throw "Failed to download repository (tried git clone SSH, HTTPS, and ZIP)"
        }
    }

    # Set per-repo config (harmless if it fails)
    Push-Location $InstallDir
    git -c windows.appendAtomically=false config windows.appendAtomically false 2>$null

    # Ensure submodules are initialized and updated
    Write-Info "Initializing submodules..."
    git -c windows.appendAtomically=false submodule update --init --recursive 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Submodule init failed (terminal/RL tools may need manual setup)"
    } else {
        Write-Success "Submodules ready"
    }
    Pop-Location

    Write-Success "Repository ready"
}

function Install-Venv {
    if ($NoVenv) {
        Write-Info "Skipping virtual environment (-NoVenv)"
        return
    }
    
    Write-Info "Creating virtual environment with Python $PythonVersion..."
    
    Push-Location $InstallDir
    
    if (Test-Path "venv") {
        Write-Info "Virtual environment already exists, recreating..."
        Remove-Item -Recurse -Force "venv"
    }
    
    # uv creates the venv and pins the Python version in one step
    & $UvCmd venv venv --python $PythonVersion
    
    Pop-Location
    
    Write-Success "Virtual environment ready (Python $PythonVersion)"
}

function Install-Dependencies {
    Write-Info "Installing dependencies..."
    
    Push-Location $InstallDir
    
    if (-not $NoVenv) {
        # Tell uv to install into our venv (no activation needed)
        $env:VIRTUAL_ENV = "$InstallDir\venv"
    }
    
    # Install main package.  Tiered fallback so a single flaky git+https dep
    # (atroposlib / tinker in the [rl] extra) doesn't silently drop
    # dashboard/MCP/cron/messaging extras.  Each tier's stdout/stderr is
    # preserved — no Out-Null swallowing — so the user can see what failed.
    #
    # Tier 1: [all] — everything, including RL git+https deps (best case).
    # Tier 2: [core-extras] synthesised locally — all PyPI-only extras we
    #         ship (web, mcp, cron, cli, voice, messaging, slack, dev, acp,
    #         pty, homeassistant, sms, tts-premium, honcho, google, mistral,
    #         bedrock, dingtalk, feishu, modal, daytona, vercel).  Drops [rl]
    #         and [matrix] (linux-only) which are the usual failure culprits.
    # Tier 3: [web,mcp,cron,cli,messaging,dev] — the minimum we strongly
    #         believe a user expects `sinoclaw dashboard` / slash commands /
    #         cron / messaging platforms to work out of the box.
    # Tier 4: bare `.` — last-resort so at least the core CLI launches.
    $installTiers = @(
        @{ Name = "all (with RL/matrix extras)"; Spec = ".[all]" },
        @{ Name = "PyPI-only extras (no git deps)"; Spec = ".[web,mcp,cron,cli,voice,messaging,slack,dev,acp,pty,homeassistant,sms,tts-premium,honcho,google,mistral,bedrock,dingtalk,feishu,modal,daytona,vercel]" },
        @{ Name = "dashboard + core platforms"; Spec = ".[web,mcp,cron,cli,messaging,dev]" },
        @{ Name = "core only (no extras)"; Spec = "." }
    )
    $installed = $false
    foreach ($tier in $installTiers) {
        Write-Info "Trying tier: $($tier.Name) ..."
        & $UvCmd pip install -e $tier.Spec
        if ($LASTEXITCODE -eq 0) {
            Write-Success "Main package installed ($($tier.Name))"
            $script:InstalledTier = $tier.Name
            $installed = $true
            break
        }
        Write-Warn "Tier '$($tier.Name)' failed (exit $LASTEXITCODE). Trying next tier..."
    }
    if (-not $installed) {
        throw "Failed to install sinoclaw-agent package even with no extras. Inspect the uv pip install output above."
    }

    # Verify the dashboard deps specifically — they're the most common thing
    # users hit and lazy-import errors from `sinoclaw dashboard` are confusing.
    # If tier 1 failed (the common case), [web] was still picked up by tiers
    # 2-3; only tier 4 leaves you without it.
    $pythonExe = if (-not $NoVenv) { "$InstallDir\venv\Scripts\python.exe" } else { (& $UvCmd python find $PythonVersion) }
    if (Test-Path $pythonExe) {
        $webOk = $false
        try {
            & $pythonExe -c "import fastapi, uvicorn" 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { $webOk = $true }
        } catch { }
        if (-not $webOk) {
            Write-Warn "fastapi/uvicorn not importable — `sinoclaw dashboard` will not work."
            Write-Info "Attempting targeted install of [web] extra as last resort..."
            & $UvCmd pip install -e ".[web]"
            if ($LASTEXITCODE -eq 0) {
                Write-Success "[web] extra installed; `sinoclaw dashboard` should now work."
            } else {
                Write-Warn "Could not install [web] extra. Run manually: uv pip install --python `"$pythonExe`" `"fastapi>=0.104,<1`" `"uvicorn[standard]>=0.24,<1`""
            }
        }
    }
    
    # tinker-atropos (RL training) is optional and OFF by default.  Matches the
    # Linux/macOS install.sh behavior.  Reasons not to auto-install:
    #   - tinker-atropos/pyproject.toml pulls atroposlib + tinker from git+https
    #     (NousResearch/atropos + thinking-machines-lab/tinker) which can fail on
    #     locked-down networks, flaky DNS, or rate-limited github.com and would
    #     previously kill the whole install mid-flight on Windows.
    #   - It's an RL training submodule, not part of the default agent surface.
    #     Users who don't do RL training never need it.
    # Users who do want it can run the one-liner we print below.
    if (Test-Path "tinker-atropos\pyproject.toml") {
        Write-Info "tinker-atropos submodule found — skipping install (optional, for RL training)"
        Write-Info "  To install later: $UvCmd pip install -e `".\tinker-atropos`""
    }
    
    Pop-Location
    
    Write-Success "All dependencies installed"
}

function Set-PathVariable {
    Write-Info "Setting up sinoclaw command..."
    
    if ($NoVenv) {
        $sinoclawBin = "$InstallDir"
    } else {
        $sinoclawBin = "$InstallDir\venv\Scripts"
    }
    
    # Add the venv Scripts dir to user PATH so sinoclaw is globally available
    # On Windows, the sinoclaw.exe in venv\Scripts\ has the venv Python baked in
    $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    
    if ($currentPath -notlike "*$sinoclawBin*") {
        [Environment]::SetEnvironmentVariable(
            "Path",
            "$sinoclawBin;$currentPath",
            "User"
        )
        Write-Success "Added to user PATH: $sinoclawBin"
    } else {
        Write-Info "PATH already configured"
    }
    
    # Set SINOCLAW_HOME so the Python code finds config/data in the right place.
    # Only needed on Windows where we install to %LOCALAPPDATA%\sinoclaw instead
    # of the Unix default ~/.sinoclaw
    $currentSinoclawHome = [Environment]::GetEnvironmentVariable("SINOCLAW_HOME", "User")
    if (-not $currentSinoclawHome -or $currentSinoclawHome -ne $SinoclawHome) {
        [Environment]::SetEnvironmentVariable("SINOCLAW_HOME", $SinoclawHome, "User")
        Write-Success "Set SINOCLAW_HOME=$SinoclawHome"
    }
    $env:SINOCLAW_HOME = $SinoclawHome
    
    # Update current session
    $env:Path = "$sinoclawBin;$env:Path"
    
    Write-Success "sinoclaw command ready"
}

function Copy-ConfigTemplates {
    Write-Info "Setting up configuration files..."
    
    # Create ~/.sinoclaw directory structure
    New-Item -ItemType Directory -Force -Path "$SinoclawHome\cron" | Out-Null
    New-Item -ItemType Directory -Force -Path "$SinoclawHome\sessions" | Out-Null
    New-Item -ItemType Directory -Force -Path "$SinoclawHome\logs" | Out-Null
    New-Item -ItemType Directory -Force -Path "$SinoclawHome\pairing" | Out-Null
    New-Item -ItemType Directory -Force -Path "$SinoclawHome\hooks" | Out-Null
    New-Item -ItemType Directory -Force -Path "$SinoclawHome\image_cache" | Out-Null
    New-Item -ItemType Directory -Force -Path "$SinoclawHome\audio_cache" | Out-Null
    New-Item -ItemType Directory -Force -Path "$SinoclawHome\memories" | Out-Null
    New-Item -ItemType Directory -Force -Path "$SinoclawHome\skills" | Out-Null

    
    # Create .env
    $envPath = "$SinoclawHome\.env"
    if (-not (Test-Path $envPath)) {
        $examplePath = "$InstallDir\.env.example"
        if (Test-Path $examplePath) {
            Copy-Item $examplePath $envPath
            Write-Success "Created ~/.sinoclaw/.env from template"
        } else {
            New-Item -ItemType File -Force -Path $envPath | Out-Null
            Write-Success "Created ~/.sinoclaw/.env"
        }
    } else {
        Write-Info "~/.sinoclaw/.env already exists, keeping it"
    }
    
    # Create config.yaml
    $configPath = "$SinoclawHome\config.yaml"
    if (-not (Test-Path $configPath)) {
        $examplePath = "$InstallDir\cli-config.yaml.example"
        if (Test-Path $examplePath) {
            Copy-Item $examplePath $configPath
            Write-Success "Created ~/.sinoclaw/config.yaml from template"
        }
    } else {
        Write-Info "~/.sinoclaw/config.yaml already exists, keeping it"
    }
    
    # Create SOUL.md if it doesn't exist (global persona file).
    # IMPORTANT: write without a BOM.  Windows PowerShell 5.1's
    # ``Set-Content -Encoding UTF8`` writes UTF-8 WITH a byte-order-mark
    # (the default PS5 behaviour), and Sinoclaw's prompt-injection scanner
    # flags the BOM as an invisible unicode character and refuses to
    # load the file.  PS7's ``-Encoding utf8NoBOM`` fixes that but we
    # don't control which PowerShell version the user has.  Go direct
    # to .NET with an explicit UTF8Encoding($false) — BOM-free on every
    # PowerShell version.
    $soulPath = "$SinoclawHome\SOUL.md"
    if (-not (Test-Path $soulPath)) {
        $soulContent = @"
# Sinoclaw Agent Persona

<!--
This file defines the agent's personality and tone.
The agent will embody whatever you write here.
Edit this to customize how Sinoclaw communicates with you.

Examples:
  - "You are a warm, playful assistant who uses kaomoji occasionally."
  - "You are a concise technical expert. No fluff, just facts."
  - "You speak like a friendly coworker who happens to know everything."

This file is loaded fresh each message -- no restart needed.
Delete the contents (or this file) to use the default personality.
-->
"@
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($soulPath, $soulContent, $utf8NoBom)
        Write-Success "Created ~/.sinoclaw/SOUL.md (edit to customize personality)"
    }
    
    Write-Success "Configuration directory ready: ~/.sinoclaw/"
    
    # Seed bundled skills into ~/.sinoclaw/skills/ (manifest-based, one-time per skill)
    Write-Info "Syncing bundled skills to ~/.sinoclaw/skills/ ..."
    $pythonExe = "$InstallDir\venv\Scripts\python.exe"
    if (Test-Path $pythonExe) {
        try {
            & $pythonExe "$InstallDir\tools\skills_sync.py" 2>$null
            Write-Success "Skills synced to ~/.sinoclaw/skills/"
        } catch {
            # Fallback: simple directory copy
            $bundledSkills = "$InstallDir\skills"
            $userSkills = "$SinoclawHome\skills"
            if ((Test-Path $bundledSkills) -and -not (Get-ChildItem $userSkills -Exclude '.bundled_manifest' -ErrorAction SilentlyContinue)) {
                Copy-Item -Path "$bundledSkills\*" -Destination $userSkills -Recurse -Force -ErrorAction SilentlyContinue
                Write-Success "Skills copied to ~/.sinoclaw/skills/"
            }
        }
    }
}

function Install-NodeDeps {
    if (-not $HasNode) {
        Write-Info "Skipping Node.js dependencies (Node not installed)"
        return
    }

    # Resolve npm explicitly to npm.cmd, NOT npm.ps1.  Node.js on Windows
    # ships BOTH npm.cmd (a batch shim) and npm.ps1 (a PowerShell shim).
    # Get-Command's default ordering picks whichever comes first in PATHEXT,
    # and on many systems that's .ps1 — but .ps1 requires scripts to be
    # enabled in PowerShell's execution policy, which most Windows users
    # don't have (the Restricted / RemoteSigned default blocks unsigned
    # .ps1 files).  .cmd has no such restriction and works on every box.
    #
    # Strategy: look next to the npm shim we found and prefer npm.cmd if
    # it exists in the same directory.  Fall back to whatever Get-Command
    # returned if we can't find a .cmd sibling.
    $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npmCmd) {
        Write-Warn "npm not found on PATH — skipping Node.js dependencies."
        Write-Info "Open a new PowerShell window and re-run 'sinoclaw setup tools' later."
        return
    }
    $npmExe = $npmCmd.Source
    if ($npmExe -like "*.ps1") {
        $npmCmdSibling = Join-Path (Split-Path $npmExe -Parent) "npm.cmd"
        if (Test-Path $npmCmdSibling) {
            Write-Info "Using npm.cmd (PowerShell execution policy blocks npm.ps1)"
            $npmExe = $npmCmdSibling
        } else {
            Write-Warn "Only npm.ps1 available — install may fail if script execution is disabled."
            Write-Info "  If it fails, either enable PS script execution or install Node via winget."
        }
    }

    # Helper: run "npm install" in a given directory and surface the real
    # error when it fails.  Returns $true on success.
    #
    # Implementation note: ``Start-Process -FilePath npm.cmd`` fails with
    # ``%1 is not a valid Win32 application`` on some PowerShell versions
    # because Start-Process bypasses cmd.exe / PATHEXT and expects a real
    # PE file.  The invocation-operator ``& $npmExe`` routes through the
    # PowerShell command pipeline which DOES honour .cmd batch shims, so
    # it works uniformly for npm.cmd, npx.cmd, and bare .exe files.
    function _Run-NpmInstall([string]$label, [string]$installDir, [string]$logPath, [string]$npmPath) {
        Push-Location $installDir
        try {
            # Redirect ALL output streams to the log file via 2>&1 and then
            # ``Tee-Object`` / ``Out-File``.  Simpler approach: call npm
            # with output redirected and inspect $LASTEXITCODE afterwards.
            & $npmPath install --silent *> $logPath
            $code = $LASTEXITCODE
            if ($code -eq 0) {
                Write-Success "$label dependencies installed"
                Remove-Item -Force $logPath -ErrorAction SilentlyContinue
                return $true
            }
            Write-Warn "$label npm install failed — exit code $code"
            if (Test-Path $logPath) {
                $errText = (Get-Content $logPath -Raw -ErrorAction SilentlyContinue)
                if ($errText) {
                    $snippet = if ($errText.Length -gt 1200) { $errText.Substring(0, 1200) + "..." } else { $errText }
                    Write-Info "  npm output:"
                    foreach ($line in $snippet -split "`n") {
                        Write-Host "    $line" -ForegroundColor DarkGray
                    }
                    Write-Info "  Full log: $logPath"
                }
            }
            Write-Info "Run manually later: cd `"$installDir`"; npm install"
            return $false
        } catch {
            Write-Warn "$label npm install could not be launched: $_"
            return $false
        } finally {
            Pop-Location
        }
    }

    # Browser tools
    if (Test-Path "$InstallDir\package.json") {
        Write-Info "Installing Node.js dependencies (browser tools)..."
        $browserLog = "$env:TEMP\sinoclaw-npm-browser-$(Get-Random).log"
        $browserNpmOk = _Run-NpmInstall "Browser tools" $InstallDir $browserLog $npmExe

        # Install Playwright Chromium (mirrors scripts/install.sh behaviour for
        # Linux).  Without this, tools/browser_tool.py::check_browser_requirements
        # returns False (no Chromium under %LOCALAPPDATA%\ms-playwright), and the
        # browser_* tools are silently filtered out of the agent's tool schema.
        # System Chrome at "C:\Program Files\Google\Chrome\..." is NOT used by
        # agent-browser — it expects a Playwright-managed Chromium.
        if ($browserNpmOk) {
            Write-Info "Installing browser engine (Playwright Chromium)..."
            # npx lives next to npm in the same bin dir.  Prefer .cmd to dodge
            # the same execution-policy gotcha that affects npm.ps1 (see above).
            $npmDir = Split-Path $npmExe -Parent
            $npxExe = $null
            foreach ($cand in @("npx.cmd", "npx.exe", "npx")) {
                $try = Join-Path $npmDir $cand
                if (Test-Path $try) { $npxExe = $try; break }
            }
            if (-not $npxExe) {
                $npxCmd = Get-Command npx -ErrorAction SilentlyContinue
                if ($npxCmd) { $npxExe = $npxCmd.Source }
            }
            if (-not $npxExe) {
                Write-Warn "npx not found — cannot install Playwright Chromium."
                Write-Info "Run manually later: cd `"$InstallDir`"; npx playwright install chromium"
            } else {
                $pwLog = "$env:TEMP\sinoclaw-playwright-install-$(Get-Random).log"
                Push-Location $InstallDir
                try {
                    & $npxExe playwright install chromium *> $pwLog
                    $pwCode = $LASTEXITCODE
                    if ($pwCode -eq 0) {
                        Write-Success "Playwright Chromium installed (browser tools ready)"
                        Remove-Item -Force $pwLog -ErrorAction SilentlyContinue
                    } else {
                        Write-Warn "Playwright Chromium install failed — exit code $pwCode"
                        Write-Warn "Browser tools will not work until Chromium is installed."
                        if (Test-Path $pwLog) {
                            $pwErr = Get-Content $pwLog -Raw -ErrorAction SilentlyContinue
                            if ($pwErr) {
                                $snippet = if ($pwErr.Length -gt 1200) { $pwErr.Substring(0, 1200) + "..." } else { $pwErr }
                                Write-Info "  playwright output:"
                                foreach ($line in $snippet -split "`n") {
                                    Write-Host "    $line" -ForegroundColor DarkGray
                                }
                                Write-Info "  Full log: $pwLog"
                            }
                        }
                        Write-Info "Run manually later: cd `"$InstallDir`"; npx playwright install chromium"
                    }
                } catch {
                    Write-Warn "Playwright Chromium install could not be launched: $_"
                    Write-Info "Run manually later: cd `"$InstallDir`"; npx playwright install chromium"
                } finally {
                    Pop-Location
                }
            }
        }
    }

    # TUI
    $tuiDir = "$InstallDir\ui-tui"
    if (Test-Path "$tuiDir\package.json") {
        Write-Info "Installing TUI dependencies..."
        $tuiLog = "$env:TEMP\sinoclaw-npm-tui-$(Get-Random).log"
        [void](_Run-NpmInstall "TUI" $tuiDir $tuiLog $npmExe)
    }
}

function Install-PlatformSdks {
    # Ensure messaging-platform SDKs matching tokens the user added to
    # ~/.sinoclaw/.env are importable.  Two problems this solves:
    #
    # 1. The tiered `uv pip install` cascade above can fall through to a
    #    lower tier when the first fails (common when RL git deps choke),
    #    which silently skips some messaging SDKs from [messaging].
    # 2. `uv` creates the venv without pip.  If a messaging SDK ends up
    #    missing, the user can't `pip install python-telegram-bot` to
    #    recover — pip simply isn't in their venv.
    #
    # Strategy: bootstrap pip via `python -m ensurepip` (idempotent), then
    # for each token set in .env, verify the matching SDK imports.  If not,
    # run one targeted `pip install` as last-chance recovery.  Keeps fresh
    # Windows installs from hitting silent "python-telegram-bot not installed"
    # at runtime.
    if ($NoVenv) {
        Write-Info "Skipping platform-SDK verification (-NoVenv: no venv to bootstrap)"
        return
    }

    $pythonExe = "$InstallDir\venv\Scripts\python.exe"
    if (-not (Test-Path $pythonExe)) {
        Write-Warn "Skipping platform-SDK verification: $pythonExe not found"
        return
    }

    $envPath = "$SinoclawHome\.env"
    if (-not (Test-Path $envPath)) { return }
    $envLines = Get-Content $envPath -ErrorAction SilentlyContinue

    # Map: env var set in .env -> (import name, pip spec matching [messaging] extra).
    # Specs mirror pyproject.toml to avoid version drift.
    $sdkMap = @(
        @{ Var = "TELEGRAM_BOT_TOKEN"; Import = "telegram";  Spec = "python-telegram-bot[webhooks]>=22.6,<23" },
        @{ Var = "DISCORD_BOT_TOKEN";  Import = "discord";   Spec = "discord.py[voice]>=2.7.1,<3" },
        @{ Var = "SLACK_BOT_TOKEN";    Import = "slack_sdk"; Spec = "slack-sdk>=3.27.0,<4" },
        @{ Var = "SLACK_APP_TOKEN";    Import = "slack_bolt";Spec = "slack-bolt>=1.18.0,<2" },
        @{ Var = "WHATSAPP_ENABLED";   Import = "qrcode";    Spec = "qrcode>=7.0,<8" }
    )

    # Which tokens are actually set (not placeholder)?
    $needed = @()
    foreach ($sdk in $sdkMap) {
        $match = $envLines | Where-Object {
            $_ -match ("^" + [regex]::Escape($sdk.Var) + "=.+") `
            -and $_ -notmatch "your-token-here" `
            -and $_ -notmatch "^\s*#"
        }
        if ($match) { $needed += $sdk }
    }
    if ($needed.Count -eq 0) { return }

    Write-Host ""
    Write-Info "Verifying platform SDKs for tokens found in $envPath ..."

    # Verify each SDK's import without triggering side-effect imports.
    # Quirk: PowerShell wraps non-zero-exit native stderr as a
    # NativeCommandError that prints even with `2>$null` / `*> $null`
    # unless we set $ErrorActionPreference to SilentlyContinue for the
    # span.  Save + restore rather than nuking globally.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    try {
        $missing = @()
        foreach ($sdk in $needed) {
            & $pythonExe -c "import $($sdk.Import)" 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) {
                $missing += $sdk
                Write-Warn "  $($sdk.Import) NOT importable (needed for $($sdk.Var))"
            } else {
                Write-Success "  $($sdk.Import) OK"
            }
        }
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if ($missing.Count -eq 0) { return }

    # Bootstrap pip into the venv if it isn't there.  `uv` creates venvs
    # without pip; ensurepip is the stdlib-blessed way to add it.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    try {
        & $pythonExe -m pip --version 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Info "Bootstrapping pip into venv (uv doesn't ship pip)..."
            & $pythonExe -m ensurepip --upgrade 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "ensurepip failed — can't auto-install missing SDKs."
                Write-Info "Manual recovery: $UvCmd pip install `"$($missing[0].Spec)`""
                return
            }
        }

        foreach ($sdk in $missing) {
            Write-Info "  Installing $($sdk.Spec) ..."
            & $pythonExe -m pip install $sdk.Spec 2>&1 | ForEach-Object { Write-Host "    $_" }
            if ($LASTEXITCODE -eq 0) {
                Write-Success "  Installed $($sdk.Import)"
            } else {
                Write-Warn "  Failed to install $($sdk.Spec). Recover manually: $pythonExe -m pip install `"$($sdk.Spec)`""
            }
        }
    } finally {
        $ErrorActionPreference = $prevEAP
    }
}

function Invoke-SetupWizard {
    if ($SkipSetup) {
        Write-Info "Skipping setup wizard (-SkipSetup)"
        return
    }
    
    Write-Host ""
    Write-Info "Starting setup wizard..."
    Write-Host ""
    
    Push-Location $InstallDir
    
    # Run sinoclaw setup using the venv Python directly (no activation needed)
    if (-not $NoVenv) {
        & ".\venv\Scripts\python.exe" -m sinoclaw_cli.main setup
    } else {
        python -m sinoclaw_cli.main setup
    }
    
    Pop-Location
}

function Start-GatewayIfConfigured {
    $envPath = "$SinoclawHome\.env"
    if (-not (Test-Path $envPath)) { return }

    $hasMessaging = $false
    $content = Get-Content $envPath -ErrorAction SilentlyContinue
    foreach ($var in @("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "WHATSAPP_ENABLED")) {
        $match = $content | Where-Object { $_ -match "^${var}=.+" -and $_ -notmatch "your-token-here" }
        if ($match) { $hasMessaging = $true; break }
    }

    if (-not $hasMessaging) { return }

    $sinoclawCmd = "$InstallDir\venv\Scripts\sinoclaw.exe"
    if (-not (Test-Path $sinoclawCmd)) {
        $sinoclawCmd = "sinoclaw"
    }

    # If WhatsApp is enabled but not yet paired, run foreground for QR scan
    $whatsappEnabled = $content | Where-Object { $_ -match "^WHATSAPP_ENABLED=true" }
    $whatsappSession = "$SinoclawHome\whatsapp\session\creds.json"
    if ($whatsappEnabled -and -not (Test-Path $whatsappSession)) {
        Write-Host ""
        Write-Info "WhatsApp is enabled but not yet paired."
        Write-Info "Running 'sinoclaw whatsapp' to pair via QR code..."
        Write-Host ""
        $response = Read-Host "Pair WhatsApp now? [Y/n]"
        if ($response -eq "" -or $response -match "^[Yy]") {
            try {
                & $sinoclawCmd whatsapp
            } catch {
                # Expected after pairing completes
            }
        }
    }

    Write-Host ""
    Write-Info "Messaging platform token detected!"
    Write-Info "The gateway handles messaging platforms and cron job execution."
    Write-Host ""
    $response = Read-Host "Would you like to start the gateway now? [Y/n]"

    if ($response -eq "" -or $response -match "^[Yy]") {
        Write-Info "Starting gateway in background..."
        try {
            $logFile = "$SinoclawHome\logs\gateway.log"
            Start-Process -FilePath $sinoclawCmd -ArgumentList "gateway" `
                -RedirectStandardOutput $logFile `
                -RedirectStandardError "$SinoclawHome\logs\gateway-error.log" `
                -WindowStyle Hidden
            Write-Success "Gateway started! Your bot is now online."
            Write-Info "Logs: $logFile"
            Write-Info "To stop: close the gateway process from Task Manager"
        } catch {
            Write-Warn "Failed to start gateway. Run manually: sinoclaw gateway"
        }
    } else {
        Write-Info "Skipped. Start the gateway later with: sinoclaw gateway"
    }
}

function Write-Completion {
    Write-Host ""
    Write-Host "┌─────────────────────────────────────────────────────────┐" -ForegroundColor Green
    Write-Host "│              ✓ Installation Complete!                   │" -ForegroundColor Green
    Write-Host "└─────────────────────────────────────────────────────────┘" -ForegroundColor Green
    Write-Host ""
    
    # Show file locations
    Write-Host "📁 Your files:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "   Config:    " -NoNewline -ForegroundColor Yellow
    Write-Host "$SinoclawHome\config.yaml"
    Write-Host "   API Keys:  " -NoNewline -ForegroundColor Yellow
    Write-Host "$SinoclawHome\.env"
    Write-Host "   Data:      " -NoNewline -ForegroundColor Yellow
    Write-Host "$SinoclawHome\cron\, sessions\, logs\"
    Write-Host "   Code:      " -NoNewline -ForegroundColor Yellow
    Write-Host "$SinoclawHome\sinoclaw-agent\"
    Write-Host ""
    
    Write-Host "─────────────────────────────────────────────────────────" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "🚀 Commands:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "   sinoclaw              " -NoNewline -ForegroundColor Green
    Write-Host "Start chatting"
    Write-Host "   sinoclaw setup        " -NoNewline -ForegroundColor Green
    Write-Host "Configure API keys & settings"
    Write-Host "   sinoclaw config       " -NoNewline -ForegroundColor Green
    Write-Host "View/edit configuration"
    Write-Host "   sinoclaw config edit  " -NoNewline -ForegroundColor Green
    Write-Host "Open config in editor"
    Write-Host "   sinoclaw gateway      " -NoNewline -ForegroundColor Green
    Write-Host "Start messaging gateway (Telegram, Discord, etc.)"
    Write-Host "   sinoclaw update       " -NoNewline -ForegroundColor Green
    Write-Host "Update to latest version"
    Write-Host ""
    
    Write-Host "─────────────────────────────────────────────────────────" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "⚡ Restart your terminal for PATH changes to take effect" -ForegroundColor Yellow
    Write-Host ""
    
    if (-not $HasNode) {
        Write-Host "Note: Node.js could not be installed automatically." -ForegroundColor Yellow
        Write-Host "Browser tools need Node.js. Install manually:" -ForegroundColor Yellow
        Write-Host "  https://nodejs.org/en/download/" -ForegroundColor Yellow
        Write-Host ""
    }
    
    if (-not $HasRipgrep) {
        Write-Host "Note: ripgrep (rg) was not installed. For faster file search:" -ForegroundColor Yellow
        Write-Host "  winget install BurntSushi.ripgrep.MSVC" -ForegroundColor Yellow
        Write-Host ""
    }
}

# ============================================================================
# Main
# ============================================================================

function Main {
    Write-Banner

    # Windows refuses to delete a directory any shell is currently cd'd
    # inside — and silently leaves orphan files behind, which then wedge
    # "is this a valid git repo" probes on re-install.  If the current
    # working dir is under $InstallDir, step out to the user's home
    # BEFORE doing anything else.  Harmless when the user ran the
    # installer from somewhere else.
    try {
        $currentResolved = (Get-Location).ProviderPath
        $installResolved = $null
        if (Test-Path $InstallDir) {
            $installResolved = (Resolve-Path $InstallDir -ErrorAction SilentlyContinue).ProviderPath
        }
        if ($installResolved -and $currentResolved.ToLower().StartsWith($installResolved.ToLower())) {
            Write-Info "Stepping out of $InstallDir so Windows can replace files there if needed..."
            Set-Location $env:USERPROFILE
        }
    } catch {}

    if (-not (Install-Uv)) { throw "uv installation failed — cannot continue" }
    if (-not (Test-Python)) { throw "Python $PythonVersion not available — cannot continue" }
    if (-not (Install-Git)) { throw "Git not available and auto-install failed — install from https://git-scm.com/download/win then re-run" }
    # Test-Node always returns $true (sets $script:HasNode on success, emits a
    # warning on failure and continues so non-browser installs still work).
    # Cast to [void] so the bare return value doesn't print "True" to the
    # console between the "Node found" line and the next installer step.
    [void](Test-Node)
    Install-SystemPackages  # ripgrep + ffmpeg in one step

    Install-Repository
    Install-Venv
    Install-Dependencies
    Install-NodeDeps
    Set-PathVariable
    Copy-ConfigTemplates
    Invoke-SetupWizard
    Install-PlatformSdks
    Start-GatewayIfConfigured

    Write-Completion
}

# Wrap in try/catch so errors don't kill the terminal when run via:
#   irm https://...install.ps1 | iex
# (exit/throw inside iex kills the entire PowerShell session)
try {
    Main
} catch {
    Write-Host ""
    Write-Err "Installation failed: $_"
    Write-Host ""
    Write-Info "If the error is unclear, try downloading and running the script directly:"
    Write-Host "  Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/sinoclaw/sinoclaw-agent/main/scripts/install.ps1' -OutFile install.ps1" -ForegroundColor Yellow
    Write-Host "  .\install.ps1" -ForegroundColor Yellow
    Write-Host ""
}
