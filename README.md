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
2. 脚本自动：修订号 +1、写 CHANGELOG、提交、打 tag，配了远端就顺手推送

也可以指定升级幅度和说明：

```powershell
# 次版本 +1
.\scripts\release.ps1 minor

# 主版本 +1（不兼容变更才用）
.\scripts\release.ps1 major

# 带一句话说明和分类，不用再手动输入
.\scripts\release.ps1 -Note "修复心跳超时" -Section 修复

# 预发布 / 转正式
.\scripts\release.ps1 minor -Label beta.1    # 0.2.0-beta.1
.\scripts\release.ps1 -Stable                # 0.2.0
```

发版前工作区必须是干净的（脚本会拦），免得把别的改动混进发版提交。

### 常用命令

```powershell
$git = 'D:\tools\git\cmd\git.exe'   # 便携版 git
& $git log --oneline -10            # 最近提交
& $git tag                          # 所有版本
& $git show v0.1.0 --stat           # 某版本改了什么
& $git diff                         # 未提交的改动
```

### 远端与 Release

推到哪里都行（GitHub / Gitee / 自建都行），远端只负责三件事：备份代码、存 tag、在 Releases 页面挂构建产物。

配好远端后，发版脚本会自动推送分支和 tag；没有远端时脚本只发到本地，并提示怎么补推。

发布页（网页端）的操作：

- 标题填 `v0.1.2`，选刚打的那个 tag
- 正文贴 `CHANGELOG.md` 里对应那一段
- 有 zip / exe 就拖进附件，用户直接下载，不用自己编译
- 预发布版（`-Label beta.1`）勾选 **Pre-release**，这样不会被当成正式版推给所有人

### 版本号怎么定

| 位 | 什么时候 +1 | 例子 |
|---|---|---|
| 主版本 | 不兼容变更：配置格式、数据结构、插件接口变了，老用户升级会坏 | 0.9.0 → 1.0.0 |
| 次版本 | 向后兼容地加功能 | 1.0.0 → 1.1.0 |
| 修订号 | 向后兼容地修 bug / 小改 | 1.1.0 → 1.1.1 |

预发布写成 `1.1.0-beta.1`、`1.1.0-rc.1`（小写、点号分段、序号递增）。
`0.x.y` 阶段随便破坏兼容性没关系，等用起来稳定了再发 `1.0.0`。

## 环境说明

git 是便携版，装在 `D:\tools\git`（没有装进系统 PATH）。
如果重装系统，重新下载 MinGit 解压到同一位置即可。

## 注意

- `astrbot-env/`、`python/`、`whisper-model/` 等运行环境目录已在 `.gitignore` 排除，不会进仓库
- 机器人运行时产生的数据在 `D:\AstrBot\bot\data`，也不在本仓库
