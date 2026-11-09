# frp v0.72.0 技术验证报告

- **验证范围**：官方 frp 源码、v0.72.0 tag、GitHub Release 元数据、CLI 源码与命令输出
- **验证日期**：2026-11-09
- **工作区**：`/Users/a1/软件开发/项目集合/dsh-seesion-1`
- **项目仓库**：`frp-simple-bash/`
- **官方源码**：`frp-upstream-v0.72.0/`
- **首期版本**：frp `v0.72.0`

> 证据分为三类：
>
> 1. **已从源码/官方文档确认**：直接来自源码、源码测试、配置示例或命令实际输出；
> 2. **从 release 资产确认**：来自 GitHub Release API 或官方 checksum manifest；
> 3. **未在官方项目中找到/需封装项目自行定义**：官方项目没有提供，或当前环境无法完成真实平台验证。

---

## 1. 仓库与版本结果

### 1.1 封装项目仓库

已安全克隆到工作区子目录：

```text
/Users/a1/软件开发/项目集合/dsh-seesion-1/frp-simple-bash
```

克隆结果：

```text
## main...origin/main
```

该目录原本不存在，因此没有覆盖工作区已有需求和设计文档。原工作区不是 Git 仓库，根目录保持未初始化状态；没有添加 root remote，也没有修改现有文档。

### 1.2 官方 frp 源码

已克隆：

```text
/Users/a1/软件开发/项目集合/dsh-seesion-1/frp-upstream-v0.72.0
```

版本信息：

```text
tag: v0.72.0
annotated tag object: 367964fb75e8431a7246ed8d8bdb9f0b23dbe981
tag target commit: bafec21abce5f91ae4a05b4bd19c55543c263128
commit subject: Merge pull request #5605 from fatedier/dev
commit date: 2026-11-05T01:07:59+08:00
```

Git tag 不是直接 commit，而是 annotated tag；实现和源码引用应使用 peeled commit `bafec21abce5f91ae4a05b4bd19c55543c263128`，同时保留 tag `v0.72.0` 作为版本标识。

### 1.3 工作区修改

未提交、未推送任何 commit。官方源码和封装项目均未被业务代码修改。

验证过程中生成了官方源码目录下的 Go 缓存 `.gopath/`；由于其中部分文件由 Go 工具链创建为当前用户不可删除的权限，清理操作出现 `Permission denied`，该临时目录目前可能仍作为官方源码仓库下的未跟踪文件存在。未强行提权删除。

---

## 2. Dashboard/API 路径和返回结构

### 2.1 frps API 路由

**类别：已从源码确认**

证据：`frp-upstream-v0.72.0/server/api_router.go:27-68`、`server/http/controller.go`、`server/http/controller_v2.go`。

frps `webServer` 路由统一经过 `AuthMiddleware`；`/healthz` 不经过认证。完整核心路由如下：

| 方法 | 路径 | 源码处理函数 | 用途 |
|---|---|---|---|
| GET | `/healthz` | `healthz` | HTTP 服务存活检查；不代表客户端已连接或代理就绪 |
| GET | `/api/serverinfo` | `APIServerInfo` | 服务端版本、端口、流量、连接数、客户端数、代理类型统计 |
| GET | `/api/clients` | `APIClientList` | 客户端列表 |
| GET | `/api/clients/{key}` | `APIClientDetail` | 单个客户端详情 |
| GET | `/api/proxy/{type}` | `APIProxyByType` | 按代理类型查询代理统计 |
| GET | `/api/proxy/{type}/{name}` | `APIProxyByTypeAndName` | 按类型和名称查询代理 |
| GET | `/api/proxies/{name}` | `APIProxyByName` | 按名称查询代理 |
| GET | `/api/traffic/{name}` | `APIProxyTraffic` | 查询代理流量历史 |
| DELETE | `/api/proxies?status=offline` | `DeleteProxies` | 清理离线代理，仅允许 `status=offline` |
| GET | `/api/v2/users` | `APIV2UserList` | 用户统计分页 |
| GET | `/api/v2/system/info` | `APIV2SystemInfo` | v2 服务端系统信息和统计 |
| POST | `/api/v2/system/prune` | `APIV2SystemPrune` | 清理离线代理统计 |
| GET | `/api/v2/clients` | `APIV2ClientList` | v2 客户端分页查询 |
| GET | `/api/v2/clients/{key}` | `APIV2ClientDetail` | v2 客户端详情 |
| GET | `/api/v2/proxies` | `APIV2ProxyList` | v2 代理分页查询 |
| GET | `/api/v2/proxies/{name}` | `APIV2ProxyDetail` | v2 代理详情 |
| GET | `/api/v2/proxies/{name}/traffic` | `APIV2ProxyTraffic` | v2 代理最近 7 天流量 |

v2 支持的查询参数在 `server/http/controller_v2.go:318-405` 及对应测试中确认：`page`、`pageSize`（默认 1/50，最大 200）、`status`、`user`、`clientID`、`runID`、`q`、`type`。

### 2.2 frps 客户端返回结构

**类别：已从源码确认**

证据：`server/http/model/types.go:43-56`、`server/http/controller.go:89-148`。

旧版 `/api/clients` 单项结构：

```json
{
  "key": "...",
  "user": "...",
  "clientID": "...",
  "runID": "...",
  "version": "...",
  "wireProtocol": "...",
  "hostname": "...",
  "clientIP": "...",
  "firstConnectedAt": 0,
  "lastConnectedAt": 0,
  "disconnectedAt": 0,
  "online": true
}
```

字段对应 `ClientInfoResp`：`Key`、`User`、`ClientID`、`RunID`、`Version`、`WireProtocol`、`Hostname`、`ClientIP`、`FirstConnectedAt`、`LastConnectedAt`、`DisconnectedAt`、`Online`。

v2 `/api/v2/clients` 返回分页包装：

```json
{
  "total": 1,
  "page": 1,
  "pageSize": 50,
  "items": [
    {
      "key": "...",
      "user": "...",
      "clientID": "...",
      "runID": "...",
      "version": "...",
      "wireProtocol": "...",
      "hostname": "...",
      "clientIP": "...",
      "firstConnectedAt": 0,
      "lastConnectedAt": 0,
      "disconnectedAt": 0,
      "online": true
    }
  ]
}
```

v2 客户端详情额外包含：

```json
"status": {
  "phase": "online",
  "curConns": 0,
  "proxyCount": 1
}
```

注意：`firstConnectedAt` 等时间字段是 Unix 时间戳类型 `int64`，封装层应转换为本地时间显示。

### 2.3 frps 代理状态返回结构

**类别：已从源码确认**

旧版 `/api/proxy/{type}` 的单项结构来自 `server/http/model/types.go:100-115`：

```json
{
  "name": "ssh",
  "conf": {},
  "user": "",
  "clientID": "",
  "todayTrafficIn": 0,
  "todayTrafficOut": 0,
  "curConns": 0,
  "lastStartTime": "...",
  "lastCloseTime": "...",
  "status": "online"
}
```

v2 `/api/v2/proxies` 返回：

```json
{
  "total": 1,
  "page": 1,
  "pageSize": 50,
  "items": [
    {
      "name": "ssh",
      "user": "",
      "clientID": "",
      "spec": {
        "type": "tcp",
        "tcp": {
          "remotePort": 6000
        }
      },
      "status": {
        "phase": "online",
        "todayTrafficIn": 0,
        "todayTrafficOut": 0,
        "curConns": 0,
        "lastStartAt": 0,
        "lastCloseAt": 0
      }
    }
  ]
}
```

证据：`server/http/model/v2.go:76-179`、`server/http/controller_v2.go:220-295, 495-510`。

v2 代理 `spec` 支持 `tcp`、`udp`、`http`、`https`、`tcpmux`、`stcp`、`sudp`、`xtcp`；HTTP/HTTPS 规格包含 `customDomains`、`subdomain`、`locations`、`hostHeaderRewrite` 等字段。

### 2.4 Dashboard/Admin API 认证

**类别：已从源码确认**

证据：

- frps：`server/api_router.go:30-35` 使用 `helper.AuthMiddleware`；
- frpc：`client/api_router.go:33-35` 使用同一类认证中间件；
- SDK：`pkg/sdk/client/client.go:117-120` 设置 HTTP Basic Authorization；
- CLI：`cmd/frpc/sub/admin.go:75-90` 从 `webServer.user/password` 设置 SDK 认证。

认证流程：

1. 将 `webServer.user` 和 `webServer.password` 配置到 frps 或 frpc 的 `webServer`；
2. 管理器请求 API 时添加 HTTP Basic Authentication；
3. 用户名和密码为空时不设置 Authorization 头；
4. 请求非 200 返回错误；
5. API 调用超时默认为 30 秒，CLI 可使用 `--api-timeout` 覆盖。

这不是 token 认证。frpc 与 frps 的连接 token 位于 `auth.token`，Dashboard/Admin HTTP Basic 凭据独立。

### 2.5 frpc Admin UI/API 路由

**类别：已从源码确认**

证据：`client/api_router.go:26-53`、`client/http/model/types.go`、`pkg/sdk/client/client.go`。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | HTTP 服务存活检查 |
| GET | `/api/status` | 所有代理状态，返回 `map[string][]ProxyStatusResp` |
| GET | `/api/reload` | 热加载；支持 `strictConfig=true` 查询参数 |
| POST | `/api/stop` | 停止运行中的 frpc |
| GET | `/api/config` | 获取当前配置文本 |
| PUT | `/api/config` | 更新配置文本 |
| GET | `/api/proxy/{name}/config` | 获取指定代理配置 |
| GET | `/api/visitor/{name}/config` | 获取指定 visitor 配置 |
| GET | `/api/store/proxies` | Store 代理列表 |
| POST | `/api/store/proxies` | 创建 Store 代理 |
| GET | `/api/store/proxies/{name}` | 查询 Store 代理 |
| PUT | `/api/store/proxies/{name}` | 更新 Store 代理 |
| DELETE | `/api/store/proxies/{name}` | 删除 Store 代理 |
| GET | `/api/store/visitors` | Store visitor 列表 |
| POST | `/api/store/visitors` | 创建 Store visitor |
| GET | `/api/store/visitors/{name}` | 查询 Store visitor |
| PUT | `/api/store/visitors/{name}` | 更新 Store visitor |
| DELETE | `/api/store/visitors/{name}` | 删除 Store visitor |

`GET /api/status` 返回：

```json
{
  "tcp": [
    {
      "name": "ssh",
      "type": "tcp",
      "status": "online",
      "err": "",
      "local_addr": "127.0.0.1:22",
      "plugin": "",
      "remote_addr": "x.x.x.x:6000",
      "source": "config"
    }
  ]
}
```

证据：`client/http/model/types.go:19-32`、`pkg/sdk/client/client.go:35-71`。

### 2.6 Dashboard/API 结论

首期封装层应优先使用：

- frps `GET /api/v2/clients` / `GET /api/v2/clients/{key}` 获取客户端状态；
- frps `GET /api/v2/proxies` / `GET /api/v2/proxies/{name}` 获取代理状态；
- frps `GET /api/v2/proxies/{name}/traffic` 获取流量历史；
- frpc `GET /api/status` 获取本机代理连接状态；
- frpc `GET /api/reload` 实现热加载；
- 统一使用 HTTP Basic Auth；
- API 访问超时默认 30 秒。

---

## 3. CLI 参数与退出码

### 3.1 `frps --help` 实际输出

**类别：已从源码确认 + 实际构建输出**

使用 `go run -tags noweb ./cmd/frps --help` 和构建后的 v0.72.0 二进制验证。`noweb` 仅用于当前源码 checkout 缺少 web dashboard `dist` 资源时运行 CLI；官方正常发布包包含 dashboard 资源。

完整 flags：

```text
--allow-ports string
--allow-unsafe strings
--bind-addr string (default "0.0.0.0")
-p, --bind-port int (default 7000)
-c, --config string
--dashboard-addr string (default "0.0.0.0")
--dashboard-port int
--dashboard-pwd string (default "admin")
--dashboard-tls-cert-file string
--dashboard-tls-key-file string
--dashboard-tls-mode
--dashboard-user string (default "admin")
--disable-log-color
--enable-prometheus
-h, --help
--kcp-bind-port int
--log-file string (default "console")
--log-level string (default "info")
--log-max-days int (default 3)
--max-ports-per-client int
--proxy-bind-addr string (default "0.0.0.0")
--quic-bind-port int
--strict-config (default true)
--subdomain-host string
--tls-only
-t, --token string
-v, --version
--vhost-http-port int
--vhost-http-timeout int (default 60)
--vhost-https-port int
```

证据：`cmd/frps/root.go`、`pkg/config/flags.go`，并由 v0.72.0 实际 help 输出确认。

### 3.2 `frpc --help` 实际输出

**类别：已从源码确认 + 实际构建输出**

```text
-c, --config string (default "./frpc.ini")
--config-dir string
--strict-config (default true)
--allow-unsafe strings
-v, --version
-h, --help
```

可用子命令：

```text
completion
help
http
https
nathole
reload
status
stop
stcp
sudp
tcp
tcpmux
udp
verify
xtcp
```

v0.72.0 实际输出中的正式子命令为：`completion`、`help`、`http`、`https`、`nathole`、`reload`、`status`、`stop`、`stcp`、`sudp`、`tcp`、`tcpmux`、`udp`、`verify`、`xtcp`。

证据：`cmd/frpc/sub/root.go`、`cmd/frpc/sub/admin.go`、`cmd/frpc/sub/proxy.go`、`cmd/frpc/sub/verify.go`，以及实际 help 输出。

### 3.3 子命令参数

**类别：已从源码确认 + 实际构建输出**

```text
frps verify [flags]
  -h, --help
  继承全部 frps 全局 flags

frpc verify [flags]
  -h, --help
  继承 frpc 全局 flags

frpc reload [flags]
  --api-timeout duration (default 30s)
  -h, --help
  继承 frpc 全局 flags

frpc status [flags]
  --api-timeout duration (default 30s)
  -h, --help
  继承 frpc 全局 flags

frpc stop [flags]
  --api-timeout duration (default 30s)
  -h, --help
  继承 frpc 全局 flags
```

### 3.4 配置校验退出码

**类别：已从源码确认 + 实际构建输出**

`cmd/frps/verify.go` 和 `cmd/frpc/sub/verify.go` 对加载/校验错误调用 `os.Exit(1)`。实际运行结果：

```text
frps verify -c <不存在文件> -> exit 1
frpc verify -c <不存在文件> -> exit 1
frps verify -c <类型错误配置> -> exit 1
frpc verify -c <类型错误配置> -> exit 1
```

成功校验退出码应为 0，但需要使用有效 v0.72.0 配置进一步执行确认。错误文本示例：

```text
open <path>: no such file or directory
field "bindPort": cannot unmarshal string into int
```

`go run` 因编译失败时的 `exit status 1` 不应当当成 frp CLI 运行码；本报告的退出码结论来自成功构建的 `.tmp-frps`/`.tmp-frpc`。

---

## 4. Release 资产与 SHA256

### 4.1 Release 信息

**类别：从 release 资产确认**

官方 Release API：

```text
https://api.github.com/repos/fatedier/frp/releases/tags/v0.72.0
```

Release 页面：

```text
https://github.com/fatedier/frp/releases/tag/v0.72.0
```

发布时间：`2026-11-04T17:19:02Z`。

### 4.2 checksum manifest

官方资产：

```text
frp_sha256_checksums.txt
```

官方下载地址：

```text
https://github.com/fatedier/frp/releases/download/v0.72.0/frp_sha256_checksums.txt
```

GitHub API asset endpoint：

```text
https://api.github.com/repos/fatedier/frp/releases/assets/710291523
```

通过 GitHub API 获取的 manifest 格式为每行两个空格分隔字段：

```text
<64 位小写 SHA256>  <文件名>
```

示例：

```text
daf719b32ca2778770bb92955e231a07414d754378b9af328aa2642862479804  frp_0.72.0_linux_amd64.tar.gz
```

### 4.3 首期三种资产校验值

| 资产 | SHA256 |
|---|---|
| `frp_0.72.0_linux_amd64.tar.gz` | `daf719b32ca2778770bb92955e231a07414d754378b9af328aa2642862479804` |
| `frp_0.72.0_linux_arm64.tar.gz` | `02180b857480302fd934e9ddbd50fe2f73229f99062f88756abc6de9df5e95cf` |
| `frp_0.72.0_windows_amd64.zip` | `5197c1b69564089cdc229fc1260a0e25db1e722b1b7db45fe1542c3c43edf9ae` |

Windows ARM64 资产的完整 checksum：

```text
e4b83b66c4045971dd9a47cdef16dd02af471aaa6667dd303e63b1a669162927  frp_0.72.0_windows_arm64.zip
```

### 4.4 下载器实现要求

首期下载器应：

1. 固定版本 `v0.72.0`；
2. 根据 OS/架构计算文件名；
3. 下载对应 Release 资产；
4. 下载 `frp_sha256_checksums.txt`；
5. 按文件名精确匹配 manifest 行；
6. 使用系统 SHA256 工具计算实际值；
7. 使用常量时间或普通安全比较验证值；
8. 校验失败不替换已安装二进制；
9. 记录 Release URL、资产名、期望值和实际值（不涉及秘密）。

---

## 5. 服务注册与平台兼容性

### 5.1 官方项目是否提供服务模板

**类别：未在官方项目中找到/需封装项目自行定义**

官方 v0.72.0 源码未找到 systemd unit、OpenRC 脚本或 Windows Service 注册脚本。源码中的 `system`/`service` 搜索结果主要是 Go 依赖和进程服务代码，不是安装器服务模板。

因此以下内容属于封装项目自定义：

- Linux systemd unit；
- Alpine OpenRC 脚本；
- Windows `New-Service`/`sc.exe` 注册；
- 服务名称、开机启动策略、日志重定向和停止等待逻辑。

### 5.2 systemd 设计

封装项目可自定义：

```ini
[Unit]
Description=frp-simple-bash frps
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
ExecStart=/usr/local/bin/frps -c /etc/frp/frps.toml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

这不是官方 frp 模板；是本项目根据已确认的 root 运行方案设计的封装模板。

### 5.3 OpenRC 设计

官方项目未提供 OpenRC 模板。封装项目自行定义：

```sh
#!/sbin/openrc-run
name="frps"
description="frp-simple-bash frps"
command="/usr/local/bin/frps"
command_args="-c /etc/frp/frps.toml"
command_background=true
pidfile="/run/${RC_SVCNAME}.pid"
output_log="/var/log/frp/frps.log"
error_log="/var/log/frp/frps.err.log"

depend() {
    need net
}
```

具体 `command_background`、pidfile 和日志行为必须在目标 Alpine 版本上实际验证。

### 5.4 Windows Service 设计

官方项目未提供 Windows Service 注册参数。封装项目自行定义：

```powershell
$serviceName = "frp-simple-bash-frps"
$binary = "C:\Program Files\frp-simple-bash\bin\frps.exe"
$config = "C:\ProgramData\frp-simple-bash\frps.toml"
$binPath = "`"$binary`" -c `"$config`""
New-Service -Name $serviceName -DisplayName "frp-simple-bash frps" -BinaryPathName $binPath -StartupType Automatic
```

frpc 使用相同结构，替换服务名、二进制和配置路径。服务注册需要管理员权限。该参数是封装项目设计，不是官方 frp 结论。

### 5.5 当前平台验证限制

Linux systemd、Alpine OpenRC、Windows Service 不能在当前 macOS 工作区直接完成真实注册测试，因此以下结论仍需目标系统验证：

```text
systemd unit 实际启动/停止行为：待 Linux 验证
OpenRC 脚本实际启动/停止行为：待 Alpine 验证
Windows Service 引号和参数行为：待 Windows 验证
Windows Service 停止超时和退出码：待 Windows 验证
```

---

## 6. 手工配置反向导入、IPv6、中文名称和编码

### 6.1 手工编辑配置

**类别：未在官方项目中找到/需封装项目自行定义**

官方 frp 提供的是：

- `includes` 配置拆分；
- `store.path` 动态配置持久化；
- `frpc reload -c` 热加载；
- `frpc verify -c` 校验；
- `frpc status -c` 查询代理状态。

官方项目没有 profile、映射目录或本项目 `index.json/state.json` 的反向导入机制。完整规则必须由封装项目自行定义：

1. 对 `profile.toml`、`mappings.toml`、`frpc.toml` 记录修改时间和 SHA256；
2. 启动菜单前检查外部变化；
3. 使用 v0.72.0 配置加载器或严格 TOML 解析器校验；
4. 只导入管理器支持的字段；
5. 将 `frpc.toml` 的 `serverAddr/serverPort` 映射回 active profile；
6. 将 `[[proxies]]` 的 `name/type/localIP/localPort/remotePort/customDomains/enabled` 映射回当前 profile；
7. 对无法映射的字段保存在 `advanced` 或显示差异，不静默丢弃；
8. 外部修改与管理器同时修改时提示导入、保留或查看差异；
9. 导入成功后更新 index/state 和哈希；
10. 导入失败保留旧有效配置。

### 6.2 IPv6

**类别：部分已从源码确认，具体展示需封装项目定义**

源码使用 Go 的网络地址处理和 `net.JoinHostPort`（证据：`pkg/sdk/client/client.go:24-27`），因此 HTTP 客户端地址拼接支持 IPv6 方括号形式。管理器应保存不带方括号的裸 IPv6 地址，展示和连接时使用：

```text
名称（[2001:db8::10]:7000）
```

frp 业务配置的 IPv6 字段和所有边界行为需要在 v0.72.0 实例上进一步测试；官方源码未在当前扫描结果中提供一份完整 IPv6 配置示例。

### 6.3 中文 profile/代理名称

**类别：未在官方项目中找到明确兼容承诺/需封装项目测试**

官方示例和源码测试主要使用 ASCII 名称。不能仅凭当前源码断言中文代理名在所有 Dashboard、API、日志和 Windows 场景都完全兼容。

本项目规则保持：

- 服务端 profile 名称允许中文、字母、数字、`-`、`_`，不允许空格；
- profile 名称是管理器本地标签，不直接作为 frp proxy name；
- 首期代理名称建议继续使用 ASCII `A-Za-z0-9_-`；
- 中文名称必须在 macOS/Linux/Windows 菜单、TOML、JSON、日志和 API 返回中做端到端测试。

### 6.4 Windows PowerShell 中文编码

**类别：未在官方 frp 项目中找到/需封装项目自行验证**

官方 frp 源码没有规定 PowerShell 中文输入输出编码。封装脚本应明确使用 UTF-8，并在 Windows PowerShell 5.1 与 PowerShell 7 分别测试：

```powershell
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
```

是否需要针对 Windows PowerShell 5.1 采用兼容写法、当前终端代码页和重定向文件编码，留待 Windows 实机测试。

### 6.5 Windows TOML UTF-8

**类别：未在官方项目中找到明确封装约定/需验证**

Go TOML 加载器通常按 UTF-8 读取配置，但官方 v0.72.0 文档未在当前材料中明确声明 BOM、非 UTF-8 文件或 PowerShell 重定向编码的兼容承诺。封装项目应：

- 使用 UTF-8 无 BOM 写入 TOML/JSON；
- 导入前检测 UTF-8；
- BOM 文件是否接受需实测；
- 非 UTF-8 直接报错并保留原文件；
- 中文 profile 名称写入和再读取执行 Windows 实测。

---

## 7. 结论与剩余事项

### 7.1 已确认、可进入封装实现的内容

- frps/frpc v0.72.0 主要配置字段；
- frps v1/v2 Dashboard API 路径；
- frps 客户端和代理 JSON 结构；
- frpc Admin API 路径、Basic Auth 和状态结构；
- CLI 全局 flags、子命令和 `--api-timeout`；
- `verify` 错误退出码为 1，成功预期为 0；
- checksum manifest URL、格式和 Windows ARM64 SHA256；
- Release 资产名称和首期三平台校验值。

### 7.2 必须由封装项目自定义的内容

- systemd、OpenRC、Windows Service 模板；
- profile 和 mappings 的反向导入规则；
- profile 编号、中文别名和菜单展示；
- 运行账户、日志轮转、备份和升级回滚；
- IPv6 展示格式；
- PowerShell 编码兼容策略；
- Windows TOML 编码检测和写入策略。

### 7.3 仍需目标环境实测

1. Linux systemd 实际注册、启动、停止、自启动；
2. Alpine OpenRC 实际行为；
3. Windows Service 引号、权限、启动和停止；
4. v0.72.0 有效配置的 `verify` 成功返回码；
5. v0.72.0 真实 Dashboard/API 的认证和响应；
6. IPv6 端到端连接；
7. 中文菜单、profile、TOML、JSON、日志和 Windows PowerShell 编码；
8. Release 资产下载和 SHA256 本地计算一致性。

---
