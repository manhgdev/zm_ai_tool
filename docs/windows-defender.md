# Windows release integrity and Defender

`Bearfoos.A!ml` is a Defender detection, not proof of a false positive. Do not
disable Defender, add exclusions, restore quarantined files automatically, or
change a binary repeatedly just to alter the detection.

The Actions artifact `windows-security-audit` includes `windows-artifact-manifest.json` (all bundled files),
`windows-SHA256SUMS.txt`, `windows-sbom.cdx.json`, `windows-signatures.json` and
`windows-defender-report.json`. The SBOM lists exact files; installed build
packages are separately recorded in `windows-build-environment.json`. It does
not claim every build dependency is shipped or that every transitive binary
dependency has been identified. No Authenticode certificate is configured:
unsigned artifacts are explicitly identified as such. Never substitute an
ad-hoc/self-signed certificate for a trusted publisher identity.

GitHub Release contains only macOS PKG, Windows Setup EXE and Windows Portable
ZIP. Audit reports remain in the corresponding Windows Actions run, not as
additional release downloads.

The full CI audit runs once for v8.3.3, or on manual workflow dispatch with
`security_audit` enabled. Later ordinary releases only verify release hashes;
they must not be described as Defender-scanned. The explicit audit scans
EXE/payload, Setup and ZIP before publishing. A missing scanner,
nonzero result, new threat detection, deleted file or changed hash blocks the
release. A pass covers those bytes with that engine/signature version only;
it does not guarantee later runtime behavior or every antivirus verdict.

For a report from a user's PC, collect:

- App version, download source and action immediately before detection.
- Protection history: detection name, time, affected paths and detection ID.
- If the original download still exists (do not restore quarantined EXE just
  for this), SHA-256 via `Get-FileHash -Algorithm SHA256 -LiteralPath '<file>'`.
- Defender engine/signature version from `Get-MpComputerStatus`.
- Relevant app log, with tokens, prompts and user paths redacted as needed.

Compare the hash against the release manifest, including the main EXE inside
Portable ZIP. The Setup EXE hash is not the installed application's hash.
Keep detected files quarantined. Submit the exact investigated sample via
Microsoft's official software-developer submission portal only after review
and authorization: https://www.microsoft.com/wdsi/filesubmission
Do not upload user projects, browser profiles, cookies or credentials.

If a trusted signing service/certificate becomes available, integrate it
before packaging the application and before hashing the installer; validate
the expected publisher and timestamp. Signing improves provenance but is not
a guarantee against malware detections.
