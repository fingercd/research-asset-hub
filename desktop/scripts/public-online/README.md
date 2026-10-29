# Public online Windows preview

This is the **fresh-install-only** packaging variant for 0.3.0-rc.1. It does not
bundle PostgreSQL, Meilisearch or Restic. The PowerShell helper downloads the
exact original supplier artifacts listed in `runtime-lock.json`, checks their
byte counts and SHA-256 hashes, validates ZIP paths, and only then copies them
to the empty installation directory. No remote script is executed.
The PostgreSQL ZIP is downloaded intact, but its unused pgAdmin/StackBuilder GUI
directories are not extracted; server tools, libraries, headers, docs and shared
resources retain their original vendor content until our local UTF-8 manifest step.

## Installation boundaries

- Windows 10 1903+ / Windows 11, x64; administrator privileges are required by
  the per-machine NSIS installer.
- Install [Microsoft Visual C++ 2015–2022 x64 Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe)
  first. Missing C++ runtime DLLs stop preparation before vendor downloads.
- Internet access to EDB, GitHub and their CDN redirects is mandatory during
  installation. The default Tauri WebView2 bootstrapper may also contact Microsoft.
  The lock records the exact engine payload download sizes.
- Existing NSIS installations and nonempty installation directories are rejected.
  This preview is **not an upgrade or repair installer**, including `/UPDATE`.
  It does not silently uninstall an existing version.
- Download/hash/archive failures stop before copying application files. A failed
  final copy removes only the newly created runtime. An interruption during
  ordinary NSIS file copying can leave an incomplete new installation; this is
  not claimed to be transactionally recoverable. Microsoft WebView2 prerequisite
  installation may already have happened even when application setup later stops.
- Data remains outside the installation directory, normally in
  `Documents\ResearchAssetHub`. Neither helper initializes a library, changes
  source roots, starts the application, or deletes library data/preferences.
- The uninstall hook runs after Tauri's running-app check. It removes only the
  installer-owned runtime with the expected ownership marker, rejects linked
  trees, and retains uncertain files rather than deleting external paths.
- The package is unsigned and a prerelease. Building an NSIS executable and
  passing CLI helper tests do **not** establish clean-machine installation,
  native GUI, upgrade, offline-first-installation or uninstall acceptance.

## Build

1. Assemble a new named `desktop/stage-*` directory with the existing
   [`stage-runtime.ps1`](../stage-runtime.ps1), `-Profile public`, exact pinned
   binary inputs and reviewed notices. This intermediate stage is local only;
   do not publish the full vendor-binary stage.
2. Keep complete corresponding-source archives and license notices for the
   Python/Rust/JavaScript components actually redistributed. In particular,
   Psycopg LGPL-3.0 and MPL-covered dependencies must not be treated as MIT.
3. Run `prepare-stage.ps1 -StageDir <absolute named public stage>`. This verifies
   empty public configuration and removes the three vendor engines from that
   stage. It does not operate on an installed application.
4. Configure Tauri `bundle.resources` to **only** the chosen stage's `app` and
   `runtime`, and `bundle.windows.nsis.installerHooks` to
   `../scripts/public-online/installer-hooks.nsh`, with `template` set to
   `../scripts/public-online/installer.nsi`. Its provenance file records the
   additional init guard/no-force-kill changes against the pinned template.
   Tauri merges resource maps: merely adding an override can retain
   unwanted old stage mappings. Preserve and restore the original config bytes
   when using a temporary local override.
5. Run `cargo tauri build --bundles nsis`; inspect the rendered NSIS resource list
   for private paths and ensure it does not contain vendor engine binaries.

`-ValidateOnly` on `prepare-runtime.ps1` validates roots and schema without
network calls, directory creation, dependency installation or application launch.
It is not an installation test. Download lock refreshes require a new direct
supplier verification; never substitute a locally repacked ZIP hash.

## Re-run safety regressions (no installation)

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File desktop/scripts/public-online/tests/Invoke-SafetyTests.ps1
```

These tests create only a unique temporary fixture directory and sentinels,
exercise ZIP/path rejection and installer-owned file cleanup, and statically
check the silent/init/uninstall ordering. They do not launch NSIS, install a
dependency, start/stop services, contact vendors, or change the real registry.
The pure ValidateOnly fixture explicitly mocks the environmental registry gate;
it is not a clean-machine installation test. Tiny temporary fixtures are retained
at the printed path for inspection.

## Licensing

The project's own application source is MIT. Other dependencies retain their
licenses. The installer includes separate corresponding-source archives for the
LGPL/MPL libraries it redistributes. The three external engines are obtained
directly from the original suppliers at installation time and remain subject to
their supplier licenses and notices; online acquisition does not relicense them.
The lock contains the original release and license references.
