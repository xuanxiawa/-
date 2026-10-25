# 大肥鱼插件项目 · 变更日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循语义化版本（主版本.次版本.修订号）。

## [0.1.2] - 2026-10-25

### 变更
- 整理仓库：补 .gitattributes，发版脚本支持预发布、自动推送、失败即报错

## [0.1.1] - 2026-10-25

### 变更
- 修复中文提交信息编码问题

## [0.1.0] - 2026-10-25

### 新增
- 项目初始化，纳入 18 个自写 AstrBot 插件
- `astrbot_plugin_heart`：好感度（Lv.1~10）+ 心情（开心/平静/闷闷的/有点难过/生气）+ 三级日记（普通 7 天 / 重要 30 天 / 非常重要永久）+ 主动找你（约定 / 想你 / 随性）+ 每日 04:00 复盘
- `astrbot_plugin_dafeiyu_soul`：情绪状态、长期记忆、梗库、聊天中自学新梗
- `astrbot_plugin_watchalong`：主人看 B 站时陪看并接话
- `astrbot_plugin_bili_lookup`：B 站视频信息查询
- `astrbot_plugin_screen_eye`：看屏幕
- `astrbot_plugin_ears`：听歌识曲
- `astrbot_plugin_lyrics`：搜歌 + 歌词
- `astrbot_plugin_stickers` / `astrbot_plugin_meme_render`：表情包发送与自制梗图
- `astrbot_plugin_qq_avatar` / `astrbot_plugin_qq_more` / `astrbot_plugin_poke` / `astrbot_plugin_toast`：QQ 头像昵称、说说、戳一戳、桌面通知
- `astrbot_plugin_terraria`：泰拉瑞亚看画面 + 按键
- `astrbot_plugin_watchdog`：QQ 掉线提醒 + NapCat 自愈
- `astrbot_plugin_batch_delay`：批量延迟发送
- `astrbot_plugin_bili_client` / `astrbot_plugin_browser_bili`：B 站客户端与浏览器控制（已停用，保留源码）

### 说明
- `astrbot_plugin_group_chat_plus`（作者 Him666233）与 `astrbot_plugin_meme_manager`（作者 anka）为第三方插件，不纳入本仓库
- 运行环境、模型、虚拟环境等大体积目录通过 `.gitignore` 排除
