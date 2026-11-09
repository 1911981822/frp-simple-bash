# frp 二次封装详细设计文档

- **文档版本**：V0.1
- **文档状态**：详细设计基线
- **编写日期**：2026-11-09
- **项目名称**：frp-simple-bash
- **代码仓库**：https://github.com/1911981822/frp-simple-bash
- **首期固定 frp 版本**：v0.72.0
- **关联文档**：
  - [frp二次封装需求文档.md](frp二次封装需求文档.md)
  - [frp二次封装概要设计文档.md](frp二次封装概要设计文档.md)
- **目标平台**：主流 Linux、Windows x64
- **命令入口**：`frps`、`frpc`
- **Linux 配置根目录**：`/etc/frp/`
- **Windows 配置根目录**：`C:\ProgramData\frp-simple-bash\`

> 本文将概要设计落实为目录、模块、数据结构、调用流程、服务模板、校验规则和测试设计。frp 具体版本及 Dashboard/API 字段在实现前必须以选定 Release 的官方文档和实际二进制验证结果为准，不能凭经验硬编码。

---

## 1. 设计基线

### 1.1 已确定规则

1. 用户输入 `frps` 或 `frpc` 后默认进入数字菜单。
2. 用户输入菜单编号后执行对应操作，操作完成后返回菜单。
3. `frpc` 采用一个实例保存多个 frps profile 的模式。
4. 用户通过服务端编号选择、切换 profile。
5. 服务端名称支持中文、字母、数字、`-`、`_`，不允许空格，名称必须唯一且允许修改。
6. 菜单显示格式为：`服务端名称（地址:业务端口）`。
7. 每个 profile 的端口映射完全独立。
8. 主菜单第 9 项进入“修改当前 frps（服务端别名）端口映射”子菜单。
9. 端口映射子菜单包含新增、查看、修改、启用/停用、删除。
10. Dashboard/API 管理端口由用户输入，直接回车使用 `7500`。
11. 运行中的 profile 切换需要确认、自动重启并等待连接结果，最多等待 30 秒。
12. profile 删除时同时删除该 profile 的映射，不要求额外自动生成备份。
13. 配置数据使用 TOML，状态和索引使用 JSON。
14. 防火墙只检测和提示，不自动修改规则。
15. HTTP/HTTPS 首期只实现基础字段，高级字段预留。
16. 文件日志保留 30 天，最大 500MB，并进行轮转。
17. Linux 使用 systemd/OpenRC，Windows 使用 Windows Service。

### 1.2 首期范围

- Linux Bash 安装和管理；
- Windows PowerShell 安装和管理；
- frps/frpc 安装、升级、卸载；
- 服务启停、状态、自启动；
- frps 配置管理；
- frpc 首次向导；
- 多 profile 和编号切换；
- TCP、UDP、HTTP、HTTPS 基础映射；
- Dashboard/API 客户端连接信息；
- 日志、诊断、配置备份和恢复。

### 1.3 首期不实现

- Web 管理面板；
- 同时运行多个 frpc 实例；
- 自动修改防火墙；
- STCP、SUDP、XTCP 完整配置菜单；
- HTTP/HTTPS 高级字段菜单；
- 自动云安全组配置；
- 集群、高可用和负载均衡。

---

## 2. 技术实现总览

### 2.1 技术形态

首期采用脚本实现，不引入独立编译型管理程序：

```text
Linux
├── Bash 5.x 或兼容版本
├── curl 或 wget
├── sha256sum 或 sha256 校验工具
├── systemd 或 OpenRC
└── frps/frpc 官方二进制

Windows
├── PowerShell 5.1+
├── Windows Service
├── Invoke-WebRequest 或 BITS
├── Get-FileHash
└── frps.exe/frpc.exe 官方二进制
```

实现要求：

- Bash 与 PowerShell 的业务概念、菜单编号和配置字段保持一致；
- 平台相关实现不得混入通用业务规则；
- 所有用户输入必须经过校验；
- 所有配置写入必须支持失败恢复；
- 命令返回码和 JSON 字段保持稳定。

### 2.2 推荐实现分层

```text
入口层
  ↓
命令解析层
  ↓
菜单/向导层
  ↓
业务服务层
  ↓
配置、状态、服务、网络适配层
  ↓
操作系统和 frp 二进制
```

### 2.3 统一业务对象

跨平台脚本需要统一以下对象名称：

```text
InstallContext
PlatformInfo
ServiceInfo
Profile
Mapping
RuntimeState
ConnectionResult
OperationResult
```

PowerShell 使用对象/哈希表实现，Bash 使用关联数组、临时 JSON/TOML 文件或统一键值读取函数实现。对外输出字段保持一致。

---

## 3. 项目目录与文件职责

### 3.1 发布仓库目录

```text
frp-simple-bash/
├── README.md
├── LICENSE
├── CHANGELOG.md
├── VERSION
├── install-frps.sh
├── install-frpc.sh
├── install-frps.ps1
├── install-frpc.ps1
├── uninstall.sh
├── uninstall.ps1
├── upgrade.sh
├── upgrade.ps1
├── scripts/
│   ├── common.sh
│   ├── platform.sh
│   ├── download.sh
│   ├── checksum.sh
│   ├── service-systemd.sh
│   ├── service-openrc.sh
│   ├── service-windows.ps1
│   ├── frps-menu.sh
│   ├── frpc-menu.sh
│   ├── frps-menu.ps1
│   └── frpc-menu.ps1
├── templates/
│   ├── frps.service
│   ├── frpc.service
│   ├── frps.openrc
│   ├── frpc.openrc
│   ├── frps-service.xml
│   └── frpc-service.xml
├── config/
│   ├── defaults.toml
│   ├── defaults.json
│   └── version-map.json
├── docs/
│   ├── install.md
│   ├── configuration.md
│   └── troubleshooting.md
└── tests/
    ├── bash/
    ├── powershell/
    ├── fixtures/
    └── integration/
```

### 3.2 Linux 安装后目录

```text
/usr/local/bin/frps
/usr/local/bin/frpc
/usr/local/bin/frpctl-common        # 可选公共脚本，不作为用户入口

/etc/frp/
├── frps.toml                        # frps 运行配置
├── frpc.toml                        # active profile 运行配置
├── state.json                       # 运行和选择状态
├── index.json                       # profile 索引
├── profiles/
│   └── <profile-id>/
│       ├── profile.toml             # 连接配置，不含或尽量少含秘密
│       ├── mappings.toml            # 当前 profile 的映射
│       └── secrets.toml             # token、密码、私钥引用或敏感字段
├── backups/
│   ├── frps-<timestamp>.tar.gz
│   ├── frpc-<timestamp>.tar.gz
│   └── profiles/<profile-id>/
├── logs/
│   ├── manager.log
│   ├── frps.log
│   └── frpc.log
└── locks/
    ├── config.lock
    └── operation.lock

/etc/systemd/system/
├── frps.service
└── frpc.service

/etc/openrc/conf.d/
├── frps
└── frpc
```

### 3.3 Windows 安装后目录

```text
C:\Program Files\frp-simple-bash\
├── bin\
│   ├── frps.exe
│   └── frpc.exe
├── scripts\
│   ├── frps.ps1
│   └── frpc.ps1
└── VERSION

C:\ProgramData\frp-simple-bash\
├── frps.toml
├── frpc.toml
├── state.json
├── index.json
├── profiles\
│   └── <profile-id>\
│       ├── profile.toml
│       ├── mappings.toml
│       └── secrets.toml
├── backups\
├── logs\
│   ├── manager.log
│   ├── frps.log
│   └── frpc.log
└── locks\

Windows Service：
├── frp-simple-bash-frps
└── frp-simple-bash-frpc
```

> Windows 路径中的反斜杠在脚本中必须通过 PowerShell 路径 API 处理，不得手工拼接未转义字符串。

---

## 4. 模块详细设计

### 4.1 入口模块

#### 4.1.1 Bash 入口

```text
frps
└── /usr/local/bin/frps -> /opt/frp-simple-bash/bin/frps-menu.sh

frpc
└── /usr/local/bin/frpc -> /opt/frp-simple-bash/bin/frpc-menu.sh
```

如果用户传入参数：

```text
frps <subcommand> [options]
frpc <subcommand> [options]
```

则跳过交互主菜单，进入命令分发器。

#### 4.1.2 PowerShell 入口

```text
frps
└── frps.ps1

frpc
└── frpc.ps1
```

Windows 环境下通过 PATH 中的包装器或 PowerShell 函数实现 `frps`、`frpc` 调用。若系统策略不允许直接执行 `.ps1`，安装器应给出执行策略提示，不得静默修改全局执行策略。

### 4.2 输入与输出模块

统一函数职责：

```text
read_menu_choice(prompt, min, max)
read_required_text(prompt, validator)
read_optional_text(prompt, default, validator)
read_secret(prompt)
confirm_action(prompt)
print_success(message)
print_warning(message)
print_error(code, message, hint)
```

规则：

- 空输入仅在字段允许时作为默认值；
- 菜单空输入不退出，重新提示；
- `0` 只在菜单规定的位置表示返回/取消；
- 高风险操作必须使用确认函数；
- 密码和 token 输入不回显；
- 菜单内容支持中文 UTF-8；
- 输出前检查终端编码，Windows 需要尽量切换到 UTF-8 输出。

### 4.3 平台检测模块

输出统一结构：

```text
PlatformInfo
├── os_family       linux/windows
├── distribution    ubuntu/alpine/rhel/windows
├── os_version
├── architecture    amd64/arm64/...
├── service_manager systemd/openrc/windows-service/none
├── shell_version
├── is_admin
├── install_root
└── supported
```

Linux 检测顺序：

1. 判断 `uname`；
2. 读取 `/etc/os-release`；
3. 判断 `systemctl`；
4. 判断 `rc-service`/`openrc`；
5. 判断 root 权限；
6. 判断架构是否有对应 Release 文件。

Windows 检测内容：

1. `$PSVersionTable.PSVersion`；
2. `[Environment]::Is64BitOperatingSystem`；
3. 当前用户管理员状态；
4. Windows Service 管理权限；
5. PowerShell 执行策略；
6. 目标目录写入权限。

### 4.4 下载与版本模块

接口：

```text
resolve_version(requested_version) -> VersionInfo
resolve_asset(version, os, arch) -> AssetInfo
download_asset(asset, target_path) -> OperationResult
verify_asset(asset, file_path) -> OperationResult
install_binary(file_path, destination) -> OperationResult
```

`VersionInfo`：

```text
version
release_url
published_at
is_pinned
checksum_url
```

`AssetInfo`：

```text
os
architecture
archive_url
archive_name
checksum
checksum_algorithm
```

下载流程：

1. 读取固定版本或用户指定版本；
2. 获取 Release 元数据；
3. 根据 OS/架构选择资产；
4. 下载到临时目录；
5. 校验文件大小和 SHA256；
6. 解压到临时目录；
7. 检查 `frps`/`frpc` 文件存在；
8. 设置执行权限；
9. 原子替换目标二进制；
10. 保存版本信息。

禁止：

- 未校验即执行二进制；
- 下载失败时覆盖旧版本；
- 以用户输入未经转义的 URL 拼接 Shell 命令。

### 4.5 配置模块

配置模块统一提供：

```text
load_toml(path)
save_toml_atomic(path, model)
load_json(path)
save_json_atomic(path, model)
validate_config(model, schema)
backup_config(paths, reason)
restore_config(backup_id)
set_secure_permissions(path, kind)
redact_secrets(model)
```

由于 Bash 原生 TOML 处理能力有限，详细实现应选择以下一种方式并在编码前固定：

1. 使用 `frp` 自身的配置校验命令，管理器负责生成模板；
2. 使用系统已存在的 TOML 工具；
3. 使用受控的轻量辅助程序；
4. 采用严格模板生成而非通用 TOML 解析。

不得在未验证的情况下通过简单字符串替换修改 TOML，避免破坏数组、引号和嵌套结构。

### 4.6 外部手工配置变更检测

管理器允许高级用户直接编辑 `/etc/frp/` 或 `C:\ProgramData\frp-simple-bash\` 下的 TOML 配置。每次进入 `frps`/`frpc` 菜单或执行管理命令时，按以下流程检测：

1. 读取配置文件修改时间、大小和内容哈希；
2. 与管理器最近一次写入记录比较；
3. 如果发现外部修改，先执行 TOML 解析和字段校验；
4. 对 `frpc.toml`/`frps.toml` 的有效修改尝试导入到管理器模型；
5. 可以映射到 profile 或 mapping 的字段同步更新 profile、映射和状态文件；
6. 无法识别、字段冲突或跨 profile 的修改不得静默覆盖；
7. 出现冲突时进入数字菜单，让用户选择“导入外部修改”“保留管理器配置”或“查看差异”；
8. 导入成功后刷新索引、更新时间和配置哈希，并记录审计日志。

外部手工编辑不应绕过敏感字段保护、权限检查和配置校验。导入失败时保留当前有效管理器配置，不破坏运行配置。

### 4.7 系统依赖安装

安装器自动检测以下工具：`curl`/`wget`、`tar`、`sha256sum`、`systemctl` 或 `openrc` 等。缺失依赖时，安装器显示待安装包和系统变更，并要求用户确认后调用对应包管理器：

- Debian/Ubuntu：`apt`；
- RHEL/CentOS/Rocky/Alma：优先 `dnf`，兼容 `yum`；
- Alpine：`apk`。

自动安装依赖必须记录日志；包管理器失败时停止安装并给出手动安装命令。Windows 所需 PowerShell、Hash、Service 等能力由系统提供，不自动安装第三方依赖。

### 4.8 首期平台资产

v0.72.0 首期只提供并验证：

- `linux_amd64`；
- `linux_arm64`；
- `windows_amd64`。

其他 Release 资产暂不作为首期安装目标，但下载器保留架构映射扩展点。

---

## 5. 数据模型与文件格式

### 5.1 全局索引 `index.json`

```json
{
  "schema_version": 1,
  "active_profile_id": "prod-01",
  "profiles": [
    {
      "id": "prod-01",
      "name": "生产服务器",
      "address": "192.0.2.10",
      "bind_port": 7000,
      "dashboard_port": 7500,
      "created_at": "2026-11-09T10:00:00+08:00",
      "updated_at": "2026-11-09T10:30:00+08:00"
    }
  ]
}
```

要求：

- `id` 永久唯一，不因改名变化；
- `name` 唯一；
- `active_profile_id` 可以为空；
- index 只保存展示和索引数据，不保存 token；
- 列表显示使用数组顺序或稳定的 `menu_order`；
- 删除 profile 时从 index 原子移除。

### 5.2 Profile `profile.toml`

```toml
schema_version = 1
id = "prod-01"
name = "生产服务器"
server_addr = "192.0.2.10"
server_port = 7000
dashboard_addr = "127.0.0.1"   # 默认仅本机监听
dashboard_port = 7500
transport_protocol = "tcp"
tls_enable = false
auth_method = "token"
created_at = "2026-11-09T10:00:00+08:00"
updated_at = "2026-11-09T10:30:00+08:00"
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `schema_version` | 是 | 管理器配置版本 |
| `id` | 是 | 不可变唯一 ID |
| `name` | 是 | 唯一服务端名称 |
| `server_addr` | 是 | IPv4、IPv6 或域名 |
| `server_port` | 是 | frps 业务连接端口 |
| `dashboard_addr` | 否 | Dashboard/API 地址 |
| `dashboard_port` | 是 | 默认 7500 |
| `transport_protocol` | 否 | 首期默认 tcp |
| `tls_enable` | 否 | 是否启用 TLS |
| `auth_method` | 否 | token 或版本支持的认证类型 |
| `created_at` | 是 | 创建时间 |
| `updated_at` | 是 | 修改时间 |

### 5.3 敏感配置 `secrets.toml`

```toml
schema_version = 1
token = "<secret>"
dashboard_username = "admin"
dashboard_password = "<secret>"
tls_ca_file = ""
tls_cert_file = ""
tls_key_file = ""
```

设计要求：

- 该文件不能被普通用户组读取；
- 菜单展示时只显示是否已设置；
- 日志和 JSON 输出不得输出原值；
- 修改时先写临时文件，再设置权限，最后原子替换；
- TLS 私钥可只保存文件路径，不强制复制私钥内容。

### 5.4 映射配置 `mappings.toml`

```toml
schema_version = 1

[[mappings]]
name = "web"
type = "tcp"
local_ip = "127.0.0.1"
local_port = 8080
remote_port = 18080
enabled = true
note = "内部 Web 服务"

[[mappings]]
name = "http-demo"
type = "http"
local_ip = "127.0.0.1"
local_port = 8081
remote_port = 0
domain = "demo.example.com"
enabled = false
note = "测试映射"
```

基础字段：

| 字段 | TCP/UDP | HTTP/HTTPS | 规则 |
|---|---:|---:|---|
| `name` | 是 | 是 | 当前 profile 内唯一 |
| `type` | 是 | 是 | tcp/udp/http/https |
| `local_ip` | 是 | 是 | 默认 127.0.0.1 |
| `local_port` | 是 | 是 | 1-65535 |
| `remote_port` | 是 | 否/可选 | 1-65535，按 frp 版本规则 |
| `domain` | 否 | HTTP/HTTPS 可选 | 域名格式校验 |
| `enabled` | 是 | 是 | true/false |
| `note` | 否 | 否 | 不得含控制字符 |

高级字段放在预留对象中，不首期暴露菜单：

```toml
[advanced]
tls_enable = false
basic_auth = false
custom_headers = []
health_check = {}
```

### 5.5 运行状态 `state.json`

```json
{
  "schema_version": 1,
  "active_profile_id": "prod-01",
  "frpc": {
    "service_state": "running",
    "connection_state": "connected",
    "last_checked_at": "2026-11-09T10:35:00+08:00",
    "last_result_code": 0,
    "last_error": ""
  },
  "frps": {
    "service_state": "stopped",
    "last_checked_at": "2026-11-09T10:35:00+08:00"
  }
}
```

运行状态属于缓存，不能作为唯一真实状态来源。每次进入主菜单都应尽量实时刷新。

### 5.6 生成的 `frpc.toml`

`frpc.toml` 只对应 active profile：

```toml
serverAddr = "192.0.2.10"
serverPort = 7000

[auth]
method = "token"
token = "<secret>"

[[proxies]]
name = "web"
type = "tcp"
localIP = "127.0.0.1"
localPort = 8080
remotePort = 18080
```

字段名称必须根据最终 frp 版本的官方配置格式生成。管理器内部字段使用 snake_case，生成 frp 配置时由版本适配器转换为对应格式。

### 5.7 生成的 `frps.toml`

```toml
bindAddr = "0.0.0.0"
bindPort = 7000

[auth]
method = "token"
token = "<secret>"

webServer.addr = "127.0.0.1"
webServer.port = 7500
webServer.user = "admin"
webServer.password = "<secret>"
```

此处仅展示设计意图，最终字段必须由版本适配器依据选定 frp 版本确认。

---

## 6. 标识符、输入与校验规则

### 6.1 服务端名称

建议正则：

```text
^[\p{Han}A-Za-z0-9_-]+$
```

实现注意：

- Bash 环境的正则和 locale 对 Unicode 支持不一致，应优先使用字符逐项校验或可靠工具；
- Windows PowerShell 使用 .NET 正则；
- 名称长度建议 1-64 个字符；
- 去除首尾空格后再校验；
- 中间空格直接拒绝；
- 空名称拒绝；
- 与现有名称不区分大小写比较，避免 Windows 选择混淆；
- 名称变更只修改 `name`，不修改 profile ID。

错误提示：

```text
名称不能为空，不能包含空格，只允许中文、字母、数字、- 和 _。
该服务端名称已存在，请重新输入。
```

### 6.2 地址

支持：

- IPv4；
- IPv6；
- DNS 域名。

校验要求：

- 去除首尾空格；
- 禁止包含空格和控制字符；
- IPv6 保存时不含菜单格式方括号，展示和生成连接字符串时再处理；
- 域名不允许包含协议前缀 `http://`、`https://`；
- 不在本地强制判断地址一定可达，使用连接检测报告真实结果。

### 6.3 端口

- 范围 1-65535；
- 0 只在最终 frp 版本明确允许自动分配时使用；
- 业务端口和 Dashboard/API 端口分别校验；
- 本机监听端口修改前检测占用；
- 远程映射端口检测当前 profile 内冲突；
- 检测到常见管理端口或系统端口时给出警告；
- 空输入仅 Dashboard/API 端口允许，使用默认 7500。

### 6.4 映射名称

建议正则：

```text
^[A-Za-z0-9_-]{1,64}$
```

首期建议映射名称使用 ASCII，避免不同终端和 frp 版本对中文代理名处理不一致。名称必须在当前 profile 内唯一。

### 6.5 菜单编号

- 主菜单和子菜单编号从 1 开始；
- `0` 固定为返回/取消；
- 非法数字不执行任何操作；
- 超出范围重新提示；
- 空输入重新提示；
- 删除和停止不允许通过一次数字输入直接执行，必须进入确认步骤。

---

## 7. 数字菜单详细设计

### 7.1 frps 主菜单

启动 `frps` 后显示：

```text
frps 服务端管理
项目：frp-simple-bash
当前状态：运行中
当前版本：vX.Y.Z
业务监听：0.0.0.0:7000
Dashboard/API：127.0.0.1:7500
配置路径：/etc/frp/frps.toml

请选择操作：
1. 查看运行状态
2. 查看版本信息
3. 查看安装路径
4. 启动 frps
5. 停止 frps
6. 重启 frps
7. 查看客户端连接信息
8. 修改服务端配置
9. 查看日志
10. 设置/取消开机自启动
11. 卸载 frps
0. 返回/退出
请输入数字：
```

分发映射：

```text
1 -> show_frps_status
2 -> show_version
3 -> show_paths
4 -> start_frps
5 -> confirm_then_stop_frps
6 -> confirm_then_restart_frps
7 -> show_client_connections
8 -> frps_config_menu
9 -> logs_menu
10 -> boot_menu
11 -> uninstall_menu
0 -> exit
```

### 7.2 frpc 主菜单

```text
frpc 客户端管理
项目：frp-simple-bash
当前状态：运行中
当前服务端：生产服务器（192.0.2.10:7000）
连接状态：已连接
当前配置：/etc/frp/frpc.toml

请选择操作：
1. 查看当前状态
2. 启动 frpc
3. 停止 frpc
4. 重启 frpc
5. 新建 frps 服务端
6. 查看服务端列表
7. 切换当前 frps 服务端
8. 修改当前 frps 配置
9. 修改当前 frps（生产服务器）端口映射
10. 查看日志与连接诊断
11. 设置/取消开机自启动
12. 卸载 frpc
0. 返回/退出
请输入数字：
```

分发映射：

```text
1 -> show_frpc_status
2 -> start_frpc
3 -> confirm_then_stop_frpc
4 -> confirm_then_restart_frpc
5 -> create_profile_wizard
6 -> list_profiles
7 -> select_and_switch_profile
8 -> current_profile_config_menu
9 -> current_profile_mapping_menu
10 -> diagnostics_menu
11 -> boot_menu
12 -> uninstall_menu
0 -> exit
```

没有 active profile 时：

- 第 2、3、4、9、10 项应显示不可用原因或进入引导；
- 第 5 项保持可用；
- 第 6 项显示“暂无服务端”；
- 第 7、8 项提示先创建 profile。

### 7.3 服务端编号列表

```text
请选择服务端：

1. 生产服务器（192.0.2.10:7000）[已连接] [3 个映射]
2. 家庭服务器（example.com:7000）[未连接] [1 个映射]
3. 测试服务器（203.0.113.10:7000）[已停止] [0 个映射]
0. 返回上一级
请输入数字：
```

编号只在当前列表会话中有效，不作为 profile 永久 ID。用户输入后通过数组索引取得 profile ID。

### 7.4 新建 profile 向导

```text
请输入服务端名称：
请输入服务端 IP、IPv6 或域名：
请输入 frps 业务连接端口：
是否启用 token 认证？(Y/n)：
请输入 token：
是否启用 TLS？(y/N)：
请输入 Dashboard/API 管理端口（直接回车使用默认端口 7500）：
是否将其设为当前服务端？(Y/n)：
```

流程：

1. 读取名称并校验；
2. 检查名称唯一性；
3. 读取地址和业务端口；
4. 读取 token/TLS；
5. 读取管理端口，空输入设为 7500；
6. 展示摘要；
7. 用户确认；
8. 创建 profile ID；
9. 写入 profile、secret、空映射文件；
10. 更新 index；
11. 如选择 active，生成运行配置；
12. 询问是否立即启动；
13. 如启动，执行连接检测。

新建 profile 时不强制配置第一条映射。

### 7.5 当前 frps 配置子菜单

```text
当前 frps：生产服务器（192.0.2.10:7000）

1. 查看连接配置
2. 修改服务端名称
3. 修改服务器地址
4. 修改服务器端口
5. 修改 token
6. 修改 TLS 配置
7. 修改 Dashboard/API 管理端口
8. 保存并返回
0. 取消并返回
```

修改服务端名称流程：

1. 显示旧名称；
2. 读取新名称；
3. 执行字符和唯一性校验；
4. 更新 profile.toml 和 index.json；
5. 不改变 profile ID、映射和连接地址；
6. 更新菜单标题；
7. 显示修改成功。

修改地址、业务端口或认证配置后：

- 运行中的 frpc 必须提示需要重启；
- 选择立即生效时执行配置生成、重启和连接检测；
- 选择稍后生效时只保存配置，并显示待重启状态。

### 7.6 当前 profile 映射子菜单

```text
当前 frps：生产服务器（192.0.2.10:7000）
端口映射管理

1. 新增端口映射
2. 查看当前服务端映射
3. 修改端口映射
4. 启用/停用端口映射
5. 删除端口映射
0. 返回上一级
```

进入前必须读取 active profile ID，并将其显示在标题中。任何情况下不从其他 profile 的 `mappings.toml` 合并数据。

新增映射流程：

```text
选择类型
  ↓
输入映射名称
  ↓
输入本地地址
  ↓
输入本地端口
  ↓
按类型输入远程端口/域名
  ↓
设置启用状态
  ↓
显示摘要
  ↓
校验并保存
  ↓
询问是否立即重启
```

查看映射显示：

```text
当前服务端：生产服务器

1. web       TCP   127.0.0.1:8080 -> 18080   已启用
2. api       HTTP  127.0.0.1:8081 -> demo.example.com  已停用
0. 返回
```

修改、启停和删除均先通过编号选择映射，再执行操作。删除必须显示映射摘要并二次确认。

---

## 8. Profile 与映射隔离实现

### 8.1 隔离原则

profile ID 是唯一隔离键：

```text
profile-id
├── profile.toml
├── secrets.toml
└── mappings.toml
```

所有业务函数必须接收 `profile_id`，不得依赖全局当前目录推断目标 profile。

### 8.2 读取规则

```text
get_active_profile_id()
  -> state.json.active_profile_id

load_profile(profile_id)
  -> profiles/<profile_id>/profile.toml

load_mappings(profile_id)
  -> profiles/<profile_id>/mappings.toml

build_runtime_config(profile_id)
  -> 只能读取该 profile 的 profile/secrets/mappings
```

### 8.3 防串配置检查

生成 `frpc.toml` 前执行：

1. profile ID 非空；
2. profile 目录存在；
3. profile.toml 的 ID 与目录名一致；
4. index 中名称和地址与 profile 文件一致；
5. mappings.toml 不包含其他 profile ID；
6. 生成临时配置中的映射名称全部来自当前 profile；
7. 生成后记录 `source_profile_id` 到 state；
8. 校验通过后才替换运行配置。

### 8.4 切换后的清理

切换 profile 时：

- 先停止旧 frpc；
- 删除或覆盖旧的临时生成文件；
- 使用新 profile 完整生成 `frpc.toml`；
- 不在新配置中保留旧 profile 的映射；
- 记录新的 active profile；
- 启动并检测新连接。

---

## 9. 服务生命周期设计

### 9.1 ServiceAdapter 接口

```text
install_service(component, config_path) -> OperationResult
uninstall_service(component) -> OperationResult
start_service(component) -> OperationResult
stop_service(component) -> OperationResult
restart_service(component) -> OperationResult
get_service_status(component) -> ServiceStatus
set_autostart(component, enabled) -> OperationResult
get_logs(component, follow, lines) -> LogResult
```

`component`：`frps` 或 `frpc`。

### 9.2 systemd unit

设计模板：

```ini
[Unit]
Description=frp-simple-bash frps service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# 首期按已确认方案使用 root/当前管理员账户运行
User=root
Group=root
ExecStart=/usr/local/bin/frps -c /etc/frp/frps.toml
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

frpc unit 将 `frps` 和配置路径替换为 `frpc`。具体命令行参数必须匹配选定 frp 版本。

systemd 操作：

```text
systemctl daemon-reload
systemctl enable/disable frps
systemctl start/stop/restart frps
systemctl is-active frps
journalctl -u frps
```

执行外部命令时参数必须使用数组/安全调用，不得拼接未校验用户输入。

### 9.3 OpenRC 服务

模板职责：

- 设置 `name`、`command`、`command_args`；
- 指定用户和组；
- 支持 `depend()` 网络依赖；
- 支持 start/stop/restart/status；
- 通过 `rc-update add/del` 设置开机启动。

### 9.4 Windows Service

服务配置：

```text
ServiceName: frp-simple-bash-frps
DisplayName: frp-simple-bash frps
BinaryPath: "C:\Program Files\frp-simple-bash\bin\frps.exe" -c "C:\ProgramData\frp-simple-bash\frps.toml"
StartupType: Automatic
```

frpc 使用对应的 exe 和 `frpc.toml`。

PowerShell 操作：

```text
New-Service
Start-Service
Stop-Service
Restart-Service
Get-Service
Set-Service
Remove-Service
```

服务注册、删除和启动要求管理员权限。失败时输出 ServiceName、错误原因和日志路径。

### 9.5 后台兼容模式

无 systemd/OpenRC 时：

- 使用受控后台进程启动；
- 将 PID 写入状态文件；
- 启动前检查旧 PID 是否仍有效；
- 停止时优先发送正常终止信号；
- 等待退出后再强制结束；
- 明确提示无法提供标准开机自启动。

---

## 10. 状态、连接与诊断设计

### 10.1 状态刷新顺序

进入主菜单时：

1. 读取缓存状态；
2. 查询服务管理器；
3. 查询进程；
4. 检查配置文件；
5. 对 frpc 查询连接状态；
6. 更新 state.json；
7. 渲染菜单。

任何单项查询失败不能导致整个菜单退出，应显示“未知”并给出诊断入口。

### 10.2 服务状态

```text
not-installed
stopped
starting
running
stopping
failed
unknown
```

### 10.3 连接状态

```text
not-configured
connecting
connected
disconnected
auth-failed
unreachable
tls-failed
config-error
timeout
unknown
```

### 10.4 连接成功判定

切换 profile 或启动 frpc 后，按以下优先级判定：

1. frp 版本适配器可提供官方 API 状态时，查询 API；
2. 检查服务端管理接口是否能看到该客户端；
3. 辅助读取 frpc 日志中的成功连接标记；
4. 服务进程仍存活不能单独视为连接成功。

最多等待 30 秒，建议每 2 秒轮询一次。成功立即停止轮询；超时返回 `E013`。

### 10.5 诊断输出

```text
当前服务端：生产服务器（192.0.2.10:7000）
服务状态：运行中
进程状态：存在
配置状态：有效
网络检测：可达
认证状态：失败
最近错误：token 不匹配
建议：进入“修改当前 frps 配置”修改 token
日志路径：/etc/frp/logs/frpc.log
```

---

## 11. Dashboard/API 详细设计

### 11.1 适配器接口

```text
dashboard_login(profile_id) -> Session
get_online_clients(profile_id) -> ClientList
get_client_detail(profile_id, client_id) -> ClientDetail
get_proxy_list(profile_id, client_id) -> ProxyList
check_client_connected(profile_id, client_identity) -> ConnectionResult
logout(session) -> OperationResult
```

### 11.2 版本能力模型

由于 Dashboard/API 可能随 frp 版本变化，适配器保存能力信息：

```text
DashboardCapabilities
├── can_list_clients
├── can_get_client_detail
├── can_list_proxies
├── can_check_connection
└── api_version
```

初始化时：

1. 读取当前 frp 版本；
2. 加载版本适配配置；
3. 探测管理接口；
4. 登录并校验凭据；
5. 保存可用能力；
6. 菜单只展示可用数据。

### 11.3 凭据隔离

- frpc token 用于 frps 连接认证；
- Dashboard 用户名/密码用于管理接口；
- 两者在数据模型和菜单中分开；
- API 请求不得把 token 当 Dashboard 密码发送；
- API 错误不得输出密码和 token。

### 11.4 API 关闭和失败处理

Dashboard/API 凭据在新建或修改 profile 时允许留空。留空表示该 profile 关闭 Dashboard/API 管理功能：

- `frps` 菜单的“查看客户端连接信息”显示“当前未配置 Dashboard/API，请先配置管理端口和凭据”；
- 不尝试使用默认用户名和密码；
- 不使用日志解析结果伪装实时客户端状态；
- profile 仍可用于 frpc 连接和端口映射管理；
- 用户可以在配置子菜单中补充管理端口、用户名和密码后重新启用。

API 适配是首期正式功能。API 已配置但暂不可用时：

- 显示 API 不可用；
- 显示可执行的网络和认证诊断；
- 可以读取日志帮助定位；
- 不将日志解析结果伪装成实时 API 状态；
- 菜单仍可执行本地服务管理功能。

---

## 12. 配置写入、备份与恢复

### 12.1 原子写入算法

```text
acquire_lock
  ↓
load_current_model
  ↓
apply_change
  ↓
validate_model
  ↓
write_temp_file
  ↓
set_temp_permissions
  ↓
validate_generated_file
  ↓
move_current_to_backup
  ↓
rename_temp_to_current
  ↓
release_lock
```

任一步骤失败：

- 删除临时文件；
- 保留旧正式文件；
- 恢复必要的备份；
- 返回错误码和日志路径。

### 12.2 普通配置备份

普通配置修改前保存备份：

```text
backups/
└── 20261109-103000-config-change/
    ├── index.json
    ├── state.json
    ├── profiles/
    └── manifest.json
```

建议保留最近 10 份普通配置备份。具体数量可以在详细实现参数中配置。

### 12.3 profile 删除

profile 删除不额外自动生成新备份，流程只执行：

1. 明确提示将删除该 profile 的全部映射；
2. 要求二次确认；
3. 停止当前连接（若删除的是 active profile 且正在运行）；
4. 删除 profile 目录；
5. 更新 index 和 state；
6. 重新生成或清理 active 配置；
7. 返回结果。

如果历史备份中已有该 profile，不在删除时主动新建备份。

### 12.4 升级备份

升级属于独立高风险流程，必须保留升级前版本和配置，以支持回滚。升级备份不等同于 profile 删除备份。

---

## 13. 日志设计

### 13.1 日志分类

```text
manager.log       安装、配置、菜单、错误和审计事件
frps.log          frps 进程日志（文件日志模式）
frpc.log          frpc 进程日志（文件日志模式）
```

### 13.2 统一格式

```text
2026-11-09T10:30:00+08:00 level=INFO component=profile action=switch profile=prod-01 result=success
```

禁止字段：

```text
token
password
private_key
secret
authorization_header
```

### 13.3 日志级别

```text
DEBUG
INFO
WARN
ERROR
```

默认 `INFO`。DEBUG 只能通过明确配置开启，并继续执行敏感信息脱敏。

### 13.4 轮转策略

- 保留 30 天；
- 文件日志总量最多 500MB；
- 单文件达到配置阈值时轮转；
- 超过总量时从最旧文件开始清理；
- 系统日志由 journald、OpenRC 日志系统或 Windows Event Log 负责；
- 管理菜单显示日志文件路径和查看方式。

### 13.5 审计事件

至少记录：

- 安装、升级、卸载；
- 启动、停止、重启；
- 新建、修改、切换、删除 profile；
- 新增、修改、启停、删除映射；
- 认证失败；
- API 访问失败；
- 配置校验失败。

---

## 14. 安装、升级与卸载详细流程

### 14.1 Linux 安装入口

```bash
bash install-frps.sh
bash install-frpc.sh
```

远程安装脚本首期发布到项目 GitHub 仓库：

```text
https://github.com/1911981822/frp-simple-bash
```

首期使用 `main` 分支，安装脚本放在仓库根目录，文件名固定为 `install-frps.sh`、`install-frpc.sh`、`install-frps.ps1`、`install-frpc.ps1`。因此安装器文档可将 `<INSTALL_URL>` 固定为：

```text
https://raw.githubusercontent.com/1911981822/frp-simple-bash/main
```

建议用户先下载、查看脚本，再执行：

```bash
curl -fsSL <INSTALL_URL>/install-frps.sh -o install-frps.sh
less install-frps.sh
bash install-frps.sh
```

脚本参数建议：

```text
--version <version>
--install-dir <dir>
--config-dir <dir>
--mirror <url>
--non-interactive
--yes
```

### 14.2 Windows 安装入口

```powershell
powershell -ExecutionPolicy Bypass -File .\install-frps.ps1
powershell -ExecutionPolicy Bypass -File .\install-frpc.ps1
```

远程脚本建议先下载和查看，再执行。安装器不得强制修改用户全局执行策略。

### 14.3 重复安装

检测内容：

- 已安装版本；
- 已存在服务；
- 已存在配置；
- 运行状态；
- 目标目录是否自定义。

处理策略：

- 默认不覆盖配置；
- 用户选择同版本重装时只更新二进制或重新安装管理文件；
- 跨版本安装提示升级流程；
- 配置版本不兼容时停止并提示迁移。

### 14.4 升级流程

```text
读取当前版本
  ↓
获取目标稳定版本或用户指定版本
  ↓
展示变更摘要
  ↓
确认
  ↓
备份配置和旧二进制
  ↓
下载并校验新二进制
  ↓
停止服务
  ↓
替换二进制
  ↓
执行配置校验
  ↓
启动服务
  ↓
执行健康检查
  ↓
失败则回滚
```

### 14.5 卸载流程

默认：

- 停止服务；
- 删除服务注册；
- 删除二进制和命令入口；
- 保留 `/etc/frp/` 或 Windows ProgramData 配置、日志和备份；
- 明确显示保留路径。

彻底清理：

- 必须二次确认；
- 删除配置、profile、映射、日志和备份；
- 删除服务用户（如果该用户由安装器创建）；
- 不自动修改防火墙规则。

---

## 15. 错误码与异常处理

### 15.1 错误码

| 错误码 | 含义 | 典型处理 |
|---|---|---|
| E001 | 不支持的系统/架构 | 提示支持范围 |
| E002 | 权限不足 | root/管理员重试 |
| E003 | 下载失败 | 检查网络和镜像 |
| E004 | 校验失败 | 删除临时文件，保留旧版本 |
| E005 | 配置格式错误 | 恢复旧配置 |
| E006 | 端口非法/占用 | 修改端口 |
| E007 | 服务启动失败 | 查看服务日志 |
| E008 | 地址不可达 | 检查 DNS、防火墙和地址 |
| E009 | token/TLS 认证失败 | 修改认证配置 |
| E010 | Dashboard/API 不可用 | 检查管理端口和凭据 |
| E011 | profile 不存在 | 刷新列表或新建 |
| E012 | 映射不存在 | 刷新当前 profile |
| E013 | 连接超时 | 30 秒后返回并诊断 |
| E014 | 配置写入失败 | 检查权限和磁盘 |
| E015 | 用户取消 | 返回菜单 |
| E016 | 锁被占用 | 等待其他操作完成 |
| E017 | 版本不兼容 | 选择支持版本或升级 |

### 15.2 异常恢复原则

- 异常不能直接退出数字菜单；
- 配置写入失败不得破坏旧配置；
- 服务启动失败不自动重复无限重启；
- 切换连接失败保留目标 profile 作为 active，还是回滚旧 profile，需要在交互中明确提示；首期建议保留目标 profile 并显示失败状态，用户可重新连接或切回旧 profile；
- 删除失败不删除 index 中的记录，直到目录删除和状态更新都成功；
- API 失败不影响本地服务启停功能。

---

## 16. 非交互 CLI 详细设计

### 16.1 frps

```text
frps status
frps start
frps stop
frps restart
frps version
frps path
frps logs [--follow] [--lines N]
frps install [options]
frps uninstall [--purge]
frps upgrade [--version VERSION]
frps status --json
```

### 16.2 frpc

```text
frpc status
frpc start
frpc stop
frpc restart
frpc logs [--follow] [--lines N]
frpc profile list [--json]
frpc profile add
frpc profile use <profile-id-or-number>
frpc profile edit <profile-id>
frpc profile rename <profile-id> <name>
frpc profile delete <profile-id>
frpc mapping list [--profile <profile-id>]
frpc mapping add [--profile <profile-id>]
frpc mapping edit <mapping-name> [--profile <profile-id>]
frpc mapping enable <mapping-name> [--profile <profile-id>]
frpc mapping disable <mapping-name> [--profile <profile-id>]
frpc mapping delete <mapping-name> [--profile <profile-id>]
frpc install [options]
frpc uninstall [--purge]
frpc upgrade [--version VERSION]
frpc status --json
```

非交互命令涉及写入或删除时：

- 默认需要 `--yes` 或明确的确认参数；
- 不允许交互等待密码时泄露密码到命令行历史；
- token 和密码通过安全输入、环境受控文件或标准输入传入；
- 返回稳定退出码。

### 16.3 JSON 输出

示例：

```json
{
  "ok": true,
  "code": 0,
  "component": "frpc",
  "service_state": "running",
  "connection_state": "connected",
  "active_profile": {
    "id": "prod-01",
    "name": "生产服务器",
    "address": "192.0.2.10",
    "port": 7000
  },
  "error": null
}
```

敏感字段永远不出现在 JSON 中。

---

## 17. 详细测试设计

### 17.1 单元测试

#### 名称校验

| 输入 | 预期 |
|---|---|
| `生产服务器` | 通过 |
| `prod-01` | 通过 |
| `prod_server_01` | 通过 |
| `生产 服务器` | 失败 |
| `prod/server` | 失败 |
| 空字符串 | 失败 |
| 重复名称 | 失败 |

#### 端口校验

| 输入 | 预期 |
|---|---|
| `1` | 通过 |
| `7000` | 通过 |
| `65535` | 通过 |
| `0` | 按字段规则处理 |
| `65536` | 失败 |
| `abc` | 失败 |
| 空字符串 | 仅管理端口使用默认 7500 |

#### profile 隔离

1. 创建 A、B 两个 profile；
2. 在 A 添加映射 A1；
3. 在 B 添加映射 B1；
4. 查看 A 只能看到 A1；
5. 查看 B 只能看到 B1；
6. 切换后生成配置只包含当前 profile 映射。

### 17.2 集成测试

- systemd 安装和服务启停；
- OpenRC 安装和服务启停；
- Windows Service 注册和删除；
- frps/frpc 实际连接；
- token 正确/错误；
- TLS 正确/错误；
- Dashboard/API 登录和查询；
- 连接 30 秒超时；
- 升级失败回滚；
- 配置写入中断恢复。

### 17.3 菜单测试

- 所有菜单数字可选；
- 输入负数、字母、空值、超大数字不会退出；
- `0` 返回正确层级；
- 每次操作后返回正确菜单；
- 删除、停止、卸载有二次确认；
- 当前服务端别名动态显示；
- 端口映射不出现在主菜单平级项；
- profile 列表通过编号选择。

### 17.4 安全测试

- token 不出现在日志；
- token 不出现在 `ps`/进程参数；
- 文件权限正确；
- 路径遍历输入失败；
- Shell 元字符不会执行；
- PowerShell 注入输入不会执行额外命令；
- 下载文件校验失败不会覆盖旧二进制；
- 未授权用户不能读取 secret 文件。

### 17.5 回归验收

每次版本升级至少验证：

1. 菜单编号和返回路径不变；
2. profile 数据可读取；
3. 映射配置不串；
4. frp 配置字段仍可生成；
5. Dashboard/API 适配能力正常；
6. Linux 和 Windows 服务可启动；
7. 原有配置不会被无提示覆盖。

---

## 18. 实现顺序与开发任务

### 阶段一：基础库和平台能力

1. 建立 Bash/PowerShell 项目结构；
2. 实现平台检测；
3. 实现路径、权限、锁和日志基础库；
4. 实现下载、校验、解压和版本信息；
5. 实现 systemd、OpenRC、Windows Service 适配器。

### 阶段二：配置和数据层

1. 固定最终 frp 版本；
2. 验证 frps/frpc TOML 字段；
3. 实现 TOML/JSON 读写；
4. 实现 profile/index/state；
5. 实现 secret 文件和权限；
6. 实现原子写入、备份和恢复。

### 阶段三：frps 管理

1. frps 安装向导；
2. frps 配置生成；
3. frps 服务生命周期；
4. frps 主菜单；
5. Dashboard/API 适配；
6. 客户端连接信息展示。

### 阶段四：frpc 和 profile

1. frpc 首次配置向导；
2. profile 新建和编号列表；
3. profile 改名、编辑、删除；
4. active profile 切换；
5. 30 秒连接检测；
6. frpc 主菜单。

### 阶段五：映射管理

1. 映射数据模型；
2. 当前 profile 映射子菜单；
3. TCP/UDP 映射；
4. HTTP/HTTPS 基础映射；
5. 映射启停和删除；
6. 配置生成和重启策略。

### 阶段六：升级和质量

1. 非交互 CLI；
2. JSON 输出；
3. 升级、回滚、卸载；
4. 跨平台集成测试；
5. 安全测试；
6. 文档和发布包。

---

## 19. 实现前必须完成的技术验证

虽然业务需求和系统结构已经明确，但以下项目必须在编码前验证：

1. 首期固定使用用户确认的 frp v0.72.0，并在编码前完成官方文档、二进制和 Dashboard/API 验证；
2. 使用 v0.72.0 验证 frps/frpc TOML 配置字段；
3. 验证 token、TLS 和可选认证机制；
4. 验证 Dashboard/API 的登录、客户端查询、代理查询和在线状态字段；
5. 验证 `frps`、`frpc` 实际命令行参数；
6. 验证 GitHub Release 资产命名和 SHA256 文件格式；
7. 验证 Bash 环境下的 TOML 生成和校验方案；
8. 验证 Windows Service 的实际启动参数和停止行为；
9. 验证 OpenRC 服务脚本在 Alpine 目标版本的行为；
10. 确定 Dashboard/API 默认监听地址和安全提示；
11. 确定 systemd/OpenRC/Windows 日志收集方式；
12. 对 IPv6、中文名称和 Windows 编码执行实际测试。

---

## 20. frp v0.72.0 技术验证记录（基于已提供文本）

本节仅记录用户提供的 frp v0.72.0 相关文本中能够直接确认的内容。未在文本中出现、无法仅凭该文本确认的项目，明确标记为“未提供/待实际验证”，不作推测。

### 20.1 配置文件格式

已确认：

- frp 自 v0.52.0 起支持 TOML、YAML、JSON；
- INI 已废弃；
- 示例主要使用 TOML；
- 新功能只会进入 TOML、YAML 或 JSON；
- 本项目管理器继续采用 TOML 生成 frps/frpc 运行配置，JSON 保存状态和索引。

### 20.2 frps TOML 字段

文本中已出现并可作为 v0.72.0 设计输入的字段：

```toml
bindPort = 7000
vhostHTTPPort = 8080
vhostHTTPSPort = 8443
tcpmuxHTTPConnectPort = 5002

webServer.addr = "127.0.0.1"
webServer.port = 7500
webServer.user = "admin"
webServer.password = "admin"
webServer.tls.certFile = "server.crt"
webServer.tls.keyFile = "server.key"

metricsServer.addr = "127.0.0.1"
metricsServer.port = 7501
metricsServer.user = "monitor"
metricsServer.password = "change-me"
metricsServer.tls.certFile = "metrics.crt"
metricsServer.tls.keyFile = "metrics.key"
metricsServer.pprofEnable = false

auth.method = "token"
auth.token = "<token>"
auth.additionalScopes = ["HeartBeats", "NewWorkConns"]

allowPorts = [
  { start = 2000, end = 3000 },
  { single = 3001 }
]
```

已确认的安全相关行为：

- `webServer.addr` 默认值为 `127.0.0.1`；需要公网访问时改为 `0.0.0.0`；
- Dashboard 用户名和密码均为可选；
- `metricsServer` 默认端口为 0，即默认关闭，并默认绑定 `127.0.0.1`；
- `/healthz` 不需要认证，只表示 HTTP 服务响应，不表示 frpc 已连接或代理就绪；
- `/metrics` 可使用独立的 Basic Auth；
- Dashboard、metricsServer 的 TLS 配置相互独立；
- frps 认证支持 token 和 OIDC；
- token 还支持文件来源 `auth.tokenSource.type = "file"`。

以下内容文本未完整提供，留空待验证：

```text
frps v0.72.0 完整配置字段清单：
frps v0.72.0 所有字段的类型、默认值和废弃字段：
frps v0.72.0 配置校验命令：
```

### 20.3 frpc TOML 字段

文本中已确认的基础连接配置：

```toml
serverAddr = "x.x.x.x"
serverPort = 7000
```

TCP 映射：

```toml
[[proxies]]
name = "ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = 6000
```

UDP 映射：

```toml
[[proxies]]
name = "dns"
type = "udp"
localIP = "8.8.8.8"
localPort = 53
remotePort = 6000
```

HTTP 映射：

```toml
[[proxies]]
name = "web"
type = "http"
localPort = 80
customDomains = ["www.example.com"]
```

HTTPS 映射：

```toml
[[proxies]]
name = "test_https2http"
type = "https"
customDomains = ["test.example.com"]
```

已确认的其他 frpc 字段和机制：

```toml
transport.tls.enable = true
transport.tls.useSystemCa = true
transport.tls.trustedCaFile = "ca.crt"
transport.tls.certFile = "certificate.crt"
transport.tls.keyFile = "certificate.key"
transport.tls.force = true
transport.useEncryption = true
transport.useCompression = true
transport.tcpMux = false
transport.protocol = "kcp"
transport.protocol = "quic"
transport.proxyURL = "http://user:pwd@192.168.1.128:8080"

includes = ["./confd/*.toml"]

[store]
path = "./db.json"
```

文本还确认支持 `tcpmux`、`stcp`、`xtcp`、插件、端口范围映射、带宽限制、连接池、负载均衡、健康检查、Host 重写、请求头、Basic Auth 等能力，但这些不进入首期基础菜单。

### 20.4 frps/frpc 命令行参数

文本中已直接确认：

```bash
./frps -c ./frps.toml
./frpc -c ./frpc.toml
frpc reload -c ./frpc.toml
frpc verify -c ./frpc.toml
frpc status -c ./frpc.toml
```

这些命令的设计用途：

- `-c`：指定配置文件；
- `reload -c`：通过客户端管理接口热加载配置；
- `verify -c`：检查配置错误；
- `status -c`：获取代理状态。

以下命令行信息文本未提供，留空待实际执行 `--help` 验证：

```text
frps --help 的完整参数：
frpc --help 的完整参数：
版本输出参数：
配置校验失败时的退出码：
reload/status API 开启失败时的退出码：
```

### 20.5 Dashboard、frpc Admin UI 和 API

已确认：

- frps Dashboard 通过 `webServer.addr` 和 `webServer.port` 开启；
- 默认 Dashboard 地址为 `127.0.0.1`；
- Dashboard 默认设计端口为 `7500`；
- `webServer.user`、`webServer.password` 可选；
- Dashboard 可显示 frp 状态和代理统计信息；
- frpc Client Admin UI 使用独立的 `webServer` 配置，示例地址 `127.0.0.1:7400`；
- frpc Admin UI 是客户端配置和代理管理入口；
- `frpc reload`、`frpc status` 需要启用客户端 `webServer` 字段；
- `store.path` 支持持久化动态代理和 visitor 配置；
- Store 配置与配置文件中的代理并存，名称冲突时 Store 条目优先；
- `metricsServer` 是独立的健康检查、Prometheus 和可选 profiling 监听器，不等同于 Dashboard/API。

文本未提供、不能直接确定的接口信息：

```text
frps Dashboard/API 的完整 HTTP 路径：
frps 在线客户端接口路径：
frps 客户端详情接口路径：
frps 代理列表接口路径：
frps 代理流量统计返回 JSON：
frpc Admin UI/API 的认证流程：
frpc reload/status 的 HTTP 请求路径：
```

因此详细实现必须先启动 v0.72.0 的真实 frps/frpc，结合官方文档、浏览器开发者工具或实际 HTTP 请求验证接口字段，再实现 `DashboardAdapter`。

### 20.6 GitHub Release 和 SHA256

用户提供的 Release 地址：

```text
https://github.com/fatedier/frp/releases/tag/v0.72.0
```

已提供并可登记的 v0.72.0 资产校验值：

| 资产 | SHA256 |
|---|---|
| `frp_0.72.0_linux_amd64.tar.gz` | `daf719b32ca2778770bb92955e231a07414d754378b9af328aa2642862479804` |
| `frp_0.72.0_linux_arm64.tar.gz` | `02180b857480302fd934e9ddbd50fe2f73229f99062f88756abc6de9df5e95cf` |
| `frp_0.72.0_windows_amd64.zip` | `5197c1b69564089cdc229fc1260a0e25db1e722b1b7db45fe1542c3c43edf9ae` |

文本中还出现 `linux_arm`、`linux_arm_hf`、`linux_loong64`、`linux_mips` 等资产，但首期不支持。

已确认 Release 页面显示 SHA256，但文本未提供以下内容：

```text
SHA256SUMS 文件的实际下载 URL：
SHA256SUMS 文件的实际格式：
windows_arm64 资产的完整 SHA256：
下载资产是否需要 GitHub API Token：
```

首期校验逻辑可使用固定版本、固定资产名和已登记 SHA256；实际发布前仍需验证 Release 页面或校验文件中的值与用户提供值一致。

### 20.7 Linux/Windows 服务注册

用户提供文本未包含 systemd、OpenRC 或 Windows Service 的注册示例，因此以下内容仍为空，不能从该文本中验证：

```text
systemd unit 的官方推荐参数：
OpenRC 服务脚本的官方推荐参数：
Windows Service 的官方注册参数：
服务进程的推荐运行账户：
服务停止时的退出行为：
```

本项目仍按已确认的封装设计实现：Linux 适配 systemd/OpenRC，Windows 适配 Windows Service；具体注册和停止行为需要在目标系统实际测试。

### 20.8 手工配置修改和自动导入

文本可以确认：

- 配置支持 `includes` 拆分到不同文件；
- frpc 支持 `store.path` 持久化动态代理和 visitor；
- `frpc reload -c` 可热加载配置；
- `frpc verify -c` 可在 reload 前检查配置错误；
- `frpc status -c` 可查询代理状态。

文本未定义本项目管理器的 profile 导入、外部文件哈希、冲突解决和反向同步规则。上述规则属于本项目自有功能，继续按详细设计第 4.6 节执行，仍需编写代码测试：

```text
外部编辑 profile.toml 后的导入字段映射：
外部编辑 mappings.toml 后的导入字段映射：
frpc.toml 反向导入到 profile 的规则：
includes/store 与 profile 隔离的冲突处理：
```

### 20.9 IPv6、中文名称和 Windows 编码

用户提供文本未给出 IPv6、中文 profile 名称或 Windows 编码处理示例，因此验证结果留空：

```text
IPv6 serverAddr 的实际配置和展示格式：
IPv6 地址与端口的方括号处理：
frp 代理 name 使用中文的兼容性：
Windows PowerShell 中文输入/输出编码：
Windows TOML 文件 UTF-8 编码要求：
```

本项目管理器仍按设计规则限制服务端名称为中文、字母、数字、`-`、`_`，不允许空格；但必须在 Windows x64 和 v0.72.0 实际环境中完成端到端测试。

---

## 21. 详细设计结论

本项目采用脚本化、模块化、平台适配的实现方式：

- `frps` 和 `frpc` 是稳定的用户入口；
- 数字菜单是默认交互方式；
- profile ID 是配置隔离主键；
- `frpc.toml` 只由当前 active profile 生成；
- 端口映射只能在当前 frps 专用子菜单中操作；
- 服务生命周期由平台适配器统一抽象；
- 配置修改采用锁、校验、备份和原子替换；
- 敏感字段单独保护并禁止进入日志；
- profile 切换自动重启并最多等待 30 秒连接结果；
- Dashboard/API 通过版本适配器实现首期正式功能；
- 防火墙只检测和提示；
- 文件日志保留 30 天且最多 500MB；
- 详细设计完成后，可以进入技术验证、原型实现和编码阶段。
