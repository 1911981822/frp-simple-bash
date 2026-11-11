# frp-simple-bash 代码审查 Bug 清单

> 审查对象：`v0.1.18-conn.1`（git HEAD `e0e446d`）
> 审查日期：2026-11-11
> 审查方式：只读静态审查（资深 QA 子 agent 全模块通读 + 主 agent 并行独立审查交叉验证）
> **状态：仅记录，未修复任何问题，未改动任何业务代码**

---

## 一、审查摘要

### 审查范围
| 模块 | 文件 | 深度 |
|---|---|---|
| 核心引擎 | `lib/manager.py`（1834 行） | 全文通读 + 只读片段实证 |
| Linux 安装/卸载 | `install-common.sh`、`install-frpc.sh`、`install-frps.sh`、`uninstall.sh`、`lib/download.sh` | 全文 |
| 服务模板 | `templates/*.service`、`templates/*.openrc` | 全文 |
| 命令入口 | `bin/frpc*`、`bin/frps*`（`.ps1`/`.cmd`/无后缀） | 全文 |
| Windows | `install-windows.ps1`、`install-frpc.ps1`、`install-frps.ps1`、`uninstall-*.ps1` | 全文 |
| 构建发布 | `tools/build_release.py` | 全文 |
| 测试 | `tests/*.py`（60 用例） | 全量运行（3.15，OK / skipped=3） |

### 验证手段
- `python3 -m unittest discover -s tests` → 60 通过
- `ast.parse(src, feature_version=(3,6))` → 语法层 3.6 兼容（**运行时 API 问题另计**）
- 强制 `_toml_lib = None` 模拟 Python 3.6 回退路径
- `PureWindowsPath` 验证路径前缀比较
- `toml()` / `_mini_toml()` round-trip 实证
- 用仓库自带 `.test-native/frp_0.72.0_darwin_arm64/frpc verify -c` 实机校验 `render()` 产物 → **通过**
- `git log -S` 定位回归提交

### 总体判断
功能面完整，安全设计（0600、SHA256、路径校验、脱敏意图、HTTPS 强制）考虑周到；但存在 **2 个致命缺陷**：
1. 菜单所有服务控制操作**实际从不执行**（`service_feedback` 回归）；
2. 声明支持的 **Python 3.6** 路径上 `_mini_toml` 不解析点号键 → 管理凭据明文泄露、frps 配置管理错乱、可能生成 frp 拒绝的 TOML。

另有 Python 3.7+ 运行时 API 混入、Windows 分支两处恒失败、升级后命令失权限、`--no-start` 完全无效等明确功能错。事务/回滚在异常路径不完整，`pending` 与实际运行态不一致。**测试全部跑在 3.11+ 且直接 `import tomllib`，3.6 唯一执行路径零覆盖，是上述问题集体漏检的根因。**

### 统计（去重合并后）
| 级别 | 数量 | 说明 |
|---|---|---|
| P0 | 2 | 服务不可用 / 凭据泄露 |
| P1 | 10 | 明显功能错误 |
| P2 | 13 | 边界、兼容、健壮性、平台差异 |
| P3 | 5 | 轻微 / 维护 |
| **合计** | **30** | 来源标注：`[QA]` 子 agent、`[主]` 主 agent 独立、`[双]` 两者独立发现 |

---

## 二、P0（致命）

### BUG-001 [P0] `service_feedback()` 从不执行实际服务操作，主菜单「启动/停止/重启」全部失效 `[QA]`
- **位置**：`lib/manager.py:1490-1498`（定义）；调用点 `:1529`、`:1552`（主菜单）、`:1151`（`configure_server`）、`:1215`（`save_frps_config`）
- **类型**：逻辑错误 / 回归
- **复现**：`frpc` → 选 2「启动 frpc」→ 服务状态不变，随后 `wait_connection()` 报 30 秒超时；`frps` 菜单「停止 frps」服务照常运行。用 spy 验证：调用期间 `service()` 只收到 `('frpc','status')`。
- **根因**：函数体只有 `state_value = service(role, 'status').strip().lower()` 与文案打印，**没有任何 `service(role, action)`**。`git log -S` 确认提交 `3728cb5 fix: report menu operation results and complete lifecycle commands` 把 `service(role, {2:'start',3:'stop',4:'restart'}[n])` 替换成了 `service_feedback(role, action)`，新函数只做状态读取。`configure_server()` / `save_frps_config()` 的「立即重启 frps 生效」同样只报告状态。
- **影响**：**服务不可用**。用户无法通过菜单启停/重启任何组件；配置改完后「立即重启生效」是假的，新配置永远不生效；`frpc` 启动路径还叠加 30 秒连接检测超时。最坏情况用户以为已重启、实际跑着旧配置排查数小时。
- **建议方向**：`service_feedback()` 内先 `service(role, action)` 再取状态反馈；或恢复调用点直调 `service()` 并保留独立反馈函数。补一条断言 `service(role, action)` 被调用的测试。

### BUG-002 [P0] Python 3.6 下 `_mini_toml` 不解析点号键 → 凭据明文泄露 + 功能全面错乱 + 生成非法 TOML `[双]`
- **位置**：`lib/manager.py:106-153`（`_mini_toml`）、`:155-160`（`_load_toml`/`read_toml`）；受影响 `:317-338`（`render`）、`:1133-1156`（`configure_server`）、`:1236-1237` / `:1799-1800`（脱敏）、`:1185-1201`（`validate_frps_config`）、`:1292-1298`（`server_clients`）、`:1352-1392`（`import_external`）、`:874-896`（diagnostics）。版本门槛：`install-common.sh:76-86`、`lib/manager.py:2`
- **类型**：兼容性 / 安全 / 逻辑（同一根因多种后果）
- **复现**：强制 `m._toml_lib = None`，对 `configure_server()` 生成的 frps.toml 执行 `read_frps_config()` → 返回 `{'auth.method':'token','auth.token':'SUPERSECRET','webServer.password':'DASHSECRET',...}`，`config show` 的 `json.dumps` 中 **token 与 Dashboard 密码明文出现**（脱敏只处理 `data['auth']['token']` / `data['webServer']['password']`）。主 agent 独立实证：`validate_frps_config()` 在 3.11 解析结果下抛「启用 Dashboard 时必须同时设置用户名和密码」，在 `_mini_toml` 结果下 **OK（校验被绕过）**。
- **根因**：`_mini_toml:152` 把 `auth.token`、`webServer.password`、`transport.tls.enable`、`log.to` 当作**字面量扁平 key**（`target()[key.strip()] = scalar(value)`），而 TOML 语义是嵌套表。项目声明「兼容 3.6、零第三方依赖」→ 3.6 上必然走此回退。后果链：
  1. **凭据泄露**：脱敏按嵌套键打码，扁平键完全绕过；
  2. **功能错乱**：`data.get('webServer', {})` / `data.get('auth', {})` 恒空 → `server_clients()` 抛「Dashboard/API 未启用」、diagnostics 不检测 Dashboard、状态栏不显示 Dashboard、`frps_config_menu` 看不到/改不了 Dashboard、`validate_frps_config` 的 token 长度检查被跳过、`import_external` 的 `canonical` 全空导致字段校验失效；
  3. **生成非法 TOML**：3.6 下 `frps_config_menu` n=3 `data.setdefault('auth', {})` 或 n=4 `data['webServer'] = {...}` 后，`toml(data)` 会同时输出扁平 `auth.method = ...` 与 `[auth]` 表 → 实测 `tomllib.loads()` 报 `Cannot declare ('auth',) twice`；frp 严格解析同样拒绝。
  4. 附注：`toml()` 输出点号键本身**是合法 TOML**，frp 0.72 `verify` 实测接受；问题纯粹在**读回路径**。
- **影响**：在声明支持的最低 Python 版本上（用户真机 `jiangshan` 正是 3.6），**管理凭据泄露**、frps 配置管理与诊断大面积失效、配置编辑可能写出 frp 无法启动的文件。这是「零依赖 + 3.6 兼容」承诺的根本性破口。
- **建议方向**：`_mini_toml` 实现点号键解析（按 `.` 分段建嵌套 dict，注意与 `[[...]]` 数组表交互）；或统一改为生成显式 `[table]` 段。补强制 `_toml_lib=None` 的测试覆盖 frps.toml 与 `render()` 读回；脱敏改为按字段名后缀（`*.token`/`*.password`）匹配，不依赖嵌套结构。

---

## 三、P1（明显功能错误）

### BUG-003 [P1] `main_menu()` 异常处理不设置 `data`，随后 `status_bar(role, data)` 崩溃并退出整个菜单 `[QA]`
- **位置**：`lib/manager.py:1503-1508`（except 只赋 `alias`）、`:1515`（`status_bar(role, data)`）
- **类型**：错误处理 / 逻辑
- **复现**：写坏 `frpc.toml` 或删掉 active profile 目录使 `status(role)` 抛异常，然后打开菜单。
- **根因**：`data` 是局部变量，首次迭代绑定失败 → `status_bar(role, data)` 触发 `UnboundLocalError`（`NameError` 子类，已离开 try 块）。若循环中曾成功过一次，则显示**陈旧状态**。
- **影响**：与 except 文案「服务管理仍可使用」相反，**配置损坏时菜单完全打不开**，用户失去唯一的图形化修复入口（备份恢复、日志诊断都在菜单里）。
- **建议方向**：except 分支设置最小骨架 `data = {'component': role, 'service_state': 'unknown', 'version': VERSION, 'config_path': ...}`。

### BUG-004 [P1] `install()` 传 `--version v0.72.0`，被安装器严格校验 100% 拒绝 `[QA]`
- **位置**：`lib/manager.py:1660-1664`、对照 `install-common.sh:37`
- **类型**：逻辑 / 平台（仅 Linux 分支）
- **复现**：`frpc install` → 实跑 `bash install-common.sh frps --version v0.72.0 --non-interactive` 输出「版本 v0.72.0 尚未完成配置与 SHA256 验证，拒绝安装/升级」，退出码 1。
- **根因**：1663 行 `'--version', 'v' + VERSION`（`VERSION='0.72.0'` → `v0.72.0`），而 `install-common.sh:37` 是 `[[ "$version" == '0.72.0' ]]` 严格相等。Windows 分支 `'-Version', 'v0.72.0'` 与 `ValidateSet('v0.72.0')` 匹配，只有 Linux 错。
- **影响**：`install` 命令永远失败，无法从管理器命令入口安装/重装组件（菜单里没有 install 项）。
- **建议方向**：Linux 分支传 `VERSION`（不带 `v`），或让安装器同时接受 `v0.72.0` 并归一。

### BUG-005 [P1] `install-common.sh` 的 `--no-start` 定义后从未使用，服务总是被启动 `[QA]`
- **位置**：`install-common.sh:7`（`start=1`）、`:30`（`--no-start) start=0`）、`:159`（无条件 `systemctl start` / `rc-service start`）
- **类型**：逻辑
- **复现**：`bash install-frps.sh --no-start` → 服务仍被启动。grep 全文确认 `$start` **零引用**（121 行的 `systemctl start` 是命令名不是变量）。
- **根因**：159 行只受配置文件是否存在及 `verify` 结果控制；`autostart` 在 148/151 被正确使用，`start` 被遗忘。
- **影响**：明确承诺的「注册服务但不启动」失效，破坏预配置/灰度/维护窗口场景，并可能在配置未就绪时带病启动。
- **建议方向**：159 行加 `((start == 1)) &&` 守卫；补断言该 flag 生效的测试。

### BUG-006 [P1] `upgrade()` 与 `restore_backup()` 的路径越界检查在 Windows 上恒为假 `[QA]`
- **位置**：`lib/manager.py:1756`（upgrade）、`:833`（restore_backup）
- **类型**：平台 / 逻辑
- **复现**：Windows 上 `frpc upgrade` 或 `frpc restore <backup>`。`PureWindowsPath` 实测：`str(root)+'/'` = `'C:\\...\\frp-simple-bash/'`，`str(dest)` = `'C:\\...\\lib\\manager.py'` → `startswith` **False**。
- **根因**：两处用 `str(X.resolve()) + '/'` 拼前缀（硬编码 POSIX `/`），Windows 上 `Path` 输出 `\`。于是第一个文件就抛「发布包路径越界」/「目标路径非法」。注意 `restore_backup:820` 与 `list_backups:805` 用 `Path` 对象比较是**正确**的。
- **影响**：Windows 上管理器升级与备份恢复**完全不可用**，报错信息误导。
- **建议方向**：改用 `os.path.commonpath` 或纯 `Path` 比较（`dest.resolve().parent == ROOT.resolve()`），避免字符串拼接分隔符。

### BUG-007 [P1] `upgrade()` 把 `bin/*` 与 `templates/*` 一律 chmod 0644，升级后命令失去可执行位 `[QA]`
- **位置**：`lib/manager.py:1763`、`:1750-1753`；对照 `install-common.sh:133`（正确 `install -m 755`）、`tools/build_release.py:51`（dist 中 `bin/frpc` 为 0755）
- **类型**：逻辑 / 平台
- **复现**：Linux 上 `frpc upgrade` 成功后执行 `frpc status` → `/usr/local/bin/frpc` 报 `Permission denied`。
- **根因**：`os.chmod(dest, 0o755 if rel.endswith('.sh') or rel.endswith('.ps1') else 0o644)`。`bin/frpc`、`bin/frps` 是**无后缀 bash 入口**，`bin/*.cmd` 也无 `.sh`/`.ps1` 后缀，全部降为 0644；`templates/*.openrc` 安装时 0755，升级后变 0644。
- **影响**：升级成功后**主命令入口立即失效**（Linux），需手工 `chmod +x`。
- **建议方向**：从 tar member 的 `m.mode` 恢复权限，或对 `bin/` 显式 0755；升级后校验关键文件可执行位。

### BUG-008 [P1] `ask()` 无 `default` 时「留空」不可用，且 `q` 返回的 `None` 被写入配置导致 `toml()` 崩溃 `[QA]`
- **位置**：`lib/manager.py:591-621`（尤其 617-620）、`:599`（hint 无条件写「留空请直接回车」）；受影响 `:1090`（新 token 留空清除）、`:1096`（Dashboard 用户名留空关闭）、`:1135/:1138/:1146`（configure_server）、`:1247/:1252/:1258`、`:1264/:1270`（vhost 端口留空禁用）；`lib/manager.py:58-63`（`scalar()` 不接受 `None`）
- **类型**：边界 / 逻辑
- **复现**：菜单「修改 token」→ 直接回车想清除 → 实测打印「此项不能为空，请重新输入。」并循环；只能输 `q`（返回 `None`）→ `s['token'] = None` → 下次 `toml(s)` 抛 `ValueError: 不支持的配置字段类型`（`toml({'k':None})` 实测抛）。
- **根因**：617 行只在 `default is not None` 时放行空输入；大量调用点在**提示语里承诺可留空**却没传 `default`（或传了非空字符串导致回车＝保留旧值，与「留空禁用」相反）。`q` 的 `None` 被赋值进 `s`/`data` 后 `toml()` 无 `None` 分支。
- **影响**：token 无法清除、Dashboard 无法关闭、vhost 端口无法禁用、无 token 认证无法配置——一批写进帮助文案的功能实际不可用；取消操作还会引发未捕获 `ValueError`（或在 `transaction()` 内回滚但用户只看到莫名错误）。
- **建议方向**：`ask()` 增加 `allow_empty=True` 让「留空」返回 `''`；所有「留空」调用点显式传 `default=''` 并归一化 `q` 的 `None`；把 599 行 hint 改为按 `allow_empty` 条件输出。

### BUG-009 [P1] `edit_profile()` 按 `q` 取消落入 `else` 保存分支，反而写盘并二次弹确认 `[QA]`
- **位置**：`lib/manager.py:1081`（`if n == 0: return`）、`:1118-1131`（else 保存分支）、`:663-678`（`choose()` 在 `q`/EOF 返回 `None`）
- **类型**：逻辑
- **复现**：进入「修改当前 frps 配置」→ 直接按 `q`。实测 `choose()` 返回 `None`；`None == 0` 为 False，也不匹配任何 `elif` → 落入 `else`。
- **根因**：`choose()` 有两种退出返回值（`0` 返回 / `None` 取消），`edit_profile` 只处理 `0`。else 会执行 `validate_profile`、备份、`atomic(profile.toml)`/`atomic(secrets.toml)`、`apply(pid)`（重写 `frpc.toml`）、`rebuild_index()`，并在 1130 行再问一次「立即重启生效？」。
- **影响**：用户取消编辑却触发**完整写事务**（把内存中可能已改未提交的值落盘）、重写运行配置、重建索引；`frpc.toml` 无条件重生成可能覆盖外部手工修改。`tests/test_manager.py:277` 只测 `'0'`，**`q` 路径假绿**。
- **建议方向**：`if n is None or n == 0: return`；补一条 `q` 取消的磁盘不变断言。

### BUG-010 [P1] `frps_config_menu` 编辑 Dashboard 时整体覆盖 `webServer`，静默丢弃手工配置的 TLS/高级字段 `[QA]`
- **位置**：`lib/manager.py:1260`（`data['webServer'] = {'addr','port','user','password'}`）、`:1254`（`pop('webServer')`）、`:958`（`api_scheme()` 依赖 `web.get('tls',{}).get('certFile')`）、`:1298`（`server_clients` 同）
- **类型**：逻辑 / 数据处理
- **复现**：`frps.toml` 手工写入 `webServer.tls.certFile`/`keyFile`（frp 0.72 支持，且 `api_scheme()` 明确按此判断 HTTPS）→ 菜单「修改 Dashboard/API」只改端口 → 保存后 TLS 子表消失，Dashboard 退回 HTTP。
- **根因**：1260 行用全新字面量 dict **整体替换** `data['webServer']`，而非 `setdefault` 后逐字段更新；`tls`、`pprof` 等其它键全丢。另注：管理器自己从不生成 `webServer.tls`，故 `api_scheme()` 恒为 `http`，与 `profile_wizard` 的 `dashboard_scheme` 默认 `https`（739 行）不匹配。
- **影响**：手工启用 Dashboard HTTPS 后一次普通编辑就静默降级为明文 HTTP，管理凭据暴露；`server_clients()`/`diagnostics()` 随后用错 scheme 访问而失败。
- **建议方向**：`web = data.setdefault('webServer', {})` 后只更新 4 个字段，保留未知子表；关闭 Dashboard 时明确保留 `tls` 或明确提示将丢弃。

### BUG-011 [P1] `connection()` 日志优先会掩盖 `proxy-error`（上一轮修复引入）`[主]`
- **位置**：`lib/manager.py:420-452`（尤其 424-426）、`:388-418`
- **类型**：逻辑错误 / 回归
- **复现**：frpc 已登录（日志有 `login to server success`），但某映射启动失败（admin `/api/status` 返回 `status:"error"`）。
- **根因**：424-426 行 `logged` 判定 `connected` 后**立即 return**，永远不检查 admin API 的代理状态。主 agent 实证：日志 login success + `{'tcp':[{'name':'ssh','status':'error'}]}` → 返回 `connected`（期望 `proxy-error`）。
- **影响**：映射实际不可用（端口不通），但状态栏/诊断/`wait_connection` 都显示「已连接」→ 故障被掩盖。这是上一轮为修「0 映射误判」把日志提到最高优先级的副作用。
- **建议方向**：日志只判定**登录态**；有映射且存在非 `online/running` 时应返回 `proxy-error`。

### BUG-012 [P1] `install-common.sh` 显式 `exit` 不触发 ERR trap，失败时服务被停且不回滚 `[主]`
- **位置**：`install-common.sh:113-126`（`rollback_install` + `trap ... ERR`）、`:127-131`（停服务/换二进制）、`:141`、`:154`、`:104`
- **类型**：错误处理 / 回滚完整性
- **复现**：重装时 `/usr/local/bin/frps` 被占用且非本项目链接 → 第 141 行 `exit 1`；或 `--non-interactive` 且配置不存在 → 第 154 行 `exit 2`。
- **根因**：`trap rollback_install ERR` 只捕获**命令失败**，Bash 中显式 `exit N` 不触发 ERR trap。此时 127 行已 `systemctl stop`、130-131 行已替换旧二进制，但 `rollback_install` 不执行 → 旧二进制不恢复、`was_running=1` 的服务保持 stopped。
- **影响**：一次失败的重装让线上 frp 服务**停在停止状态**，且提示信息（"安装未替换该命令"）与实际（二进制已换、服务已停）不符；最坏是业务中断需手工恢复。
- **建议方向**：改用 `trap ... EXIT` + 成功末尾 `trap - EXIT`；或所有显式 `exit` 前统一调用回滚；把 `/usr/local/bin` 冲突检查前置到停服务之前。

---

## 四、P2（边界 / 兼容 / 健壮性 / 平台差异）

### BUG-013 [P2] `uninstall.sh --purge` 漏检 OpenRC 服务文件，边界情况下误删共享目录 `[双]`
- **位置**：`uninstall.sh:39-42`，对照 `:27-30`（OpenRC 用 `/etc/init.d/$role`）
- **类型**：平台 / 错误处理
- **复现**：OpenRC 系统上另一组件仅残留 `/etc/init.d/$other`（native 与链接已移除、systemd unit 本就不存在）时执行 `uninstall.sh frps --purge --yes`。
- **根因**：40 行三重检查只覆盖 `/usr/local/bin/$other`、`native/$other`、`/etc/systemd/system/$other.service`，**没有 `/etc/init.d/$other`**；42 行随即 `rm -rf /etc/frp /opt/frp-simple-bash`。Windows 对应 `uninstall-windows.ps1:33` 只查 `Get-Service`，有同类缺口（`bin/$other.cmd` 残留）。
- **影响**：共享配置目录与安装根被删，残留的 OpenRC 服务指向不存在的二进制/配置，另一组件彻底损坏（数据丢失路径）。
- **建议方向**：`other` 检查补 `/etc/init.d/$other`（及 Windows 的 `bin/$other.cmd`）；purge 前枚举所有已知安装痕迹统一判定。

### BUG-014 [P2] `_mini_toml` 字符串转义解码顺序错误，含字面 `\n` / `\r` / `\uXXXX` 的值被静默破坏 `[QA]`
- **位置**：`lib/manager.py:123`、`:55-63`（`toml()` 用 `json.dumps` 编码）
- **类型**：兼容 / 数据处理
- **复现**：3.6 路径下 round-trip 含字面反斜杠+n 的 token。主 agent 独立实证：`'a\\nb'` → `toml()` → `_mini_toml()` → `'a\\\nb'`（变成真换行）**BROKEN**；`'C:\\path\\to\\cert'` → `'C:\\path\\\to\\cert'` **BROKEN**；`'tok\\rvalue'` / `'ctrl\\u0001x'` 本次恰好未损（但 `\r`/`\u` 完全未处理）。
- **根因**：123 行按 `.replace('\\"','"') → .replace('\\n','\n') → .replace('\\t','\t') → .replace('\\\\','\\')` 顺序，**先展开短转义再合并反斜杠**，导致 `\\n` 被误当换行；且完全不处理 `\r`、`\b`、`\f`、`\uXXXX`（`json.dumps` 会输出 `\u0001` 这类合法 TOML 转义）。
- **影响**：3.6 上 token、Dashboard 密码、TLS 证书路径（Windows 路径含反斜杠）被静默改写，认证失败或找不到证书，极难排查。
- **建议方向**：用 `json.loads('"'+body+'"')` 或单次扫描状态机；补含 `\n`/`\r`/`\\`/`\u` 的 round-trip 测试。

### BUG-015 [P2] `rotate_logs()` 使用 Python 3.7+ 的 `time.time_ns()`，3.6 上日志轮转时崩溃 `[QA]`
- **位置**：`lib/manager.py:193`；版本门槛 `install-common.sh:76-86`、`lib/manager.py:2`
- **类型**：兼容 / 边界
- **复现**：Python 3.6 环境让 `ROOT/logs/frpc.log` 超过 50MB，运行任意命令（`run()` → `initialize()` → `rotate_logs()` 是无条件路径，`:1771`、`:180`）。
- **根因**：`time.time_ns()` 是 3.7 引入；只在 `p.suffix == '.log' and size > 50MB` 分支执行，日常不触发，一旦轮转必抛 `AttributeError`。3.15 的 unittest 与 `ast.parse(feature_version=(3,6))` 都发现不了（运行时 API 非语法）。
- **影响**：3.6 上日志轮转时所有管理命令（含 `status`）直接崩溃退出。
- **建议方向**：改用 `int(time.time() * 1e9)` 或 `secrets.token_hex()`（同文件 84 行已有用法）。

### BUG-016 [P2] `upgrade()` 不强制镜像 HTTPS，且把 `off`/空反转回默认镜像 `[QA]`
- **位置**：`lib/manager.py:1715-1721`、`:1697-1700`（`_https_get` 名不副实）；对照 `lib/download.sh:4-12`、`install-common.sh:16`、`install-frps.sh:16`、`install-windows.ps1:28` 全部强制 `https://`
- **类型**：安全 / 逻辑
- **复现**：`FRP_MIRROR_URL=http://attacker.example frpc upgrade` → 明文下载并覆盖 root 执行的 `manager.py`。
- **根因**：`mirror = os.environ.get(...).rstrip('/')` 后无 `https://` 校验；`_https_get` 只是函数名，`urlopen` 未校验 scheme。同源 SHA256 清单（已知设计）无法防「HTTP 中间人同时替换 bundle 与清单」——其它所有下载路径都已强制 HTTPS，这是唯一缺口。另 1716-1717 把 `''`/`off`/`OFF` **重置为默认镜像**，与 `lib/download.sh:6`（`off` 表示禁用镜像）语义相反。
- **影响**：可经明文 HTTP 覆盖以 root 执行的 `manager.py`；`off` 无法用于离线/禁网环境（仍联网）。
- **建议方向**：与 `lib/download.sh:7` 一致的 `https://` + 空白字符校验；`off`/空保持禁用或明确提示；`_https_get` 内校验 `urlparse(url).scheme == 'https'`。

### BUG-017 [P2] `upgrade()` 无事务/回滚，发布包缺文件也能「升级成功」；`install-windows.ps1` 永不被更新 `[双]`
- **位置**：`lib/manager.py:1737-1765`、`:1738-1740`（`manager_files` 白名单）、`:1743-1744`（只校验第一个文件存在）
- **类型**：错误处理 / 逻辑
- **复现**：构造 `lib/manager.py` 存在但 `uninstall.sh` 缺失、或某 member `extractfile()` 抛异常的合法 SHA256 包 → 部分文件被 `os.replace` 覆盖后异常退出。
- **根因**：循环逐个 `open(.new)` + `os.replace`，**没有先全部落临时目录再统一提交，也没有备份/回滚**；只查第一个文件存在。对比 `install-common.sh:113-126` 与 Windows 安装器 140-209 都有回滚，`upgrade()` 两者皆无。另 `manager_files` 不含 `install-windows.ps1`，它既不以 `bin/` 也不以 `templates/` 开头（1750-1753），**永远不会被升级覆盖**；而 `install-frps.ps1`/`install-frpc.ps1` 会被更新 → Windows installer 版本可能与管理脚本不一致。
- **影响**：中途失败留下 `manager.py` 与 `install-*.sh`/`bin/` 版本混杂（接口不兼容、程序起不来），无自动恢复；Windows 重装会拉旧 installer。
- **建议方向**：先全部解压到 `INSTALL_ROOT/.upgrade-<token>/` 校验完整性，再一次性 `os.replace` 提交；失败删临时目录、原文件不动；`install-windows.ps1` 纳入白名单。

### BUG-018 [P2] `transaction()` 只捕获 `Exception`，且回滚自身失败会掩盖原始异常 `[QA]`
- **位置**：`lib/manager.py:96-104`
- **类型**：并发 / 错误处理
- **复现**：`with lock(), transaction():` 写入中途 Ctrl-C（`KeyboardInterrupt` 属 `BaseException`）或 `sys.exit`；或磁盘满触发回滚。
- **根因**：`except Exception:` 不覆盖 `KeyboardInterrupt`/`SystemExit` → 事务不回滚，但 `lock()` 的 `finally: p.rmdir()`（174 行）**照常释放锁**，半写入状态被后续当作合法（`mark_hash` 已变，`import_external` 无法自动修复）。其次 `except` 块内 `atomic(p, ...)` 自身抛异常会**掩盖原始异常**且剩余文件不再回滚。再者快照只含 `managed_paths()`（`ROOT/*.json`、`*.toml`、`profiles/*/*.toml`），不含 `logs/manager.log`（`audit()` append）、新建 `backups/` 目录、以及 `service()` 的服务状态副作用。
- **影响**：异常退出后配置可能半更新且锁已释放，呈现静默不一致；审计日志与备份目录无法回滚。
- **建议方向**：`except BaseException:`（或 `finally` + 显式提交标志）；回滚块内逐个 try/except 保护，最后 `raise` 保留原始异常链；文档化不回滚的副作用。

### BUG-019 [P2] `pending` 状态与实际运行态不一致 `[QA]`
- **位置**：`lib/manager.py:971`（唯一设 True）、`:977` 与 `:350`（唯二清 False）；缺失 `:1126-1130`（edit_profile 保存）、`:1396-1405`（import_external）、`:1529`（主菜单重启）、`:1776-1778`（CLI start/stop/restart）
- **类型**：状态一致性
- **复现**：菜单改 token/地址/协议并保存（1126 `apply(pid)` 重写 `frpc.toml`）→ 状态栏无「有待重启修改」，但运行中的 frpc 仍用旧配置。反向：改端口映射（设 `pending=True`）→ 主菜单「重启 frpc」（1529）→ 仍显示「有待重启修改」。
- **根因**：`pending` 只在 `save_mappings()` 维护，其它重写 `frpc.toml` 的路径都不设；清除只发生在 `set_current()` 与 `save_mappings` 自己的「立即重启」分支。frpc 无热重载，配置写入与运行态必须靠此标志关联。
- **影响**：用户被误导「配置已生效」或「仍有未生效修改」，排障方向错误；`diagnostics()` 944 行提示不可信。
- **建议方向**：把维护收敛到 `apply()`（写 `frpc.toml` 即置位）与 `service('frpc','restart'/'start')` 成功后清位；或改用 `frpc.toml` 内容 hash 直接推导，去掉手工标志。

### BUG-020 [P2] `_login_state_from_log()` 的两个失败标记在 frp 0.72.0 中不存在，认证失败只能等 30 秒超时 `[QA]`
- **位置**：`lib/manager.py:406-417`（尤其 407）、`:424-426`、`:454-476`（`wait_connection` 终态判定）
- **类型**：逻辑 / 兼容性（与 frp 二进制耦合）
- **复现**：profile 配错 token 后启动 frpc 并执行连接检测。QA 用仓库自带 `frp_0.72.0_darwin_arm64/frpc` 的 `strings` 核实：`login to server failed` **ABSENT**、`control writer is closing` **ABSENT**；只有 `login to server success, get run id [%s]`、`try to reconnect`、`connect to server error` 存在（认证失败实际输出类似 `User authentication failed` / `Failed to authenticate`，不在候选列表）。
- **根因**：407 行 `failure = max(rfind('login to server failed'), rfind('try to reconnect'), rfind('control writer is closing'), rfind('connect to server error'))` 有两个候选恒为 `-1`；412-416 的 `auth-failed`/`tls-failed` 又依赖 `failure` 命中后 256 字节窗口内出现 `token`/`tls`。token 错误时 `failure` 常为 `-1`、`success` 被 404-405 截断到最近启动后也为 `-1` → 返回 `None` → `connection()` 落到 452 行 `unknown`。而 `wait_connection()` 只把 `connected`/`auth-failed`/`tls-failed` 当终态（461-467），`unknown`/`connecting` 一直轮询到超时。
- **影响**：认证/TLS 错误无法即时准确报出，用户每次改动都要白等 30 秒并得到笼统超时提示，与上一轮修复意图相悖。
- **建议方向**：对照 frp 0.72 实际日志字符串补充候选；把「本次启动后无 success 但有明确认证错误」直接判 `auth-failed`；加断言覆盖。

### BUG-021 [P2] `install-common.sh:148` 用 `A && B || C || true`，enable 失败被吞掉且反被执行 disable `[QA]`
- **位置**：`install-common.sh:148`；对照 `:151`（OpenRC 用了正确 `if/else`）
- **类型**：错误处理 / 逻辑
- **复现**：systemd 上 `systemctl enable frps` 因 unit 被 mask / 权限 / 并发 daemon-reload 失败 → 安装不报错继续。
- **根因**：`((autostart == 1)) && systemctl enable "$component" || systemctl disable "$component" || true`。`&&`/`||` 同优先级左结合，enable 失败时执行 `disable` 把自启状态反转，最后 `|| true` 抹平退出码，`set -Eeuo pipefail` 与 ERR trap 都无法感知。
- **影响**：开机自启静默失败甚至反转，安装脚本仍打印「安装完成」；用户重启主机后服务没起来。
- **建议方向**：改成 151 行那样的 `if/else`，让 enable 失败触发 ERR trap 回滚。

### BUG-022 [P2] `rollback_install` 只回滚 native 二进制，不回滚已覆盖的 manager / 服务单元 / 符号链接 / 自启状态 `[QA]`
- **位置**：`install-common.sh:113-126`（回滚函数）、`:130-152`（ERR trap 生效范围内的全部写入）
- **类型**：错误处理 / 回滚完整性
- **复现**：重装时让 162 行 `false`（配置校验失败）或 148 行失败触发 ERR trap → 观察 `lib/manager.py`、`/etc/systemd/system/frps.service`、`/usr/local/bin/frps` 链接、`systemctl enable` 状态。
- **根因**：`rollback_install()` 只处理 `$work/previous`（旧二进制）和 `was_running`；但 130-152 行已覆盖 `bin/*`、`lib/manager.py`、`lib/download.sh`、`install-*.sh`、`uninstall.sh`、`templates/*`，安装了新 unit、改了 enable/disable、重建了链接，都不在回滚集合。
- **影响**：升级/重装失败后是「旧二进制 + 新 manager + 新服务单元 + 可能已变的自启状态」混搭，版本不匹配可能导致管理命令异常；与函数打印的「已尝试恢复旧二进制及先前运行状态」给人的完整回滚印象不符。
- **建议方向**：把 130-152 行所有目标先备份到 `$work`，回滚时统一恢复；或把 manager/unit 写入延后到二进制与配置校验全部通过之后。

### BUG-023 [P2] `lock()` 用 mkdir 目录锁且无过期/持有者信息，进程异常退出后永久阻塞所有写操作 `[双]`
- **位置**：`lib/manager.py:167-174`、`:176-180`（`initialize()` 不清理）
- **类型**：并发 / 健壮性
- **复现**：`kill -9` 或掉电发生在 `with lock()` 期间，残留 `ROOT/.operation.lock`；此后任何 `save_mappings`/`switch`/`create_profile`/`restore_backup`/`edit_profile` 保存都抛「另一个配置操作正在执行」。
- **根因**：`finally: p.rmdir()` 只覆盖正常 Python 异常路径，不覆盖 SIGKILL/掉电；锁目录内不写 PID/时间戳，`initialize()` 也不做陈旧锁清理，恢复责任完全交给用户手工 `rmdir`（注释里承认了）。
- **影响**：一次崩溃后**所有配置写操作永久不可用**，直到管理员介入。
- **建议方向**：锁目录内写 `pid + 时间戳 + hostname`，`FileExistsError` 时检查 PID 存活/超时安全接管；或 POSIX 分支改用 `fcntl.flock`（进程退出内核自动释放）。

### BUG-024 [P2] `port()` 接受全角/Unicode 数字 `[主]`
- **位置**：`lib/manager.py:228-230`
- **类型**：边界 / 兼容
- **复现**：主 agent 实证 `port('７０００')` → 返回 `7000` 通过校验；`port(' 7000')` 被拒。
- **根因**：`str(v).isdigit()` 对 Unicode 数字返回 True，`int()` 也能解析全角数字，缺少 ASCII 约束。
- **影响**：从网页/文档复制带全角字符的端口时，配置能通过自身校验但可能被 frp 拒绝，错误信息滞后到 `frp verify` 才暴露。
- **建议方向**：改用 `re.fullmatch(r'[0-9]{1,5}', str(v))`。

### BUG-025 [P2] `import_external()` 对外部配置缺字段直接 `KeyError`，错误信息不友好 `[主]`
- **位置**：`lib/manager.py:1387`、`1382`、`1385`（`config['serverAddr']` / `x['remotePort']` / `x['customDomains'][0]`）
- **类型**：边界 / 错误处理
- **复现**：手工编辑 `/etc/frp/frpc.toml` 删掉 `serverPort`，再 `frpc import` → 抛裸 `KeyError: 'serverPort'`。
- **根因**：直接下标访问未校验的外部输入；`run()` 的 `menu` 分支捕获 `KeyError` 但会误报成「外部配置未同步」，`import` 命令则直接落到最外层 `except Exception`。
- **影响**：用户看到 `错误：'serverPort'` 而非可操作提示。
- **建议方向**：`.get()` + 显式校验并抛 `ValueError('外部配置缺少 serverAddr/serverPort')`。

### BUG-026 [P2] `api()` 硬编码 2 秒超时，状态刷新可能卡顿 `[主]`
- **位置**：`lib/manager.py:378-386`（`opener.open(req, timeout=2)`）、`:429`、`:444`
- **类型**：健壮性 / 平台差异
- **复现**：Dashboard 地址不可达时，`status_bar()` → `status()` → `connection()` → 先 admin API（2s）再 Dashboard API（2s）。
- **根因**：超时固定 2 秒且无短路；日志已有 `login success` 时会提前返回，但**断线/重连场景**（`logged` 为 `connecting`/`None`）会走完两次超时。验证报告建议 30 秒，这里又过短。
- **影响**：每次打开菜单/查看状态最多卡 4 秒；高延迟网络下 Dashboard 误判。
- **建议方向**：超时可配置（环境变量/常量），admin 与 Dashboard 分开设置，已知不可达时做短期失败缓存。

### BUG-027 [P2] `status()` 在 active profile 无效/文件损坏时抛异常，JSON 命令输出非 JSON `[主]`
- **位置**：`lib/manager.py:1407-1411`
- **类型**：边界 / 错误处理
- **复现**：`state.json` 的 `active_profile_id` 指向已删除 profile，运行 `frpc status`。
- **根因**：`if role == 'frpc' and active(): ... load_profile(active())` 未包 try；`main_menu` 有 `except (...KeyError...)` 保护，但 `run()` 的 `status`/`version` 路径没有。
- **影响**：`frpc status` 报 `错误：...`，脚本化调用方拿到非 JSON 输出（与 BUG-003 不同层：那里是菜单崩溃，这里是命令契约破坏）。
- **建议方向**：`status()` 内对 `load_profile`/`connection` 兜底，返回 `active_profile: None` + 说明字段。

---

## 五、P3（轻微 / 维护）

### BUG-028 [P3] `check_port_conflict()` 不区分 TCP/UDP，`proto` 参数从未使用 `[QA]`
- **位置**：`lib/manager.py:1155-1168`、调用点 `:1278-1279`、`:947`
- **类型**：逻辑 / 测试缺口
- **复现**：本机 UDP 监听 7000，而 frps `bindPort=7000`（TCP）→ 误报「已被其他进程监听」。
- **根因**：`ss -lntuH` 同时列 TCP/UDP，函数只按 `local.endswith(':'+port)` 匹配，从不看 `fields[0]`（Netid）或 `proto` 入参；`proto`/`exclude_pid` 是死参数；1164 行 `local != str(port_value)` 永不成立。
- **影响**：诊断误报，用户可能放弃合法端口配置。
- **建议方向**：用 `fields[0]` 过滤并按 `proto` 比较；或 `ss -lntH`/`-lnuH` 分开查询。

### BUG-029 [P3] `manager.uninstall()` 未给 Linux 脚本传 `--yes`，双重确认且两次格式不同 `[QA]`
- **位置**：`lib/manager.py:1666-1671`、`uninstall.sh:16-19`；对照 Windows 分支 1670 正确带 `-Yes`
- **类型**：平台 / 交互流程
- **复现**：菜单「卸载 frpc」→ `confirm()` 输 `y` → 脚本再 `read -r -p '输入 YES 确认'`，输 `y` 被 `[[ "$answer" == YES ]]` 拒绝，卸载取消；但 `main_menu` 已置 `terminal=True`，菜单直接退出且无结果提示。
- **根因**：`manager.uninstall()` 自己确认过一次，却没把「已确认」传给脚本；`uninstall.sh` 的 `--yes`（10 行）未被使用。`--purge` 还要求 `--yes` 与 `--purge` 同时给（38 行），从菜单根本无法触发 purge。
- **影响**：Linux 用户须输入两种不同格式的确认，取消后菜单静默退出。
- **建议方向**：Linux `args` 追加 `--yes`；或去掉 `manager.uninstall()` 里的 `confirm()` 统一由脚本确认。

### BUG-030 [P3] 工具链 / 解析器小项 `[双]`
本条合并 4 个轻微问题：
1. **`lib/manager.py:106-133` `_mini_toml` 不支持内联表 / `0x10` 十六进制 / 数组内字符串含逗号** —— 实证 `a = {b = 1}` 与 `a = 0x10` 抛「无法解析的值」，`a = ["x,y"]` 按逗号错切。仅影响用户手工编辑的配置且 3.6 回退路径，`verify()` 会用 frp 二进制兜底。建议标注「子集解析器」边界或增强数组切分（引号感知）。
2. **`lib/manager.py:62-68` `toml()` 对混合类型列表抛不透明错误** —— 实证 `toml({'k':[1,{'a':1}]})` 抛「不支持的配置字段类型」；`[{'a':1}]` 被当 array-of-tables、空 `[]` 被当空数组，行为随首元素类型而变。内部 schema 不会出现混合列表，实际不可达，但错误不带 key 名。
3. **`lib/manager.py:286-298` / `:96-104` 事务回滚不删除新建的 profile 空目录** —— `create_profile()` 失败时 `shutil.rmtree(folder)` 清理，但 `transaction()` 的 `managed_paths() - set(originals)` 只 `unlink_quiet`（删文件），残留空目录。影响有限（`profiles()` 的 glob 会跳过空目录）。
4. **`tools/build_release.py:13` `DEFAULT_VERSION` 仍是 `v0.1.9-ux2.1`** —— 不带 `--version` 直接运行会构建出陈旧版本包；实际发布每次都显式传 `--version` 故未暴露。建议动态读取 `install-frpc.sh` 或强制要求参数。

---

## 六、交叉验证记录

主 agent 对子 agent 的关键结论做了独立复核，以下**均已实证确认**：

| 结论 | 复核方式 | 结果 |
|---|---|---|
| `service_feedback()` 不含 `service(role, action)` | 读 1490-1498 + grep 调用点 + `git log -S` | ✅ 确认，回归源自 `3728cb5` |
| 3.6 dotted key 解析不一致 | 强制 `_toml_lib=None` + 两套解析结果对比 + `validate_frps_config` 双跑 | ✅ 确认，3.11 抛错 / 3.6 放行 |
| `render()` 产物被 frp 接受 | 子 agent 用仓库自带 frpc `verify -c` | ✅ 通过（问题只在读回路径） |
| `_mini_toml` 转义顺序损坏 | 主 agent round-trip 实证 `a\\nb` / `C:\\path\\to\\cert` | ✅ 确认 BROKEN |
| `--no-start` 零消费 | grep `$start` | ✅ 确认，仅 7/30 两行 |
| `time.time_ns()` 3.7+ | 读 193 行 + `ast.parse(feature_version=(3,6))` | ✅ 语法通过但运行时 API 缺失 |
| `install()` 传 `v0.72.0` | 读 1663 行 + `install-common.sh:37` | ✅ 确认不匹配 |
| `edit_profile` 只判 `n == 0` | 读 1081 行 | ✅ 确认 `q`→`None` 落 else |
| `ask()` 空输入循环 / `toml({'k':None})` 抛错 | 读 617-620 + 实证 | ✅ 确认 |
| 3.6 语法整体兼容 | `ast.parse(feature_version=(3,6))` | ✅ 通过（BUG-015 是运行时 API） |

**两份清单差异**：子 agent 更深地覆盖了 Windows 平台、安装器失败路径、`ask()` 语义与回归历史；主 agent 独立补充了「日志优先掩盖 `proxy-error`」（BUG-011）、「显式 `exit` 不触发 ERR trap」（BUG-012）、`port()` 全角数字（BUG-024）、`import_external` KeyError（BUG-025）、`api()` 2 秒超时（BUG-026）、`status()` 命令契约（BUG-027）。

---

## 七、排除项（考虑过但判定为非 bug）

| 点 | 理由 |
|---|---|
| 不用 `tomllib`/`tomli`、自带 `_mini_toml` | 为兼容 3.6 的**有意设计**（但 BUG-002/014/030 揭示其实现不完整） |
| `render()` 生成的 TOML 是否被 frp 接受 | 实机 `frpc verify -c` 通过；点号键、`clientID`、布尔小写均合法 |
| Dashboard API v2 `code/msg/data` 信封 | `api():384` 要求 `code==200` 且 `data` 存在，与真实抓包及上游 `V2Response` 一致 |
| Python 3.6 **语法**兼容 | `ast.parse(feature_version=(3,6))` 通过；`run_cmd():41-50` 正确规避 3.7+ 参数；无类型注解故不需 future import |
| 空 token 允许（`validate_frps_config` 跳过长度检查） | `configure_server`/`frps_config_menu` 都要求用户二次确认「无 token 认证」，属主动选择 |
| 同源 SHA256 清单、镜像失败 fallback GitHub、拒绝自定义目录、`--version` 只收 `0.72.0` | 已知设计，代码有注释 |
| `render()` 的 frpc 端空 token 不写 `auth.method` | 两端需人工一致，属设计权衡 |
| `import_external` n==3 递归重显菜单、`bin/frpc.cmd` 的 `-ExecutionPolicy Bypass` | 纯交互 / 既定绕开执行策略方案 |
| `uninstall.sh` 删除前 `grep -Fq ExecStart` 防护、`atomic()` 0600+`os.replace`、`verify()` 不回显 stderr、`api()` 禁用代理、`pdir()` ID 校验、`restore_backup` 的 `..`/绝对路径/`is_symlink` 校验 | 均合理 |
| `install-common.sh:46` 空 mirror 时 `[[ -n ]] && export` 在 `set -e` 下 | 实测脚本继续且退出 0，非 bug |
| `upgrade()` 不更新 systemd/OpenRC 服务单元、不更新当前运行实例 | 设计权衡，未报 |
| 已修复项复查（`connection` 的 `online`、0 映射日志兜底、Dashboard 超时不误报、结果框折行、子菜单 `clear_screen`、`upgrade` 不更新实例、`from __future__` 移除） | 未发现这些修复自身引入新问题；唯一相关是 BUG-011（日志优先副作用）与 BUG-020（失败标记字符串），均单列 |
| 纯框线 / 颜色 / 暂停等待风格 | 按范围要求未报（BUG-029 例外，因其导致「取消后菜单静默退出」的功能性后果） |

---

## 八、测试覆盖缺口

1. **Python 3.6 唯一路径零覆盖（最严重）**：`tests/test_manager.py:10` 直接 `import tomllib`，全部用例跑在 3.11+ 且 `_toml_lib` 恒非 None；**没有任何用例强制 `_toml_lib = None`** → BUG-002 / BUG-014 / BUG-030 完全漏检。`render()` 相关断言全用 `tomllib.loads()`，在 3.6 上是假绿。
2. **`service_feedback()` 零覆盖**：全 tests 目录 grep 无该函数；`setUp` 把 `TEST=True`（`service():353` 恒返回 `'stopped'`），即使写测试也不会发现「未调用 `service(role, action)`」→ BUG-001 漏检。建议加 spy 断言。
3. **取消路径只测 `0` 不测 `q`**：`tests/test_manager.py:277` 输入 `['2','新名称','0']`；`choose()` 返回 `None`（BUG-009）与 `ask()` 返回 `None`（BUG-008）均无断言。
4. **`ask()` 空输入 / `None` 落配置零覆盖**：无用例覆盖「留空清除 token / 关闭 Dashboard / 禁用 vhost」；`toml({'k':None})` 抛错无断言。
5. **`upgrade()` 成功路径零覆盖**：只测参数校验与下载失败；无用例构造合法 tar 验证 chmod（BUG-007）、路径越界（BUG-006）、`install-windows.ps1` 缺失（BUG-017）、中途失败回滚（BUG-017）。
6. **安装器 `--no-start` / `--mirror off` / ERR trap 回滚零覆盖**：`test_installer.py` 只有 4 个只读用例；无用例断言 `start` 被消费（BUG-005）、enable 失败被吞（BUG-021）、`rollback_install` 恢复 manager/unit（BUG-022）。
7. **`uninstall.sh --purge` 零覆盖**：无 OpenRC 分支共享目录保护验证（BUG-013），也无 Windows purge 检查。
8. **native 测试默认全跳过**：`tests/test_native.py:30` `@skipUnless(FRP_NATIVE_DIR)`，默认 3 个真实 frp 用例全 skip；`render()` 产物是否被真 frp 接受在 CI 默认路径未验证（本次手工补验通过）。
9. **`frps.toml` 手写高级字段保留性零覆盖**：无用例在 `webServer.tls` 存在时走「修改 Dashboard/API」→ BUG-010 漏检。
10. **`pending` 标志零覆盖**：无用例断言 `edit_profile`/`import_external`/主菜单重启后 `state()['pending']` → BUG-019 漏检。
11. **锁异常退出零覆盖**：`test_lock_exclusion` 只测并发互斥，不测残留锁目录（BUG-023）与 `KeyboardInterrupt` 下事务回滚（BUG-018）。
12. **连接状态组合零覆盖**：无「日志 login success + 代理 status=error」用例（BUG-011）；无 frp 0.72 真实认证失败日志字符串用例（BUG-020）；无 Dashboard 不可达超时用例（BUG-026）。

---

## 九、声明

- 本次审查**全程只读**：未修改/写入任何业务代码，未运行安装/卸载/服务命令，未联网（仅本地 `frpc verify -c` 与本地文件检查），未提交 git。
- 本清单文件本身是审查交付物，不属于代码修复。
- **所有 30 条 bug 均保持未修复状态**，等待后续指示。
