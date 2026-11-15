# frp-simple-bash

frp v0.72.0 的终端数字菜单管理封装。Linux 用 Bash 安装入口，Windows 用 PowerShell，macOS 用 Bash（仅客户端 frpc）；共享业务核心是单文件 Python（**3.6+，零第三方依赖**，自带 TOML 解析回退）。

**当前版本：v1.0.0-rc.1（预发布候选版）。** Linux（Debian 12/systemd）已完成实机安装、启停、自启动、profile/映射切换与卸载；macOS frpc 已完成引擎与 launchd 逻辑真机验证；Windows、OpenRC 及其他发行版仍待实机验收。生产环境请先在目标平台验证后再部署。

## 一键安装

Linux（需要 `curl`、`tar`；已登录 root 时去掉 `sudo`）：

```bash
# 服务端
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frps.sh)" && sudo bash -c "$script"

# 客户端
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frpc.sh)" && sudo bash -c "$script"
```

macOS（**仅支持客户端 frpc**，不支持服务端 frps）：

```bash
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frpc.sh)" && sudo bash -c "$script"
```

Windows（**管理员 PowerShell**，需先装 Python 3.6+ 并加入 PATH）：

```powershell
irm https://download.sueiyuan.top/frp-simple-bash/install-frps.ps1 | iex   # 服务端
irm https://download.sueiyuan.top/frp-simple-bash/install-frpc.ps1 | iex   # 客户端
```

装完运行菜单：Linux 用 `frps`/`frpc`；**macOS 用 `sudo frpc`**（配置目录 `/etc/frp` 仅 root 可访问）；Windows 开新终端输 `frps`/`frpc`。输编号回车即可，`0` 返回。

> `curl` 那两个超时参数：`--connect-timeout 10` 只管连接阶段，`--max-time 60` 管整个请求。它们**只约束下载这个几 KB 的引导脚本**，后续 13.8 MB 的 frp 二进制有更宽松的内部超时。

## 交互方式

**按键语义（全程序统一，只有一套规则）：**

| 位置 | 按键 | 作用 |
|---|---|---|
| 菜单 | `0` | 返回上一级（主菜单中即为退出程序） |
| 菜单 | **空回车** | 返回上一级；主菜单中只刷新，**不会**退出 |
| 菜单 | 数字 | 执行对应操作 |
| 输入框 | `q` | 取消本次操作（回到菜单） |
| 输入框 | `b` | 上一步（向导中） |
| 输入框 | 回车 | 使用默认值 / 留空 |
| 结果页 | 回车 | 继续 |

菜单里只有两个"离开"语义：`0`（主菜单=退出程序）与空回车（主菜单=刷新）；`q` 只用于输入框取消本次操作。输入错误会重新提示，不会退出程序。

关键状态（运行中/已停止、已连接/未连接、启用/停用、成功/失败、可达/不可达、在线/离线）统一用**颜色**区分：绿色=正常，红色=异常，黄色=过渡中；否定状态（未启用/不可达/不通过）优先判定为红色，不会被误染绿。`NO_COLOR=1` 可关闭颜色。

进入 `frpc` 后先显示《当前状态》（状态、版本、配置路径、服务端、连接、映射数），再进入操作菜单：

```text
── 服务控制 ──             ── 端口映射管理 ──
1) 启动                    9) 管理（当前服务端）端口映射
2) 停止                   10) 显示所有映射（跨全部服务端）
3) 重启
                          ── 运维 ──
── 服务端管理（FRPS） ──   11) 查看日志与连接诊断
4) 新建服务端             12) 备份与恢复配置
5) 查看服务端             13) 设置/取消开机自启动
6) 切换服务端（当前 …）
7) 修改服务端（…）配置     ── 危险操作 ──
8) 删除服务端/数据        14) 卸载 frpc
                          15) 升级 frpc
```

- 第 8 项「删除服务端/数据」：先选服务端，再选「清空该服务端全部映射（保留服务端）」或「删除整个服务端（含映射）」，两者都要输入 `YES`。
- 第 9 项映射子菜单：新增 / 查看 / 修改 / 启用停用 / 删除 / 0 返回。
- `frps` 主菜单同样是「先显示《当前状态》再操作」，编号 1-10。

一个 frpc 同一时刻连一个 frps，可保存多个 profile。每个 profile 的映射、token、TLS 相互隔离；编号只在当前列表有效，内部用不可变随机 ID；改名不影响 ID 与映射。删除当前服务端入口在「修改当前 frps 配置」内，会同时删除其映射且不额外备份。

## 系统要求

| 项目 | Linux | macOS | Windows |
|---|---|---|---|
| 组件 | frps + frpc | **仅 frpc** | frps + frpc |
| 架构 | amd64 / arm64 | amd64 / arm64 | x64 |
| 权限 | root | root（`sudo frpc` 运行） | 管理员 |
| Python | 3.6+（Ubuntu 18.04 起均满足） | 3.6+（系统自带） | 3.6+ |
| 工具 | curl、tar、sha256sum | curl、tar、shasum | — |
| 服务管理 | systemd 或 OpenRC | launchd | Windows Service（WinSW 2.12.0） |

Linux 缺依赖时自动用 **apt-get / dnf / yum / apk** 安装（apt 已加 `DEBIAN_FRONTEND=noninteractive`，并会检测 dpkg 锁冲突给出提示）。macOS 缺依赖只打印 `brew install` 命令，不自动装。没有服务管理器（systemd/OpenRC/launchd）的容器会明确报错，不会假装完成自启动注册。

从源码安装（可先审阅脚本）：

```bash
git clone https://github.com/1911981822/frp-simple-bash.git && cd frp-simple-bash
sudo bash install-frps.sh     # 或 install-frpc.sh（macOS 只支持 frpc）
```

## 下载源

默认国内镜像 `https://download.sueiyuan.top`，失败回退 GitHub 官方源。**校验失败立即停止，不会改用未经验证的包。**

镜像完全不可用时，脚本原本会回退到 GitHub `main` 分支源码，但该路径**没有固定 SHA256**，因此默认拒绝执行。如确认信任当前网络，可显式授权：

```bash
sudo FRP_ALLOW_UNVERIFIED=1 bash -c "$script"
```

- `FRP_MIRROR_URL`：自定义 HTTPS 镜像；设为空或 `off` 禁用
- `FRP_ARCHIVE_PATH`：本地 frp 包路径，仍校验固定 SHA256

服务器无法在线下载时：

```bash
sudo FRP_ARCHIVE_PATH=/absolute/path/frp_0.72.0_linux_amd64.tar.gz bash install-frps.sh
```

## 数据与路径

| 项目 | Linux / macOS | Windows |
|---|---|---|
| 管理器 | `/opt/frp-simple-bash` | `%ProgramFiles%\frp-simple-bash` |
| 原生 frp | `/opt/frp-simple-bash/native` | 安装目录 `native` |
| 配置 | `/etc/frp` | `%ProgramData%\frp-simple-bash` |
| 用户命令 | `/usr/local/bin/frps`、`frpc` | 安装目录 `bin` 下 CMD |
| 服务单元 | systemd unit / OpenRC 脚本 / **launchd plist** | Windows Service |

```text
/etc/frp/
├── state.json      index.json
├── frps.toml       frpc.toml        # frpc.toml 仅当前 profile 的生成配置
├── profiles/<ID>/  profile.toml  secrets.toml  mappings.toml
├── backups/
└── logs/
```

配置为 TOML，状态索引为 JSON。token/密码不经命令行传递。写入使用独占锁 + 临时文件 + 原子替换 + 失败回滚；普通备份保留最近 10 份，升级前额外生成独立快照。

## 映射与连接

- TCP/UDP：本地地址、本地端口、远程端口
- HTTP/HTTPS：本地地址、端口、域名（服务端需自启 vhost 端口并配 DNS）
- 停用映射不会写进生成的代理列表
- 运行中切换 profile 会先确认中断，最多等 30 秒；连接失败保留新 profile
- frpc Admin API 默认 `127.0.0.1:7400`（随机密码）；frps Dashboard 默认 7500（凭据留空即关闭）
- Dashboard/API 不可用时如实提示，不把旧缓存显示为实时在线
- **通过映射端口 SSH 登录时改配置不会中断操作**：frpc 已有启用映射时，重启会先问你选择「立即重启」还是「后台延迟重启」。选后者会在 2 秒后由**脱离会话的独立进程**执行重启，即使你的 SSH 连接因此断开，重启仍会完成，重连后新映射即生效。
- **自动生成的 frps token 会明文显示一次**（首次配置向导与「修改 token」处），请立即记录；之后配置查看一律脱敏。
- **映射表带「状态」列**（在线/未就绪/停用），"启用"不再等同于"已生效"；状态需 frpc 运行且本机管理端口可达才能读取。
- **日志页支持高亮与过滤**：ERROR 红色、WARN 黄色，并有「只看错误与警告」菜单项；清理归档前会显示数量与体积并要求确认。
- **客户端信息先给概览再给明细**（在线/离线数与代理在线数），明细分页显示。
- **诊断报告每项带 ✓/✗ 符号**，可直接看出哪一项异常。
- **修改配置有未保存保护**：编辑连接配置时若未保存就返回/退出，会先确认，避免整段修改静默丢失。
- **安装向导逐字段容错**：某一项输错只重问该项，不会因此回滚整个安装。

## 命令行

```bash
frpc status [--json]        frps status [--json]
frpc start|stop|restart     frps start|stop|restart
frpc logs [--lines N] [--follow]
frpc profile list|add|use <ID或编号>|edit|rename|delete
frpc mapping list|add|edit|enable|disable|delete
frpc install|uninstall [--purge]|upgrade [<version>]
frps configure|logs|install|uninstall [--purge]|upgrade [<version>]
```

> macOS 上 `frpc` 需加 `sudo`（`sudo frpc ...`），配置目录 `/etc/frp` 仅 root 可访问。

`--json` 输出稳定信封（`ok/code/component/service_state/connection_state/active_profile/error`），适合脚本采集。写操作支持 `--yes` 跳过确认。

**升级会覆盖全部 23 个管理器文件**（`lib/`、安装/卸载脚本、`bin/` 6 个入口、`templates/` 5 个模板），但**不动** `native/` 二进制与 `/etc/frp` 配置。升级后若检测到已注册的服务单元（systemd/OpenRC/launchd）与当前模板不一致，会询问是否刷新（旧单元保留在 `backups/` 下可回滚）。也可单独执行：

```bash
frpc refresh-units [--yes]     # 只刷新服务单元，不升级脚本
```

## 卸载

```bash
sudo bash /opt/frp-simple-bash/uninstall.sh frpc                 # 保留配置
sudo bash /opt/frp-simple-bash/uninstall.sh frpc --purge --yes   # 彻底清理
```

菜单里选「卸载」也可以，会询问保留还是彻底清理（彻底清理需输入 `YES`）。卸载某组件只删该组件的专属配置，**绝不动另一组件的配置**（如卸载 frpc 不影响 frps 的 `frps.toml`）。Windows 用 `uninstall-frpc.ps1` / `uninstall-frps.ps1`，支持 `-Purge`。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
```

隔离目录查看菜单（不碰真实服务）：

```bash
FRP_TEST_MODE=1 FRP_CONFIG_DIR=/tmp/frp-simple-demo bash bin/frpc
```

`FRP_TEST_MODE=1` 仅用于测试，**生产环境不要设置**。

## 文档

- [需求文档](<docs/frp二次封装需求文档.md>) · [概要设计](<docs/frp二次封装概要设计文档.md>) · [详细设计](<docs/frp二次封装详细设计文档.md>)
- [项目结构与功能说明](<docs/项目结构与功能说明.md>) · [交互界面设计](<docs/交互界面设计.md>)
- [上游技术验证报告](<docs/frp-v0.72.0技术验证报告.md>) · [Debian 12 真机测试报告](<docs/Debian12真机测试报告.md>)
- [国内下载站部署与维护](<docs/国内下载站部署与维护.md>) · [macOS frpc 完整流程测试清单](<docs/macOS-frpc-完整流程测试清单.md>)

## 尚待验收

- OpenRC 及其他 Linux 发行版、Windows 实机的安装/启动/自启动/卸载
- Windows 端到端配置校验、API 请求、IPv6、PowerShell 5.1/7 中文编码与 ACL
- 跨版本 upgrade、长时间日志轮转（30 天 / 500MB 上限）

防火墙规则始终由用户管理，本项目不自动修改。服务运行账户按需求使用 root/管理员，仅应部署在可信主机。
