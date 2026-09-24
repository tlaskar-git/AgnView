# Signing and notarising the macOS app

`.github/workflows/macos.yml` builds `AgnView.app` and `AgnView-macos.dmg` on
every pull request, every push to `main` and every `v*` tag. It signs and
notarises them only when the secrets below exist. Without them the build is
signed ad hoc, every other step still runs, and the run stays green with a
notice that says what was skipped.

An unsigned or un-notarised build opens on a Mac only with right-click, Open,
then Open in the dialog, or with System Settings, Privacy & Security, Open
Anyway. A signed and notarised build opens with a double-click.

## What you need

- A paid Apple Developer Program membership.
- A **Developer ID Application** certificate. Not "Apple Development" and not
  "Mac App Store": those do not pass Gatekeeper outside the App Store.
- For notarisation, either an app-specific password for your Apple Account,
  or an App Store Connect API key.

## GitHub Actions secrets

Add them in the repository under Settings, Secrets and variables, Actions,
New repository secret. The names must match exactly.

| Secret | Required for | Value |
|---|---|---|
| `MACOS_CERT_P12_BASE64` | Signing | The Developer ID Application certificate and its private key, exported as `.p12`, then base64 encoded |
| `MACOS_CERT_PASSWORD` | Signing | The password you set when you exported the `.p12` |
| `MACOS_SIGNING_IDENTITY` | Signing | The identity name, for example `Developer ID Application: Example Ltd (ABCDE12345)` |
| `MACOS_NOTARY_APPLE_ID` | Notarisation, route A | The Apple Account email that belongs to the developer team |
| `MACOS_NOTARY_PASSWORD` | Notarisation, route A | An app-specific password for that Apple Account |
| `MACOS_NOTARY_TEAM_ID` | Notarisation, route A | The ten-character Team ID |
| `MACOS_NOTARY_API_KEY_P8_BASE64` | Notarisation, route B | The App Store Connect API key `.p8` file, base64 encoded |
| `MACOS_NOTARY_API_KEY_ID` | Notarisation, route B | The key ID shown beside the key |
| `MACOS_NOTARY_API_ISSUER` | Notarisation, route B | The issuer ID shown above the key list |

Signing needs all three signing secrets. Notarisation needs signing plus one
complete route. When both routes are set, the workflow uses route B.

## How to get each value on a Mac

### The certificate: `MACOS_CERT_P12_BASE64` and `MACOS_CERT_PASSWORD`

1. If you do not have the certificate yet: open Keychain Access, choose
   Keychain Access, Certificate Assistant, Request a Certificate From a
   Certificate Authority, and save the request to disk. At
   developer.apple.com, open Certificates, IDs & Profiles, Certificates, add
   a **Developer ID Application** certificate, upload the request, download
   the certificate and double-click it to add it to your login keychain.
2. In Keychain Access, open the login keychain, My Certificates. Find
   `Developer ID Application: <your name or company> (<Team ID>)`. Expand it
   and check a private key sits under it.
3. Select the certificate (not the key on its own), choose File, Export
   Items, and save it as `cert.p12` in the Personal Information Exchange
   format. Set a strong export password. That password is
   `MACOS_CERT_PASSWORD`.
4. In Terminal, copy the file as base64 to the clipboard:

   ```sh
   base64 -i cert.p12 | pbcopy
   ```

   Paste the clipboard as the value of `MACOS_CERT_P12_BASE64`.
5. Delete `cert.p12` once the secret is saved:

   ```sh
   rm cert.p12
   ```

### The identity: `MACOS_SIGNING_IDENTITY`

```sh
security find-identity -v -p codesigning
```

Copy the quoted name of the Developer ID Application line, without the
quotes and without the long hex number in front of it, for example
`Developer ID Application: Example Ltd (ABCDE12345)`.

### The Team ID: `MACOS_NOTARY_TEAM_ID`

At developer.apple.com, open Account, Membership details. The Team ID is the
ten-character value there. It is also the value in brackets at the end of
the signing identity.

### Route A: `MACOS_NOTARY_APPLE_ID` and `MACOS_NOTARY_PASSWORD`

1. `MACOS_NOTARY_APPLE_ID` is the email of an Apple Account in the developer
   team.
2. Sign in at appleid.apple.com, open Sign-In and Security, App-Specific
   Passwords, and create one named for example `AgnView notarisation`. Copy
   it once, as Apple shows it only once. That is `MACOS_NOTARY_PASSWORD`.
   Never use the Apple Account password itself.

### Route B: the App Store Connect API key

1. At appstoreconnect.apple.com, open Users and Access, Integrations, Team
   Keys. Create a key with the Developer role.
2. Download the `.p8` file. Apple lets you download it once.
3. `MACOS_NOTARY_API_KEY_ID` is the Key ID in the list.
   `MACOS_NOTARY_API_ISSUER` is the Issuer ID above the list.
4. Copy the key file as base64 and paste it as `MACOS_NOTARY_API_KEY_P8_BASE64`:

   ```sh
   base64 -i AuthKey_XXXXXXXXXX.p8 | pbcopy
   ```

5. Store the `.p8` file somewhere safe offline, or delete it.

## What the workflow does with them

1. Creates a temporary keychain in the runner's temporary folder with a
   random password, imports the `.p12` into it, and removes the `.p12` file
   at once.
2. `tools/build-macos.sh` signs every binary inside `AgnView.app`, then the
   bundle, with the hardened runtime and `tools/macos/entitlements.plist`,
   and signs the disk image.
3. `xcrun notarytool submit --wait` sends the disk image to Apple, then
   `xcrun stapler staple` attaches the ticket to it.
4. `codesign --verify` and `spctl --assess` check the app and the disk image.
5. Deletes the temporary keychain, whether the job passed or failed.

Secret values are never printed. GitHub masks them in the log, and the
workflow masks the temporary keychain password as well.

## Checking a build on a Mac

After downloading `AgnView-macos.dmg` from a release or a workflow run:

```sh
spctl --assess --type open --context context:primary-signature -v AgnView-macos.dmg
xcrun stapler validate AgnView-macos.dmg
codesign --verify --deep --strict --verbose=2 /Applications/AgnView.app
spctl --assess --type execute -v /Applications/AgnView.app
```

A notarised build reports `source=Notarized Developer ID`.
