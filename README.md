# 大肥鱼插件项目

AstrBot 上运行的「大肥鱼」QQ 机器人插件合集（自写部分）。

## 目录结构

```
插件/
├─ plugins/                 # 自写插件（每个是一个 AstrBot 插件）
│  ├─ astrbot_plugin_heart         好感度 / 心情 / 日记 / 主动
│  ├─ astrbot_plugin_dafeiyu_soul  心智 / 记忆 / 梗库
│  ├─ astrbot_plugin_watchalong    陪看 B 站
│  ├─ astrbot_plugin_stickers      表情包
│  ├─ ...
├─ scripts/release.ps1      # 发版脚本
├─ 发布新版本.bat           # 双击发版
├─ CHANGELOG.md             # 变更日志
└─ VERSION                  # 当前版本号
```

第三方插件（`astrbot_plugin_group_chat_plus`、`astrbot_plugin_meme_manager`）不纳入本仓库。

## 版本管理

仓库用 git 管理，版本号在 `VERSION`，历史在 `CHANGELOG.md`，每次发版打一个 `vX.Y.Z` 标签。

### 发版方式

双击 `发布新版本.bat`，按提示：

1. 输入这次改了什么（一句话）
2. 脚本自动：修订号 +1、写 CHANGELOG、提交、打 tag

也可以指定升级幅度：

```powershell
# 次版本 +1
.\scripts\release.ps1 minor

# 主版本 +1（大改版）
.\scripts\release.ps1 major
```

### 常用命令

```powershell
$git = 'D:\tools\git\cmd\git.exe'   # 便携版 git
& $git log --oneline -10            # 最近提交
& $git tag                          # 所有版本
& $git show v0.1.0 --stat           # 某版本改了什么
& $git diff                         # 未提交的改动
```

## 环境说明

git 是便携版，装在 `D:\tools\git`（没有装进系统 PATH）。
如果重装系统，重新下载 MinGit 解压到同一位置即可。

## 注意

- `astrbot-env/`、`python/`、`whisper-model/` 等运行环境目录已在 `.gitignore` 排除，不会进仓库
- 机器人运行时产生的数据在 `D:\AstrBot\bot\data`，也不在本仓库
