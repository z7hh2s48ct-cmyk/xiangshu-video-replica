# CW-024: the unique signed release flow for the customer-cloud desktop
# installer. This script is the SOLE sanctioned channel to a signed customer
# artifact: CI builds are contract-only test artifacts labeled
# internal-test-unsigned (RELEASE-CHANNEL.txt) and must never be distributed.
#
# Fail-closed by design: without signing material, without an approved cloud
# origin, or without a verified Authenticode signature on the produced
# installer, the script aborts and emits NO release artifact.
#
# Required environment:
#   VIDEO_REPLICA_RELEASE_SIGN_THUMBPRINT  Authenticode cert thumbprint in the
#                                          machine/user cert store (keys stay
#                                          out of the repo and out of CI)
#   VITE_API_BASE_URL                      approved routable HTTPS cloud origin
# Optional environment:
#   VIDEO_REPLICA_RELEASE_EXPECTED_SIGNER  substring the signer subject must
#                                          contain (guards the wrong certificate)
# Output (under -OutputRoot, default dist-release):
#   customer-cloud/<version>/<installer>.exe
#   customer-cloud/<version>/SHA256SUMS.txt
#   customer-cloud/<version>/release-manifest.json   version/platform/signature/SHA
#
# Real-machine upgrade acceptance (signed installer run on supported legacy
# paths) is CW-046; real legacy-data migration is CW-051. Runbook:
# docs/客户版桌面升级与签名发布手册.md

param(
  [string]$OutputRoot = 'dist-release',
  [string]$TimestampUrl = 'http://timestamp.digicert.com'
)

$ErrorActionPreference = 'Stop'

if (-not $env:VIDEO_REPLICA_RELEASE_SIGN_THUMBPRINT) {
  throw 'VIDEO_REPLICA_RELEASE_SIGN_THUMBPRINT is required: a customer release installer must be Authenticode-signed'
}
if (-not $env:VITE_API_BASE_URL) {
  throw 'VITE_API_BASE_URL is required: release builds point at the approved cloud origin, never the CI placeholder'
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$overlayPath = ''
Push-Location $repoRoot
try {
  # One frozen version across every manifest is a CW-011/CW-021 contract; the
  # release flow re-verifies it at build time instead of trusting the tree.
  $version = (Get-Content 'client/src-tauri/tauri.conf.json' -Raw | ConvertFrom-Json).version
  if (-not $version) { throw 'cannot read version from client/src-tauri/tauri.conf.json' }
  $versionSources = @(
    @{ name = 'package.json'; value = (Get-Content 'package.json' -Raw | ConvertFrom-Json).version },
    @{ name = 'client/package.json'; value = (Get-Content 'client/package.json' -Raw | ConvertFrom-Json).version },
    @{ name = 'package-lock.json'; value = (Get-Content 'package-lock.json' -Raw | ConvertFrom-Json).version },
    @{ name = 'package-lock.json packages.client'; value = ((Get-Content 'package-lock.json' -Raw | ConvertFrom-Json).packages.client).version }
  )
  $cargoToml = Get-Content 'client/src-tauri/Cargo.toml' -Raw
  if ($cargoToml -notmatch '(?ms)\[package\][^\[]*?version\s*=\s*"([^"]+)"') {
    throw 'cannot read version from client/src-tauri/Cargo.toml [package]'
  }
  $versionSources += @{ name = 'client/src-tauri/Cargo.toml'; value = $Matches[1] }
  $pyproject = Get-Content 'server/pyproject.toml' -Raw
  if ($pyproject -notmatch '(?ms)\[project\][^\[]*?version\s*=\s*"([^"]+)"') {
    throw 'cannot read version from server/pyproject.toml [project]'
  }
  $versionSources += @{ name = 'server/pyproject.toml'; value = $Matches[1] }
  foreach ($source in $versionSources) {
    if ($source.value -ne $version) {
      throw "version mismatch in $($source.name): $($source.value) != $version"
    }
  }

  # Signing inputs enter ONLY through a temporary overlay config that is
  # deleted right after the build; nothing signing-related is committed, and
  # the CI workflow never sees any of these values.
  $overlayPath = Join-Path ([System.IO.Path]::GetTempPath()) (
    'cw24-release-overlay-' + [guid]::NewGuid().ToString('N') + '.json'
  )
  $overlay = @{
    bundle = @{
      windows = @{
        certificateThumbprint = $env:VIDEO_REPLICA_RELEASE_SIGN_THUMBPRINT
        digestAlgorithm = 'sha256'
        timestampUrl = $TimestampUrl
      }
    }
  }
  # WriteAllText keeps the overlay BOM-free so Tauri's JSON parser accepts it.
  [System.IO.File]::WriteAllText($overlayPath, ($overlay | ConvertTo-Json -Depth 6))

  npm run require:customer-api-base
  if ($LASTEXITCODE -ne 0) { throw 'require:customer-api-base rejected the release origin' }
  npm run tauri --workspace client -- build --config src-tauri/tauri.customer.conf.json --config $overlayPath --bundles nsis --ci -- --no-default-features
  if ($LASTEXITCODE -ne 0) { throw 'customer NSIS release build failed' }

  # The release gate: an installer whose Authenticode signature is not valid
  # must never leave this machine. A failed check means NO artifact, not a
  # repackaged unsigned one.
  $installer = Get-ChildItem -LiteralPath '.cargo-target/release/bundle/nsis' -Filter '*.exe' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($null -eq $installer) { throw 'customer NSIS installer was not produced' }
  $signature = Get-AuthenticodeSignature -LiteralPath $installer.FullName
  if ($signature.Status -ne 'Valid') {
    throw "release installer signature is not valid: $($signature.Status) $($signature.StatusMessage)"
  }
  if ($env:VIDEO_REPLICA_RELEASE_EXPECTED_SIGNER -and
    -not $signature.SignerCertificate.Subject.Contains($env:VIDEO_REPLICA_RELEASE_EXPECTED_SIGNER)) {
    throw "signer subject mismatch: $($signature.SignerCertificate.Subject)"
  }

  # Unique-artifact records: version, platform, signature and SHA.
  $sha256 = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash
  $releaseDir = Join-Path $OutputRoot "customer-cloud\$version"
  New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
  Copy-Item -LiteralPath $installer.FullName -Destination $releaseDir -Force
  Set-Content -LiteralPath (Join-Path $releaseDir 'SHA256SUMS.txt') -Value "$sha256 *$($installer.Name)" -Encoding utf8
  $manifest = [ordered]@{
    'channel' = 'signed-release'
    'version' = $version
    'platform' = 'windows-x86_64'
    'artifact' = $installer.Name
    'sha256' = $sha256
    'signature' = [ordered]@{
      'status' = [string]$signature.Status
      'subject' = $signature.SignerCertificate.Subject
      'thumbprint' = $signature.SignerCertificate.Thumbprint
      'timestampUrl' = $TimestampUrl
    }
    'generatedAt' = (Get-Date).ToUniversalTime().ToString('o')
  }
  [System.IO.File]::WriteAllText(
    (Join-Path $releaseDir 'release-manifest.json'),
    ($manifest | ConvertTo-Json -Depth 5)
  )

  Write-Host "signed customer release ready: $releaseDir"
  Write-Host "version=$version platform=windows-x86_64 sha256=$sha256"
} finally {
  if ($overlayPath -and (Test-Path -LiteralPath $overlayPath)) {
    Remove-Item -LiteralPath $overlayPath -Force
  }
  Pop-Location
}
