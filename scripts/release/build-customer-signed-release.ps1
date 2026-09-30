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
#   TAURI_SIGNING_PRIVATE_KEY              updater (minisign) private key; the
#                                          matching public key lives in
#                                          tauri.customer.conf.json. Required so
#                                          every signed release also carries
#                                          self-update artifacts (.sig + stable.json)
# Optional environment:
#   VIDEO_REPLICA_RELEASE_EXPECTED_SIGNER  substring the signer subject must
#                                          contain (guards the wrong certificate)
#   TAURI_SIGNING_PRIVATE_KEY_PASSWORD     passphrase of the updater private key
# Output (under -OutputRoot, default dist-release):
#   customer-cloud/<version>/<installer>.exe
#   customer-cloud/<version>/<installer>.exe.sig       updater minisign signature
#   customer-cloud/<version>/stable.json               updater manifest (upload to
#                                                      <origin>/downloads/customer-cloud/)
#   customer-cloud/<version>/SHA256SUMS.txt
#   customer-cloud/<version>/release-manifest.json   version/platform/signature/SHA
#
# Real-machine upgrade acceptance (signed installer run on supported legacy
# paths) is CW-046; real legacy-data migration is CW-051. Runbook:
# docs/客户版桌面升级与签名发布手册.md

param(
  [string]$OutputRoot = 'dist-release',
  [string]$TimestampUrl = 'http://timestamp.digicert.com',
  # 客户可读的更新日志，写进 stable.json 的 notes 并展示在升级弹窗里。
  [string]$ReleaseNotes = ''
)

$ErrorActionPreference = 'Stop'

if (-not $env:VIDEO_REPLICA_RELEASE_SIGN_THUMBPRINT) {
  throw 'VIDEO_REPLICA_RELEASE_SIGN_THUMBPRINT is required: a customer release installer must be Authenticode-signed'
}
if (-not $env:VITE_API_BASE_URL) {
  throw 'VITE_API_BASE_URL is required: release builds point at the approved cloud origin, never the CI placeholder'
}
if (-not $env:TAURI_SIGNING_PRIVATE_KEY) {
  throw 'TAURI_SIGNING_PRIVATE_KEY is required: a signed release must carry updater artifacts so installed clients can self-update'
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

  # The updater public key must already be real in the committed customer
  # overlay; a placeholder pubkey would make every build silently un-updatable.
  $customerConf = Get-Content 'client/src-tauri/tauri.customer.conf.json' -Raw | ConvertFrom-Json
  $updaterPubkey = $customerConf.plugins.updater.pubkey
  if (-not $updaterPubkey -or $updaterPubkey -eq 'REPLACE_WITH_UPDATER_PUBLIC_KEY') {
    throw 'plugins.updater.pubkey in tauri.customer.conf.json must hold the real updater public key before a signed release'
  }

  # Signing inputs enter ONLY through a temporary overlay config that is
  # deleted right after the build; nothing signing-related is committed, and
  # the CI workflow never sees any of these values. The updater private key
  # itself never enters the overlay: the Tauri CLI reads it from the
  # TAURI_SIGNING_PRIVATE_KEY[_PASSWORD] env vars while createUpdaterArtifacts
  # produces the .sig sidecar.
  $overlayPath = Join-Path ([System.IO.Path]::GetTempPath()) (
    'cw24-release-overlay-' + [guid]::NewGuid().ToString('N') + '.json'
  )
  $overlay = @{
    bundle = @{
      createUpdaterArtifacts = $true
      windows = @{
        certificateThumbprint = $env:VIDEO_REPLICA_RELEASE_SIGN_THUMBPRINT
        digestAlgorithm = 'sha256'
        timestampUrl = $TimestampUrl
      }
    }
    plugins = @{
      updater = @{
        # 静态升级清单与安装包同源同主机：发布时由已通过校验的云端 origin
        # 推导，客户端编译期写入。CI 的占位 endpoint 在这里被真实地址覆盖。
        endpoints = @(
          "$($env:VITE_API_BASE_URL.TrimEnd('/'))/downloads/customer-cloud/stable.json"
        )
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

  # Updater artifacts are part of the release contract: without a .sig the
  # produced installer could never be installed through self-update, so its
  # absence fails the release instead of shipping a broken update channel.
  $sigPath = "$($installer.FullName).sig"
  if (-not (Test-Path -LiteralPath $sigPath)) {
    throw 'updater signature was not produced: expected <installer>.exe.sig next to the NSIS bundle (createUpdaterArtifacts + TAURI_SIGNING_PRIVATE_KEY)'
  }

  # Unique-artifact records: version, platform, signature and SHA.
  $sha256 = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash
  $sigSha256 = (Get-FileHash -LiteralPath $sigPath -Algorithm SHA256).Hash
  $releaseDir = Join-Path $OutputRoot "customer-cloud\$version"
  New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
  Copy-Item -LiteralPath $installer.FullName -Destination $releaseDir -Force
  Copy-Item -LiteralPath $sigPath -Destination $releaseDir -Force
  Set-Content -LiteralPath (Join-Path $releaseDir 'SHA256SUMS.txt') -Value "$sha256 *$($installer.Name)`n$sigSha256 *$($installer.Name).sig" -Encoding utf8

  # stable.json = the static update manifest served at
  # <origin>/downloads/customer-cloud/stable.json (nginx). Signature is the
  # .sig CONTENT (not its hash) per the updater manifest format; the download
  # URL shares the validated cloud origin so clients never see a second host.
  $downloadBase = $env:VITE_API_BASE_URL.TrimEnd('/')
  $stableManifest = [ordered]@{
    'version' = $version
    'notes' = $ReleaseNotes
    'pub_date' = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    'platforms' = [ordered]@{
      'windows-x86_64' = [ordered]@{
        'signature' = (Get-Content -LiteralPath $sigPath -Raw)
        'url' = "$downloadBase/downloads/customer-cloud/$version/$($installer.Name)"
      }
    }
  }
  [System.IO.File]::WriteAllText(
    (Join-Path $releaseDir 'stable.json'),
    ($stableManifest | ConvertTo-Json -Depth 5)
  )

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
    'updater' = [ordered]@{
      'endpoint' = "$downloadBase/downloads/customer-cloud/stable.json"
      'signatureArtifact' = "$($installer.Name).sig"
      'signatureSha256' = $sigSha256
      'manifestArtifact' = 'stable.json'
    }
    'generatedAt' = (Get-Date).ToUniversalTime().ToString('o')
  }
  [System.IO.File]::WriteAllText(
    (Join-Path $releaseDir 'release-manifest.json'),
    ($manifest | ConvertTo-Json -Depth 5)
  )

  Write-Host "signed customer release ready: $releaseDir"
  Write-Host "version=$version platform=windows-x86_64 sha256=$sha256"
  Write-Host "updater: endpoint=$downloadBase/downloads/customer-cloud/stable.json installer=$($installer.Name)"
} finally {
  if ($overlayPath -and (Test-Path -LiteralPath $overlayPath)) {
    Remove-Item -LiteralPath $overlayPath -Force
  }
  Pop-Location
}
