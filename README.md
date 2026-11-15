# frp-simple-bash

> 一行命令装好 frp，数字菜单管理穿透 —— 不用记配置文件路径，不用手写 TOML。

![frpc 菜单演示](docs/images/demo-frpc-menu.png)

**当前版本：v1.0.0-rc.7**

---

## 这是什么？

[frp](https://github.com/fatedier/frp) 是一个内网穿透工具，但原生配置是手写 TOML 文件，对新手不友好。

这个项目把 frp v0.72.0 包了一层**终端数字菜单**：
- 输数字回车就能完成 启动 / 停止 / 加映射 / 切服务端 / 看日志
- 状态用颜色区分（绿=正常，红=异常，黄=过渡）
- 支持保存多个服务端，随时切换
- SSH 隧道里改配置也不会断连（后台延迟重启）

---

## 一键安装

**Linux**（需要 curl、tar）：

```bash
# 服务端（跑在有公网 IP 的机器上）
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frps.sh)" && sudo bash -c "$script"

# 客户端（跑在内网机器上）
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frpc.sh)" && sudo bash -c "$script"
```

**macOS**（仅支持客户端 frpc）：

```bash
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frpc.sh)" && sudo bash -c "$script"
```

**Windows**（管理员 PowerShell，需先装 Python 3.6+）：

```powershell
irm https://download.sueiyuan.top/frp-simple-bash/install-frps.ps1 | iex   # 服务端
irm https://download.sueiyuan.top/frp-simple-bash/install-frpc.ps1 | iex   # 客户端
```

装完输入 `frps` 或 `frpc` 打开菜单（macOS 用 `sudo frpc`），输编号回车即可。

---

## 菜单里有什么？

进入 `frpc` 后，顶部直接显示当前状态（运行中/已停止、连接状态、映射数量），下方是分组菜单：

| 分组 | 功能 |
|---|---|
| 服务控制 | 启动 / 停止 / 重启 |
| 服务端管理 | 新建 / 查看 / 切换 / 修改 / 删除服务端 |
| 端口映射管理 | 添加 / 查看 / 修改 / 删除 TCP、UDP、HTTP 映射 |
| 运维 | 日志查看、连接诊断、备份恢复、开机自启动 |
| 危险操作 | 卸载、升级 |

**交互规则（全程序统一）**：
- `0` = 返回上一级（唯一的返回键）
- 数字 = 执行对应操作
- 回车 = 刷新菜单
- 主菜单连按两次 `0` 退出程序

---

## 典型使用流程

以"把内网机器的 SSH（22 端口）穿透到公网"为例：

1. **在公网服务器上**安装 frps，按向导设置业务端口（默认 7000）和 token
2. **在内网机器上**安装 frpc，新建服务端，填公网 IP、端口、token
3. 添加端口映射：本地地址 `127.0.0.1`，本地端口 `22`，远程端口 `6000`
4. 启动 frpc，看到"已连接"
5. 现在可以从外部用 `ssh -p 6000 user@公网IP` 连到内网机器

---

## 系统要求

| 项目 | Linux | macOS | Windows |
|---|---|---|---|
| 组件 | frps + frpc | 仅 frpc | frps + frpc |
| 架构 | amd64 / arm64 | amd64 / arm64 | x64 |
| 权限 | root | root | 管理员 |
| Python | 3.6+ | 3.6+（系统自带） | 3.6+ |
| 服务管理 | systemd / OpenRC | launchd | Windows Service |

---

## 常用命令

```bash
frpc status                    # 查看状态
frpc start|stop|restart        # 服务控制
frpc logs [--lines N]          # 查看日志
frpc mapping list|add|delete   # 映射管理
frpc upgrade                   # 升级管理器
sudo bash /opt/frp-simple-bash/uninstall.sh frpc --purge --yes   # 彻底卸载
```

`--json` 输出稳定 JSON，适合脚本采集。

---

## 安全说明

- 所有下载均校验 SHA256，校验失败立即停止
- token / 密码不经命令行传递，配置文件权限 0700
- 危险操作（删除、卸载）需输入 `YES` 确认
- 删除服务端前自动备份，可从「备份与恢复」还原

---

## 文档

| 文档 | 说明 |
|---|---|
| [需求文档](docs/frp二次封装需求文档.md) | 功能需求与验收标准 |
| [概要设计](docs/frp二次封装概要设计文档.md) | 架构设计与模块划分 |
| [详细设计](docs/frp二次封装详细设计文档.md) | 接口定义、错误码、状态机 |
| [项目结构与功能说明](docs/项目结构与功能说明.md) | 目录结构与文件说明 |
| [交互界面设计](docs/交互界面设计.md) | 菜单流程与按键规范 |
| [frp v0.72.0 技术验证报告](docs/frp-v0.72.0技术验证报告.md) | 上游 frp 行为验证 |
| [Debian 12 真机测试报告](docs/Debian12真机测试报告.md) | Linux 实机验证记录 |
| [macOS frpc 完整流程测试清单](docs/macOS-frpc-完整流程测试清单.md) | macOS 操作 checklist |
| [国内下载站部署与维护](docs/国内下载站部署与维护.md) | 镜像站运维说明 |
| [全量代码审查 Bug 报告](docs/全量代码审查Bug报告.md) | 代码审查记录 |

---

## 开发验证

```bash
python3 -m unittest discover -s tests -v
```

隔离目录查看菜单（不碰真实服务）：

```bash
FRP_TEST_MODE=1 FRP_CONFIG_DIR=/tmp/frp-simple-demo bash bin/frpc
```

---

## 尚待验收

- Windows 实机端到端验证
- OpenRC 及其他 Linux 发行版实机验证
- 跨版本 upgrade、长时间日志轮转

防火墙规则始终由用户管理，本项目不自动修改。
