# DSH 插件（独立于 AstrBot）

> 建目录时间：2026-10-25
> 规则：以后给 DSH（DeepSeek Harness）装的插件都放这里，不放 `node_modules`，也不放 C 盘用户目录。

## 目录清单

| 路径 | 是什么 | 现在怎么用 |
|---|---|---|
| `dsh\dsh-tts-bridge\` | 朗读桥 1.2.9（DSH 插件包，MIT，第三方） | DSH profile 用 `file:D:/大肥鱼/插件/dsh/dsh-tts-bridge` 依赖它 |
| `dsh\bin\windows-mcp.exe` | computer-use MCP 的可执行入口 | `cordis.patch.yml` 里的 `mcp-windows` 条目调用它 |
| `dsh\uv-tools\windows-mcp\` | 上面那个 exe 的 Python 3.14 venv（uv 管） | 不要手删；删了 exe 就跑不起来 |

`dsh\bin\`、`dsh\uv-tools\`、`dsh\dsh-tts-bridge\` 已在 `.gitignore` 里排除，不进仓库（第三方包各自有仓库）。

## 以后加东西的两种做法

### 1) DSH 插件包（界面插件 / bundle）

```powershell
# 1. 把插件源码放进 D:\大肥鱼\插件\dsh\<包名>\
# 2. 在 profile 里登记（会写进 package.json + 锁文件）
$node='C:\Users\HONOR\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe'
$pnpm='C:\Users\HONOR\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\pnpm\bin\pnpm.mjs'
Set-Location 'C:\Users\HONOR\.dsh\profiles\desktop'
& $node $pnpm add 'file:D:/大肥鱼/插件/dsh/<包名>'
```

再把 `<包名>` 加进 `C:\Users\HONOR\.dsh\profiles\desktop\package.json` 的
`dsh.profile.bundles` 数组，重启 DSH。

### 2) MCP 服务器（Python 那类）

```powershell
$env:UV_TOOL_DIR     = 'D:\大肥鱼\插件\dsh\uv-tools'
$env:UV_TOOL_BIN_DIR = 'D:\大肥鱼\插件\dsh\bin'
& "$env:USERPROFILE\.local\bin\uv.exe" tool install <包名> --python 3.14
```

然后在 `C:\Users\HONOR\.dsh\profiles\desktop\cordis.patch.yml` 加一条：

```yaml
- id: mcp-<名字>
  name: "@deepseek-ai/dsh-mcp-client"
  config:
    serverName: <名字>          # 工具名会变成 mcp__<名字>__<工具>
    transport: stdio
    command: 'D:\大肥鱼\插件\dsh\bin\<可执行文件>.exe'
    args: [serve]
```

## 注意

- **DSH 那边不认这个目录**：它认的是 `C:\Users\HONOR\.dsh\profiles\desktop\` 里的
  `package.json`（依赖 + bundles）和 `cordis.patch.yml`（MCP 条目）。这里的文件只是"真实存放位置"。
- 朗读桥的**浏览器扩展**要在 Edge 里单独加载，路径：
  `D:\大肥鱼\插件\dsh\dsh-tts-bridge\extension`
- 朗读桥没发 npm，丢了就重新下：

```powershell
Set-Location 'D:\大肥鱼\插件\dsh'
curl.exe -L -o tts.zip 'https://codeload.github.com/Yuuyuko-uu/dsh-tts-bridge/tar.gz/refs/heads/main'
```

- MCP 想换版本：`uv tool install windows-mcp --python 3.14 --force`（记得带上面两个环境变量）。
