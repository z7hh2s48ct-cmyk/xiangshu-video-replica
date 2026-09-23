; The customer-cloud edition supersedes the legacy internal desktop edition.
;
; CW-024 upgrade guard — the legacy silent uninstall is gated on a verified
; data-protection sequence, executed for EVERY legacy install path that a
; CW-003 supported version can actually have produced:
;   1. running-instance guard: a write-mode open of the legacy uninstaller
;      fails while the legacy edition is executing (Windows locks running
;      executable images), so the install aborts with ZERO mutation instead
;      of killing the user's session;
;   2. full archive: the whole legacy install directory is copied into the
;      customer app-data tree before anything is touched;
;   3. archive verification + manifest: the archive must exist on disk and
;      record its origin in LEGACY-BACKUP-MANIFEST.txt;
;   4. only then the legacy silent uninstall runs; on any failure the
;      customer install aborts while the readable backup stays in place.
; Recovery steps live in docs/客户版桌面升级与签名发布手册.md. Real-machine
; upgrade acceptance is CW-046; real legacy-data migration is CW-051.
; Frozen credential namespaces (CW-003: the DPAPI envelope under the app-data
; identifier and its registry mirror) live OUTSIDE these install directories
; and are never touched here.
;
; The normal Tauri upgrade flow separately replaces an older customer-cloud
; installation.

!define CW24_LEGACY_BACKUP_ROOT "$LOCALAPPDATA\短视频复刻客户云工作台\legacy-backup"

!macro NSIS_HOOK_PREINSTALL
  ; ---- Legacy generation 1: 短视频复刻工作台 (0.1.12 / 0.1.13 / 0.1.15 / 0.1.16 #92) ----
  IfFileExists "$LOCALAPPDATA\短视频复刻工作台\uninstall.exe" legacy_internal_present legacy_internal_done

legacy_internal_present:
  ; Running-instance guard: probe the legacy product executable first (a
  ; running process locks its own image against write access); fall back to
  ; the uninstaller itself. A write-mode open that fails means the legacy
  ; edition is running or a file is held open — abort with ZERO mutation.
  ClearErrors
  IfFileExists "$LOCALAPPDATA\短视频复刻工作台\短视频复刻工作台.exe" legacy_internal_probe_app legacy_internal_probe_uninst
legacy_internal_probe_app:
  FileOpen $R9 "$LOCALAPPDATA\短视频复刻工作台\短视频复刻工作台.exe" a
  Goto legacy_internal_probe_done
legacy_internal_probe_uninst:
  FileOpen $R9 "$LOCALAPPDATA\短视频复刻工作台\uninstall.exe" a
legacy_internal_probe_done:
  IfErrors legacy_internal_running
  FileClose $R9
  CreateDirectory "${CW24_LEGACY_BACKUP_ROOT}\短视频复刻工作台"
  ClearErrors
  CopyFiles /SILENT "$LOCALAPPDATA\短视频复刻工作台\*.*" "${CW24_LEGACY_BACKUP_ROOT}\短视频复刻工作台"
  IfErrors legacy_internal_archive_failed
  IfFileExists "${CW24_LEGACY_BACKUP_ROOT}\短视频复刻工作台\uninstall.exe" legacy_internal_archived legacy_internal_archive_failed

legacy_internal_archived:
  ClearErrors
  FileOpen $R8 "${CW24_LEGACY_BACKUP_ROOT}\短视频复刻工作台\LEGACY-BACKUP-MANIFEST.txt" w
  IfErrors legacy_internal_archive_failed
  FileWrite $R8 "legacy_edition=短视频复刻工作台$\r$\n"
  FileWrite $R8 "source_install_dir=%LOCALAPPDATA%\短视频复刻工作台$\r$\n"
  FileWrite $R8 "supported_versions=0.1.12 / 0.1.13 / 0.1.15 / 0.1.16$\r$\n"
  FileWrite $R8 "archived_by=customer-cloud installer CW-024 upgrade guard$\r$\n"
  FileClose $R8
  ClearErrors
  ExecWait '"$LOCALAPPDATA\短视频复刻工作台\uninstall.exe" /S' $0
  IfErrors legacy_internal_failed
  IntCmp $0 0 legacy_internal_done legacy_internal_failed legacy_internal_failed

legacy_internal_running:
  MessageBox MB_ICONSTOP|MB_OK "检测到旧版「短视频复刻工作台」正在运行或其文件被占用。为保护数据，本次安装已中止且未改动任何文件；请退出旧版后重新运行安装程序。"
  Abort

legacy_internal_archive_failed:
  MessageBox MB_ICONSTOP|MB_OK "旧版「短视频复刻工作台」的数据备份或备份校验未通过。为保护历史数据，本次安装已中止，旧版未被改动；请确认磁盘空间充足后重试。"
  Abort

legacy_internal_failed:
  MessageBox MB_ICONSTOP|MB_OK "旧版「短视频复刻工作台」卸载失败。其数据已完整备份到 %LOCALAPPDATA%\短视频复刻客户云工作台\legacy-backup\短视频复刻工作台（含 LEGACY-BACKUP-MANIFEST.txt），保留可读备份；本次安装已中止，请按《客户版桌面升级与签名发布手册》恢复或手动卸载后重试。"
  Abort

legacy_internal_done:

  ; ---- Legacy generation 2: 众墅之家 (0.1.16 W1 品牌替换版, commit 1fb997a) ----
  IfFileExists "$LOCALAPPDATA\众墅之家\uninstall.exe" legacy_rebrand_present legacy_rebrand_done

legacy_rebrand_present:
  ClearErrors
  IfFileExists "$LOCALAPPDATA\众墅之家\众墅之家.exe" legacy_rebrand_probe_app legacy_rebrand_probe_uninst
legacy_rebrand_probe_app:
  FileOpen $R9 "$LOCALAPPDATA\众墅之家\众墅之家.exe" a
  Goto legacy_rebrand_probe_done
legacy_rebrand_probe_uninst:
  FileOpen $R9 "$LOCALAPPDATA\众墅之家\uninstall.exe" a
legacy_rebrand_probe_done:
  IfErrors legacy_rebrand_running
  FileClose $R9
  CreateDirectory "${CW24_LEGACY_BACKUP_ROOT}\众墅之家"
  ClearErrors
  CopyFiles /SILENT "$LOCALAPPDATA\众墅之家\*.*" "${CW24_LEGACY_BACKUP_ROOT}\众墅之家"
  IfErrors legacy_rebrand_archive_failed
  IfFileExists "${CW24_LEGACY_BACKUP_ROOT}\众墅之家\uninstall.exe" legacy_rebrand_archived legacy_rebrand_archive_failed

legacy_rebrand_archived:
  ClearErrors
  FileOpen $R8 "${CW24_LEGACY_BACKUP_ROOT}\众墅之家\LEGACY-BACKUP-MANIFEST.txt" w
  IfErrors legacy_rebrand_archive_failed
  FileWrite $R8 "legacy_edition=众墅之家$\r$\n"
  FileWrite $R8 "source_install_dir=%LOCALAPPDATA%\众墅之家$\r$\n"
  FileWrite $R8 "supported_versions=0.1.16 (品牌替换版)$\r$\n"
  FileWrite $R8 "archived_by=customer-cloud installer CW-024 upgrade guard$\r$\n"
  FileClose $R8
  ClearErrors
  ExecWait '"$LOCALAPPDATA\众墅之家\uninstall.exe" /S' $0
  IfErrors legacy_rebrand_failed
  IntCmp $0 0 legacy_rebrand_done legacy_rebrand_failed legacy_rebrand_failed

legacy_rebrand_running:
  MessageBox MB_ICONSTOP|MB_OK "检测到旧版「众墅之家」正在运行或其文件被占用。为保护数据，本次安装已中止且未改动任何文件；请退出旧版后重新运行安装程序。"
  Abort

legacy_rebrand_archive_failed:
  MessageBox MB_ICONSTOP|MB_OK "旧版「众墅之家」的数据备份或备份校验未通过。为保护历史数据，本次安装已中止，旧版未被改动；请确认磁盘空间充足后重试。"
  Abort

legacy_rebrand_failed:
  MessageBox MB_ICONSTOP|MB_OK "旧版「众墅之家」卸载失败。其数据已完整备份到 %LOCALAPPDATA%\短视频复刻客户云工作台\legacy-backup\众墅之家（含 LEGACY-BACKUP-MANIFEST.txt），保留可读备份；本次安装已中止，请按《客户版桌面升级与签名发布手册》恢复或手动卸载后重试。"
  Abort

legacy_rebrand_done:
!macroend
