# Release Notes — public-full

**Audience**: public
**Source commit**: `51b201e2e72f9bdbf56467b229dd7811d2ec058f`
**Plan digest**: `279a21181003f04f66cf2e0aec4ff30a4d08deeaec29bdb432dd04fd83932db7`
**Built at**: 2026-11-03T17:40:54.940364+00:00

## 组件清单

### disk_manager v0.0.0


### recorder v0.0.0


### modelscope_model_update v0.0.0


### accounting v0.0.0


### dev_toolkit v0.0.0


### life_design v0.0.0


### arknights_gacha v0.0.0


### wuwa_gacha v0.0.0


### endfield_gacha v0.0.0


### yihuan_gacha v0.0.0



## 文件统计

- 文件总数：1823
- 总大小：28795829 字节

## ZIP 校验

ZIP 文件的 SHA-256 校验和见同目录下的 `parcae-public-full.zip.sha256` 文件。

验证方法：

```bash
certutil -hashfile parcae-public-full.zip SHA256
# 比对 .zip.sha256 文件中的值
```

## 已知豁免

本包含以下敏感内容豁免（已审计，认为可安全分发）：

- `.agents/skills/_index.md` — sensitive_line_skips
- `.agents/skills/bat_writing/SKILL.md` — sensitive_line_skips
- `.agents/skills/bat_writing/references/guide.md` — sensitive_line_skips
- `.agents/skills/computer_use/apps/unrealeditor.md` — sensitive_line_skips
- `.agents/skills/daily/cross-machine-delivery-bundle/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/douyin_abdm_download/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/douyin_abdm_download/references/pitfalls.md` — sensitive_line_skips
- `.agents/skills/daily/douyin_abdm_download/scripts/batch_download.py` — sensitive_line_skips
- `.agents/skills/daily/esp32-arduino-offline-install/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/godot-mcp-toolchain-deploy/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/media-file-organizer/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/media-file-organizer/references/bilibili-works-ingest.md` — sensitive_line_skips
- `.agents/skills/daily/media-file-organizer/references/naming-and-entity-verification.md` — sensitive_line_skips
- `.agents/skills/daily/media-file-organizer/references/worked-example-20261023.md` — sensitive_line_skips
- `.agents/skills/daily/media-file-organizer/scripts/ingest_media_batch.py` — sensitive_line_skips
- `.agents/skills/daily/media-file-organizer/scripts/scan_media.py` — sensitive_line_skips
- `.agents/skills/daily/media-file-organizer/scripts/verify_media_folder.py` — sensitive_line_skips
- `.agents/skills/daily/memory-hygiene-two-layer/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/music-library-metadata-forensics/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/qoder-history-recall/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/vmware-bridged-networking-this-host/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/wb-history-recall/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/wb-takeover/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/windows-recycle-and-space-reclaim/SKILL.md` — sensitive_line_skips
- `.agents/skills/daily/windows-recycle-and-space-reclaim/scripts/recycle_audit.py` — sensitive_line_skips
- `.agents/skills/daily/windows-recycle-and-space-reclaim/scripts/recycle_purge.py` — sensitive_line_skips
- `.agents/skills/deep_research/references/pdf_report_style.md` — sensitive_line_skips
- `.agents/skills/dev/dev-toolkit-deploy/SKILL.md` — sensitive_line_skips
- `.agents/skills/dev/parcae-remote-sync/SKILL.md` — sensitive_line_skips
- `.agents/skills/dev/windows-kernel-readonly-forensics/SKILL.md` — sensitive_line_skips
- `.agents/skills/dev/windows-process-disk-io-audit/SKILL.md` — sensitive_line_skips
- `.agents/skills/office_docs/references/pdf_basic.md` — sensitive_line_skips
- `.agents/skills/temp_cleanup.md` — sensitive_line_skips
- `.gitignore` — sensitive_line_skips
- `CHANGELOG.md` — sensitive_line_skips
- `README_public.md` — sensitive_line_skips
- `docs/adr/0040-memory-telemetry-aggregate-before-purge-and-repair-labels-before-gating.md` — sensitive_line_skips
- `docs/browser-profile-safety.md` — sensitive_line_skips
- `docs/changelog-archive.md` — post_process_remove_lines
- `docs/changelog-archive.md` — sensitive_line_skips
- `docs/deployment.md` — sensitive_line_skips
- `docs/dev-workflow.md` — sensitive_line_skips
- `docs/environment-constraints.md` — sensitive_line_skips
- `docs/operations-manual.md` — sensitive_line_skips
- `docs/vm-ops.md` — sensitive_line_skips
- `lib/agent_runtime/builtin_tools/base.py` — sensitive_line_skips
- `lib/recorder/processor/stt_engine.py` — sensitive_line_skips
- `server/agent_guide_data.py` — sensitive_line_skips
- `server/browser/supervisor/browser_paths.py` — sensitive_line_skips
- `server/mcp_whitelist.py` — sensitive_line_skips
- `tests/browser/test_supervisor.py` — sensitive_line_skips
- `tests/dbview/test_dbview_api.py` — sensitive_line_skips
- `tests/files_tools/test_spacesniffer.py` — sensitive_line_skips
- `tests/guide_loops/test_inbox.py` — sensitive_line_skips
- `tests/llm_vision/test_model_manager_mindforge.py` — sensitive_line_skips
- `tests/release_ci/test_release_compiler.py` — sensitive_line_skips
- `tests/release_ci/test_release_orchestrator.py` — sensitive_line_skips
- `tests/release_ci/test_rule_liveness.py` — sensitive_line_skips
- `tests/server_endpoints/test_port_binding.py` — sensitive_line_skips
- `tests/server_endpoints/test_todos.py` — sensitive_line_skips
- `tests/test_config_schema.py` — sensitive_line_skips
- `tools/browser/start_debug_browser.py` — sensitive_line_skips
- `tools/deploy/apply_placeholders.py` — sensitive_line_skips
- `tools/disk_io_audit/README.md` — sensitive_line_skips
- `tools/disk_io_audit/srum_attrib.py` — sensitive_line_skips
- `tools/image_organizer/test_models.py` — sensitive_line_skips
- `tools/image_organizer/view_db.py` — sensitive_line_skips
- `tools/image_organizer/view_failed.py` — sensitive_line_skips
- `tools/img_tools.py` — sensitive_line_skips
- `tools/media_classifier/bilibili_classifier.py` — sensitive_line_skips
- `tools/media_classifier/bilibili_mover.py` — sensitive_line_skips
- `tools/media_classifier/mimo_lrc_analyze.py` — sensitive_line_skips
- `tools/media_classifier/print_anime.py` — sensitive_line_skips
- `tools/media_classifier/wenzhang_classifier.py` — sensitive_line_skips
- `tools/mem_watchdog/run_watchdog.bat` — sensitive_line_skips
- `tools/minitouch/arm64-v8a/minitouch` — sensitive_line_skips
- `tools/minitouch/armeabi-v7a/minitouch` — sensitive_line_skips
- `tools/minitouch/armeabi/minitouch` — sensitive_line_skips
- `tools/minitouch/x86/minitouch` — sensitive_line_skips
- `tools/minitouch/x86_64/minitouch` — sensitive_line_skips
- `tools/release/engine/prepare.py` — sensitive_line_skips
- `tools/simulator.py` — sensitive_line_skips
- `tools/spacesniffer/README.md` — sensitive_line_skips
- `tools_manifest.json` — sensitive_line_skips
- `workspace/disk_manager/SKILL.md` — sensitive_line_skips
- `workspace/disk_manager/references/sns_format.md` — sensitive_line_skips
- `workspace/disk_manager/scripts/assoc_common.py` — sensitive_line_skips
- `workspace/disk_manager/scripts/backup_env.py` — sensitive_line_skips
- `workspace/disk_manager/scripts/backup_groups.py` — sensitive_line_skips
- `workspace/disk_manager/settings_watch/FILE_ASSOCIATION.md` — sensitive_line_skips
- `workspace/disk_manager/settings_watch/base.py` — sensitive_line_skips
- `workspace/disk_manager/settings_watch/items.py` — sensitive_line_skips
- `workspace/disk_manager/tests/test_parse_sns.py` — sensitive_line_skips

## 审计追溯

本构建计划由 `prepare_release()` 产出不可变 `PreparedRelease`，经用户审批后由 `build_release()` 构建产物。plan_digest 绑定所有输入字段，任何输入变化都会让 plan_digest 变化，使旧审批失效。

- **plan_digest**: `279a21181003f04f66cf2e0aec4ff30a4d08deeaec29bdb432dd04fd83932db7`
- **profile_id**: `public-full`
- **audience**: `public`
- **source_commit**: `51b201e2e72f9bdbf56467b229dd7811d2ec058f`
- **built_at**: 2026-11-03T17:40:54.940364+00:00

## 许可证

Apache License 2.0 — 版权所有 (c) 2026 <copyright_holder>

详见 `LICENSE` 文件。
