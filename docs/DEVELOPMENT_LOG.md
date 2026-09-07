# JobPilot 开发日志

本文档按 Phase 记录每个阶段做了什么、遇到了什么问题、怎么解决的，方便后续回顾和排查问题时对照。规则：每个 Phase 完成并通过验收后才会追加对应章节，不倒填、不事后美化过程——踩过的坑如实记录。

---

## Phase 0：本地 App + 插件基座（2026-09-05 ~ 2026-09-07）

### 目标

跑通"Chrome 插件 <-> 本地 App"这条最底层的连接：本地 App 能独立启动、数据库结构完整；插件能检测到本地 App、完成配对、维持心跳连接。这是后续所有功能模块的地基，本身不含任何业务逻辑。

### 实现内容

- 后端：FastAPI + SQLAlchemy + Alembic + SQLite，八张核心表（`profile_basic`、`experience_entry`、`experience_bullet`、`jd_record`、`match_score`、`resume_version`、`qa_bank`、`model_config`）的迁移脚本；配对 token 生成与校验；`/api/health`（无鉴权探活）、`/api/status`（鉴权状态查询）、`/ws/heartbeat`（WebSocket 心跳）三个接口；所有鉴权接口同时校验配对 token 和请求 Origin。
- 插件：Chrome Manifest V3 骨架，后台 service worker 维护"未连接/已检测待配对/连接中/已连接/配对失败"状态机，侧边栏提供配对入口和实时状态展示。
- 自动化测试：12 条 pytest 用例（数据库迁移、配对握手的各种场景）。

### 遇到的问题与解决

**问题一：本地 App 启动后日志戛然而止，看起来像卡死**

现象：用户在 Windows 上跑 `python -m app.main`，日志只打印到 Alembic 迁移的三行就没有任何输出，既没有 uvicorn 的 "Application startup complete"，也没有我们自己代码里的启动确认日志。

原因：Alembic 生成的 `alembic/env.py` 里默认调用 `logging.config.fileConfig(alembic.ini)` 来配置日志，这个函数有一个不直觉的默认参数 `disable_existing_loggers=True`——凡是在这次调用之前就已经存在、但没有在 `alembic.ini` 的 `[loggers]` 里登记的 logger，都会被静默禁用。因为数据库迁移是在 FastAPI 的 `lifespan` 启动阶段、进程内直接调用 Alembic（而不是单独跑 `alembic upgrade head` 命令行）触发的，这时候 uvicorn 自己的 logger 和我们代码里 `logging.getLogger("jobpilot")` 创建的 logger 都已经存在，一遇到这行调用就被禁用掉，后面所有日志全部消失。服务本身其实已经正常启动，只是不再输出任何日志。

解决：在 `alembic/env.py` 里把这一行改成 `fileConfig(config.config_file_name, disable_existing_loggers=False)`。同时补了一条回归测试 `test_migration_does_not_disable_other_loggers`（在 `backend/tests/test_migrations.py` 里），验证迁移跑完之后指定的几个 logger 没有被意外禁用；把修复临时撤掉重跑这条测试，确认能精确复现原始报错，再改回来确认测试转绿，证明这条测试确实能锁住这个坑。

**问题二：Chrome 插件侧边栏一直卡在"检测中"，点"重新检测"也没反应**

现象：本地 App 已经确认正常启动（能看到完整的 uvicorn 启动日志），但插件侧边栏永远停在初始占位文案"检测中…"，点击"重新检测"按钮没有任何反应。

原因：`background/service_worker.js` 里用了 `chrome.alarms` API（做断线重连的周期性健康检查），但 `manifest.json` 的 `permissions` 字段只声明了 `["sidePanel", "storage"]`，漏掉了 `"alarms"`。没有这个权限声明，`chrome.alarms` 在插件运行时是 `undefined`。脚本执行到 `chrome.alarms.onAlarm.addListener(...)` 这一行时直接抛出 TypeError，而这一行代码在源文件里排在"注册消息监听器"（处理侧边栏"重新检测"按钮发来的消息）之前，导致抛异常之后,连消息监听器都没能注册上——整个后台脚本从加载的那一刻起就没有真正跑起来。

解决：
1. 在 `manifest.json` 的 `permissions` 里补上 `"alarms"`。
2. 把 `chrome.runtime.onMessage.addListener(...)` 的注册顺序提到最前面，保证不管后面哪一段代码抛异常，消息监听器都已经注册好了。
3. 给所有 `chrome.alarms` 相关调用包一层 `try/catch`，防止类似的权限遗漏问题再次导致整个脚本被"炸掉"。
4. 侧边栏 `sidepanel.js` 增加 `sendMessageSafe` 封装，检查 `chrome.runtime.lastError`，联系不上后台时明确显示"插件后台异常"提示，而不是无限停留在初始占位文案上。
5. 新增命令行自检脚本 `extension/scripts/check_permissions.js`：扫描代码里用到的 `chrome.*` API，对照 `manifest.json` 里声明的 `permissions`，缺失时直接报错退出（退出码非 0）。故意去掉 `alarms` 权限验证过这个脚本确实能抓住问题，加回来后脚本通过。

**踩过的坑之三（工具链层面，非代码 bug）：Windows 挂载目录默认不允许删除文件**

在往用户机器上直接提交代码时发现，连接的文件夹默认没有删除权限，`git status`/`git commit` 内部创建的 `.git/index.lock` 之类的临时文件删不掉，导致 git 操作报 "Operation not permitted"。解决办法是显式申请该文件夹的删除权限（用户会看到一次授权弹窗），批准后 git 相关操作恢复正常。记在这里是提醒后续任何需要在用户机器上做文件删除/移动的操作，都可能先撞上这同一个权限限制。

### 验证结果

- `backend`：12 条 pytest 用例全部通过，覆盖数据库迁移（含日志不被禁用的回归测试）和配对握手的正常/异常场景。
- `extension`：`node scripts/check_permissions.js` 权限自检通过；`node --check` 语法检查通过；用户在自己的 Chrome 里实测，插件能正常完成"检测本地 App -> 配对 -> 连接 -> 心跳"的完整流程。
- 直接在用户本机（Windows，通过设备桥接的 Linux 执行环境）用一个独立的 Python 虚拟环境重新跑过一遍 pytest，确认不是只有云端沙盒环境能跑通。

### 涉及文件

`backend/app/`、`backend/alembic/`、`backend/tests/`、`extension/`、`README.md`

---
