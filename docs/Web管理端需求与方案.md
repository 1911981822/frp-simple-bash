# frpc Web 管理端 —— 需求与方案（定稿 v1.1）

> 状态：**需求已确认，施工中**
> 本文档为施工依据。若与你预期不符，请直接指出要改的章节。

---

## 1. 目标

给 frpc 提供一个**常驻 Web 管理端**，默认监听 `127.0.0.1:7501`，把终端菜单的全部功能搬到浏览器，并支持把面板自身通过 frp 映射到公网访问。

---

## 2. 决策记录（已确认）

| # | 项目 | 决定 |
|---|---|---|
| 1 | 前端技术栈 | **Vue 3 + Vite** |
| 2 | 后端技术栈 | **Python 标准库 `http.server`**（不引入 FastAPI/Flask，保持零依赖） |
| 3 | 构建产物 | **预构建随包发布**（`web/dist` 打进发布包，用户安装无需 Node） |
| 4 | 仓库结构 | 接受新增 `web/` 源码目录 + `web/dist` 产物入库 |
| 5 | 运行形态 | **直接安装为常驻服务**（systemd/launchd/Windows 服务） |
| 6 | 默认端口 | **7501**（避开 frpc admin API 7400、frps dashboard 7500） |
| 7 | 默认绑定 | `127.0.0.1`；**允许改绑**但强制警告并要求已设密码 |
| 8 | 认证 | **用户名 + 密码** |
| 9 | 危险操作 | 卸载 / 升级 / 删除服务端**全部进 Web**，二次确认 |
| 10 | 实时推送 | **SSE**（日志与状态） |
| 11 | 公网访问 | **面板自身可映射到公网**，自映射支持 **TCP + HTTP** 两种 |
| 12 | 暴露防护 | **强密码 + 登录限流 + 审计**（基础级，不含 IP 白名单/TOTP） |
| 13 | 防自锁 | **Web 可改关键配置，但保留 5 分钟逃生窗口 + 失败自动回滚**（见 §9） |

---

## 3. 功能需求（页面级）

### 3.1 概览页

| 区块 | 内容 |
|---|---|
| 状态大卡 | 服务状态（运行中/已停止/异常）、连接状态（已连接/连接中/认证失败/未连接） |
| 当前服务端 | 名称、`地址:端口`、token 是否已设置（脱敏） |
| 映射统计 | 总数 / 启用 / 在线 / 离线 |
| 流量 | 今日入/出（来自 frpc admin API） |
| 未生效提示 | 「有配置修改待重启」黄色条 + 一键重启 |
| 需关注 | 与终端 `_status_body` 的 concerns 一致 |
| 版本信息 | frpc 版本、管理器版本 |
| 公网访问 | 面板是否已被映射、当前公网地址、一键创建/删除自映射 |

### 3.2 服务控制

- 启动 / 停止 / 重启
- 重启时若有启用映射 → 弹「立即重启 / 后台延迟重启」（沿用 `restart_safely`）
- 结果反馈：成功/失败 + 失败原因 + 查看日志入口

### 3.3 端口映射

表格列：名称 / 类型 / 本地 / 远端 / 外部地址 / 在线 / 启用 / 操作

- 新增（类型联动字段：tcp/udp 填远程端口，http/https 填域名）
- 编辑、启用/停用、删除
- 一键复制外部访问地址
- 实时校验：重名、重复 remote_port、HTTP 必须有域名
- 标识自映射用的特殊映射（见 3.8）

### 3.4 服务端（profile）管理

- 列表：名称 / 地址 / 当前标记 / 连接状态
- 新建：分步表单（连接 → 认证 → TLS → 高级）
- 编辑：回填
- 切换：确认后用 `switch`（含可达性预检与失败回滚）
- 删除：二次确认 + 显示将删除的映射数

### 3.5 日志

- SSE 实时尾随
- 级别过滤（ERROR/WARN/INFO/DEBUG）、关键字搜索
- ERROR 红 / WARN 黄高亮
- 暂停/恢复自动滚动
- 下载当前日志

### 3.6 诊断

- 一键运行 `diagnostics`
- 逐项 ✓/✗ + 建议
- 显示每项耗时

### 3.7 备份与恢复

- 列表（时间、原因、文件数）
- 立即创建备份
- 恢复（确认后执行，恢复前自动 `pre-restore` 备份）

### 3.8 公网访问（自映射）

- 显示面板当前监听地址与端口
- **创建 TCP 映射**：选远程端口 → 自动生成映射（本地 `127.0.0.1:7501`）
- **创建 HTTP 映射**：填域名（要求 frps 已配 vhostHTTPPort）
- 自映射映射统一命名 `_webui-tcp` / `_webui-http`，在列表中特殊标记
- 删除自映射
- **强制警告**：暴露管理端到公网 = 暴露 root 控制面，必须已设强密码

### 3.9 设置

| 项目 | Web 可改？ |
|---|---|
| 开机自启动 | ✅ 可改 |
| 面板端口 / 绑定地址 / 用户名 / 密码 | ✅ **可改，但保留 5 分钟逃生窗口**（见 §9） |
| 查看配置文件路径、版本 | ✅ 只读 |

终端兜底命令（改错了/忘密码时用）：
```bash
sudo frpc web --set-port 7501 --set-bind 127.0.0.1
sudo frpc web --set-password
sudo frpc web --disable          # 临时停用（保留单元文件）
sudo frpc web --enable           # 重新启用
```

### 3.10 危险操作区

- 卸载 frpc（需输入 `frpc` 确认；会同时停止 Web 服务）
- 升级 frpc（需确认；升级后自动重启 Web 服务）
- 删除服务端（需输入服务端名称确认）

---

## 4. 技术架构

### 4.1 目录结构

```
frp-simple-bash/
├── lib/
│   ├── manager.py          # 现有核心（新增 web 子命令入口）
│   └── webui.py            # 新增：HTTP 服务、路由、认证、SSE、静态文件
├── web/                    # 新增：Vue 3 源码
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   └── src/
│       ├── main.js
│       ├── App.vue
│       ├── api.js
│       ├── components/
│       └── views/          # 概览/映射/服务端/日志/诊断/备份/设置
├── web/dist/               # 新增：构建产物（入库，随发布包分发）
├── bin/frpc-web            # 新增：入口脚本
└── templates/
    └── frp-simple-bash-web.service   # 新增
```

### 4.2 后端要点

- `http.server.ThreadingHTTPServer`（Python 3.6 用 `socketserver.ThreadingMixIn`）
- 路由：手写前缀匹配，`/api/*` 走 JSON，其余走静态文件（`web/dist`）
- 静态文件：带 ETag/Last-Modified，SPA fallback 到 `index.html`
- 所有业务操作**直接调用 `manager.py` 现有函数**，不复制逻辑
- 写操作串行化：复用现有 `lock()`；冲突时返回 409 + 友好提示
- 长任务：后台线程 + 任务表（id/状态/结果），前端轮询 `/api/tasks/{id}`
- SSE：`Content-Type: text/event-stream`，日志与状态两条流
- 错误统一信封：`{ok, code, error}`（复用 `api_envelope` 风格）

### 4.3 前端要点

- Vue 3 + Vite，无 UI 框架依赖（手写样式，控制体积）或引入轻量组件库（待定，默认手写）
- 路由：vue-router（hash 模式，避免服务端 history fallback 复杂度）
- 状态：Pinia 或简单 reactive（默认 reactive，减少依赖）
- 构建产物 < 300KB（gzip 后 < 100KB）
- 所有接口错误统一 toast 呈现

### 4.4 构建与发布

- 我本地 `npm run build` → 产出 `web/dist`
- `web/dist` 入库（否则发布包取不到）
- `.gitignore` 忽略 `web/node_modules`
- `tools/build_release.py` 白名单新增：`lib/webui.py`、`bin/frpc-web`、`web/dist/**`、`templates/frp-simple-bash-web.service`
- 发布包体积预计增加 300-500KB

---

## 5. 认证与安全模型

### 5.1 凭据

- 配置文件：`/etc/frp/web.json`（0600）
- 密码存储：`PBKDF2-HMAC-SHA256`，随机 salt，迭代 20 万次（stdlib 实现）
- 首次启动：生成随机 16 位密码，打印到终端/日志一次，并要求首次登录后修改
- 会话：服务端内存 `session` 表 + **不透明随机 token**（256 bit）放在 Cookie 里（`HttpOnly`、`SameSite=Strict`、`Secure` 仅在 HTTPS 时）。
  不用 HMAC 签名 Cookie 的原因：签名 Cookie 无法即时吊销，而内存表可以在改密时立刻让
  其它会话失效（`clear_except`）。代价是进程重启后需要重新登录，对管理端可接受。

### 5.2 防护

| 措施 | 说明 |
|---|---|
| 登录限流 | 单 IP 5 次失败锁 15 分钟，记录审计 |
| CSRF | 双提交 Cookie + 自定义 Header 校验 |
| 会话超时 | 空闲 30 分钟失效 |
| 敏感字段 | token/密码/私钥路径一律只返回「已设置」 |
| 审计 | `logs/web-audit.jsonl`：时间、IP、用户、动作、结果 |
| 改绑警告 | 绑定非回环地址时，启动日志与页面双重警告 |
| 危险操作 | 需输入确认文本，且记录审计 |

### 5.3 明确的风险声明（需你接受）

- Web 服务**以 root 运行**（要控制 systemd/防火墙）。认证被绕过 = 服务器失守。
- 面板映射到公网后，任何能访问该公网端口的人都能到达登录页，仅靠密码保护。
- 因此：**密码必须强、必须改掉初始密码、建议不要长期暴露**。

---

## 6. API 设计

```
POST   /api/login                       登录（username/password）
POST   /api/logout
GET    /api/session                     当前会话信息

GET    /api/overview                    概览
GET    /api/status                      状态（SSE 也可推）

POST   /api/service/start|stop|restart

GET    /api/profiles
POST   /api/profiles
PUT    /api/profiles/{id}
DELETE /api/profiles/{id}?confirm=<name>
POST   /api/profiles/{id}/switch

GET    /api/mappings
POST   /api/mappings
PUT    /api/mappings/{name}
POST   /api/mappings/{name}/toggle
DELETE /api/mappings/{name}?confirm=<name>

GET    /api/logs?lines=200&level=warn
GET    /api/logs/stream                 SSE
GET    /api/events                      SSE（状态变化）
GET    /api/logs/download

POST   /api/diagnose
GET    /api/backups
POST   /api/backups
POST   /api/restore
POST   /api/autostart

GET    /api/webui                      面板配置（只读，脱敏）
POST   /api/webui/expose               创建自映射 {type: tcp|http, remote_port|domain}
DELETE /api/webui/expose               删除自映射

POST   /api/uninstall?confirm=frpc
POST   /api/upgrade
GET    /api/tasks/{id}
```

---

## 7. 配置与文件

```jsonc
// /etc/frp/web.json  (0600)
{
  "schema_version": 1,
  "bind": "127.0.0.1",
  "port": 7501,
  "username": "admin",
  "password_hash": "pbkdf2_sha256$200000$<salt>$<hash>",
  "idle_timeout_minutes": 30,
  "login_max_failures": 5,
  "login_lock_minutes": 15
}
```

- 首次启动自动生成该文件（随机密码打印一次）
- 端口/绑定/认证：**Web 与终端都可改**。Web 改走 5 分钟逃生窗口（§9），
  终端用 `frpc web --set-port/--set-bind/--set-password`（改错或忘密码时的兜底）

---

## 8. 常驻服务

| 平台 | 单元 |
|---|---|
| systemd | `frp-simple-bash-web.service`（`ExecStart=/opt/frp-simple-bash/bin/frpc-web`） |
| OpenRC | `frp-simple-bash-web` 脚本 |
| launchd | `frp-simple-bash.web.plist`（macOS 仅 frpc，支持） |
| Windows | WinSW 服务 `frp-simple-bash-web` |

- 安装 frpc 时**默认安装并启用** Web 服务（决策 5）
- 卸载 frpc 时**同时停止并移除** Web 服务
- 升级时**重启** Web 服务（否则仍跑旧代码）

---

## 9. 防自锁：逃生窗口机制（v1.1 变更）

Web 端允许修改**端口 / 绑定地址 / 用户名 / 密码**，但必须保证"改错了还能救回来"。

### 9.1 改端口 / 绑定地址

```
用户在 Web 点「保存并生效」
   │
   ├─ 1. 校验新配置（端口 1-65535、绑定地址合法、端口未被占用）
   │     不合法 → 直接报错，不落盘、不改监听
   │
   ├─ 2. 备份旧配置 web.json → web.json.bak
   │
   ├─ 3. 写入新配置
   │
   ├─ 4. **启动第二个监听**（新地址:新端口），旧监听**继续服务**
   │     启动失败（端口被占/权限不足）→ 立即回滚配置，旧监听照常，页面报错
   │
   ├─ 5. 成功 → 页面展示：
   │     「新地址已生效：http://<new>:<port>
   │       ⏳ 旧地址将在 4:59 后停止（逃生窗口）
   │       [已确认新地址可用，立即关闭旧监听]」
   │
   └─ 6. 5 分钟内：
          • 用户点「已确认」→ 旧监听立即关闭
          • 或超时 → 旧监听自动关闭
          • 期间两个地址都能访问（会话共享，无需重新登录）
```

**关键点**：新监听启动失败时**配置自动回滚**，旧监听从未中断，用户不会被锁在门外。

### 9.2 改用户名 / 密码

不涉及监听变更，采用**新旧凭据同时有效**的过渡：

```
1. 写入新凭据（PBKDF2）
2. 旧凭据标记为 valid_until = now + 5min
3. 登录时任一匹配即通过
4. 5 分钟后旧凭据自动失效
5. 页面提示：「密码已修改，旧密码仍可用 4:59」
```

这样即使新密码记错了，5 分钟内还能用旧密码进来改回去。

### 9.3 进程重启后的窗口

逃生窗口是**内存态**：
- 期间若服务重启，窗口消失，只剩新配置生效
- 因此修改配置后**不自动重启服务**，而是热切换监听
- 若用户手动重启服务，以磁盘上的新配置为准（已通过第 4 步校验，安全）

### 9.4 终端兜底

即使一切失效，终端仍可恢复：

```bash
sudo frpc web --set-port 7501        # 改回默认端口
sudo frpc web --set-password          # 交互输入新密码（不回显，需输两次）
sudo frpc web --disable               # 临时停用 Web 服务（保留单元文件）
sudo frpc web --enable                # 重新启用
```

### 9.5 实现约束

- 双监听在同一进程内：主 `ThreadingHTTPServer` + 备 `ThreadingHTTPServer`，共享同一 App 状态
- 会话 Cookie 的 `Domain` 不限定 host，切换端口后仍然有效
- 逃生窗口状态**纯内存**：新配置在启动第二监听之前就已落盘，进程若崩溃，旧监听本就随之消失，
  重启后直接以磁盘上的新配置为准，不存在“无法判断哪个生效”的问题。
  反过来把它落盘会造成“磁盘写旧端口、实际新端口在监听”的不一致，因此刻意不持久化。

## 10. 多平台

| 平台 | Web 端 |
|---|---|
| Linux（systemd/OpenRC） | ✅ |
| macOS（仅 frpc） | ✅ |
| Windows | ✅（纯 Python + 静态文件） |

- Windows 下 `frpc-web` 用 `.cmd`/`.ps1` 入口
- 路径全部用 `pathlib`，无平台硬编码

---

## 11. 非目标

- ❌ 不做 frps 的 Web 端
- ❌ 不做多用户/角色权限
- ❌ 不做 TOTP / IP 白名单（本次不含，后续可加）
- ❌ 不做 HTTPS 证书自动申请（可后续用 Nginx 反代）
- ❌ 不引入数据库

---

## 12. 实施阶段与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| P1 | 后端骨架：静态服务、认证、错误信封、`/api/status` `/api/overview` | `curl` 全部只读接口通；未登录返回 401 |
| P2 | 前端骨架：Vue 工程、登录页、概览页、路由与 API 封装 | 浏览器能登录看到真实状态 |
| P3 | 映射页 + 服务控制 + 服务端管理（含增删改切换） | 与 CLI 行为一致 |
| P4 | 日志 SSE + 诊断 + 备份恢复 + 设置 | 日志实时滚动、诊断逐项 |
| P5 | 自映射（TCP/HTTP）+ 危险操作 + 审计 | 能从公网打开面板 |
| P6 | 常驻服务安装、卸载清理、升级重启、文档、真机验证 | 装/卸/升级闭环 |

### 测试计划

- 单元：认证（密码校验、限流、会话）、路由匹配、参数校验、CSRF、敏感字段脱敏
- 集成：每个 API 的成功/失败路径（约 40-60 条）
- 真机：Linux 安装 → 浏览器全流程 → 自映射 → 卸载
- 回归：现有 298 条测试必须全绿

---

## 13. 最终确认结果

| 项目 | 确认 |
|---|---|
| 后端保持 Python 标准库 | ✅ 确认 |
| 面板以 root 常驻运行 | ✅ 确认 |
| 关键配置修改方式 | ✅ **Web 可改 + 5 分钟逃生窗口**（v1.1 变更，详见 §9） |

**需求已定稿，进入施工。**
---

## 14. 审查整改记录（P1 完成后）

对 P1 代码做了两轮独立审查：**代码审查**（功能/易维护性/复用性/闭环/多系统/容错/安全）与**人因工程审查**（易用性/便捷性）。以下是修复结果。

### 14.1 代码审查修复（高危）

| 问题 | 后果 | 修复 |
|---|---|---|
| 无请求体上限、无 socket 超时、无并发上限 | 未认证 slowloris 即可耗尽线程（实测 CL=100MB 卡死线程、30 空闲连接=30 线程） | `Handler.timeout=15` + 1MB 体积上限 + 64 并发信号量，超出直接 503 |
| `webui.py`/`web/dist` 未入库、未进发布白名单、安装器不安装 | 装好后 `frpc web` 直接 `Errno 2`，`/` 返回 500 | `.gitignore` 加 `!web/dist/`、`build_release` 白名单、`install-common.sh` 安装、`UPGRADE_FILES` 与升级快照覆盖 `web/dist/` |

### 14.2 代码审查修复（中危）

- **HEAD 响应带 body** → HEAD 不再写 body，且自动复用 GET 处理器；方法不符返回 405 + `Allow`
- **413 不关连接** → 未读净 body 时 `close_connection=True`，避免请求错位（CL desync）
- **chunked 未处理** → 显式 411 拒绝
- **非法 URL 未捕获** → `urlsplit` 包 try，返回 400
- **错误信封缺 hint** → `{ok, code(字符串), error, hint, retry_after, details, request_id}` + `X-Request-Id` 头
- **OSError 被当 400 且泄露路径** → ValueError→400，OSError→500「磁盘/权限问题」，500 统一泛化，细节只进审计
- **`.bak` 与审计日志 0644** → 均 0600（原来泄漏 password_hash 与会话材料、登录 IP）
- **`verify_password` 无迭代边界** → 1000~2,000,000 之外直接 False
- **非 ASCII CSRF 头抛 TypeError** → 先 `isascii()` 校验，返回 403 并记审计
- **未知配置键被丢弃** → 读写保留全部键（`must_change_password` 等扩展字段不再失效）
- **端口越界无校验** → 1-65535，非法回落并标记
- **`Server` 头泄露 Python 版本 / health 泄露版本** → `sys_version=''`，health 只回 `{status, first_run}`
- **dist 缺失泄露绝对路径** → 泛化文案 + 审计记录
- **`assets/` 缓存按 URL 前缀判定** → 改为按真实路径，`/assets/../index.html` 不再长缓存
- **SIGTERM 未处理** → 接 SIGTERM/SIGINT，优雅关闭所有监听并写审计
- **双 manager 模块实例** → 新增 `bind_manager()`，由 manager 注入真实模块
- **`_proxy_status_map` 返回 `{}` 无法区分未知** → 返回 `None` 表示读不到，新增公有 `mapping_online_status()`
- **`/api` 被 SPA fallback 吞成 200 HTML** → 统一进 JSON 404
- **SSE 声明 keep-alive 但无定界** → 明确 `Connection: close`

### 14.3 人因工程修复

| 问题 | 修复 |
|---|---|
| 导航 7 项且服务控制无入口 | 收为 **5 项**：概览 / 端口映射 / 服务端（FRPS）/ 日志与诊断 / 设置 |
| 概览是空壳，最高频闭环（看状态→重启→复制）缺失 | 概览页落地：状态带 + 4 指标 + 需关注 + 待重启 + 启停重启 + 管理端地址一键复制 |
| 首次登录无法自救（密码只打印在 stdout） | 登录页加「首次使用 / 忘记密码」帮助区，给 3 条可复制命令；`/api/health` 下发 `first_run` |
| 锁定提示无倒计时、无法解除 | 429 下发 `retry_after`，前端实时倒计时并给解除命令 |
| 错误文案不可操作、英文直出 | 四类分流：网络 / 401 / 429 / 5xx，各带 hint 与重试按钮 |
| 登录页缺基本功 | 自动聚焦、密码显隐、CapsLock 提示、防重复提交、`autocapitalize=none` |
| 前端丢弃 CSRF，写操作必 403 | 新增 `lib/api.js`：自动带 CSRF、401 跳登录、20s 超时、统一 `ApiError` |
| 无退出登录入口 | 侧栏底部加入用户区 + 退出按钮 + 会话剩余时间 |
| 状态只有英文枚举、无颜色语义 | 后端下发 `{label, tone}`（`service_view`/`connection_view`）；前端 `StatusBadge`（圆点+文字+底色，不只靠颜色）；`lib/status.js` 仅作兜底 |
| 概览在线数恒为 0 | 修 `online` 恒空 bug；读不到时显示 `—`（未知），绝不谎报 0 |
| 映射读取失败伪装成"没有映射" | `overview` 返回 `mappings_error`，前端显示「读取失败 + 去恢复备份」 |
| 需关注清单在 Web 丢失 | 抽出 `manager.status_concerns()`，终端与 Web 共用同一份判定 |
| 零响应式，手机不可用 | 3 档断点：≤820px 侧栏转顶部横条、≤480px 指标两列；触控目标 ≥40px |
| 只有深色，原生控件白底 | `color-scheme: dark light` + 明暗两套 token |
| 旧浏览器白屏无解释 | `nomodule` 兜底提示 |
| 无访问日志/审计不可见 | 新增 `/api/audit`；CSRF 失败也记审计 |
| 日志只能手动刷新 | `Logs.vue` 用 SSE 实时尾随，支持级别过滤/暂停/清屏 |

### 14.4 前端基础设施

新增（对应需求文档 §4.1）：
```
web/src/lib/api.js       统一请求、CSRF、401、超时、ApiError
web/src/lib/status.js    枚举→中文→tone 兜底字典
web/src/lib/format.js    时间/外部地址格式化
web/src/components/      StatusBadge / EmptyState / ErrorState / ConfirmDialog
```

### 14.5 验证

- webui 测试从 21 条增至 **40 条**（HEAD 无 body、413 关连接、411 chunked、405、非法 URL、错误信封、锁定 retry_after、审计 0600、`.bak` 0600、未知键保留、端口回落、Server 头、非 ASCII CSRF、在线未知语义、mappings_error、concerns、服务控制 CSRF、服务控制调用）
- 全量 **338 条通过**
- 真实启动验证：首页 200 / 静态 200 / health / HEAD 200 / 405 / 登录 / overview 均正常
- 构建：主包 104KB（gzip 41KB）+ 按路由分割的懒加载块

---

## 15. P2-P6 完成记录（v1.0.0-rc.19）

### 15.1 已实现的接口（共 33 条）

| 分组 | 接口 |
|---|---|
| 认证 | `POST /api/login`、`POST /api/logout`、`GET /api/session`、`GET /api/health` |
| 状态 | `GET /api/status`、`GET /api/overview`、`GET /api/events`(SSE) |
| 服务控制 | `POST /api/service/start`、`/stop`、`/restart`（支持 detached 延迟重启） |
| 映射 | `GET/POST /api/mappings`、`PUT/DELETE /api/mappings/{name}`、`POST /api/mappings/{name}/toggle` |
| 服务端 | `GET/POST /api/profiles`、`PUT/DELETE /api/profiles/{id}`、`POST /api/profiles/{id}/switch` |
| 日志 | `GET /api/logs`、`GET /api/logs/stream`(SSE)、`GET /api/audit` |
| 诊断 | `POST /api/diagnose` |
| 备份 | `GET/POST /api/backups`、`POST /api/restore` |
| 设置 | `GET /api/settings`、`POST /api/settings/apply|confirm|rollback|password`、`POST /api/autostart` |
| 公网访问 | `POST/DELETE /api/expose` |
| 危险操作 | `POST /api/uninstall`、`POST /api/upgrade` |

### 15.2 页面

| 页面 | 状态 |
|---|---|
| 概览 | ✅ 状态带 + 4 指标 + 需关注 + 启停重启 + 地址复制 + 公网访问 |
| 端口映射 | ✅ 列表/搜索/筛选/新增/编辑/启停/删除/一键复制/冲突校验 |
| 服务端（FRPS） | ✅ 卡片列表/新建/编辑/切换/删除（输入名称确认） |
| 日志与诊断 | ✅ 实时日志(SSE) + 连接诊断（Tab 切换） |
| 设置 | ✅ 面板访问/逃生窗口/账号安全/自启动/备份恢复/危险操作 |

### 15.3 关键设计决策

1. **非交互上下文**：新增 `manager.non_interactive()`，Web 复用 CLI 业务函数时
   `confirm()` 直接放行（确认已由前端完成），而 `choose()/ask()` 会抛错，
   避免静默卡住。这样 CLI 与 Web 不会出现两套业务逻辑。
2. **诊断字段命名**：`POST /api/diagnose` 用 `passed` 表示"检查是否通过"，
   避免与信封 `ok`（请求是否成功）冲突——否则"诊断发现问题"会被前端当成接口失败。
3. **新建服务端自动激活**：当前没有选中服务端时，新建后自动设为当前。
4. **新建服务端补齐 `admin_password`**：`render()` 与本地 admin API 依赖该字段，
   Web 建的服务端若不生成，切换/启动会 `KeyError`。
5. **自映射**：特殊命名 `_webui-tcp` / `_webui-http`，暴露前强制要求已设面板密码。
6. **逃生窗口**：改端口/绑定时启动第二个监听，旧监听保留 300 秒；后台 watchdog
   到点自动关闭；改密码则新旧凭据同时有效 300 秒。
7. **危险操作**：卸载/升级在后台线程执行（因为会删掉/替换当前进程的文件），
   响应先返回再执行。

### 15.4 常驻服务

- 单元模板：`templates/frp-simple-bash-web.{service,openrc,plist}`
- 入口：`bin/frpc-web`（+ `.cmd` / `.ps1`）
- 安装时自动注册（`FRP_NO_WEB=1` 跳过）；卸载 frpc 时自动移除
- CLI：`frpc web --install-service | --uninstall-service | --status`

### 15.5 验证

- 全量 **348 测试通过**（webui 50 条）
- 端到端实测：新建服务端（自动激活）→ 新建两条映射 → 停用 → 备份 → 备份列表 → 诊断 → 重复端口被拒
- 发布包 48 个文件，含 `lib/webui.py`、`web/dist/**`、三个服务模板、三个入口脚本

---

## 16. QA 轮次与修复（发布前）

对 Web 管理端执行了 4 路独立 QA：**接口功能 QA**、**安全渗透 QA**、**浏览器 E2E QA**、**安装链路自测**。

### 16.1 功能 QA（11 项）

| 编号 | 级别 | 问题 | 修复 |
|---|---|---|---|
| QA-1 | **高** | 并发写映射丢失更新：10 个并发创建全返回 200，仅 1 个落盘（read-modify-write 在锁外） | 映射 CRUD 的「读-改-写」全部移入 `write_lock`；复测 10/10 落盘 |
| QA-2 | 中 | 不存在的 profile 返回 500 `E_IO`（误导为磁盘故障） | 新增 `_require_profile()`：不存在 → 404 `E_NOT_FOUND`；损坏 → 500 `E_CONFIG_CORRUPT` |
| QA-3 | 中 | `enabled:"false"` 被 `bool()` 强转成 `true`（本意禁用却启用并推到公网） | 新增 `_strict_bool()`，字符串按语义解析，非法值 400 |
| QA-4 | 中 | `local_ip:"999.1.1.1"` 被当作域名接受 | 新增 `local_address()`：拒绝"像 IPv4 但非法"的输入 |
| QA-5 | 中 | 配置损坏返回 400 + Python 解析器英文原文，不指出文件 | 新增 `ConfigCorruptError`；返回 500 `E_CONFIG_CORRUPT` + 文件名（不含绝对路径） |
| QA-6 | 中 | `HEAD /api/logs/stream`、`/api/events` 返回 body 且启动无限流 | HEAD 只发头、不启动流、立即关连接 |
| QA-7 | 低 | PATCH/OPTIONS/TRACE、414 返回 HTML 错误页 | 新增 `do_PATCH/do_OPTIONS/do_TRACE` 与 `send_error` 覆写，统一 JSON 信封 |
| QA-8 | 低 | `first_run` 恒 false，首装/凭据丢失无提示 | 生成初始配置时置 `must_change_password`；`password_hash` 为空也算 first_run |
| QA-9 | 低 | 端口字段 str/number 混用 | 新增 `_to_int()` 归一 |
| QA-10 | 低 | Web 写操作把 CLI 结果框打进服务日志 | `non_interactive()` 内同时重定向 stdout |
| QA-11 | 低 | 卸载/升级后台线程异常未捕获，失败无痕迹 | 捕获全部异常并写审计 |

### 16.2 安全 QA（7 项，无严重漏洞）

| 编号 | 级别 | 问题 | 修复 |
|---|---|---|---|
| SEC-1 | 中 | 64 个未认证空闲连接即可占满全局连接池（远程 DoS） | 全局池 64→128 + **单 IP 并发上限 16**；复测第 17 个连接起 429，释放后恢复 |
| SEC-2 | 中 | 限流只认 socket IP；反代后任何人 5 次错密码可锁死管理员 | 新增 `trusted_proxies` 配置：仅可信代理的 `X-Forwarded-For` 才采信 |
| SEC-3 | 低 | 重复/非法 `Content-Length` 被当 0 并复用连接（请求走私面） | 重复且不一致 → 400 + 关连接；非法值 → 400 + 关连接 |
| SEC-4 | 低 | 304 响应缺安全头 | 抽出 `_security_headers()`，200/304 共用 |
| SEC-5 | 低 | 错误响应回显服务器绝对路径 | 新增 `_sanitize()`：路径替换为 `<路径>`；日志/备份路径改为相对 |
| SEC-6 | 低 | Cookie 无 Secure、无 HSTS | HTTPS 请求（含 `X-Forwarded-Proto`）时加 `Secure`；HTTPS 响应加 HSTS |
| SEC-7 | 低 | 改密不吊销既有会话；"旧密码 5 分钟有效"名不副实 | 实现旧哈希宽限期校验；改密时吊销除当前外的全部会话；清理旧哈希 |

### 16.3 浏览器 E2E（4 项）

| 编号 | 级别 | 问题 | 修复 |
|---|---|---|---|
| E2E-5 | **高** | 编辑映射改名失败（PUT 用了新名字定位资源） | 记录 `originalName`，PUT 用编辑前的名字 |
| E2E-10 | **高** | 概览/映射页 ErrorState「重试」点击无效（缺 `@retry`） | 补 `@retry="load"` |
| E2E-3 | 中 | 侧栏会话剩余时间永不显示（`/api/session` 不返回 `expires_in`） | session 与 login 响应都返回 `expires_in` |
| E2E-2 | 低 | 第 5 次密码错误要等第 6 次才提示锁定 | 触发锁定的那一次直接返回 429 + `retry_after` |

### 16.4 测试期间发现并修复的自身回归

- `process_request` 被我方补丁重复定义 → 每个请求抛 `Semaphore released too many times` 且限额失效（QA agent 预警）；已合并为单一定义，复测 0 异常。

### 16.5 最终验证

- 全量 **363 测试通过**（webui 65 条）
- 实测复验：并发 10/10 落盘、404 语义、布尔严格、非法 IP 拒绝、HEAD SSE 0 字节、PATCH→405 JSON、`first_run` 正确、端口类型归一
- 前端构建：主包 104.77KB（gzip 41.27KB）
- 3.6 语法 / shell 语法 / ps1 ASCII 全部通过

---

## 17. 文档与实现一致性整改（rc.22）

发布前对"文档承诺但代码未兑现"的项做了一次专项核对。**先取证再改**：

| 文档位置 | 原表述 | 实测结果 | 处理 |
|---|---|---|---|
| §5.1（:186） | "会话：HMAC 签名 Cookie" | `session_secret` 只在生成/断言/文档出现，**从未参与任何签名**；会话是内存表 + 不透明随机 token | 删除 `session_secret` 死字段；文档改写为实际设计并说明为何比签名 Cookie 更合适（可即时吊销） |
| §7 配置结构（:264） | 列出 `session_secret` 字段 | 同上 | 从示例删除 |
| §3.9（:114）/ §9.4（:353） | `sudo frpc web --disable` | 实测 `错误：未知参数：--disable` | **补实现** `--disable`，并新增对称的 `--enable` |
| §9.4（:350） | `--set-password # 重置密码（打印新密码）` | 实际是 `getpass` 交互输入两次，不打印 | 改文档（交互输入更安全：不进 history/ps） |
| §9.5（:358） | "逃生窗口存内存 + 每分钟落盘一次" | 纯内存，无任何持久化 | 改文档：说明**为何刻意不落盘**（新配置已先落盘；进程崩溃时旧监听本就消失，落盘反而制造磁盘与实际监听的不一致） |
| §7（:274） | "端口/绑定/认证**仅终端可改**" | 与 §3.9/§9（Web 也能改 + 5 分钟逃生窗口）自相矛盾，是 v1.0 早期遗留 | 改为"Web 与终端都可改" |

### 17.1 顺带发现并修复的可用性问题

核对"首次密码如何告知用户"时发现：`print` 到管道（systemd 下 stdout 不是 TTY）是**块缓冲**，
进程运行期间 `journalctl -u frp-simple-bash-web | grep 密码` **看不到任何内容**，只有进程退出才落盘。
而文档恰恰让用户用这条命令取密码。

→ 抽出 `announce_startup()`，打印后立即 `flush` stdout+stderr；补测试断言"打印后必须 flush"。

### 17.2 验证

- 取证方式：全仓 `grep` 引用点 + 实际执行 CLI + 读代码路径，确认 4 项均真实存在后才修改
- 新增 5 条一致性测试：死字段已移除、遗留字段读取时清除、`--disable/--enable` 可用、
  `--help` 列出全部文档承诺的参数、启动信息必须 flush
- 全量 **379 测试通过**
