; Online preview rejects existing installations before any uninstall choice.
!define RAH_PUBLIC_ONLINE_DIR "${__FILEDIR__}"
; Unlike Tauri's default gate, this preview NEVER kills a running application,
; including under /S. Users must exit their existing application themselves.
!macro RAH_PUBLIC_ONLINE_RUNNING_GUARD
  Push $R0
  nsis_tauri_utils::FindProcess "${MAINBINARYNAME}.exe"
  Pop $R0
  ${If} $R0 = 0
    MessageBox MB_OK|MB_ICONSTOP "ResearchAssetHub is running. Exit it from its tray yourself before installing/uninstalling. This preview will not terminate it." /SD IDOK
    Pop $R0
    SetErrorLevel 2
    Abort
  ${EndIf}
  Pop $R0
!macroend
; Called by this variant's pinned template in .onInit, before pages, WebView2,
; downloads or target writes. Unlike page callbacks it also runs under /S.
!macro RAH_PUBLIC_ONLINE_INIT_GUARD
  ${If} $UpdateMode = 1
    MessageBox MB_OK|MB_ICONSTOP "This fresh-install-only online preview does not support /UPDATE." /SD IDOK
    SetErrorLevel 2
    Quit
  ${EndIf}
  Push $0
  Push $1
  InitPluginsDir
  File /oname=$PLUGINSDIR\rah-online-init.ps1 "${RAH_PUBLIC_ONLINE_DIR}\prepare-runtime.ps1"
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\rah-online-init.ps1" -RegistryOnly'
  Pop $0
  Pop $1
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Existing installation detected or registration check failed. Nothing will be installed.$\r$\n$\r$\n$1" /SD IDOK
    Pop $1
    Pop $0
    SetErrorLevel 2
    Quit
  ${EndIf}
  Pop $1
  Pop $0
  !insertmacro RAH_PUBLIC_ONLINE_RUNNING_GUARD
!macroend
!macro RAH_HOOK_REINSTALL_PAGE
  MessageBox MB_OK|MB_ICONSTOP "Online preview is for fresh installs only. Existing installation/data are unchanged." /SD IDOK
  SetErrorLevel 2
  Quit
!macroend
; Embed trusted validation code in the uninstaller, not an installed writable copy.
; The read-only gate runs before Tauri deletes ANY resource file.
!macro NSIS_HOOK_PREUNINSTALL
  Push $0
  Push $1
  InitPluginsDir
  File /oname=$PLUGINSDIR\rah-online-remove.ps1 "${RAH_PUBLIC_ONLINE_DIR}\remove-runtime.ps1"
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\rah-online-remove.ps1" -InstallDir "$INSTDIR" -ValidateOnly'
  Pop $0
  Pop $1
  ${If} $0 != 0
    DetailPrint "$1"
    MessageBox MB_OK|MB_ICONSTOP "Uninstall safety check failed. No application resources have been deleted.$\r$\n$\r$\n$1" /SD IDOK
    Pop $1
    Pop $0
    SetErrorLevel 2
    Abort
  ${EndIf}
  Pop $1
  Pop $0
!macroend
; This hook runs AFTER Tauri's running-app gate and ordinary file removal.
!macro NSIS_HOOK_POSTUNINSTALL
  Push $0
  Push $1
  InitPluginsDir
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\rah-online-remove.ps1" -InstallDir "$INSTDIR"'
  Pop $0
  Pop $1
  DetailPrint "$1"
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "Some runtime files were safely retained. User library/data was not deleted.$\r$\n$\r$\n$1" /SD IDOK
  ${EndIf}
  RMDir "$INSTDIR"
  Pop $1
  Pop $0
!macroend
!macro NSIS_HOOK_PREINSTALL
  Push $0
  Push $1
  ; Pages are skipped under /S: enforce this boundary in the Install section too.
  ${If} $UpdateMode = 1
    MessageBox MB_OK|MB_ICONSTOP "This online preview does not support /UPDATE or existing installations." /SD IDOK
    Pop $1
    Pop $0
    SetErrorLevel 2
    Abort
  ${EndIf}
  InitPluginsDir
  File /oname=$PLUGINSDIR\rah-online-prepare.ps1 "${RAH_PUBLIC_ONLINE_DIR}\prepare-runtime.ps1"
  File /oname=$PLUGINSDIR\rah-online-lock.json "${RAH_PUBLIC_ONLINE_DIR}\runtime-lock.json"
  DetailPrint "Downloading SHA-256 pinned runtimes directly from suppliers. This may take several minutes."
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\rah-online-prepare.ps1" -InstallDir "$INSTDIR" -CacheDir "$PLUGINSDIR\rah-online" -LockFile "$PLUGINSDIR\rah-online-lock.json"'
  Pop $0
  Pop $1
  ${If} $0 != 0
    DetailPrint "$1"
    MessageBox MB_OK|MB_ICONSTOP "Online preparation failed. Installation stopped before copying application files. Existing installations and user data are unchanged.$\r$\n$\r$\n$1" /SD IDOK
    Pop $1
    Pop $0
    SetErrorLevel 2
    Abort
  ${EndIf}
  DetailPrint "$1"
  Pop $1
  Pop $0
!macroend
!macro NSIS_HOOK_POSTINSTALL
  Push $0
  Push $1
  File /oname=$PLUGINSDIR\rah-pg-utf8.py "${RAH_PUBLIC_ONLINE_DIR}\..\postgres-utf8-manifests.py"
  nsExec::ExecToStack '"$INSTDIR\runtime\python\python.exe" -B "$PLUGINSDIR\rah-pg-utf8.py" --postgres-directory "$INSTDIR\runtime\postgres" --report "$INSTDIR\runtime\postgres-utf8-manifests.json"'
  Pop $0
  Pop $1
  ${If} $0 != 0
    DetailPrint "$1"
    MessageBox MB_OK|MB_ICONSTOP "PostgreSQL UTF-8 preparation failed. Do not launch the app. User data is separate and unchanged.$\r$\n$\r$\n$1" /SD IDOK
    Pop $1
    Pop $0
    SetErrorLevel 2
    Abort
  ${EndIf}
  Pop $1
  Pop $0
!macroend
