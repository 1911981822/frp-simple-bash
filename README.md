# frp-simple-bash

frp v0.72.0 的终端数字菜单管理封装。Linux 用 Bash 安装入口，Windows 用 PowerShell；共享业务核心是单文件 Python（**3.6+，零第三方依赖**，自带 TOML 解析回退）。

**当前是开发版。Debian 12/systemd 已完成实机安装、启停、自启动、profile/映射切换与卸载；OpenRC、其他发行版和 Windows 仍待实机验收，请先在测试环境验证。**

## 一键安装

Linux（需要 `curl`、`tar`；已登录 root 时去掉 `sudo`）：

```bash
# 服务端
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frps.sh)" && sudo bash -c "$script"

# 客户端
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frpc.sh)" && sudo bash -c "$script"
```

Windows（**管理员 PowerShell**，需先装 Python 3.6+ 并加入 PATH）：

```powershell
irm https://download.sueiyuan.top/frp-simple-bash/install-frps.ps1 | iex   # 服务端
irm https://download.sueiyuan.top/frp-simple-bash/install-frpc.ps1 | iex   # 客户端
```

装完输入 `frps` 或 `frpc` 打开菜单（Windows 请开新终端），输编号回车即可，`0` 返回。

> `curl` 那两个超时参数：`--connect-timeout 10` 只管连接阶段，`--max-time 60` 管整个请求。它们**只约束下载这个几 KB 的引导脚本**，后续 13.8 MB 的 frp 二进制有更宽松的内部超时。

## 交互方式

菜单数字选择，`0` 返回，`q` 取消，`b` 上一步。输入错误会重新提示，不会退出程序。

frpc 主菜单：

```text
1) 查看当前状态        8) 修改当前 frps 配置
2) 启动 frpc           9) 修改当前 frps（别名）端口映射
3) 停止 frpc          10) 查看日志与连接诊断
4) 重启 frpc          11) 备份与恢复配置
5) 新建 frps 服务端    12) 设置/取消开机自启动
6) 查看服务端列表      13) 卸载 frpc
7) 切换服务端          14) 升级 frpc
```

第 9 项映射子菜单：新增 / 查看 / 修改 / 启用停用 / 删除 / 0 返回。

一个 frpc 同一时刻连一个 frps，可保存多个 profile。每个 profile 的映射、token、TLS 相互隔离；编号只在当前列表有效，内部用不可变随机 ID；改名不影响 ID 与映射。删除当前服务端入口在「修改当前 frps 配置」内，会同时删除其映射且不额外备份。

## 系统要求

| 项目 | Linux | Windows |
|---|---|---|
| 架构 | amd64 / arm64 | x64 |
| 权限 | root | 管理员 |
| Python | 3.6+（Ubuntu 18.04 起均满足） | 3.6+ |
| 工具 | curl、tar、sha256sum | — |
| 服务管理 | systemd 或 OpenRC | Windows Service（WinSW 2.12.0） |

缺依赖时自动用 **apt-get / dnf / yum / apk** 安装（apt 已加 `DEBIAN_FRONTEND=noninteractive`，并会检测 dpkg 锁冲突给出提示）。没有 systemd/OpenRC 的容器会明确报错，不会假装完成自启动注册。

从源码安装（可先审阅脚本）：

```bash
git clone https://github.com/1911981822/frp-simple-bash.git && cd frp-simple-bash
sudo bash install-frps.sh     # 或 install-frpc.sh
```

## 下载源

默认国内镜像 `https://download.sueiyuan.top`，失败回退 GitHub 官方源。**校验失败立即停止，不会改用未经验证的包。**

- `FRP_MIRROR_URL`：自定义 HTTPS 镜像；设为空或 `off` 禁用
- `FRP_ARCHIVE_PATH`：Linux 本地 frp 包路径，仍校验固定 SHA256

服务器无法在线下载时：

```bash
sudo FRP_ARCHIVE_PATH=/absolute/path/frp_0.72.0_linux_amd64.tar.gz bash install-frps.sh
```

## 数据与路径

| 项目 | Linux | Windows |
|---|---|---|
| 管理器 | `/opt/frp-simple-bash` | `%ProgramFiles%\frp-simple-bash` |
| 原生 frp | `/opt/frp-simple-bash/native` | 安装目录 `native` |
| 配置 | `/etc/frp` | `%ProgramData%\frp-simple-bash` |
| 用户命令 | `/usr/local/bin/frps`、`frpc` | 安装目录 `bin` 下 CMD |

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

`--json` 输出稳定信封（`ok/code/component/service_state/connection_state/active_profile/error`），适合脚本采集。写操作支持 `--yes` 跳过确认。

**升级会覆盖全部 22 个管理器文件**（`lib/`、安装/卸载脚本、`bin/` 6 个入口、`templates/` 4 个模板），但**不动** `native/` 二进制与 `/etc/frp` 配置。升级后若检测到已注册的 systemd/OpenRC 单元与当前模板不一致，会询问是否刷新并 `daemon-reload`（旧单元保留在 `backups/` 下可回滚）。也可单独执行：

```bash
frpc refresh-units [--yes]     # 只刷新服务单元，不升级脚本
```

## 卸载

```bash
sudo bash /opt/frp-simple-bash/uninstall.sh frpc                 # 保留配置
sudo bash /opt/frp-simple-bash/uninstall.sh frpc --purge --yes   # 彻底清理
```

菜单里选「卸载」也可以，会询问保留还是彻底清理（彻底清理需输入 `YES`）。Windows 用 `uninstall-frpc.ps1` / `uninstall-frps.ps1`，支持 `-Purge`。

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
- [国内下载站部署与维护](<docs/国内下载站部署与维护.md>)

## 尚待验收

- OpenRC 及其他 Linux 发行版、Windows 实机的安装/启动/自启动/卸载
- Windows 端到端配置校验、API 请求、IPv6、PowerShell 5.1/7 中文编码与 ACL
- 跨版本 upgrade、长时间日志轮转（30 天 / 500MB 上限）

防火墙规则始终由用户管理，本项目不自动修改。服务运行账户按需求使用 root/管理员，仅应部署在可信主机。
