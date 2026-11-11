# macOS frpc 完整流程测试清单

> 适用范围：macOS（Darwin），仅客户端 frpc（macOS 不提供 frps）
> 前置：管理员权限（sudo）、已装 Homebrew（可选，仅用于补依赖）
> 版本：v0.1.33-frpc-only.1 及以上

## 关键路径速查

| 项目 | 路径 |
|---|---|
| 命令入口 | `/usr/local/bin/frpc` → `/opt/frp-simple-bash/bin/frpc` |
| 安装根 | `/opt/frp-simple-bash` |
| 配置目录 | `/etc/frp` |
| frp 二进制 | `/opt/frp-simple-bash/native/frpc` |
| launchd 服务 | `/Library/LaunchDaemons/frp-simple-bash.frpc.plist` |
| launchd label | `frp-simple-bash.frpc` |
| 日志 | `/etc/frp/logs/frpc.log` |

---

## 阶段 1：安装

### 1.1 一键安装（在线）
```bash
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frpc.sh)" && sudo bash -c "$script"
```

**预期结果**：
- 下载并校验 frp 0.72.0 darwin 包（SHA256 校验通过）
- 安装 native/frpc 到 `/opt/frp-simple-bash/native/`
- 建立 `/usr/local/bin/frpc` 软链接
- 生成 launchd plist 到 `/Library/LaunchDaemons/frp-simple-bash.frpc.plist`
- 首次进入配置向导（新建 frps 服务端 profile）

### 1.2 验证安装产物
```bash
# 命令可用
which frpc                          # 期望 /usr/local/bin/frpc
frpc version                        # 期望 JSON 含 manager/frp=0.72.0

# 二进制存在且可执行
/opt/frp-simple-bash/native/frpc --version   # 期望 0.72.0

# 目录结构
ls -la /etc/frp                     # state.json/profiles/backups/logs/frpc.toml
ls -la /opt/frp-simple-bash/native/ # frpc

# plist 已生成且语法合法
plutil -lint /Library/LaunchDaemons/frp-simple-bash.frpc.plist   # 期望 OK
```

### 1.3 验证 launchd 服务注册
```bash
sudo launchctl print system/frp-simple-bash.frpc | grep "state ="
# 期望：state = running（安装后已启动）

# 验证自启动是 enabled
sudo launchctl print-disabled system | grep frp-simple-bash
# 期望：frp-simple-bash.frpc 不在 disabled 列表（即 enabled）
```

---

## 阶段 2：配置（frpc 客户端）

### 2.1 首次配置向导（新建 frps 服务端）
```bash
frpc   # 进入菜单
# 菜单 5「新建 frps 服务端」，填写：
#   - 服务端名称（如「生产服务器」）
#   - 服务器地址（frps 的 IP 或域名）
#   - 业务端口（默认 7000）
#   - token（留空自动生成强 token）
#   - Dashboard 用户名（可选，留空关闭远程查询）
```

**预期结果**：
- profile 保存到 `/etc/frp/profiles/<24位hex>/profile.toml`
- secrets 保存到 `secrets.toml`（token/password）
- index.json 更新

### 2.2 新增端口映射
```bash
frpc   # 菜单 9「修改当前 frps（别名）端口映射」
# 子菜单 1「新增端口映射」，填写：
#   - 名称、类型(tcp/udp/http/https)、本地地址、本地端口、远程端口
```

**预期结果**：
- `mappings.toml` 保存
- `frpc.toml` 重新生成
- 运行中则提示重启

### 2.3 启动并验证连接
```bash
frpc   # 菜单 2「启动 frpc」
# 等待 30 秒内显示「已连接」
```

**验证日志**：
```bash
tail -20 /etc/frp/logs/frpc.log
# 期望看到 "login to server success"
```

---

## 阶段 3：服务管理（launchd）

### 3.1 启停/重启（菜单）
```bash
frpc   # 菜单 2/3/4 启动/停止/重启
```

**等价 launchctl 命令（验证用）**：
```bash
sudo launchctl kickstart system/frp-simple-bash.frpc        # 启动
sudo launchctl kill SIGTERM system/frp-simple-bash.frpc     # 停止
sudo launchctl kickstart -k system/frp-simple-bash.frpc     # 重启
```

### 3.2 自启动开关（重点：上一轮修复的语义）
```bash
frpc   # 菜单 12「设置/取消开机自启动」
```

**验证取消自启动后持久化**：
```bash
# 取消自启动后
sudo launchctl print-disabled system | grep frp-simple-bash
# 期望：frp-simple-bash.frpc 出现在 disabled 列表

# 重启 Mac 后验证（关键！）
sudo reboot
# 重启后：
sudo launchctl print system/frp-simple-bash.frpc
# 期望：报 "Could not find service"（未自动启动）
```

**验证重新启用自启动**：
```bash
frpc   # 菜单 12 → 启用
sudo launchctl print-disabled system | grep frp-simple-bash
# 期望：frp-simple-bash.frpc 不在 disabled 列表
```

---

## 阶段 4：升级

```bash
frpc   # 菜单 14「升级 frpc」
# 或
frpc upgrade [--yes]
```

**预期结果**：
- 自动检查镜像站最新版本
- 覆盖全部管理器脚本（lib/、bin/、templates/、安装卸载脚本）
- 不动 native/frpc 二进制、不动 /etc/frp 配置
- 升级前生成快照，可回滚

---

## 阶段 5：卸载

### 5.1 保留配置卸载
```bash
sudo bash /opt/frp-simple-bash/uninstall.sh frpc
# 或菜单 13「卸载 frpc」→ 1「保留配置和日志」
```

**预期结果**：
- 停止并注销 launchd 服务
- 删除 `/usr/local/bin/frpc` 软链接、`native/frpc` 二进制
- **保留** `/etc/frp` 全部配置

### 5.2 彻底清理卸载
```bash
sudo bash /opt/frp-simple-bash/uninstall.sh frpc --purge --yes
# 或菜单 13「卸载 frpc」→ 2「彻底清理」→ 输入 YES
```

**预期结果**：
- 删除 frpc 专属配置：`/etc/frp/frpc.toml`、`/etc/frp/profiles/`、`/etc/frp/index.json`
- state.json 里清掉 frpc 相关键（保留 frps 键，若 Linux 上装了 frps）
- 若 Linux 的 frps 不存在（macOS 上必然如此），删除整个 `/etc/frp` 和 `/opt/frp-simple-bash`

### 5.3 卸载后验证
```bash
which frpc                          # 期望：无输出（已删除）
ls /Library/LaunchDaemons/ | grep frp-simple-bash   # 期望：无输出
ls /etc/frp 2>&1                    # 彻底清理后：No such file or directory
```

---

## 阶段 6：异常场景

### 6.1 macOS 上尝试装 frps（应被拒绝）
```bash
script="$(curl -fsSL https://download.sueiyuan.top/frp-simple-bash/install-frps.sh)" && sudo bash -c "$script"
# 期望：报错「macOS 仅支持客户端 frpc，不提供 frps 服务端安装」

frps
# 期望：报 [E001] macOS 仅支持客户端 frpc
```

### 6.2 依赖缺失（Homebrew 提示）
```bash
# 模拟：移除 curl 后安装
# 期望：打印 brew install curl 命令，不自动安装
```

### 6.3 端口冲突检测
```bash
frpc   # 菜单 9 → 新增映射，填一个已被占用的本地端口
# 期望：提示端口被占用（若 ss 可用）
```

---

## 执行结果记录表

| 阶段 | 命令 | 预期 | 实际 | 通过? |
|---|---|---|---|---|
| 1.1 安装 | install-frpc.sh | 成功 + 配置向导 | | |
| 1.3 服务注册 | launchctl print | state = running | | |
| 2.3 连接 | tail frpc.log | login success | | |
| 3.2 取消自启 | print-disabled | disabled 列表 | | |
| 3.2 重启验证 | reboot 后 print | not found | | |
| 4 升级 | frpc upgrade | 到最新版本 | | |
| 5.2 彻底清理 | --purge --yes | 全删 | | |
| 6.1 拒绝 frps | install-frps.sh | 报错 | | |

> 将实际结果填到表格，若有失败项，把完整报错发回以便修复。
