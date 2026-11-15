# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [v1.2.0] - 2026-11-15

### Added
- 新增管理员指令 `/好友列表`（别名 `friend_list`），以合并转发的方式展示 Bot 当前的好友列表，每条包含 QQ 号与昵称/备注。
- 新增管理员指令 `/群列表`（别名 `group_list`），以合并转发的方式展示 Bot 当前所在的群列表，每条包含群号、群名与人数。
- 新增「好友 / 群列表指令」配置分组：可开关列表指令、限制仅私聊使用、设置单次最多展示条数与每个转发节点的条数。

### Changed
- 列表指令仅对 AstrBot 管理员可用（`PermissionType.ADMIN`），默认为仅私聊使用，避免好友 QQ 号在群聊中泄露。
- 整理 `main.py` 的导入顺序与注释，统一使用 `astrbot.api` 公开接口。

## [v1.1.3] - 2026-11-12

### Fixed
- 依 Deepseek 的仓库代码检查，修复了「不传入人格」的问题，并优化了部分代码。

## [v1.1.2] - 2026-11-08

### Added
- 添加 GitHub Actions 工作流，支持在手动触发或推送版本标签时，自动依文件提取版本号和更新日志并发布 Release。
- 增加并补全了 `CHANGELOG.md` 文件。

## [v1.1.1]

### Changed
- chore: update metadata with platform and astrbot version requirements by @LoreHan64 in #11

## [v1.1.0]

### Added
- 增加了「判定理由」供选定收信对象了解 LLM 的决定思路。
- 可选「处理好友申请的人设」。

### Fixed
- 本次更新修复了不传入人格至 LLM 的问题。

### Removed
- 移除「处理好友申请的判断模型」由于无法确认该功能运作正常与否。

## [v1.0.2]

### Fixed
- 试图修复一些问题。