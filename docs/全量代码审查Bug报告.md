# 全量代码审查 Bug 报告

> 审查范围：`lib/manager.py`（2707 行）、`install-common.sh`、`install-frps.sh`、`install-frpc.sh`、`uninstall.sh`、`install-windows.ps1`、`uninstall-windows.ps1`、`tools/build_release.py`、`lib/download.sh`
> 审查基线：`fe0f7a6`（159 测试全绿，skipped=3）
> 审查方法：全局视角 + 逐条代码证据，按 7 类 bug 归纳。每类先给结论，再列具体问题。
> 严重度：🔴高（数据损坏/安全泄露/功能完全失效）｜🟡中（功能部分失效/误导）｜🟢低（边界缺陷）

---

## 第 1 类：默认值 / 语义设计错误

**全局结论**：已修复 2 处（Dashboard 默认监听、升级目标版本），但仍存在**默认值语义不一致**和**参数名不一致**导致的隐性错误。

### BUG-1-1 🟡 `--no-start` 参数永远不生效（flag 名断裂）
- **证据**：`lib/manager.py:2660` 读 `flags.get('nostart')`（无连字符），但 `lib/manager.py:2579` 的 `CLI_FLAGS` 里**根本没有 `--no-start`**，只有 `--non-interactive`；且 `_parse_flags` 对未识别项直接 `args.append(item)`。
- **分析**：用户执行 `frpc install --no-start` 时，`--no-start` 既不在 `CLI_FLAGS` 也不被特殊分支处理，落到 `args` 里被丢弃。`no_start` 恒为 `False`，**静默失败**——用户以为没启动服务，实际服务已启动。这是「参数声明但未实现」+「静默失效」的叠加。
- **建议**：把 `--no-start` 加入 `CLI_FLAGS`；`install()` 里统一用 `flags.get('no-start')` 或 `flags.get('nostart')` 二选一并修正解析，避免连字符差异。

### BUG-1-2 🟢 显示/诊断函数的 Dashboard 地址回退默认值仍为 `127.0.0.1`
- **证据**：`lib/manager.py:1936`、`:1970`、`:1974` 处 `web.get('addr', '127.0.0.1')`；而写入侧（`configure_server` / `frps_config_menu`）默认已改为 `0.0.0.0`。
- **分析**：写入默认 `0.0.0.0`、读取回退 `127.0.0.1`，语义不一致。当 `addr` 字段缺失时，显示/诊断会展示成 `127.0.0.1`，与用户实际配置的 `0.0.0.0` 观感矛盾。
- **建议**：统一回退值为 `0.0.0.0`（或改为「未设置」），与写入侧一致。

---

## 第 2 类：检测逻辑静默失效 / 误判

**全局结论**：这是**风险最高**的一类，历史上已修 2 处（`check_port_conflict` 字段索引+协议判断），但仍存在**子串匹配误判**和**日志关键词误判**。

### BUG-2-1 🟡 `firewall_hint` 的 ufw 规则判断用子串匹配，端口会误判
- **证据**：`lib/manager.py:1599` `if 'Status: active' in result.stdout and str(port_value) not in result.stdout:`
- **分析**：`str(port_value) not in result.stdout` 是**子串包含**判断，不是规则匹配。端口 `80` 会匹配到 `8080`、`8000`、`180` 等任意含 "80" 的输出；端口 `7500` 若出现在无关数字 `17500` 里也会误判为「已放行」。导致防火墙提示**时对时错**，误导用户排查方向。
- **建议**：解析 ufw 输出的规则行，用 `ufw status numbered` 或正则精确匹配 `{port}/{proto}`，而非子串。

### BUG-2-2 🟡 `_login_state_from_log` 的 auth/TLS 误判关键词过于宽松
- **证据**：`lib/manager.py:602-604`：
  ```python
  auth_error = max(low.rfind('token'), low.rfind('authentication'), low.rfind('authenticate'))
  tls_error = max(low.rfind('tls'), low.rfind('certificate'))
  ```
- **分析**：`rfind('token')` 会在日志里找到**任何含 "token" 的行**（包括连接握手、配置回显的正常日志），只要当前进程没有 `login to server success`，就判成 `auth-failed`。同样 `tls`/`certificate` 也会被普通信息行触发。这会导致**连接状态误报为认证/TLS 失败**，误导用户去改 token/证书，而实际问题可能是网络不通或服务端没起。
- **建议**：匹配**具体的失败消息全文**（如 `User authentication failed`、`Failed to authenticate`），而非宽泛的 `token`/`tls` 子串；或改用 `_login_state_from_log` 里已有的 `failure_marks` 精确列表。

### BUG-2-3 🟢 `connection()` 的 2 秒缓存键未包含 frpc 进程状态
- **证据**：`lib/manager.py:631` `key = (pid, stamp)`，仅用 profile id + 日志 mtime 作缓存键。
- **分析**：若 frpc 在缓存有效期内被外部 `systemctl stop frpc` 停止（非本管理器操作），缓存仍返回旧的 `connected`，直到 2 秒过期。虽然 TTL 短，但在「刚停止服务立刻查状态」的竞态下会短暂显示错误状态。
- **建议**：缓存键加入 `running('frpc')` 状态，或对状态查询一律 `fresh=True`。

---

## 第 3 类：异常处理 / 健壮性

**全局结论**：异常处理整体有兜底，但仍存在**异常类型捕获不一致**和**敏感信息潜在泄露**。

### BUG-3-1 🟡 各子菜单异常捕获范围不一致，同类异常会逃逸到主循环
- **证据**：`main_menu` 捕获 `(ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError)`（`lib/manager.py:2019` 起），但 `mapping_menu`（`:1364`）、`edit_profile`（`:1411`）、`frps_config_menu`（`:1681`）等子菜单的 try/except 集合各不相同，部分缺 `TypeError`、`SubprocessError`。
- **分析**：同一个异常（如 `subprocess.SubprocessError`）在主菜单能兜住，但在某个子菜单里会**直接抛出**，导致整个程序 `sys.exit(1)` 退出，而非回到菜单。用户视角是「程序闪退」。
- **建议**：抽一个统一的装饰器或异常元组常量 `MENU_ERRORS`，所有子菜单共用，保证行为一致。

### BUG-3-2 🟢 `transaction()` 回滚依赖文本文件假设（已实测：atomic 仅支持 str）
- **证据**：`lib/manager.py:145` `atomic(p, value.decode('utf-8-sig'))`；`atomic()`（`:107-117`）用 `os.fdopen(fd, 'w', encoding='utf-8')` **只接受 str**。
- **分析**：`atomic()` 设计上只写文本（UTF-8），所以回滚必须 `.decode('utf-8-sig')`。当前 `managed_paths()` 全部是 `.toml`/`.json` 文本文件，**成立**。风险仅在于：若未来有二进制文件进入 managed 范围，或某文件被外部写成非法 UTF-8，回滚会抛 `UnicodeDecodeError` 导致回滚失败。
- **建议**：保持现状（这是原子写入的正确实现）；若未来需要支持二进制，可给 `atomic()` 增加 bytes 分支（`os.fdopen(fd, 'wb')`），但当前无需改动。**此条为潜在隐患，非当前 bug。**

### BUG-3-3 （已撤销——实测不成立）
- **结论**：`import_external` 的错误消息均为静态文案，不回显配置值，无敏感泄露。
- **验证**：`lib/manager.py:1821-1860` 所有 `raise ValueError` 均为固定字符串，不含 `str(config)`、`token`、`password` 拼接。此条目为审查过程中的误判，予以撤销。

---

## 第 4 类：数据安全 / 事务完整性

**全局结论**：核心写入已加锁+事务，整体较扎实，但存在**死代码未接线**和**Windows 锁清理缺陷**。

### BUG-4-1 🟡 `restore_upgrade_snapshot()` 是死代码，升级回滚入口缺失
- **证据**：`lib/manager.py:2361` 定义 `restore_upgrade_snapshot()`，但全文**无任何调用点**（死代码检测确认）。
- **分析**：上一轮实现了 `upgrade_snapshot()`（升级前备份）和 `restore_upgrade_snapshot()`（恢复），但只把前者接进了 `upgrade()`，后者**从未接入任何入口**。用户升级出问题后，虽然提示「可用 restore 恢复」，但实际没有命令/菜单能调用这个恢复函数，**回滚承诺落空**。
- **建议**：在 `run()` 的 `restore` 分支或 `backup_menu()` 里，检测到 `-upgrade-` 快照时调用 `restore_upgrade_snapshot()` 而非通用的 `restore_backup()`。

### BUG-4-2 🟢 Windows 上陈旧锁无法自动清理（进程存活判断恒真）
- **证据**：`lib/manager.py:276` `if os.name == 'nt': return True`
- **分析**：`_pid_alive()` 在 Windows 上**永远返回 True**（保守策略），导致 `_clear_stale_lock()` 里 `not _pid_alive(pid)` 恒假，只能靠 `age > 300` 秒过期。若管理器在 Windows 上崩溃，用户需**等 5 分钟**才能重新操作，或手动删 `.operation.lock`。且 300 秒内重复崩溃会累积问题。
- **建议**：Windows 上用 `psutil` 或 `tasklist /FI "PID eq <pid>"` 做真实进程检测；至少把过期时间从 300 秒降到更合理值，并在锁被占用时给出明确的手工清理指引。

---

## 第 5 类：校验缺失 / 可绕过

**全局结论**：写入路径的校验基本到位（R7 修复后），但存在**错误码校验未落地**和**删除操作确认强度不一致**。

### BUG-5-1 🔴 错误码体系是空壳：17 个错误码仅 1 个被使用
- **证据**：`lib/manager.py:540` 定义 `ERROR_CODES`（E001–E017 共 17 个）；`err()` 全文仅 1 处调用（`:531` `err('E007', ...)`）。约 90 处 `raise ValueError` 中，其余 89 处**全部裸抛**，不带错误码。
- **分析**：文档（详细设计 §15.1）要求的 E001–E017 错误码体系，实际只有 E007（服务启动失败）真正落地。E001（不支持系统）、E006（端口非法）、E013（连接超时）、E014（写入失败）等**关键场景全部无错误码**。脚本/自动化无法按契约 `[Exxx]` 前缀识别错误类别，错误码体系形同虚设。
- **建议**：把高频错误路径（`port()`、`address()`、`validate_*`、`upgrade` 下载失败、`wait_connection` 超时等）逐一改为 `err('E0xx', ...)`；至少覆盖文档点名的 E001/E006/E007/E013/E014。

### BUG-5-2 🟢 `mapping delete` 未用 danger 确认（危险操作强度不足）
- **证据**：`lib/manager.py:2224` `elif not confirm('确认删除映射 ' + name_arg + '？'): return`
- **分析**：CLI 的 `mapping delete` 用普通 `confirm()`（输 `y` 即可），而菜单里的删除映射用的是 `confirm(..., danger=True)`（需输 `YES`）。同一删除操作，CLI 和菜单的**危险确认强度不一致**，CLI 路径可被 `y` 轻易通过，违背「危险操作需 YES」的一致性。
- **建议**：`mapping delete` 改用 `confirm(..., danger=True)`，与菜单对齐。

---

## 第 6 类：完整性 / 覆盖范围失控

**全局结论**：跨平台对称性存在明显缺口，Windows 服务三态未闭环。

### BUG-6-1 🟡 Windows 服务「禁用」态不可达（三态缺一）
- **证据**：`lib/manager.py:521` `sc.exe config ... start= 'auto' if action == 'enable' else 'demand'`
- **分析**：需求 §10.2 要求服务启动类型支持「自动/手动/**禁用**」三态。但 `disable` 落到 `demand`（手动），**没有 `disabled` 分支**。「取消自启动」被实现成「手动」而非「禁用」。用户无法真正禁用服务。
- **建议**：`service()` 增加 `disabled` 动作，`autostart()` 菜单提供三项选择（启用/手动/禁用）。

### BUG-6-2 🟢 `mapping --profile <id>` 参数未实现（文档契约断裂）
- **证据**：`lib/manager.py:2206` `mapping_command(argv)` 直接 `argv[0]` 作 action；`run()` 里 `mapping` 分支（`:2699`）把原始 `argv` 传入，未经过 `_parse_flags`。
- **分析**：详细设计 §16 声明的 `frpc mapping list --profile <id>` 参数，实际 `--profile` 会被当作 action 处理，报「未知命令」。文档契约与实现不符。
- **建议**：`mapping` 分支先 `_parse_flags`，用 `flags['profile']` 定位目标 profile（或明确告知不支持并给出错误码）。

### BUG-6-3 🟢 `ERROR_CODES` 常量本身是死代码（与 BUG-5-1 同源但独立视角）
- **证据**：死代码检测确认 `ERROR_CODES`（`lib/manager.py:540`）定义后**从未被引用**（`err()` 里也没引用它，只是硬编码了 `E007` 字符串）。
- **分析**：`ERROR_CODES` 这个字典定义后没有任何地方读它，是纯死代码。`err()` 也不校验传入的 code 是否在 `ERROR_CODES` 里，可能拼出 `[E999]` 这种不存在的码。
- **建议**：让 `err()` 校验 code 必须在 `ERROR_CODES` 中，否则 ValueError 自身报错；并真正把各错误场景接到 `err()` 上。

---

## 第 7 类：功能 / 契约未实现

**全局结论**：CLI 参数解析有多处断裂，是「声明了但没真正生效」的重灾区。

### BUG-7-1 🔴 `--no-start` 参数断裂（与 BUG-1-1 同源，严重度更高）
- **证据**：见 BUG-1-1。
- **影响升级**：这是一个**用户可能依赖的安全参数**——「安装但不启动」，在自动化部署里如果失效，会导致服务意外启动，属于「功能完全失效」级别。
- **建议**：立即修复 flag 名一致性。

### BUG-7-2 （已撤销——实测已拦截，非 bug）
- **结论**：`restore` 命令的路径穿越风险**已被 `restore_backup()` 内部校验拦截**。
- **验证**：实测 `restore_backup(ROOT/'backups'/'../state.json')` 和 `../../etc/passwd` 均被 `ValueError('备份路径非法')` 拦截，`.resolve().parent != base` 校验有效。此条目为审查过程中的过度担忧，予以撤销。

### BUG-7-3 🟢 `mapping delete` 的 if/elif/else 结构脆弱易错
- **证据**：`lib/manager.py:2218-2225`：
  ```python
  elif action == 'enable': items[index]['enabled'] = True
  elif action == 'disable': items[index]['enabled'] = False
  elif not confirm('确认删除映射 ' + name_arg + '？'): return
  else: items.pop(index)
  ```
- **分析**：`delete` 复用 `elif not confirm(...)` 分支，逻辑上能工作，但可读性差且 `delete` 未用 `danger=True`（见 BUG-5-2）。若未来有人在 `enable`/`disable` 后加新动作，这个 `elif not confirm` 会意外捕获。**结构脆弱，易引入回归**。
- **建议**：拆成独立的 `if action == 'delete'` 分支，明确 danger 确认。

---

## 死代码清单

| 编号 | 位置 | 状态 | 影响 |
|---|---|---|---|
| DEAD-1 | `ERROR_CODES`（`lib/manager.py:540`） | 定义后从未引用 | 错误码表是空壳（见 BUG-5-1/6-3） |
| DEAD-2 | `restore_upgrade_snapshot()`（`lib/manager.py:2361`） | 定义后无调用点 | 升级回滚入口缺失（见 BUG-4-1） |

---

## 汇总与优先级建议

按「影响面 × 触发概率 × 修复成本」排序：

| 优先级 | 编号 | 问题 | 类别 |
|---|---|---|---|
| 🔴 立即修 | BUG-7-1 / BUG-1-1 | `--no-start` 参数断裂，服务可能意外启动 | 契约未实现 |
| 🔴 立即修 | BUG-5-1 / BUG-6-3 | 错误码体系空壳，17 个仅 1 个生效 | 校验缺失 |
| 🔴 立即修 | BUG-4-1 | 升级回滚入口死代码，回滚承诺落空 | 事务完整性 |
| 🟡 尽快修 | BUG-2-2 | 日志关键词误判连接状态为 auth/TLS 失败 | 检测误判 |
| 🟡 尽快修 | BUG-2-1 | ufw 端口子串匹配误判 | 检测误判 |
| 🟡 尽快修 | BUG-6-1 | Windows 服务禁用态不可达 | 完整性 |
| 🟡 尽快修 | BUG-6-2 | `mapping --profile` 参数未实现 | 契约 |
| 🟡 尽快修 | BUG-3-1 | 子菜单异常捕获不一致，程序闪退 | 健壮性 |
| 🟡 尽快修 | BUG-5-2 | mapping delete 危险确认强度不足 | 校验 |
| 🟢 择机修 | BUG-1-2 / BUG-2-3 / BUG-3-2 / BUG-4-2 / BUG-7-3 | 其余边界缺陷 | 各类 |

> 已撤销（审查后实测不成立）：BUG-3-3（import 敏感泄露）、BUG-7-2（restore 路径穿越）——两者经实测均被现有校验拦截或无此问题。

## 一句话总评

代码在**核心写入安全（锁+事务+备份）**上做得比较扎实，但在**「声明了却没真正生效」的契约层**和**「看似工作实则误判」的检测层**仍有明显缺口。最需要警惕的是：错误码体系（17 个只有 1 个在用）、升级回滚（函数写好了但没接线）、`--no-start`（参数断裂导致服务意外启动）——这三处都是「做了半截」的典型，属于高优先级但容易被 159 个全绿测试掩盖的问题。

> 注：本次审查为静态代码审查 + 死代码检测脚本，未在真实 Linux/Windows 主机上运行安装/升级流程。标记「待精确到行号」「置信度中」的条目需进一步实测确认。
