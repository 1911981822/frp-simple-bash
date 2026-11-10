# frp-simple-bash

frp v0.72.0 的终端数字菜单管理封装。Linux 使用 Bash 安装入口，Windows 使用 PowerShell 安装入口；共享业务核心使用 Python：3.11+ 采用标准库；Linux 3.9/3.10 使用发行版 `tomli` 包，不通过 pip 修改系统 Python。

**当前是开发版。Debian 12/systemd 已完成实际安装、启停、自启动注册、profile/映射切换和卸载测试；OpenRC、其他发行版和 Windows 仍待实机验收。请先在测试环境验证。**

## 一键安装

复制对应命令到终端执行，按提示完成配置。Linux 需要 `curl` 和 `tar`；普通用户使用下面的 `sudo` 命令，已登录 root 时去掉 `sudo`。先成功下载脚本，再执行安装：下载失败立即停止，并保留终端交互输入。

### Linux 服务端 frps

```bash
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frps.sh)" && sudo bash -c "$script"
```

### Linux 客户端 frpc

```bash
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frpc.sh)" && sudo bash -c "$script"
```

### Windows 服务端 frps

以**管理员身份打开 PowerShell**，执行：

```powershell
irm https://download.sueiyuan.top/frp-simple-bash/install-frps.ps1 | iex
```

### Windows 客户端 frpc

以**管理员身份打开 PowerShell**，执行：

```powershell
irm https://download.sueiyuan.top/frp-simple-bash/install-frpc.ps1 | iex
```

Windows 需要预先安装 Python 3.11+ 并加入 PATH。以上命令会下载并执行本仓库的安装脚本；如需先查看脚本，可使用后文的下载后安装方式。安装包下载受限时，参见 Linux 安装章节的本地包入口。

安装后输入对应命令打开菜单（Windows 请打开一个新终端）：

```text
frps
frpc
```

然后输入功能编号并回车即可操作，输入 `0` 返回或退出。

## 交互方式

安装后输入 `frps` 或 `frpc`，再输入菜单数字并回车。`0` 返回。无效输入重新提示。服务端名称支持中文、字母、数字、`-`、`_`，不允许空格且必须唯一。

frpc 前 8 项：

```text
1. 查看当前状态
2. 启动 frpc
3. 停止 frpc
4. 重启 frpc
5. 新建 frps 服务端
6. 查看服务端列表
7. 切换当前 frps 服务端
8. 修改当前 frps 配置
9. 修改当前 frps（服务端别名）端口映射
```

第 9 项子菜单：

```text
1. 新增端口映射
2. 查看当前服务端映射
3. 修改端口映射
4. 启用/停用端口映射
5. 删除端口映射
0. 返回上一级
```

一个客户端同一时刻连接一个 frps，保存多个 profile。每个 profile 的映射、token 和 TLS 配置相互隔离。编号只在当前列表中有效，内部使用不可变随机 ID。修改名称不改变 ID 和映射。删除当前服务端的入口在“修改当前 frps 配置”子菜单内，同时删除其映射，不额外生成删除备份。

## Linux 安装（测试环境）

首期架构：amd64、arm64。需要 root、Python 3.9+（3.11 以下需要 tomli）、curl、tar、sha256sum、systemd 或 OpenRC。

```bash
git clone https://github.com/1911981822/frp-simple-bash.git
cd frp-simple-bash
sudo bash install-frps.sh
# 或
sudo bash install-frpc.sh
```

缺少基础依赖时支持 apt/dnf/yum/apk 安装，先显示包列表并确认；Python 3.9/3.10 自动检测发行版 TOML 包并提示安装。旧系统无法提供 Python 3.9 或 TOML 包时明确停止，安装器不会静默添加第三方软件源。没有 systemd/OpenRC 的容器不会假装完成自启动安装。

下载脚本后安装（可先查看脚本内容）：

```bash
curl -fsSL https://download.sueiyuan.top/frp-simple-bash/install-frps.sh -o install-frps.sh
sudo bash install-frps.sh
```

客户端把脚本名改为 `install-frpc.sh`。单文件入口优先下载国内站点的固定管理器版本 `v0.1.9-ux2.1`，核对源码包 SHA256 后解压；国内源下载失败时回退 GitHub。原生 frp 与 WinSW 无论从何处下载，都必须符合内置的固定 SHA256。

服务器下载受限时，可以先从官方渠道下载并上传对应的 0.72.0 安装包，再使用：

```bash
sudo FRP_ARCHIVE_PATH=/absolute/path/frp_0.72.0_linux_amd64.tar.gz bash install-frps.sh
```

该入口同样核对固定 SHA256，校验失败时不会执行二进制或修改服务。在线下载失败时也会尝试 GitHub 官方资产 API。

## Windows 安装（测试环境）

Windows x64、管理员 PowerShell、Python 3.11+。运行：

```powershell
powershell -File .\install-frps.ps1
# 或
powershell -File .\install-frpc.ps1
```

安装器使用 WinSW v2.12.0 注册 Windows Service，原生 frp.exe 不直接当成 SCM 服务程序。WinSW 优先从国内站点下载，失败后回退官方地址，使用固定 SHA256：`05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da`。该值是从官方固定 asset ID 93386820 下载登记的校验记录，不代表官方另行发布的签名。国内源成功时无需查询 GitHub metadata，不会跳过校验运行下载程序。配置 ACL 只允许 SYSTEM 和 Administrators。

在新终端输入 `frps`/`frpc`，CMD 包装器转发到 PowerShell，再调用共享核心。配置与中文输出使用 UTF-8 无 BOM。当前开发机没有 PowerShell/Windows，SCM、ACL 和终端编码尚待实机验证。

## 下载源与版本

默认国内源：`https://download.sueiyuan.top`，优先提供管理器源码、Linux/Windows frp 0.72.0 和 WinSW 2.12.0；下载失败后回退官方源。校验失败会立即停止，不会改用未经验证的包。

- `FRP_MIRROR_URL`：自定义 HTTPS 镜像根地址；显式设置为空或 `off` 禁用镜像。
- `FRP_ARCHIVE_PATH`：Linux 本地 frp 包，始终校验固定 SHA256。
- 源码发布包路径：`/frp-simple-bash/releases/v0.1.9-ux2.1/source.tar.gz`。
- 原生包路径：`/frp/v0.72.0/<官方文件名>`。
- WinSW 路径：`/winsw/v2.12.0/WinSW-x64.exe`。

完整部署、同步和 Cloudflare DNS 注意事项见[国内下载站部署与维护](<docs/国内下载站部署与维护.md>)。网站经过 Cloudflare 代理时，源站在国内不代表用户直连国内；要直连请在 Cloudflare 将下载域名设为“仅 DNS”（灰云），并确认源站 IP 和证书正常。

禁用镜像示例（已经下载本地安装脚本时）：

```bash
sudo env FRP_MIRROR_URL=off bash install-frps.sh
```

## 下载故障：证书域名不匹配

如果出现 `curl: (60) SSL: no alternative certificate subject name matches target host name`，脚本尚未下载，常见原因是 `/etc/hosts` 固定了过期/错误的 GitHub IP，或网络代理返回了不匹配的证书。请先检查：

```bash
getent hosts raw.githubusercontent.com
getent -s dns ahostsv4 raw.githubusercontent.com
sed -n '/github/p' /etc/hosts
curl -I --connect-timeout 10 --max-time 20 https://raw.githubusercontent.com
```

若 hosts 中存在确认错误的 GitHub 映射，先备份 `/etc/hosts`，只移除对应域名的错误记录，让系统重新通过 DNS 解析；不要删除其他主机条目。不要使用 `curl -k` 跳过证书校验，也不要将网上某个 GitHub IP 长期写死。

root 用户可在修复解析后执行：

```bash
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frps.sh)" && bash -c "$script"
```

客户端将 `install-frps.sh` 改为 `install-frpc.sh`。其中 `&&` 保证下载失败时不启动安装；证书错误也不能通过已经下载不到的安装脚本自修复。

若 Raw 域名仍无法访问，而官方 Codeload 可正常验证 HTTPS，可下载完整仓库后安装：

```bash
curl -fL --connect-timeout 10 --max-time 120 https://codeload.github.com/1911981822/frp-simple-bash/tar.gz/refs/heads/main -o frp-simple-bash-main.tar.gz && tar -xzf frp-simple-bash-main.tar.gz && sudo bash frp-simple-bash-main/install-frps.sh
```

下载和解压到新目录，已有同名目录时先检查内容；客户端替换末尾脚本名。此方式仍保留证书校验，后续原生 frp 安装包仍验证固定 SHA256。

## 数据与路径

| 项目 | Linux | Windows |
|---|---|---|
| 管理器 | `/opt/frp-simple-bash` | `%ProgramFiles%\frp-simple-bash` |
| 原生 frp | `/opt/frp-simple-bash/native` | 安装目录 `native` |
| 配置 | `/etc/frp` | `%ProgramData%\frp-simple-bash` |
| 用户命令 | `/usr/local/bin/frps`、`frpc` | 安装目录 `bin` 下 CMD |

原生程序与用户菜单命令必须分开，系统服务始终调用 `native/frps` 或 `native/frpc`。

```text
/etc/frp/
├── state.json
├── index.json
├── frps.toml
├── frpc.toml                  # 仅当前 profile 的生成配置
├── profiles/<ID>/
│   ├── profile.toml
│   ├── secrets.toml
│   └── mappings.toml
├── backups/
└── logs/
```

配置存储为 TOML，状态索引为 JSON。token、密码不通过命令行传递。保存使用独占锁、临时文件、原子替换及多文件失败回滚；备份保留旧模型。进程被硬杀或机器断电的跨文件恢复尚未实现日志式事务，异常锁需确认没有操作进程后手工清理。

## 映射与连接

- TCP/UDP：本地地址、本地端口、远程端口。
- HTTP/HTTPS：本地地址、端口和域名；服务端需自行启用 vhostHTTPPort/vhostHTTPSPort 并配置 DNS。
- 停用映射不会写入生成的代理列表，不用全局 `start=[]` 假装停用。
- 映射保存后提示重启；拒绝重启时显示待生效。
- 运行中切换 profile 先确认中断，最多等待 30 秒；连接失败保留新 profile。
- 客户端 Admin API 默认 `127.0.0.1:7400`，随机独立管理密码。服务端 Dashboard/API 默认管理端口 7500，凭据留空表示关闭。
- 服务端菜单使用 v0.72.0 v2 API 展示客户端/代理分页数据。API 暂不可用会提示，不能将旧缓存显示为实时在线。
- 无映射时不能凭空 API 列表判定登录成功；采用当前启动日志或可访问的服务端 API 辅助判断。

## 手工编辑配置

进入菜单时检测运行配置哈希。有效基础字段可以导入当前 profile；冲突时选择导入、保留管理器配置或查看字段。`includes`、Store、插件及尚不支持的高级字段不能自动无损导入，保留外部文件并提示。直接编辑 profile/mappings TOML 时读取会执行字段校验。

目前自动同步以交互菜单和 `import` 命令为入口；请不要在待同步的配置上直接执行其他写入命令。完整多来源配置导入是后续验收重点。

## 命令补充

```bash
frpc status --json
frpc profile list
frpc profile use <ID>
frpc mapping list
frpc import
frpc start
frpc stop
frpc restart
frps status
frps configure
frps logs
```

`status` 默认即 JSON。自动化参数目前仅覆盖已实现命令；新增/编辑仍进入安全向导。当前 `upgrade` 仅支持固定 0.72.0 的重新安装，跨版本升级需增加版本适配与校验记录后开放。

## 卸载

```bash
sudo bash /opt/frp-simple-bash/uninstall.sh frpc
```

Windows：`uninstall-frpc.ps1` 或 `uninstall-frps.ps1`。默认保留配置、日志和备份；Windows 支持显式确认的 `-Purge`。Linux 全清理尚未开放。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile lib/manager.py
```

可用隔离目录查看菜单（不调用真实服务）：

```bash
FRP_TEST_MODE=1 FRP_CONFIG_DIR=/tmp/frp-simple-demo bash bin/frpc
```

`FRP_TEST_MODE=1` 仅用于测试，会跳过原生配置校验和服务操作，**生产安装不要设置**。

## 设计依据与验证记录

- [国内下载站部署与维护](<docs/国内下载站部署与维护.md>)
- [下载镜像部署验证记录](<docs/下载镜像部署验证记录.md>)
- [需求实现覆盖审计](<docs/需求实现覆盖审计.md>)
- [终端交互界面设计](<docs/交互界面设计.md>)
- [深度功能补齐记录](<docs/深度功能补齐记录.md>)
- [项目结构与各文件功能说明](<docs/项目结构与功能说明.md>)
- [需求文档](<docs/frp二次封装需求文档.md>)
- [概要设计](<docs/frp二次封装概要设计文档.md>)
- [详细设计](<docs/frp二次封装详细设计文档.md>)
- [上游技术验证报告](<docs/frp-v0.72.0技术验证报告.md>)
- [首期实现决策、实测结果与尚待验收项](<docs/首期实现与验证说明.md>)
- [Debian 12 真机测试报告与清理证据](<docs/Debian12真机测试报告.md>)

## 首期尚待验收

- OpenRC、其他 Linux 发行版及 Windows 实机安装、启动、自启动、卸载；真实重启后的自启动。
- Windows 上固定版原生 frp 的端到端配置校验、API 请求、IPv6 连接（macOS ARM64 与 Debian 12 amd64 已完成相关实测）。
- Windows PowerShell 5.1/7 中文编码和 ACL。
- 完整故障注入、配置冲突导入、网络下载失败恢复。
- 跨版本 upgrade、Linux purge、完整脚本化编辑参数。
- 长时间日志 30 天/500MB 上限（当前按管理器启动时执行清理）。

防火墙规则始终由用户管理；本项目不自动修改系统规则。服务运行账户按需求采用 root/系统管理员，应仅部署在可信主机。
