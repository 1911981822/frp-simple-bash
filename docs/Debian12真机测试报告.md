# Debian 12 真机测试报告

- 日期：2026-11-09
- 主机：用户授权的 SSH 主机 `134.175.183.101`
- 系统：Debian GNU/Linux 12，x86_64，Linux 6.1.0-54-amd64
- Python：3.11.2
- 服务管理器：systemd
- frp：官方 v0.72.0 Linux amd64 发布包
- 结论：**修复启动频率限制后，本次 Linux 真机测试全部通过；测试资源已清理。**

## 1. 测试边界

测试前确认以下路径、命令和服务均不存在：

- `/opt/frp-simple-bash`
- `/etc/frp`
- `/usr/local/bin/frps`、`/usr/local/bin/frpc`
- frps/frpc systemd 服务文件与开机启动链接

测试使用动态空闲端口，控制连接、Dashboard、客户端 Admin API 和代理监听均限制在回环地址。没有开放公网监听、修改防火墙或重启系统。

主机原有 nginx、Java 及其他业务未纳入测试操作。测试前系统已经处于 degraded 状态，唯一失败单元是 `spring_fabric-server-mc.service`；该状态在测试后保持不变。

## 2. 安装包和网络

使用官方 Linux amd64 包，SHA256 为：

```text
daf719b32ca2778770bb92955e231a07414d754378b9af328aa2642862479804
```

服务器直接下载 GitHub Release 超时，服务器访问官方资产 API 返回 HTTP 404。因此通过开发机从官方资产 API 下载完整安装包，验证 SHA256 后通过 SSH 上传，服务器再次验证同一 SHA256。

实际安装通过 `FRP_ARCHIVE_PATH` 使用已上传安装包。**这次验证覆盖了本地已校验安装包的完整安装流程，没有把在线下载安装链路标记为通过。**

安装器现新增：

- 本地安装包入口，仍执行固定 SHA256 校验；
- 在线直接下载失败后，尝试 GitHub 官方资产 API；
- 清晰的 SHA256 校验失败提示。

防篡改测试：故意传入损坏安装包，安装器退出码为 `1`，未执行下载的程序，未创建安装目录或修改服务。

证据：[安装包防篡改结果](<debian12-integrity-test.json>)。

## 3. Linux 原生与行为测试

命令：

```bash
FRP_NATIVE_DIR=/root/frp-simple-bash-test-20261109/frp_0.72.0_linux_amd64 \
  python3 -m unittest discover \
  -s /root/frp-simple-bash-test-20261109/source/tests -v
```

结果：**22 项测试通过，0 项跳过，执行时间 5.345 秒。**

范围包括：

- 中文服务端名称、唯一性、非法字符和路径校验；
- token 特殊字符 TOML 往返和文件权限；
- profile 映射隔离、停用不生成、删除、端口冲突拒绝；
- 配置校验失败及后置写入故障时回滚；
- 外部基础配置导入和不支持字段拒绝；
- 取消编辑、无效编号重试、30 秒超时逻辑；
- 官方 frps/frpc verify 与真实 token/TLS 登录；
- TCP/UDP/HTTP/HTTPS 四种代理注册；
- TCP 隧道数据回声往返；
- Dashboard v2 API、客户端 Admin API、HTTP Basic Auth；
- IPv6 `::1` 客户端登录。

证据：[完整原生测试输出](<debian12-native-tests.txt>)。

## 4. 系统级测试

使用[显式授权的真机测试脚本](<../tests/real_host_validation.py>)，检查真实安装路径和 systemd，未 mock 服务管理动作。

| 阶段 | 最终结果 |
|---|---|
| 安装前路径和服务检查、测试资源归属标记 | 通过 |
| 准备回环监听配置和随机独立认证信息 | 通过 |
| 执行 frps 安装脚本，服务 active 且 enabled | 通过 |
| 创建独立 profile A，生成客户端配置 | 通过 |
| 执行 frpc 安装脚本，服务 active 且 enabled | 通过 |
| Dashboard/Admin API 与 TCP 数据转发 | 通过 |
| systemd stop/start/restart、enable/disable | 通过 |
| 独立回环 frps B 与 profile B | 通过 |
| 数字编号选择、运行中/停止状态切换、映射隔离 | 通过 |
| 映射停用/启用、保存后重启生效 | 通过 |
| 手工配置导入、删除 profile 不新增备份、已安装命令菜单 | 通过 |
| 卸载服务和命令，同时保留配置内容 | 通过 |

最终成功执行 ID：`20261109-161041-1e0b40f2`。

证据：[成功执行原始报告](<debian12-verified-run.json>)。

### 自启动验证说明

实际验证了服务安装到 systemd、`enabled` 状态，以及 enable/disable 的切换。为保护正在运行的业务，**没有重启操作系统**，因此没有宣称通过重启后的启动验证。

## 5. 发现并修复的问题

第一次执行中，安装、API、systemd 基础操作和 profile 切换通过，但连续重启 frpc 时触发了 systemd 默认的启动次数限制：

```text
start of the service was attempted too often
```

这会使用户在菜单里连续修改配置或切换服务端后，无法再次启动 frpc。

修复：

- frps/frpc 服务模板增加 `StartLimitIntervalSec=0`；
- 保留 `Restart=on-failure` 和 `RestartSec=5`，异常退出仍间隔重试；
- 卸载时只对对应 frp 服务执行 `reset-failed`，清理自身失败状态；
- 重新从零安装并重跑完整系统级流程，12 个阶段全部通过。

相关文件：

- [frpc systemd 模板](<../templates/frpc.service>)
- [frps systemd 模板](<../templates/frps.service>)
- [Linux 卸载脚本](<../uninstall.sh>)

失败记录也已保留，便于追溯：[首次运行原始报告](<debian12-first-run.json>)。

## 6. 清理与业务核对

系统级测试结束后验证：

- 测试 frps/frpc 服务、开机启动链接、命令入口均已移除；
- `/opt/frp-simple-bash`、`/etc/frp` 已删除（均为此次首次创建并带归属标记的测试路径）；
- 先验证卸载默认保留配置，再由测试脚本清理自身配置，未混淆这两种行为；
- 独立 frps B 和本地回声后端已停止；
- 运行中的原服务集合与测试前一致；
- 原 nginx、Java 主进程仍存活；
- 原有失败单元不变，未残留 frps/frpc 失败单元。

证据：[清理及业务核对结果](<debian12-cleanup-audit.json>)。

日志和报告下载完成后，核对测试目录的绝对路径、原始上传包指纹和目录内容，再移除了远程测试暂存目录。服务器没有保留本次 frp 测试安装。

## 7. 当前仍未验证的范围

- Alpine/OpenRC、其他 Linux 发行版；
- Windows SCM、WinSW、ACL、PowerShell 编码；
- 真实系统重启后的自启动；
- 公网访问、跨机器 NAT 和云安全组；
- 在线下载在此服务器上的完整成功链路；
- UDP/HTTP/HTTPS 完整业务数据转发（本次四种代理注册通过，数据往返重点验证 TCP）；
- 升级回滚、长期运行、日志 30 天/500MB 边界与断电恢复。

本报告不等同于所有平台、所有需求已经全部验收。当前代码尚未推送 GitHub。
