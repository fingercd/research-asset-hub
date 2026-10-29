; Preserve user configuration and move complete old resource trees before copying
; new resources. Never schedule deletion of paths shared by old/new installations.
!define RAH_UPGRADE_HOOK_DIRECTORY "${__FILEDIR__}"
; The pinned custom template expands this after all variables and version checks,
; before creating the reinstall radio buttons. It does not touch any files.
!macro RAH_HOOK_REINSTALL_PAGE
  ${If} $WixMode != 1
    !if "${ALLOWDOWNGRADES}" == "false"
      ${If} $R0 = -1
        MessageBox MB_OK|MB_ICONSTOP "此安装包不允许降级，已有安装保持原位。"
        Quit
      ${EndIf}
    !endif
    StrCpy $UpdateMode 1
    Abort
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREINSTALL
  Push $0
  Push $1
  InitPluginsDir
  File /oname=$PLUGINSDIR\rah-preinstall-upgrade.ps1 "${RAH_UPGRADE_HOOK_DIRECTORY}\preinstall-upgrade.ps1"
  File /oname=$PLUGINSDIR\rah-preinstall-validate.py "${RAH_UPGRADE_HOOK_DIRECTORY}\preinstall-validate.py"
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\rah-preinstall-upgrade.ps1" -InstallDir "$INSTDIR" -MappingScript "$PLUGINSDIR\rah-preinstall-validate.py"'
  Pop $0
  Pop $1
  ${If} $0 != 0
    DetailPrint "$1"
    MessageBox MB_OK|MB_ICONSTOP "升级准备失败，安装已停止。请先从系统托盘退出研究资产库并关闭其后台进程，然后重试。已有资源已保留；如果回滚失败，请查看安装详情中的 previous 目录。$\r$\n$\r$\n$1"
    Pop $1
    Pop $0
    Abort
  ${EndIf}
  DetailPrint "$1"
  Pop $1
  Pop $0
!macroend
