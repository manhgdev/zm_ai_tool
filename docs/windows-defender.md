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

GitHub Release contains only macOS PKG and Windows Setup EXE.
Audit reports remain in the corresponding Windows Actions run, not as
additional release downloads.

The full CI audit runs on manual workflow dispatch with `security_audit`
enabled. Ordinary releases only verify release hashes;
they must not be described as Defender-scanned. The explicit audit checks
exclusions for the bundle, every native binary, Setup and bundled PowerShell
scripts, then scans EXE/payload and Setup before publishing. A missing scanner,
nonzero result, new threat detection, deleted file or changed hash blocks the
release. A pass covers those bytes with that engine/signature version only;
it does not guarantee later runtime behavior or every antivirus verdict.

## Diagnose an existing release

Run `Build Windows app` manually with `audit_release=v8.6.77`. This selects a
separate `windows-2022` job with read-only repository access; the build and
publication job is skipped. It downloads the original Setup, verifies its
GitHub SHA-256 digest, scans it, installs silently into the runner's temporary
directory, inventories/scans the installed payload, and runs the packaged
runtime-import, yt-dlp and native desktop startup checks. Repository credentials are removed before
executing Setup. Quarantined files are not restored.

GitHub's Windows image excludes both `C:\` and `D:\` and disables archive
scanning ([image configuration](https://github.com/actions/runner-images/blob/main/images/windows/scripts/build/Configure-WindowsDefender.ps1)).
On the disposable GitHub-hosted VM, both diagnostics and explicit build audits
attempt to remove these two blanket exclusions and enable archive scanning and
real-time protection. Preferences before/after and any preparation error are
saved. The actual exclusion coverage and effective engine protection state are
checked separately. Excluded scan targets block the audit.

The `windows-release-diagnostic` Actions artifact is saved even on failure.
`diagnosis.json` records stages, hashes, signature/publisher information,
process/scan exit codes and missing files. Detection records, Defender events,
engine/signature versions and the Setup log are saved separately when available.
`scanner_unavailable` means the runner could not provide a running Defender engine;
`hash_mismatch` means the download did not match the release metadata;
`scan_excluded` means Defender excludes the Setup, install directory or main EXE;
`detected` means Defender recorded a threat. None is a clean-scan verdict.
The runner's real-time protection state is recorded separately. On-demand scans
can run with real-time protection disabled, but cannot establish whether a user's
PC will quarantine the app during normal use. `desktop-startup.json` separately
records a normal native-window launch using the existing updater readiness
handshake, a 10-second observation, the EXE hash, version and effective real-time
protection state. New builds also synthesize system TTS through the packaged EXE,
verify nonempty, nonsilent WAV samples, and retain `system-tts.json` and the WAV.
New builds also install the app's pinned VieNeu CPU runtime into an isolated
temporary home and synthesize a v3turbo preset through the frozen app's real
worker path. `vieneu-tts.json`, its progress log and WAV record that result. The
runtime directory is cleaned after the worker shuts down and is not uploaded.
The original-release diagnostic does not run TTS because earlier launchers lack
those checks. This covers ONNX/CPU; the hosted runner has no NVIDIA GPU and does
not test CUDA inference. It does not prove that React rendered successfully.
A signature-update failure retains
the recorded definitions and is explicitly recorded in the report.

For a new build, leave `audit_release` empty. Manual builds save a separate
`windows-test-build` Actions artifact and smoke-test the executable; a manual
run on a branch does not publish a GitHub release. Tests without `security_audit`
do not establish a Defender verdict for the new bytes.

## Windows system TTS

The system voice engine previously inserted text, voice and output paths into a
PowerShell program, escaping only ASCII apostrophes. PowerShell also treats curly
single quotes as delimiters: a TTS input with such a quote could change the
program. All three values now travel as ASCII-escaped JSON on stdin, and the
PowerShell program is fixed. Unicode and long text no longer depend on shell
quoting or the Windows command-line length limit. Synthesis errors remain failures
and the speech synthesizer is disposed in `finally`.

This is a confirmed input/code boundary bug in system TTS. It does not establish
the cause of a particular Defender detection, nor does it apply to CapCut,
ElevenLabs or the JSON-based VieNeu worker.

## Driver repair launch

The opt-in NVIDIA repair runs the fixed bundled `nvidia_driver_update.ps1` with
`-File` and `RemoteSigned`, using the same policy as the app updater. Callback,
token, operation ID and accepted EULA fingerprint are validated script parameters.
It no longer builds a script in memory or launches elevated `-EncodedCommand`.
The worker continues to use Windows Update and the existing explicit UAC/EULA flow.
This removes an unnecessary encoded-command launch pattern; it does not prove
that this function caused a particular `Bearfoos` detection. A source diff alone
cannot establish which bytes or behavior Microsoft's classifier flagged.

For a report from a user's PC, collect:

- App version, download source and action immediately before detection.
- Protection history: detection name, time, affected paths and detection ID.
- If the original download still exists (do not restore quarantined EXE just
  for this), SHA-256 via `Get-FileHash -Algorithm SHA256 -LiteralPath '<file>'`.
- Defender engine/signature version from `Get-MpComputerStatus`.
- Relevant app log, with tokens, prompts and user paths redacted as needed.

Compare the hash against the release manifest, including the main EXE in the
application payload. The Setup EXE hash is not the installed application's hash.
Keep detected files quarantined. Submit the exact investigated sample via
Microsoft's official software-developer submission portal only after review
and authorization: https://www.microsoft.com/wdsi/filesubmission
Do not upload user projects, browser profiles, cookies or credentials.

If a trusted signing service/certificate becomes available, integrate it
before packaging the application and before hashing the installer; validate
the expected publisher and timestamp. Signing improves provenance but is not
a guarantee against malware detections.
