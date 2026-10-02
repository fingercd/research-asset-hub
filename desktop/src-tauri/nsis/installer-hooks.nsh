; 0.1.2 之前的安装包曾携带独立 Node.js 与代码图服务运行时。
; 新包不再声明这些资源，覆盖安装本身不会自动删除旧目录，因此在文件复制
; 完成后显式清理。/REBOOTOK 允许旧进程暂时持有文件时在重启后完成删除。
!macro NSIS_HOOK_POSTINSTALL
  RMDir /r /REBOOTOK "$INSTDIR\runtime\node"
  RMDir /r /REBOOTOK "$INSTDIR\runtime\gitnexus"
!macroend
