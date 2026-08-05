[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("voices", "synthesize")]
    [string]$Mode,
    [string]$SpeechPath,
    [string]$Voice,
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
$synthesizer = $null
try {
    Add-Type -AssemblyName System.Speech
    $synthesizer = [System.Speech.Synthesis.SpeechSynthesizer]::new()
    if ($Mode -eq "voices") {
        @($synthesizer.GetInstalledVoices() |
            Where-Object { $_.Enabled } |
            ForEach-Object { [PSCustomObject]@{ name = $_.VoiceInfo.Name; culture = $_.VoiceInfo.Culture.Name } } |
            Sort-Object -Property culture, name) | ConvertTo-Json -Compress
        exit 0
    }

    if (-not (Test-Path -LiteralPath $SpeechPath -PathType Leaf)) { throw "speech missing" }
    if ([string]::IsNullOrWhiteSpace($Voice) -or [string]::IsNullOrWhiteSpace($OutputPath)) { throw "arguments invalid" }
    $available = @($synthesizer.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Name -eq $Voice })
    if ($available.Count -ne 1) { throw "voice unavailable" }
    $speech = [System.IO.File]::ReadAllText($SpeechPath, [System.Text.Encoding]::UTF8)
    if ([string]::IsNullOrWhiteSpace($speech)) { throw "speech empty" }
    $synthesizer.SelectVoice($Voice)
    $synthesizer.SetOutputToWaveFile($OutputPath)
    try { $synthesizer.Speak($speech) } finally { $synthesizer.SetOutputToNull() }
    if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) { throw "audio missing" }
    exit 0
}
catch {
    [Console]::Error.WriteLine("Windows TTS failed.")
    exit 1
}
finally {
    if ($null -ne $synthesizer) { $synthesizer.Dispose() }
}
