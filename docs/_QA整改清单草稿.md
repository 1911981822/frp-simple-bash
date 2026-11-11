# 文档符合性整改清单（草稿，待 6 个子 agent 结果合并）

> 依据：`docs/frp二次封装需求文档.md` / `概要设计文档.md` / `详细设计文档.md`
> 基线：git HEAD `acebffd`，101 测试全绿
> 原则：**按文档思路补齐，增量并存保持向后兼容，不改坏现有功能**

## 独立核查已确认项（主 agent）

### A 类：低风险补齐（纯新增字段/展示/参数，不动现有结构）

| # | 文档依据 | 现状 | 整改方向 |
|---|---|---|---|
| A1 | 详细设计 §5.2 / 需求 §8.2 | `profile.toml` 缺 `created_at`/`updated_at`/`auth_method`；缺 profile「启用状态」 | `create_profile()` 写入时补齐；`edit_profile()` 保存时刷新 `updated_at`；读取全部用 `.get()` 兼容旧数据 |
| A2 | 详细设计 §5.4 / 需求 §8.3 | `mappings.toml` 缺 `note`（备注）字段 | `validate_mapping()` 允许并校验（禁控制字符）；`mapping_wizard` 可选输入；`render()` 不写进 frp 配置（仅管理器存储） |
| A3 | 需求 §7.1(3) 路径信息 | `show_status` 只显示配置文件路径，缺二进制/服务文件/日志/备份路径、安装来源 | 追加展示行（不改已有 `ljust(10)` 断言行） |
| A4 | 需求 §7.1(1) 运行状态 | 缺服务 PID、启动时间、最近退出原因 | 从 `systemctl show` / `sc.exe` 尽力读取，失败显示「不可用」而非编造 |
| A5 | 需求 §13.3 / 详细设计 §13.5 | 审计仅 5 种事件（created/selected/mappings-saved/deleted/backup-restored），缺安装/升级/配置修改/启停/切换 profile | 在对应操作成功后追加 `audit(event)`，事件名固定 |
| A6 | 详细设计 §16.1/16.2 CLI | `logs --lines N` / `--follow` 未实现（仅位置参数）；`frpc profile edit/rename <id>` 忽略 `<id>`（只编辑 active）；`mapping --profile <id>` 未实现；非交互写操作 `--yes` 未实现 | 解析 `--lines`/`--follow`/`--yes`；`profile edit/rename` 支持按 ID 定位；`mapping` 支持 `--profile`（需临时切换 active 语义或明确拒绝并提示） |
| A7 | 详细设计 §16.3 JSON 输出 | `status --json` 输出原始 dict，缺 `{ok, code, component, service_state, connection_state, active_profile, error}` 信封；`--json` 参数被忽略（任意参数都输出 JSON） | 保留原字段 + 追加信封字段；显式识别 `--json` |
| A8 | 需求 §7.1(2) 版本信息 | 只有当前版本，缺安装来源、可用更新版本 | `show_status` 追加「安装来源」；可用更新标注「如可检测」→ 显示不可检测原因 |

### B 类：中风险对齐（增量并存，保持向后兼容）

| # | 文档依据 | 现状 | 整改方向 |
|---|---|---|---|
| B1 | 详细设计 §5.1 `index.json` | 只写 `{'schema_version','profiles'}`，缺 `active_profile_id`；profiles 元素字段是 `server_addr/server_port` 而非文档 `address/bind_port`，且无 `created_at/updated_at` | **保留**原 `profiles` 结构（现有代码依赖），**追加** `active_profile_id` 与每项 `address/bind_port/created_at/updated_at` 展示字段 |
| B2 | 详细设计 §5.5 `state.json` | 扁平 `schema_version/active_profile_id/pending/frpc_hash/frps_hash/frpc_log_offset`，缺文档 `frpc{service_state,connection_state,last_checked_at,last_result_code,last_error}` / `frps{...}` 嵌套 | **保留**扁平字段（大量代码依赖），**追加** `frpc`/`frps` 嵌套缓存字段；`status()` 刷新时写入 `last_checked_at`；文档说明「运行状态属于缓存，不作唯一真实来源」 |
| B3 | 详细设计 §8.3 防串配置检查 8 步 | 只实现 1-3（ID 校验、目录存在、ID 与目录一致），缺 4-8 | 在 `render()` 前补：index 一致性、mappings 不含其他 profile ID、映射名称来源、`source_profile_id` 写入 state、校验通过才替换 |
| B4 | 详细设计 §8.4 切换后清理 | 缺「删除旧临时生成文件」「记录 source_profile_id」 | `switch()` 补清理与记录 |
| B5 | 详细设计 §12.2 备份保留 | 无「保留最近 10 份」策略 | `backup()` 后清理超出 10 份的旧备份（**仅普通配置备份**，升级备份独立保留） |
| B6 | 详细设计 §15.1 错误码 E001-E017 | 完全无错误码体系，全用裸 `ValueError('中文')` | 新增 `AppError(code, message)` + `ERROR_CODES` 映射；**附加**错误码到现有消息（如 `[E006] 端口...`），不改异常类型，保证现有捕获不破 |
| B7 | 详细设计 §10.2/10.3 状态枚举 | 服务状态只映射 active/stopped/failed/unknown 等；连接状态缺 `not-configured/disconnected/unreachable/config-error/timeout/not-configured` | 补齐翻译表与判定映射；**保留**现有 state 字符串（不破坏调用方） |
| B8 | 详细设计 §10.5 诊断输出 8 行 | `diagnostics()` 缺「进程状态/配置状态/最近错误/日志路径」等明确行 | 追加这些行（保留现有 `服务状态` 断言） |
| B9 | 需求 §11 提示四要素 | 多数错误只有原因，缺「影响/建议/日志路径」 | 在 `result_error` 的 hint 中统一补充日志路径与建议（模板化，不改业务） |

### C 类：测试与文档补齐（不改生产逻辑）

| # | 文档依据 | 现状 | 整改方向 |
|---|---|---|---|
| C1 | 详细设计 §17.2 集成测试 | 几乎无（systemd/OpenRC/Windows/实连/TLS/30秒超时/升级回滚/写入中断） | 补可离线运行的集成测试（mock 服务管理器）+ 标注需真机的用例为 skip |
| C2 | 详细设计 §17.3 菜单测试 | 缺「输入负数/字母/空值/超大数字不退出」「0 返回正确层级」「二次确认」「别名动态显示」 | 补 `choose()`/`main_menu` 边界测试 |
| C3 | 详细设计 §17.4 安全测试 | 缺「token 不在日志/ps」「路径遍历」「Shell 元字符」「PowerShell 注入」「未授权读 secret」 | 补只读安全断言测试 |
| C4 | 详细设计 §17.1 / 需求 §15 | `test_native.py` 3 用例默认全 skip；3.6 回退路径已有测试但需覆盖新增字段 | 让 native 测试在提供二进制时可运行；补 A/B 类新增字段的测试 |
| C5 | 需求 §13.2 版本判断不散落 | `0.72.0`/`v0.72.0`/`710291514` 散落 7+ 文件 | 建 `lib/version.py` 或在 `build_release.py` 注入统一常量；**谨慎**：安装脚本是独立执行入口，需保证不引入新依赖 |

## FLOW 角色（边界与业务闭环）独有发现（已交叉验证）

| # | 文档依据 | 现状 | 整改方向 | 优先级 |
|---|---|---|---|---|
| F1 | 详细 §15.2(4) 切换失败保留目标 profile | `switch()` 中 `service('frpc','start')` 抛异常 → transaction 回滚 active 到旧 profile，但已 stop 的服务**不重启** → 死状态 | 把 start/wait 移出事务；或 except 中重启旧服务并明确提示；统一「启动失败」与「连接检测失败」语义 | P1 |
| F2 | 详细 §12.1 原子写入 10 步 | `apply()`/`switch()` 写 `frpc.toml` 无 lock/backup/写后复校验；`import_external` n==2 分支无锁无事务直接 apply | 统一 10 步写入器；import 分支纳入 lock+transaction | P1 |
| F3 | 详细 §10.1 单项失败显示「未知」+诊断入口 | active 指向损坏 profile 时 `status()` 静默置 `active_profile=None`，菜单显示「未选择服务端」，无错误提示/恢复入口 | 单独标记 config-error，菜单显式提示 + 提供「清除 active/从备份恢复/重新选择」入口 | P1 |
| F4 | 详细 §15.2(1) 输错一个字段不退出程序 | `edit_profile` while 无内层 try、`mapping_wizard` 的 `port()` 在 try 外 → 单个字段输错即抛到主菜单，整段编辑会话丢失 | 字段级 try/except + `result_error` 后重问该项，保留上下文 | P1 |
| F5 | 详细 §12.4 升级必须保留升级前版本和配置 | `upgrade()` 旧文件仅内存 dict，成功即丢弃；未调 `backup()`，备份菜单看不到升级前快照 | 升级前写入持久化快照（含 manifest），在 list_backups/restore 可见 | P1 |
| F6 | 概要 §6.6 删除后重新选择 active 或提示创建 | `delete_profile_id` 与 `edit_profile` 删除分支置空 active 后直接返回，不留提示；`pending/frpc_hash/frpc_log_offset` 未清；两处删除入口重复且一处漏 audit | 统一走 `delete_profile_id`；删除后引导/自动选择；清理残留状态 | P1 |
| F7 | 需求 §6.2 危险操作需 YES | 两个删除 profile 入口都用普通 `confirm()`（y 即可），运行中/未运行确认次数还不一致 | 统一 `danger=True`，固定两次确认语义 | P2 |
| F8 | 详细 §12.3(3)/§15.2(5) | 删除流程「停止 frpc」在事务外，删除失败事务回滚文件但**服务不重启** | 停服纳入可回滚范围或推迟到文件操作成功后 | P2 |
| F9 | 概要 §12.2 锁粒度 | `switch()` 持锁期间做交互确认 + 最多 30 秒 wait_connection，长阻塞其它终端 | 交互/连接检测移出临界区，仅「校验+原子替换」持锁 | P2 |
| F10 | 详细 §4.6 外部配置变更检测 | 未运行切换 / CLI `profile use` 直接覆盖手工改过的 `frpc.toml` 并更新 hash，绕过 import_external 检测 | switch 写盘前比对 hash 并提示；统一文档「未运行只切 active」口径 | P2 |
| F11 | 详细 §12.1 失败四要素/§12.2 manifest | `backup()` 失败残留半成品目录、无完整性校验；`restore` 不校验文件可解析；多余「幽灵」备份 | 备份用临时目录+原子重命名；manifest 记每文件 sha256；恢复前校验 | P2 |
| F12 | 详细 §12.1/§15.1 错误码与日志路径 | 原子写入失败无错误码、无日志路径、无 audit | 统一在 result_error hint 附日志路径 + 写 audit（与 B6 合并实施） | P2 |
| F13 | 详细 §12.1 第 6/7 步 | `set_temp_permissions` / `validate_generated_file` 无独立实现（0o600 隐含、frps 写后不复读） | atomic 显式 chmod + 写后回调校验；frps 写后 read_toml 复读 | P3 |

**FLOW 疑点实证结论（主 agent 已跑）**：`transaction()` 能完整恢复被 `shutil.rmtree` 删除的 profile 目录与文件（`managed_paths` 前后完全一致、active 回滚正确）→ §15.2(5) 文件层成立，F1/F8 的真问题是**服务进程状态**不回滚，严重度维持 P1/P2 不上调。

**F1 实证复现（主 agent 已跑）**：mock `service('frpc','start')` 抛 ValueError 后，`switch()` 的调用序列为 `status→stop→start(抛错)`，transaction 把 active 回退到旧 profile、frpc.toml 恢复旧内容，但**没有补偿重启**，服务停留在 stopped，且无「已回退请重新启动」提示。确认 P1 成立，整改 `switch()` 时必须把 start/wait 移出事务或补补偿重启。

**FLOW 文档条款张力（需主 agent 定口径）**：
- 需求 §8.2「未运行只切 active」vs 详细 §8.4「切换时完整生成 frpc.toml」——按「写 frpc.toml 前先检测外部修改」折中，不强行二选一。
- 概要 §6.6「重新选择 active」是自动选还是仅提示——按「至少明确提示 + 提供入口」实现，避免自作主张切换用户配置。

## PLAT 角色（安装/升级/卸载/跨平台）独有发现（已交叉验证）

| # | 文档依据 | 现状 | 整改方向 | 优先级 |
|---|---|---|---|---|
| P1 | 详细 §12.4/§14.4 升级保留旧版本+配置 | `upgrade()` 仅内存回滚，成功即丢弃 → 与 FLOW F5 同源，合并实施 | 升级前 `backup()` 持久化 + 输出回滚命令 | P1 |
| P2 | 详细 §14.3/§15.2 回滚完整性 | install-common.sh EXIT trap 未快照/恢复：符号链接、自启状态、config_root 权限、新建配置、回滚后未 daemon-reload | 快照补这几项；回滚末尾 daemon-reload；区分新建/原有 | P1 |
| P3 | 详细 §14.3/需求 §12.6 重复安装 | 无已安装版本/服务/运行状态检测，无确认即覆盖 unit/脚本/templates；Windows 每次装强制重置 Automatic | 安装前展示摘要 + 对服务注册/自启状态要求确认；保留用户已设的 StartupType | P1 |
| P4 | 需求 §10.2 服务启动类型三态 | WinSW XML 硬编码 Automatic、`disable` 实际落 demand、「禁用」不可达 | 三态 auto/demand/disabled；菜单提供三项 | P1 |
| P5 | 需求 §13.2 版本判断不散落 / §12.5 完整性 | 版本/SHA256 散落 12+ 处；**PowerShell 侧下载 manager.py/install-windows.ps1 未传 ExpectedSha256 即执行** | 统一版本常量源；PowerShell 下载强制校验同源 manifest | P1 |
| P6 | 概要 §16.3/详细 §14.5 彻底清理 | `--purge`/`-Purge` 从 CLI/菜单入口不可达；install 不传 `--yes/--no-start`；upgrade 无非交互路径 | run() 解析参数并下传；卸载菜单加「保留/彻底清理」子选择 | P1 |
| P7 | 详细 §14.5 purge 二次确认 | Linux `--yes --purge` 跳过二次确认（只需一个 --yes）；Windows `-Yes` 被架空、输错确认词提前 return 污染 PATH、漏检 xml、purge 不删 Program Files 共享脚本 | 统一二次确认语义；Windows 修 return 路径与清理范围 | P1 |
| P8 | 需求 §10.2/§11 启动失败四要素 | Windows 启动失败只抛原生异常，无 ServiceName/日志路径/建议 | catch 统一输出四要素 | P2 |
| P9 | 详细 §9.1/§4.3/概要 §9.5 | 无 ServiceAdapter 接口、无统一 paths() 结构，路径与平台判断散落 | 抽 ServiceAdapter + paths()（架构级，视风险） | P2 |
| P10 | 需求 §4.3 安装器显示版本/系统/架构/地址 | 全程不打印 | 下载前打印四项 + SHA256 前 8 位 | P2 |
| P11 | 详细 §14.4 升级流程 | `upgrade <version>` CLI 不可达、不比对 before/target、无变更摘要、无 GitHub fallback、不刷新已注册 unit | run() 接受版本参数；比对+摘要+fallback；unit 刷新纳入事务 | P2 |
| P12 | 详细 §13.5/需求 §13.3 审计 | install/uninstall/upgrade/启停全无 audit → 与 A5 合并实施 | service() 与 install/uninstall/upgrade 入口追加 audit | P2 |
| P13 | 详细 §14.4 回滚完整性 | 升级回滚不恢复原 mode、不删本次新建文件、Windows os.replace 失败被静默吞掉 | backups 记 (exists, mode)；回滚恢复权限+删新建文件；失败提示人工步骤 | P2 |
| P14 | 详细 §4.3/需求 §4.2 | Windows 缺 PSVersion 检测、`-NoStart`/`-Yes`；防火墙「检测」只返回固定提示（Linux 真检测） | PSVersion 门控、补参数、Windows 只读查询防火墙规则 | P2 |
| P15 | 详细 §4.7/§14.3 | 依赖自动安装无日志/失败无手工命令；无 schema_version 比对/迁移门控 | 打印手工命令 + 日志；install 前校验 schema_version | P2 |
| P16 | 详细 §9.2 systemd 模板 | 缺 `NoNewPrivileges=true`（安全加固） | 补该行（与 uninstall.sh 的 ExecStart 守护字符串自洽即可） | P3 |
| P17 | 详细 §4.4 步骤 5/10、§12.1 | 无文件大小校验、无版本信息持久化；失败回滚后 `$work` 未清理 | 落盘 version.json；rollback 退出前 rm -rf work；校验大小下限 | P3 |

**PLAT 疑点裁决（主 agent）**：
1. **后台兼容模式**：我写的共享基准把它误列为「首期不实现」，已勘误。需求 §10.1/概要 §9.3/详细 §9.5 均要求 → **判为 P1 功能缺失**，整改时按「受控后台进程 + PID 状态文件 + 明确提示自启受限」实现，不动 systemd/OpenRC 主路径。
2. **PowerShell 未校验即执行**：基准豁免的是「fallback 行为」与「清单非独立签名」，**不豁免**「官方源完全不传 ExpectedSha256」→ PLAT-005 成立，判 P1 安全隐患。
3. Windows purge 不删 Program Files 共享脚本：按 §16.3「删除管理命令」判 P2，纳入 P7 一并修。
4. `NoNewPrivileges` 不在 §1.3 豁免清单 → PLAT-016 成立（P3）。
5. Windows 服务注册无实测记录：纳入 C 类测试补齐（标注需真机）。

## 勘误记录
- 共享基准初版误把「后台兼容模式」「映射 note」列为首期不实现，已更正；整改按文档原文口径。

## 待其余 3 个子 agent 返回后补充
- REQ / STATE / MAINT 三个角色的发现（FLOW、PLAT 已合并；BUG 角色失败，主 agent 已自补回归验证）
- 交叉验证与去重
- 优先级重排（P0/P1 先做，P2/P3 视风险）
- 明确「不实施」项

## 关键决策记录
1. **state.json / index.json 采用增量并存，不强行改成文档结构** —— 否则会破坏所有现有读写代码，违反「不产生新 bug」。
2. **错误码采用「附加前缀」而非替换异常体系** —— 保持所有 `except ValueError` 捕获不变。
3. **文档「首期不实现」（§1.3）之外才整改** —— 后台兼容模式、多 frpc 实例、Web 面板等不补。
4. **每组整改后立即跑全量测试**，最后统一发版。

## REQ 角色独有发现（16 条，已交叉验证）

| # | 文档依据 | 现状 | 优先级 |
|---|---|---|---|
| R1 | 需求 §7.1-6/§15.2 | **frps 完全没有 TLS 配置**（configure_server/frps_config_menu/validate_frps_config 全链路无 TLS 字段） | P1 |
| R2 | 需求 §7.1-8/§12-7 | 卸载「彻底清理」在菜单/CLI 均不可达（`uninstall()` 从不传 `--purge`） | P1 |
| R3 | 需求 §7.3/§12-10/§6.2 | 公网开放/危险端口/0.0.0.0 风险提示与端口占用校验在写入路径缺失 | P1 |
| R4 | 概要 §5.9/§5.11 | 原子写入非 10 步、Dashboard 无能力模型、API 失败不分类 | P2 |
| R5 | 需求 §6.2/详细 §7.5 | 改配置不显示旧值、字段层无「0 取消」 | P2 |
| R6 | 需求 §7.1-5/§7.2 | 客户端信息缺连接时间/心跳；API 不可用不提示真实原因 | P2 |
| R7 | 需求 §7.3/§12-1 | **首次向导绕过 token 最小长度校验**（实证：写入 abc 成功，validate 会拒） | P1 |
| R8 | 需求 §8.2 | 服务端列表缺编号/连接状态；Dashboard 端口无占用校验 | P2 |
| R9 | 需求 §8.3 | 映射本地端口占用/远程端口冲突范围不足 | P2 |
| R10 | 需求 §8.3 | 无 active profile 时报错而非引导选择/新建 | P2 |
| R11 | 需求 §14/详细 §16 | 非交互写入仍走向导、缺 `--yes`、`use` 不收编号、`upgrade --version` 被忽略 | P2 |
| R12 | 需求 §14/详细 §16.3 | `--json` 被静默忽略，status schema 缺 ok/code/error/connection_state | P2 |
| R13 | 需求 §7.1-7 | 日志无「持续跟踪」「错误摘要」，只有 50/200 两档 | P2 |
| R14 | 详细 §7.1/§7.2/§7.5 | 菜单编号/项数与文档错位（frps 11 vs 9、frpc 14 vs 12、配置 11 vs 8） | P2 |
| R15 | 需求 §7.3 | 管理端口与业务端口无「分离」校验 | P3 |
| R16 | 需求 §11 | 错误提示缺「影响」「日志路径」两要素 | P3 |

## MAINT 角色独有发现（11 条，已交叉验证）

| # | 文档依据 | 现状 | 优先级 |
|---|---|---|---|
| M1 | 需求 §13.2 | 版本常量/asset_id/SHA256 散落 7+ 文件 ≥20 处，Linux 用 `0.72.0`/Windows 用 `v0.72.0` 两套方言 | P1 |
| M2 | 需求 §13.2/详细 §2.1 | Shell/PowerShell 配置模型不共享，下载/服务逻辑两套重写，`Get-HttpsFile` 重复 3 份 | P2 |
| M3 | 详细 §2.2/§3.1 | 单文件 2138 行/0 class/117 函数，违背分层与文件职责 | P2（规范偏差） |
| M4 | 详细 §5.6/§5.7/§11 | 无版本适配器，0.72 字段/日志/API 形状硬编码 7 处 | P2 |
| M5 | 详细 §5.1-§5.5 | 数据模型字段缺失（index 无 active_profile_id、profile 无时间戳、secrets 无 schema_version、state 无 frpc/frps 嵌套） | P2 |
| M6 | 详细 §13.2/§13.3/§13.5 | audit 无 level/component/action/result，无级别概念，事件仅 5 种 | P2 |
| M7 | 需求 §13.3/§14 | status/--json 不满足脚本采集契约 | P2 |
| M8 | 详细 §17.1-§17.5 | §17.2 集成测试零覆盖、Windows 分支零覆盖、§17.3 多级向导零用例、§17.4 安全测试缺 | P2 |
| M9 | 详细 §2.3 | 3.6 静态兼容通过，但 `tests/test_manager.py:10` 无条件 `import tomllib` → 3.6 上测试无法启动；CI 固定 3.11 无 3.6 matrix | P2 |
| M10 | 详细 §3.1/需求 §12 | **发布包打包 57 文件含 docs/tests/tools/.github**，泄漏内部文档、`push_mirror.sh` 的 SSH 别名与服务器路径、`/Users/a1/...` 本机路径；required 清单与 upgrade 不一致；版本注入不全 | P1 |
| M11 | 需求 §6.2/§13.2 | 重复代码/magic number/死代码/注释不符/异常捕获宽窄不一/硬编码路径重复 | P3 |

## 最终实施范围（按风险分级）

**第 1 组（低风险，纯增量）**：R7 token 校验、A1/A2 数据模型字段、A3/A4/A8 状态展示、A5/P12/M6 审计事件+格式、B6 错误码、M10 发布包白名单、R14 文档编号对齐说明
**第 2 组（闭环修复）**：FLOW F1-F8、R2 purge 可达、R10 无 active 引导、R1 frps TLS、R3 端口风险提示
**第 3 组（CLI/跨平台）**：R11/R12 CLI 参数与 JSON 信封、A6 logs/profile edit、PLAT P3/P4/P6/P7
**第 4 组（测试）**：C1-C5 + M8 + M9 条件导入
**不实施（记录差异）**：M3 单文件分层重构、M2 Shell/PowerShell 共用层、M1 版本常量单一源（改动面过大，风险高于收益；在文档中标注差异与后续计划）

---

# 实施进度（已完成并验证）

| 项 | 文档依据 | 修复内容 | 验证 |
|---|---|---|---|
| STATE-001 | 详细 §10.1 单项失败不退出菜单 | `status_bar` 的 `mappings()` 包 try；`status()` 增 `profile_error` 区分「未选择」与「配置损坏」 | `test_status_bar_survives_broken_mappings_file` |
| STATE-002/008 | 详细 §10.1/§10.2/§10.3 | 新增 `SERVICE_STATES`/`CONNECTION_STATES` 单一映射（覆盖文档 7+10 枚举）；状态栏与 show_status 共用 | `test_state_labels_cover_documented_enums`、`test_broken_profile_reports_config_error` |
| STATE-013 | 需求 §8.2 分类失败原因 | `wait_connection` 对 stopped/config-error **立即失败**并给专属建议，不再空等 30 秒 | `test_wait_connection_fails_fast_when_service_stopped` |
| STATE-011 | 需求 §12.8 不泄露敏感 | `_mini_toml` 解析异常不再回显原始值（只给字段/行级提示） | `test_toml_errors_do_not_echo_values` |
| R7 | 需求 §7.3/§12.1 强制 token 最小长度 | 首次向导写入前调 `validate_frps_config`；留空自动生成 24 字节强 token；`none` 需 YES 确认；`frps_config_menu` 同步强化 | `test_wizard_rejects_weak_token`、`test_wizard_autogenerates_strong_token_when_blank` |
| R1 | 需求 §7.1-6/§15.2 frps 可配 TLS | `validate_frps_config` 支持 `transport.tls`（证书/私钥成对且文件存在）；向导与菜单新增 TLS 项 | `test_validate_frps_accepts_tls_when_files_exist`、`test_validate_frps_rejects_tls_without_key` |
| R15 | 需求 §7.3 管理端口与业务端口分离 | `validate_frps_config` 与向导拒绝 Dashboard 端口 == 业务端口 | `test_validate_frps_rejects_same_dashboard_port` |
| M10 | 详细 §3.1 发布目录 / 需求 §12 | `build_release.py` 改**白名单**（仅 22 个运行时文件），docs/tests/tools/.github/README 不再入包；版本注入覆盖 7 个文件；mtime 固定使构建可复现 | 实测 57→22 文件、零泄漏、两次构建 SHA256 一致 |
| FLOW F1 | 详细 §15.2(4) 切换失败保留目标 profile | `switch()` 重构：交互确认/启动/30 秒探测移出 lock+transaction；启动失败补偿回退运行配置并明确提示；目标 profile 保留 | `test_switch_start_failure_restores_old_runtime`、`test_switch_does_not_hold_lock_during_probe` |

**验证状态**：114 测试全绿（skipped=3）、Python 3.6 语法通过、shell 语法通过、真 frp 0.72 `verify -c` 通过、13 项离线下载测试通过、发布包可复现。

**剩余待实施**：第 2 组其余（apply 备份/删除后引导/字段级重试/备份保留与校验/升级持久化快照）、第 3 组（CLI 参数/审计事件/错误码）、第 4 组（菜单编号对齐/集成与安全测试/3.6 条件导入）。

## 第 2 组（业务闭环）已完成

| 项 | 文档依据 | 修复内容 | 测试 |
|---|---|---|---|
| FLOW F2 | 详细 §12.1 原子写入 10 步 | `apply()` 增：写前备份旧 frpc.toml、写后从磁盘复读校验、写 audit；`import_external` n==2 补 lock+transaction | 备份测试 |
| FLOW F11 + §12.2 | 详细 §12.1 失败处理 / §12.2 保留 10 份 | `backup()` 改临时目录→逐文件校验大小→原子重命名，失败清理；manifest 增 sha256；新增 `_prune_backups` 保留最近 10 份（升级快照不剪） | `test_backup_captures_pre_change_state` |
| FLOW F6 | 概要 §6.6 / 详细 §12.3 | 删除 active profile 后清理 `pending/frpc_hash/frpc_log_offset` 并给出引导；`edit_profile` 删除分支复用 `delete_profile_id` | `test_delete_active_profile_guides_and_clears_state`、`test_delete_last_profile_guides_to_create` |
| FLOW F7 | 需求 §6.2 危险操作需 YES | 两处删除 profile 统一 `danger=True`（需 YES），文案明示删除全部映射、不额外备份、不可撤销 | `test_delete_profile_requires_yes_confirmation` |
| FLOW F4 | 详细 §15.2(1) / 需求 §13.1 | `mapping_wizard` 把 remote_or_domain 的 port() 纳入 try 只重问该字段；`edit_profile` 增 `field()` 保护，单字段失败保留其他修改 | 3 条 F4 测试 |

**验证**：120 测试全绿、3.6 语法通过、shell 语法通过、真 frp verify 通过、13 项离线下载通过、发布包 22 文件可构建。

**剩余**：升级持久化快照(F5)、第3组(CLI/JSON/审计/错误码)、第4组(菜单编号/集成与安全测试/3.6条件导入)。
