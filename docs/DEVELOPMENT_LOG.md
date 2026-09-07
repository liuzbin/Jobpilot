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

## Phase 1：简历解析建画像 + JD 粘贴解析 + 打分引擎 + 最简 Dashboard（2026-09-07）

### 目标

跑通"端到端打分闭环"这个 MVP 最核心的验收点：用户能在本地浏览器打开的 Dashboard 页面里，上传简历自动建立/补全画像，粘贴一段 JD，点一下"分析"，看到一个可解释、且多次重复分析同一份 JD 和画像必须给出完全一致分数的匹配结果。本 Phase 不做插件端抓取 JD、不做简历重制，这两块留到 Phase 2/3。

### 实现内容

- **模型配置与密钥存储**：新增 `app/core/secrets.py`，用对称加密（`cryptography.fernet.Fernet`）把 API Key 加密后存本地文件，取代最初方案里设想的 OS keyring（原因见下方"设计取舍"）。`app/core/llm_client.py` 定义 `LLMClient` 协议、真实调用的 `OpenAICompatibleClient`（任意 OpenAI 兼容的 Chat Completions 接口都能接）、测试用的 `FakeLLMClient`（按调用顺序返回预设的固定 JSON，不发真实网络请求）。`app/core/llm_factory.py` 按"轻量/重量"两个槽位从数据库读配置、组装出对应的 `LLMClient`。
- **简历解析建画像**：`app/services/resume_ingest.py` 支持 `.pdf`（pdfplumber）/`.docx`（python-docx）/`.txt`/`.md` 四种格式提取纯文本，再交给轻量模型按 A/B/C 三层结构 + 基本信息字段做结构化抽取（prompt 里明确要求"只抽取真实写出来的内容，不要编造"）。`app/services/profile_service.py` 负责把抽取结果合并写入画像：公司/项目按名称不区分大小写模糊匹配（匹配上复用、匹配不上新增），贡献句按去除首尾空白后的精确文本去重；基本信息则是"只填空字段，不覆盖用户已经手动确认过的值"。
- **JD 粘贴解析**：`app/services/jd_ingest.py` 里，JD 原文交给轻量模型抽取学历/年限/清关等硬性要求和加分技能；而 LinkedIn 常见的附加信息行（"2 weeks ago · 80 people clicked apply · Promoted"这种格式）改用纯正则规则解析，不走 LLM，保证这部分结果绝对稳定。
- **打分引擎**（本 Phase 的核心）：`app/services/scoring.py` 把"语义判断"和"分数计算"严格拆开——重量模型只负责输出技能契合度打分、是否满足学历/清关这类需要理解语义的判断，所有加减分的具体数值（年限差扣分、加分技能封顶等）全部由 `compute_score()` 这个纯 Python 函数按固定规则计算，不假手于 LLM。这样只要 LLM 给出的语义判断稳定，最终分数就是完全确定性、可复现的，直接对应用户"打分必须整体稳定一致且客观"的硬性要求。
- **编排与状态机**：`app/services/analysis.py` 的 `analyze_jd()` 把"JD 解析 -> 打分"串起来，同时维护 `JDRecord.status`（pending -> analyzing -> analyzed，失败回退到 pending）。
- **Dashboard**：新增 `app/api/routes_dashboard.py` + 6 个 Jinja2 模板（`base`/`profile`/`jobs_list`/`job_new`/`job_detail`/`models`），覆盖画像编辑、简历上传、JD 列表与详情、JD 新建、一键分析、模型配置六个页面/动作。Dashboard 是用户直接在本机浏览器打开的（不经过插件），所以鉴权方式和插件走的配对 token 机制不一样，见下方"设计取舍"。

### 设计取舍（有意简化，供后续 Phase 参考）

1. **密钥存储用对称加密文件代替 OS keyring**：Linux 服务器/CI 环境通常没有可用的 Secret Service，keyring 库在这类环境下容易直接抛异常，给自动化测试和无图形界面场景带来不必要的复杂度。改用 Fernet 对称加密 + 本地文件（`~/.jobpilot/secret.key` 权限限制为仅当前用户可读），同样满足"API Key 不在数据库里明文存储"的核心诉求，后续如果要换回 OS keyring，只需要替换 `secrets.py` 内部实现。
2. **画像合并目前是全自动合并，不做交互式 diff**：原方案设想"新旧内容冲突时弹出来给用户选合并还是替换"，本 Phase 先用"名称模糊匹配 + 精确去重"的自动合并规则跑通闭环，交互式手动合并留到 Phase 2。
3. **Dashboard 认证与插件认证分离**：Phase 0 里所有需要鉴权的接口都走 `require_paired_request`（校验配对 token + `chrome-extension://` Origin）。但 Dashboard 页面是用户直接拿浏览器打开的普通页面导航，根本不会带配对 token，Origin 也不是 `chrome-extension://` 开头，如果直接复用插件那套鉴权，Dashboard 会被自己的鉴权规则挡在门外。所以单独加了一个更轻量的 `require_local_browser`：GET 页面不做任何鉴权（本地只读、只监听 127.0.0.1，风险可控），POST 动作只做最基本的防护——Origin 不存在（同源导航的常见情况）或者 Origin 就是本地服务自己时放行，来自其他站点的 Origin 一律拒绝，防止用户打开的恶意网页用隐藏表单跨站提交篡改本地数据。

### 遇到的问题与解决

**问题一：在 FastAPI 依赖函数里直接抛"模型未配置"异常，导致路由层的友好提示逻辑失效**

现象（设计阶段自查发现，没有等到真正跑起来才暴露）：最初打算让 `get_light_client`/`get_heavy_client` 这两个 FastAPI 依赖在模型没配置时直接 `raise ModelNotConfiguredError`。

原因：FastAPI 的依赖解析发生在路由函数体真正执行之前，依赖函数里抛出的异常，路由函数体里包的 `try/except` 根本捕获不到——异常会直接变成一个 500 错误页面，而不是我们想要的"请先到模型配置页面填写"这种友好提示。

解决：改成依赖函数内部自己 `try/except` 掉 `ModelNotConfiguredError`，"没配置"这个状态用返回 `None` 表示，路由函数体显式检查 `is None` 再决定怎么提示用户（`app/api/deps_llm.py`）。

**问题二：`analyze_jd()` 最初的函数签名没法脱离真实网络单独测试**

现象（设计阶段自查发现）：最初 `analyze_jd(db, settings, jd_id)` 内部自己调 `build_client()` 组装 LLM 客户端，这样写测试时要么真的配一套可用的 API Key 和网络，要么要 mock 掉一整条"读数据库配置 -> 读加密存储 -> 组装 client"的链路，非常繁琐。

解决：把 `light_client`/`heavy_client` 改成由调用方（路由层，通过 FastAPI 依赖注入）传入，`analyze_jd()` 本身只依赖 `LLMClient` 这个协议，不关心具体实现。测试里用 `app.dependency_overrides` 把 `get_light_client`/`get_heavy_client` 换成返回预设 JSON 的 `FakeLLMClient`，就能把"粘贴 JD -> 分析 -> 出分数"整条链路在没有真实 API Key、没有真实网络请求的情况下完整跑通（`backend/tests/test_dashboard.py`）。

**问题三：模板里引用了一个不存在的 ORM 属性，页面渲染时才会报错**

现象（自测时发现）：`job_detail.html` 里写了 `{{ jd.parsed_meta_pretty }}`，但 `JDRecord` 模型上根本没有这个字段，只有 `parsed_meta`（一个 dict）。

解决：不在模板里做 JSON 序列化，改成路由函数里算好 `parsed_meta_pretty = json.dumps(jd.parsed_meta, ensure_ascii=False, indent=2)`，作为单独的模板变量传进去。这里也顺带定下一条约定：模板只管展示，任何需要转换格式的逻辑都放在路由函数里做，避免类似"模板引用了不存在的属性"这种要跑起来才能发现的错误。

**问题四：简历上传接口里一处临时文件清理逻辑写得比较脆弱**

现象（写代码时自查发现）：最初 `finally` 块里用 `except NameError` 来判断"临时文件路径变量到底有没有被赋值过"，这种写法依赖于"变量未定义时访问它会抛 NameError"这个隐晦的行为，可读性差，以后改代码时很容易被绕过。

解决：改成在 `try` 之前显式初始化 `tmp_path: Path | None = None`，`finally` 里判断 `if tmp_path is not None` 再删除，逻辑清晰、也方便测试覆盖到"文件解析失败也要清理临时文件"这条路径。

**问题五：直接在用户机器上装依赖、跑测试时，发现之前的虚拟环境是 Windows 环境创建的，设备桥接用的 Linux 执行环境没法直接用**

现象：这个 Phase 收尾在用户机器上验证时，发现 `backend/venv`、`backend/.venv` 两个虚拟环境目录里是 `Include/Lib/Scripts`（Windows venv 的目录结构），而设备桥接实际跑命令用的是一个独立的 Linux 执行环境，两者不兼容，直接用会报"No such file or directory"。

原因：这两个虚拟环境是之前在用户的 Windows PowerShell 里直接创建的，物理上放在了 Windows 项目目录下（所以设备桥接的 Linux 环境也能"看到"这个目录），但虚拟环境本身内部的可执行文件和目录结构是操作系统相关的，不能跨平台复用。

解决：不去动用户 Windows 上已有的这两个虚拟环境（那是用户自己在 Windows 上跑 `python -m app.main` 用的，功能是好的），而是在设备桥接的 Linux 执行环境自己的 home 目录下（不在项目文件夹里）单独建一个 Linux 版虚拟环境，只用来跑自动化测试做验证，跑完即弃，不污染用户的项目目录。这条也记下来提醒自己：以后每次要在"设备桥接"环境里跑 Python 相关命令，先确认虚拟环境是不是这个环境自己建的，不要想当然复用项目目录里看到的虚拟环境。

**观察到但暂不处理：一处无害的 Pydantic 警告**

`models_update` 路由的 `model_name` 表单字段会触发 `UserWarning: Field "model_name" ... has conflict with protected namespace "model_"`。这是 Pydantic v2 对以 `model_` 开头的字段名的保护性提示，不影响功能（表单照常提交、保存、回显都正常，自动化测试全部通过），先记录在这里，不在本 Phase 处理。

### 验证结果

- 新增 4 个 pytest 文件，共 32 条新用例（`test_profile_service.py` 11 条、`test_jd_ingest.py` 7 条、`test_scoring.py` 14 条 —— 含明确的"同样输入连续调用 5 次结果必须完全一致"的确定性测试，以及硬性要求不重复扣分、年限差封顶、加分技能封顶、总分 clamp 到 [0,100] 等边界场景、`test_dashboard.py` 16 条），加上 Phase 0 遗留的 12 条，`backend` 目录下共 **56 条 pytest 用例全部通过**。
- Dashboard 相关测试全部通过 `app.dependency_overrides` 注入 `FakeLLMClient`，覆盖了"模型未配置时的友好提示"和"模型配置好之后完整走一遍解析+打分"两条路径，全程不依赖真实网络和真实 API Key。
- 先在云端沙盒环境跑通全部 56 条用例，再把代码同步到用户本机项目目录（`C:\liuzhibin\aI-agent\jobpilot`），逐文件核对 SHA-256 校验和确认同步无损坏，在设备桥接的 Linux 执行环境里重新装一遍依赖、重新跑一遍全部 56 条用例，结果一致。

### 涉及文件

`backend/requirements.txt`、`backend/app/main.py`、`backend/app/api/deps.py`、`backend/app/api/deps_llm.py`、`backend/app/api/routes_dashboard.py`、`backend/app/core/secrets.py`、`backend/app/core/llm_client.py`、`backend/app/core/llm_factory.py`、`backend/app/services/`（新增目录：`profile_service.py`、`resume_ingest.py`、`jd_ingest.py`、`scoring.py`、`analysis.py`）、`backend/app/templates/`（新增：`base.html`、`profile.html`、`jobs_list.html`、`job_new.html`、`job_detail.html`、`models.html`）、`backend/tests/conftest.py`、`backend/tests/test_profile_service.py`、`backend/tests/test_jd_ingest.py`、`backend/tests/test_scoring.py`、`backend/tests/test_dashboard.py`

---
