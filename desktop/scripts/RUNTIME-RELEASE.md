# Windows runtime packaging

The public profile packages empty sources and taxonomy templates. The personal
profile copies the existing config and validates its immutable source root mapping.
Before upgrading an existing installation, preserve its app/config with
`preserve-legacy-config.ps1` and the explicit original source workspace root.
Existing data_dir/config is always reused. Neither script migrates source originals.

Ordinary GUI upgrades of an existing NSIS installation automatically use the
resource-preserving update path. The pinned custom template enables UpdateMode
after registry detection and version comparison, then skips the reinstall page
before creating its uninstall radio buttons. Explicit `/UPDATE` remains supported.
The entry hook changes only installer state: cancelling the wizard leaves old
app/runtime in place. The running-application gate also precedes PREINSTALL, so
cancelling that prompt occurs before resource preservation.

`nsis/installer.upstream.nsi` is the official Tauri CLI 2.11.4 template with its
SHA-256 and exact two replacements recorded in `installer-template-source.json`.
The customized `installer.nsi` is byte-reproducible from that baseline: the other
change moves the running-application gate before PREINSTALL. Its upstream MIT
license is retained in `installer-template-LICENSE-MIT.txt`. When changing Tauri
CLI versions, review and regenerate this template against the matching upstream;
do not silently replace it with a rendered machine-specific installer script.

PREINSTALL embeds its own PowerShell 5 helper and mapping validator. Fresh
installation requires no old Python or checkout. Existing installation uses its
bundled Python (or a valid legacy interpreter hint) and explicitly imports its
own app registry. Legacy config is copied to user data only when no user config
exists; managed-only config without sources.yaml aborts rather than overwriting
it. Source roots are checked through the old registry, including missing roots,
without requiring the original source workspace to exist. Registered roots and
user data/config cannot overlap app/runtime; linked configuration and linked
path ancestors are rejected before preservation.

The complete old app/runtime directories are renamed to a unique `.previous-*`
directory beneath the installation root before new resources are copied. If a
rename fails, already moved directories are restored and installation aborts
with an instruction to exit the tray app. User database directories and external
originals are never moved. Previous directories are retained without automatic
deletion; rollback failure leaves the precise previous location in the error.

`stage-runtime.ps1` requires local PostgreSQL zip or a verified PostgreSQL directory, Meilisearch exe, Restic exe,
their exact version labels and independently verified SHA-256 inputs, plus a
reviewed ThirdPartyNotices file. `-ValidateOnly` checks inputs without creating a
runtime or rebuilding the Web app. A normal run uses uv.lock with hashes to build
a clean runtime venv and includes Python sources from the explicit runtime list.
No development .venv, node_modules, personal intake, or source workspace hint is
included in public builds. The app includes `apps/mcp_server`, whose entry is
`python -m apps.mcp_server`.

For disconnected installation, also supply `-WebView2OfflineInstaller` and
`-WebView2Sha256`, then build Tauri with `--config tauri.offline.conf.json`.
Run the supplied Microsoft offline installer before the application installer on
a machine without WebView2. The override skips Tauri's network bootstrapper;
the supplied offline installer remains in the bundled runtime directory.
The default installer uses Tauri's online bootstrapper. Merely placing the offline
installer in runtime does not select offline installation.

Required notice review covers CPython, each pinned Python distribution and native
library, PostgreSQL, Meilisearch, Restic, WebView2 redistribution terms, and Rust/
JavaScript dependencies. Project MIT licensing does not replace their licenses.
Any remaining copyleft dependencies require review before release. PyMuPDF, if
present in the lock export, is AGPL/commercial. Archive notice texts must come from the actual input versions.
This script deliberately requires a reviewed notices input rather than generating
an unsupported claim of license compliance.

The runtime manifest records the lock hash, build profile, binary version labels
and supplied hashes. Version labels are declarations; builders must verify the
vendor artifact and version before supplying them. Clean-machine installation,
upgrade, backup and restore acceptance must be executed separately; input checks
and unit tests do not establish that acceptance.
