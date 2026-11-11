# macOS 真机测试报告

> 测试时间：2026-11-12
> 测试环境：macOS 27.3 (arm64, Apple Silicon)，Python 3.15.2，uid 501（admin 组）
> 测试范围：能免 sudo 验证的全部引擎逻辑 + macOS 专属代码（launchd/plist/shasum）

## 一、环境约束（重要）

本机是 macOS 真机，但 `sudo` 需要密码，且审批策略为「从不询问」，无法自动提权。
所有需要 root 的路径（`/etc/frp`、`/opt/frp-simple-bash`、`/Library/LaunchDaemons`、
`/usr/local/bin`）均被 macOS 系统权限（uid 501 + SIP）阻止，**无法在本轮自动执行完整安装**。

因此本轮采用：
1. `FRP_CONFIG_DIR` / `FRP_BINARY_DIR` 环境变量指向用户目录 → 免 sudo 跑引擎全流程
2. 真实 frp 0.72.0 darwin_arm64 二进制 → 真机 `verify -c` 校验配置渲染
3. `plutil -lint` → 真机校验 plist 语法
4. `launchctl` 子命令签名 → 真机校验命令参数

## 二、已验证通过项（真机证据）

### 1. plist 模板语法合法
```
plutil -lint templates/frps.plist  → OK
plutil -lint templates/frpc.plist  → OK
```

### 2. shasum 输出格式与 sha256sum 一致
```
shasum -a 256 输出: f2ca1bb...  /path/file   （hash + 两个空格 + 文件名）
sha256sum 输出:   f2ca1bb...  /path/file
```
`install-common.sh` 的 `awk '{print $1}'` 解析正确。

### 3. launchctl 命令参数正确
```
launchctl print system/com.apple.apsd  → 返回 "state = running"
launchctl kill 签名: launchctl kill <signal> <service-target>
launchctl enable/disable 签名: launchctl enable/disable <service-target>
```
- `service()` status 的 `'state = running' in stdout` 判断 **正确**
- `system/<label>` target 前缀 **正确**（LaunchDaemons 属 system 域）

### 4. frps 配置生成 + frps verify -c 真机通过
```
frps: the configuration file .../frps.toml syntax is ok
```

### 5. frpc profile/映射渲染 + frpc verify -c 真机通过
```
frpc: the configuration file ... syntax is ok
```

### 6. 引擎全流程（免 sudo）
- 初始化（state.json/profiles/backups/logs）✓
- 系统识别 `_service_manager()=='launchd'` ✓
- `installed_unit_path`/`template_path` 返回 .plist ✓
- 升级快照 + 恢复 ✓
- 错误码 E006 / err 拒绝未知码 ✓

## 三、发现并修复的 bug（本轮）

### BUG-M1 🔴 launchd「取消自启动」重启后失效
- **根因**：`service()` 和 `install-common.sh` 用 `launchctl load/unload` 控制自启动。
  plist 放在 `/Library/LaunchDaemons` 且 `RunAtLoad=true`，`unload` 只影响当前会话，
  **重启后 launchd 会重新加载**，导致「取消自启动」失效。
- **修复**：改用 `launchctl enable/disable system/<label>`（持久化 boot policy）。
  - `enable` → `launchctl enable system/<label>`
  - `disable`/`manual` → `launchctl disable system/<label>`
  - 卸载时先 `disable` 清 boot policy，再 `unload` 停止当前
- **证据**：`launchctl help enable/disable` 确认这是持久化自启动的正确接口。

## 四、仍需 sudo 真机验证的项（无法自动完成）

以下需要 root，本机 uid 501 无法执行，需你在终端手动跑一次：

1. **完整安装**：`sudo bash install-frps.sh` 和 `install-frpc.sh`
2. **launchd 真实服务注册**：安装后 `launchctl print system/frp-simple-bash.frps` 应显示 `state = running`
3. **真实自启动**：`sudo launchctl enable/disable` 后**重启 Mac**，验证 disable 后不再自动启动
4. **真实卸载**：`sudo bash uninstall.sh frps --purge --yes` 完整走一遍
5. **`/usr/local/bin` 命令链接**：安装后 `frps`/`frpc` 命令可用，卸载后消失

## 五、手动 sudo 测试清单

```bash
# 1. 安装 frps（会装到 /etc/frp + /opt/frp-simple-bash + /Library/LaunchDaemons）
script="$(curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 https://download.sueiyuan.top/frp-simple-bash/install-frps.sh)" && sudo bash -c "$script"

# 2. 验证服务已注册并运行
sudo launchctl print system/frp-simple-bash.frps | grep "state ="

# 3. 验证自启动是 enabled
sudo launchctl print-disabled system | grep frp-simple-bash

# 4. 取消自启动，重启后验证不再自动启动
frps   # 菜单 9「设置/取消开机自启动」→ 取消
sudo launchctl print-disabled system | grep frp-simple-bash   # 应显示 disabled

# 5. 卸载（彻底清理）
sudo bash /opt/frp-simple-bash/uninstall.sh frps --purge --yes
```

## 六、结论

macOS 专属代码（launchd + plist + shasum）**经真机校验正确**，并修复了 1 个
「自启动失效」的语义 bug。完整安装/卸载流程因 sudo 权限无法自动执行，
需按第五节清单在终端手动验证一次。核心引擎逻辑（配置/映射/渲染/升级/错误码）
已通过真机 frp 二进制 verify 验证。
