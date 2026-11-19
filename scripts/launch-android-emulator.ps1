param(
    [string]$AvdName
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$sdkRoot = if ($env:ANDROID_HOME) {
    $env:ANDROID_HOME
} elseif ($env:ANDROID_SDK_ROOT) {
    $env:ANDROID_SDK_ROOT
} else {
    Join-Path $env:LOCALAPPDATA 'Android\Sdk'
}

$adbPath = Join-Path $sdkRoot 'platform-tools\adb.exe'
$emulatorPath = Join-Path $sdkRoot 'emulator\emulator.exe'
foreach ($requiredPath in @($adbPath, $emulatorPath)) {
    if (-not (Test-Path $requiredPath)) {
        throw "Android SDK tool not found: $requiredPath. Set ANDROID_HOME or ANDROID_SDK_ROOT."
    }
}

$availableAvds = @(& $emulatorPath -list-avds)
if ($LASTEXITCODE -ne 0 -or $availableAvds.Count -eq 0) {
    throw 'No Android Virtual Devices are configured in this SDK.'
}

if ($AvdName) {
    if ($availableAvds -notcontains $AvdName) {
        throw "AVD '$AvdName' was not found. Available AVDs: $($availableAvds -join ', ')"
    }
} else {
    $AvdName = $availableAvds[0]
}

$requestedAvdName = $PSBoundParameters.ContainsKey('AvdName')
$onlineEmulator = @(& $adbPath devices) |
    Select-String '^emulator-\d+\s+device$' |
    Select-Object -First 1

$serial = $null
if ($onlineEmulator) {
    $serial = ($onlineEmulator.Line -split '\s+')[0]
    $avdResponse = @(& $adbPath -s $serial emu avd name 2>$null)
    $runningAvdName = $avdResponse |
        Where-Object { $_.Trim() -and $_.Trim() -ne 'OK' } |
        Select-Object -First 1

    if ($runningAvdName) {
        $runningAvdName = $runningAvdName.Trim()
        $emulatorProcess = Get-CimInstance Win32_Process -Filter "Name = 'emulator.exe'" |
            Where-Object {
                $avdArgument = [regex]::Match(
                    $_.CommandLine,
                    '(?i)(?:^|\s)-avd\s+(?:"([^"]+)"|(\S+))'
                )
                if (-not $avdArgument.Success) {
                    return $false
                }

                $processAvdName = if ($avdArgument.Groups[1].Success) {
                    $avdArgument.Groups[1].Value
                } else {
                    $avdArgument.Groups[2].Value
                }
                $processAvdName -eq $runningAvdName
            } |
            Select-Object -First 1

        if ($emulatorProcess -and $emulatorProcess.CommandLine -match '(?i)(?:^|\s)-(?:no-window|qt-hide-window)(?=\s|$)') {
            Write-Host "Found headless emulator $serial ($runningAvdName); restarting it with a visible window."
            if (-not $requestedAvdName) {
                $AvdName = $runningAvdName
            }

            & $adbPath -s $serial emu kill | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Could not shut down headless emulator $serial."
            }

            $deadline = (Get-Date).AddSeconds(30)
            do {
                Start-Sleep -Seconds 1
                $processStillRunning = Get-Process -Id $emulatorProcess.ProcessId -ErrorAction SilentlyContinue
                $deviceStillRegistered = @(& $adbPath devices) |
                    Select-String "^$([regex]::Escape($serial))\s"
            } while ((Get-Date) -lt $deadline -and ($processStillRunning -or $deviceStillRegistered))

            if ($processStillRunning -or $deviceStillRegistered) {
                throw "Headless emulator $serial did not shut down within 30 seconds."
            }

            $serial = $null
        } else {
            Write-Host "Using running emulator $serial"
        }
    } else {
        Write-Host "Using running emulator $serial"
    }
}

if (-not $serial) {
    Write-Host "Starting AVD '$AvdName'"
    $emulatorArguments = @('-avd', "`"$AvdName`"", '-no-boot-anim')
    $emulatorProcess = Start-Process -FilePath $emulatorPath -ArgumentList $emulatorArguments -PassThru
    $deadline = (Get-Date).AddMinutes(3)
    while ((Get-Date) -lt $deadline -and -not $serial) {
        if ($emulatorProcess.HasExited) {
            throw "Emulator process exited with code $($emulatorProcess.ExitCode) before appearing in adb."
        }

        Start-Sleep -Seconds 2
        $onlineEmulator = @(& $adbPath devices) |
            Select-String '^emulator-\d+\s+device$' |
            Select-Object -First 1
        if ($onlineEmulator) {
            $serial = ($onlineEmulator.Line -split '\s+')[0]
        }
    }

    if (-not $serial) {
        throw 'The emulator did not appear in adb within 3 minutes. Check the AVD configuration and emulator logs.'
    }
}

Write-Host "Waiting for Android to finish booting on $serial"
$deadline = (Get-Date).AddMinutes(3)
do {
    $bootCompleted = & $adbPath -s $serial shell getprop sys.boot_completed 2>$null
    if ($bootCompleted -and $bootCompleted.Trim() -eq '1') {
        break
    }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)

if (-not $bootCompleted -or $bootCompleted.Trim() -ne '1') {
    throw "Android did not finish booting on $serial within 3 minutes."
}

Push-Location (Join-Path $repoRoot 'android-app')
try {
    & .\gradlew.bat :app:assembleDebug
    if ($LASTEXITCODE -ne 0) {
        throw "Gradle build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

$apkPath = Join-Path $repoRoot 'android-app\app\build\outputs\apk\debug\app-debug.apk'
& $adbPath -s $serial install -r $apkPath
if ($LASTEXITCODE -ne 0) {
    throw "APK installation failed with exit code $LASTEXITCODE."
}

& $adbPath -s $serial shell am start -n 'com.example.pluginhost/.MainActivity'
if ($LASTEXITCODE -ne 0) {
    throw "App launch failed with exit code $LASTEXITCODE."
}

Write-Host "Plugin Host is running on $serial."
