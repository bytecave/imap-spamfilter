# Outlook Spam Add-in — Dev Environment Setup Guide

Target machine: Windows Dev VM, classic desktop Outlook already installed.

## 1. Install Visual Studio Build Tools (no full IDE)

1. Download **Build Tools for Visual Studio** (free, standalone installer, not the full Visual Studio IDE):
   https://visualstudio.microsoft.com/downloads/ → scroll to "Tools for Visual Studio" → "Build Tools for Visual Studio"
2. Run the installer. Select the workload:
   - **Office/SharePoint Development Build Tools** (or under "Individual components," ensure the following are checked if the workload isn't listed as-is):
     - Visual Studio Tools for Office (VSTO)
     - .NET Framework 4.8 targeting pack
     - MSBuild
3. Finish install. Confirm from a terminal:
   ```
   where msbuild
   msbuild -version
   ```
4. Confirm Office Primary Interop Assemblies (PIAs) are present — the VSTO workload installs these automatically. Verify:
   ```
   dir "C:\Program Files (x86)\Microsoft Visual Studio\Shared\Visual Studio Tools for Office\PIA\Office15"
   ```
   (Path may vary by installed Office version; VSTO templates reference the correct PIA automatically once the workload is installed.)

No Visual Studio GUI is required for any step going forward — the `.csproj`, ribbon XML, and C# source can be generated directly by any coding agent or editor, and built via `msbuild` from the command line.

## 2. Confirm Outlook Classic Setup

1. Confirm Outlook version/build:
   - Outlook → File → Office Account → About Outlook
2. Confirm the following folders exist in the test mailbox — this is the fixed topology defined in `outlook_spam_addin_requirements.md` (Section 2, REQ-010–REQ-013). Create them manually if missing, for dev/testing only; the add-in itself must never create these folders in production.
   ```
   Inbox
   ├── Allowlist
   └── Blocklist

   Junk Email
   ├── Train-Ham
   └── Train-Spam
   ```
3. Enable "Trust access to the VBA project object model" is NOT required for VSTO (that's a VBA-specific setting) — skip.

## 3. Generate a Self-Signed Code-Signing Certificate

ClickOnce requires a code-signing certificate to sign the manifest. Public CAs (including Let's Encrypt) do not issue free code-signing certificates — Let's Encrypt only issues TLS/SSL certificates for websites, which is a different certificate type. For personal/internal use, a self-signed certificate is the practical option.

### 3a. Generate the certificate (PowerShell, run as Administrator)

```powershell
$cert = New-SelfSignedCertificate `
  -Type CodeSigningCert `
  -Subject "CN=YourNameOrOrg SpamAddin" `
  -KeyUsage DigitalSignature `
  -FriendlyName "Outlook Spam Addin Code Signing" `
  -CertStoreLocation "Cert:\CurrentUser\My" `
  -NotAfter (Get-Date).AddYears(5)

# Export to .pfx for use in the ClickOnce publish step
$pwd = ConvertTo-SecureString -String "ChangeThisPassword" -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath "C:\certs\SpamAddinSigning.pfx" -Password $pwd
```

Keep the `.pfx` and password secure — this file signs every release you publish.

### 3b. Trust the certificate on each install machine

Because this is a self-signed cert, Windows will show an "Unknown Publisher" warning on install unless the cert is trusted first. On each machine that will install the add-in:

```powershell
# Export the public cert only (no private key) for distribution
Export-Certificate -Cert $cert -FilePath "C:\certs\SpamAddinSigning.cer"

# On the installing machine, import into Trusted Root + Trusted Publishers
Import-Certificate -FilePath "C:\certs\SpamAddinSigning.cer" -CertStoreLocation Cert:\LocalMachine\Root
Import-Certificate -FilePath "C:\certs\SpamAddinSigning.cer" -CertStoreLocation Cert:\LocalMachine\TrustedPublisher
```

This requires admin rights on the installing machine (one-time step per machine, independent of the per-user ClickOnce install itself).

### Alternative if the warning is unacceptable

A paid code-signing certificate from a public CA (DigiCert, Sectigo, SSL.com — roughly $70–400/year) removes the warning for all installers without the manual trust step. Not required for this project's scope but worth knowing if distribution grows beyond a small known group.

## 4. Verify the Toolchain End-to-End

Once build tools and the cert are ready, confirm a minimal VSTO project builds and publishes before development on the real add-in begins:

```
msbuild MyTestAddin.csproj /t:Build
msbuild MyTestAddin.csproj /t:Publish /p:PublishDir=.\publish\
```

If both commands succeed without errors, the environment is ready.
