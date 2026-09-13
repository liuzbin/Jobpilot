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
## Phase 2：画像深化（追问式访谈 + 关键词三元组）+ K 值驱动的简历重制与技能延伸确认（2026-09-08 ~ 2026-09-10）

### 目标

在写代码之前先和用户完整讨论清楚了这个 Phase 要解决的真实问题：单薄的简历 bullet 撑不起"既贴合实际又贴合 JD"的简历重制，需要更深的项目背景输入，也需要让 AI 能识别"JD 要求的技能和用户真实项目技术背景高度相关、值得作为可主动突击学习的延伸"这种情况。讨论中用户明确了一条本 Phase 必须落地、且要写进实施方案文档的核心价值主张：**JobPilot 不是单纯的效率工具，而是帮用户看清并补齐自己能力相对市场需求短板的工具**——这条价值主张直接决定了本 Phase 几乎每一个设计取舍：延伸建议必须给理由、必须用户逐条确认才能进简历、且延伸建议永远不能反向影响客观打分（打分脏了，用户就没法准确看清自己真实的差距）。

按用户要求的顺序，本 Phase 先把《JobPilot 实施方案》文档更新到 v2（新增"核心价值主张"章节，补全 Phase 2 详细设计），提交推送到 GitHub 之后，才开始写 Phase 2 的代码。

### 实现内容

- **数据模型扩展**：`ExperienceEntry` 新增 `background_notes`（AI 整理后的背景描述文本）、`background_qa`（原始问答历史，JSON 数组，保留审计追溯）；`ExperienceBullet` 新增 `keywords`/`action_summary`/`result_summary` 三个字段，对应"关键词 + 行为 + 结果"三元组，`content` 原文永远保持不变作为唯一真值，三元组是可重新计算的派生索引。新增 `ClaimedSkill` 表（技能名、所属经历、行为/结果摘要、生成理由、来源 JD、创建/更新时间），作为跨 JD 复用的"已确认、现在真的要去学"技能清单。迁移脚本 `3eea4bce52ea_phase2_profile_depth_and_claimed_skill.py` 用 `alembic revision --autogenerate` 生成后逐项核对确认与模型改动完全对应。
- **画像深化服务**（`app/services/profile_deepening.py`）：`extract_bullet_triads()` 把一批 bullet 原文交给轻量模型批量抽取三元组，对模型返回数量少于输入的情况做防御性补空，保证顺序和数量始终和输入对齐；`backfill_bullet_triads()` 只处理 `keywords is None` 的 bullet（按经历分组批量调用），避免重复抽取浪费调用次数；`generate_background_questions()` 根据已有的职位/项目/bullet 内容生成针对性追问，而不是甩给用户一个空白大文本框；`merge_background_answers()` 把多轮问答整合成连贯的背景描述文本（空白答案自动跳过、本轮全部跳过则不调用模型），原始问答历史累积保留在 `background_qa` 里。
- **简历重制服务**（`app/services/resume_tailor.py`，本 Phase 的核心）：`collect_jd_keywords()` / `find_missing_keywords()` / `find_hit_bullets()` 找出 JD 要求的技能里哪些在用户真实经历里有直接命中、哪些完全缺失；`select_keywords_to_extend()` 是纯函数，按 `ceil(缺失技能数 × K / 10)` 确定性地从缺失技能里选出本次要生成延伸建议的数量，K 值越大延伸范围越大；`rewrite_hit_bullets()` 把命中的真实 bullet 按三元组重新组织表达，不编造内容；`generate_extension_suggestions()` 调重量模型，对每个候选缺失技能要求模型给出"是否技术背景上站得住脚（plausible）+ 具体理由 + 对应哪段真实经历 + 校准过的行为和结果摘要"，不合理（`plausible=false`）或者经历下标非法的建议会被直接过滤掉，不会展示给用户。`build_resume_draft()` 把上面这些串成一份草稿；`confirm_and_finalize()` 是唯一的写入口——只有用户在页面上逐条勾选确认过的延伸建议才会真正进入生成的简历内容，同时 upsert 进 `ClaimedSkill` 表（按技能名+经历去重，不重复插入）。整个 `resume_tailor.py` 模块开头用文档字符串明确写死一条约束：这个模块产出的任何东西都不会被 `compute_score()` 读取。
- **Dashboard 界面**：新增"经历详情"页（展示 bullet 的关键词标签、背景描述、历史问答，"深化这段经历"入口）、"追问式访谈"页（渲染生成的问题列表，大文本框自由作答）、"简历草稿"页（K 值输入，只读的命中项展示，每条延伸建议一个"勾选是否采纳 + 可编辑的关键词/行为/结果/理由"区块）、"简历结果"页（Markdown 预览 + 结构化 JSON）。草稿页的状态传递没有用 session，走的是无状态服务端渲染的老套路：GET 计算草稿、把命中项打包成一个隐藏的 JSON 字段和一组按下标编号的延伸建议字段一起渲染出来，POST 确认时用 `await request.form()` 手动解析这些变长的按下标字段（不用 FastAPI 的 `Form(...)` 类型参数，因为延伸建议条数是动态的）。

### 设计取舍（有意简化，供后续 Phase 参考）

1. **画像深化访谈是手动触发，不是上传简历后自动弹出**：用户在讨论里认可了这个推荐方案——追问式访谈需要用户投入认真作答的精力，自动弹出容易打断用户本来想做的"先看看画像对不对"的流程，改成经历详情页里一个明确的"深化这段经历"按钮，用户自己决定什么时候投入时间来做这件事。
2. **关键词三元组只在"没有的时候"才抽取，抽取过和 bullet 原文本身修改后都不做自动失效重算**：本 Phase 先解决"从无到有"，如果用户后续手动编辑了 bullet 原文，三元组要不要跟着自动重新抽取，这类"编辑联动"逻辑留给后续 Phase（目前也还没有 bullet 编辑功能）。
3. **`ClaimedSkill` 和打分引擎之间是单向且永久的隔断，不是"可以配置的开关"**：这条不是性能或者复杂度上的简化，而是直接对应用户强调的核心价值——如果延伸建议、哪怕是用户已经确认要去学的技能，能够反过来影响 `compute_score()` 的客观打分，用户就没法再通过这个分数看清自己和 JD 真实要求之间的差距，"诚实地暴露短板"这条核心价值就会被破坏。所以特意没有做成一个"要不要把已认领技能计入打分"的可配置项，写了一条独立的回归测试（`test_claimed_skill_does_not_affect_scoring`）钉死这个边界。

### 遇到的问题与解决

**问题一：`FakeLLMClient` 按调用顺序弹出预设响应，和测试 fixture 复用之间产生了一次隐蔽的对不上号**

现象：新写的 `test_tailor_draft_and_confirm_full_flow`（`backend/tests/test_dashboard.py`）跑起来时，`assert "Spark" in r.text` 失败——草稿页命中项和延伸建议都是空的。

原因是三层叠在一起的：(1) 这条测试最初复用了已有的 `_seed_jd_with_score(client)` 辅助函数，它会先跑一次 `POST /jobs/{id}/analyze`，用共享的 `FAKE_JD_EXTRACTION_RESPONSE`（`key_skills=["Python","SQL"]`）把结果缓存进了 `jd.parsed_meta`；而 `build_resume_draft()` 为了避免重复解析同一份 JD，有一段"`parsed_meta` 里已经有 `key_skills` 就直接复用缓存，不再调用 `structure_jd_text()`"的逻辑——这条缓存判断悄悄短路掉了测试自己准备的、`key_skills=["Hadoop","Spark","RAG"]` 的 `jd_parse_fake`，实际生效的一直是 Python/SQL，和任何真实 bullet 都不命中，也不缺失任何"该延伸"的技能。(2) 因为没有命中的 bullet，`rewrite_hit_bullets([])` 直接空返回、根本没消费掉排在队列前面那条"重写后 bullet"形状的预设响应；(3) `FakeLLMClient` 的语义是"不管是哪个 prompt 触发的调用，一律按顺序弹队列里下一条"，于是这条本该给重写步骤用的响应，被紧随其后的 `generate_extension_suggestions()` 调用错误地当成了自己的响应消费掉，取出来的 JSON 形状对不上，`result.get("suggestions")` 拿到的是 `None`。排查过程中还确认了一个此前的误判：`_seed_position_via_upload` 辅助函数灌进去的是"Payments"主题的简历内容（Acme Corp / Backend Engineer / Payments，bullet 是"支付服务"和"延迟降低 30%"），并不是我最初以为的、混进来自某个早期独立冒烟测试的 Hadoop/ETL 内容。

解决：把这条测试改成不经过 `_seed_jd_with_score`，直接 `POST /dashboard/jobs` 建一条全新的 JD 记录（跳过预先分析这一步），保证 `build_resume_draft()` 一定会自己调用 `structure_jd_text()`、吃到测试准备的 `jd_parse_fake`；同时把 `jd_parse_fake` 的 `key_skills` 故意设成和已灌入的 Payments bullet 毫无关联的 `["Spark", "RAG"]`，让"零命中"这个前提是设计出来的，而不是依赖 bullet 关键词匹配的偶然结果；GET 请求的 `k` 参数从 5 改成 10，保证两个缺失技能都进入延伸建议的候选池；重量模型的预设响应从"重写 + 延伸"两条精简成"延伸"一条，因为零命中的情况下 `rewrite_hit_bullets` 根本不会触发模型调用。修完之后完整跑了一遍全部用例确认转绿，也顺带把这条踩坑记录下来，提醒以后写涉及 `FakeLLMClient` 的测试时，任何"缓存/短路"逻辑都可能让准备好的预设响应从未被真正消费，进而错位到后面的调用上——这类问题只看"最终响应内容对不对"很难发现，得回头看"到底是不是预期的那次调用消费了它"。

### 验证结果

- 新增 2 个 pytest 文件（`test_profile_deepening.py` 9 条、`test_resume_tailor.py` 18 条），扩展 `test_dashboard.py`（新增 8 条，覆盖经历详情/追问式访谈路由、简历草稿生成与确认全流程、模型未配置时的友好提示、访问不存在的简历版本返回 404），加上 Phase 0/1 遗留的 56 条，`backend` 目录下共 **93 条 pytest 用例全部通过**。
- 专门写了 `test_claimed_skill_does_not_affect_scoring` 回归测试：用完全相同的打分相关预设输入，对比"存在一条已确认的 `ClaimedSkill`"和"不存在"两种情况下 `compute_score()` 的输出，确认分数完全一致，钉死"已认领技能永不影响客观打分"这条对应核心价值主张的硬约束。
- 延伸建议确认流程有专门测试（`test_tailor_confirm_rejects_unaccepted_suggestions`）验证"AI 提议的东西不会未经确认就悄悄写进简历"。
- 先在云端沙盒环境跑通全部 93 条用例，再把新增/修改的 15 个文件打包同步到用户本机项目目录（`C:\liuzhibin\aI-agent\jobpilot`），逐文件核对 SHA-256 校验和确认同步无损坏，在设备桥接的 Linux 执行环境里（复用 Phase 1 收尾时建好的独立 Linux 虚拟环境）重新装一遍依赖、重新跑一遍全部 93 条用例，结果一致。数据库结构升级走的是本地 App 启动时自动执行的 `run_migrations()`（`app/core/migrate.py`），本 Phase 新增的迁移会在用户下次启动本地 App 时自动应用，不需要手动跑 alembic 命令。

### 涉及文件

`docs/JobPilot_实施方案.md`（v2，新增"核心价值主张"章节 + Phase 2 详细设计）、`backend/app/models/tables.py`、`backend/alembic/versions/3eea4bce52ea_phase2_profile_depth_and_claimed_skill.py`、`backend/app/services/profile_deepening.py`（新增）、`backend/app/services/resume_tailor.py`（新增）、`backend/app/api/routes_dashboard.py`、`backend/app/templates/position_detail.html`（新增）、`backend/app/templates/position_interview.html`（新增）、`backend/app/templates/resume_tailor.html`（新增）、`backend/app/templates/resume_result.html`（新增）、`backend/app/templates/base.html`、`backend/app/templates/profile.html`、`backend/app/templates/job_detail.html`、`backend/tests/test_profile_deepening.py`（新增）、`backend/tests/test_resume_tailor.py`（新增）、`backend/tests/test_dashboard.py`

---

## Phase 2 补完：事实字段护栏校验、PDF 渲染、qa_bank 问答收集、工作经历树增删改（2026-09-11 ~ 2026-09-12）

### 目标

进入 Phase 3 之前，按用户"每完成一个 Phase 就停下来汇报"的要求做了一次收尾自查：对照《JobPilot 实施方案》第八节 Phase 2 清单逐项核对代码，发现虽然访谈式画像深化、bullet 三元组抽取、K 值驱动的简历重制主链路都已经落地并通过了测试，但清单里另外 4 项实际上没有真正做完——之前把"核心链路跑通"当成了"Phase 2 完成"来汇报，是不准确的：

1. 事实字段护栏校验完全缺失——这是 v1 就定好、文档里写明"Phase 2 不做改动"的硬约束，但代码里根本没有这一步。
2. PDF 渲染（WeasyPrint）在代码里被显式跳过，只有 Markdown/JSON 两个产物。
3. `qa_bank` 表从 Phase 0 就建好了，但没有任何写入/收集流程。
4. 工作经历树的增删改交互仍然是 Phase 1 的自动合并简化版，没有升级成文档里说的交互式合并/替换。

把这 4 项发现和处理方案交给用户选择后，用户明确要求"先补完全部 4 项，再进 Phase 3"，所以本次收尾按这个顺序做：事实字段护栏校验 → PDF 渲染 → qa_bank 收集流程 → 工作经历树增删改 + 合并冲突确认，每做完一项跑一遍全量测试确认零回归，全部完成后统一收尾汇报。

### 实现内容

- **事实字段护栏校验**（`backend/app/services/resume_tailor.py`）：两层校验，规则层用正则 `_COMPANY_SUFFIX_PATTERN` 扫描生成文本里形如"XX科技/集团/有限公司/Inc./LLC"的公司名模式，命中but不在已知公司名单里的判定为违规；模型层把一批命中项/延伸建议的文本批量丢给轻量模型做语义一致性判断（复用 `extract_bullet_triads` 已经验证过的"一次请求批量处理多条"模式，避免每条都单独调用）。两种违规来源合并后按统一策略处理：命中项（`hit_items`，对应用户真实经历改写出来的内容）校验失败会先重试一次改写，仍失败则直接回退成真实原文——因为真实原文本身必然能通过校验，这是"最坏情况下也不会比不做校验更差"的兜底；延伸建议（`suggestions`，凭 JD 关键词推出来的技能延伸）校验失败会重试一次生成，仍失败则整条丢弃、不展示给用户，因为延伸建议没有"真实原文"可以兜底。新增 9 条测试，包括自测方案里明确要求的"故意在生成结果里注入一个不存在的公司名，断言护栏校验能拦截"这条场景，覆盖回退成功、回退失败降级为原文、延伸建议两次失败后被丢弃三条路径。
- **PDF 渲染**（`backend/app/services/resume_pdf.py` + `backend/app/templates/resume_styles/default.html`，新增）：用 Jinja2 把结构化简历 JSON 渲染成独立的 HTML（不继承 Dashboard 的 `base.html`，是完全独立的一套排版），再用 WeasyPrint 转成 PDF 落盘到 `~/.jobpilot/resumes/resume_{version_id}.pdf`，`confirm_and_finalize()` 在写入 `resume_version` 记录后顺带生成 PDF、把路径存回 `pdf_path` 字段；PDF 是附加产物，渲染失败会被捕获并记日志，不会拖垮整个确认流程（简历的 Markdown/JSON 主产物不受影响）。`style_id` 和模板文件一一对应，为后续 Phase 5 加更多风格模板留了口子。Dashboard 简历结果页新增 PDF 下载链接，新增下载路由。测试用 `HTML(string=...).render().pages` 直接拿 WeasyPrint 自己的分页结果做断言（不依赖额外的 PDF 解析库），验证短简历是 1 页、故意撑长的简历能正确分页成 3 页而不是内容重叠或被截断。
- **qa_bank 问答收集流程**（`backend/app/services/qa_bank_service.py`，新增）：支持两种录入方式——手动直接填问题+答案；或者基于画像内容（目标职位、工作经历摘要）让轻量模型建议一批常见面试/申请问题，用户逐条选择要不要现在就填答案、跳过的不写入。写入时按问题文本归一化后去重（同一个问题多次回答按最新的覆盖，不是重复插入）。Dashboard 新增题库列表页（增删）和"生成常见问题建议"页。
- **工作经历树增删改 + 合并冲突确认**（`backend/app/services/profile_service.py`）：新增公司/职位/贡献句的手动增删改接口（`add_company`/`delete_company`/`add_position`/`update_position_fields`/`delete_position`/`add_bullet`/`update_bullet_content`/`delete_bullet`），级联删除依赖 `ExperienceEntry` 已有的 `cascade="all, delete-orphan"` 关系，删公司连带删掉底下所有职位和贡献句。编辑贡献句原文时会把已抽取的三元组字段清空，交给下次上传/深化时的 `backfill_bullet_triads` 惰性重新抽取，不做同步的 LLM 调用。合并冲突检测：新贡献句和同一职位下已有贡献句用 `difflib.SequenceMatcher` 算文本相似度，≥0.82 判定为"像是同一件事的不同措辞"，不再自动写入，而是收集成 `bullet_conflicts` 列表；职位的起止时间、是否在职字段如果新旧值都非空且不一致，收集成 `position_field_conflicts`；两种情况都遵循"空白字段自动填补不算冲突，只有双方都有值且不一致才算冲突"的原则（沿用 Phase 1 定下的 `fill_blank_profile_basic_fields` 设计哲学）。上传简历时如果产生了冲突，路由不再直接跳转回画像页，而是渲染一个"确认合并冲突"页面，用隐藏字段把每条冲突需要的上下文（职位 ID、已有贡献句 ID、新旧文本/字段值）编好下标传下去，用户逐条选完提交到统一的 resolve 接口应用。

### 设计取舍（有意简化，供后续 Phase 参考）

1. **护栏校验的重试策略是"重试一次就降级/丢弃"，不是无限重试**：无限重试在模型持续给出不一致结果时会让用户等待时间不可控，"重试一次仍失败就用有把握的兜底方案"在体验和正确性之间是更稳的选择，尤其是命中项永远有真实原文可以兜底，风险很低。
2. **PDF 渲染失败不影响主流程，只记日志**：PDF 是简历的第三种呈现形式（Markdown/JSON 是主产物，Dashboard 上直接能看到），本地环境的字体、依赖库版本可能有差异导致渲染偶发失败，不应该让这种边缘情况挡住用户拿到 Markdown/JSON 结果。
3. **qa_bank 的问题去重按归一化文本，不做语义级别的相似问题合并**：语义相似度合并需要 embedding，而 embedding 检索是文档里明确写给 Phase 4（自动化填表）用的能力，这里先用最简单的文本归一化去重把"重复点两次生成建议"这种最常见的重复场景挡住，语义级别的合并留给 Phase 4 一起做。
4. **贡献句相似度阈值 0.82 是经验值，不是从数据集调出来的**：选择的依据是"要能挡住换了几个词但说的是同一件事的重复表达，同时不能把两件真正不同的事误判成冲突"，目前用有限的几个测试用例验证了这个直觉成立，后续如果真实使用中发现阈值偏松或偏紧，是一个容易独立调整的参数，不涉及架构改动。

### 遇到的问题与解决

**问题一：`_batch_validate_facts` 从"每条单独校验"改成"批量校验"时，丢了原有的空文本提前返回逻辑**

现象：`test_validate_item_facts_skips_llm_call_for_blank_text` 测试失败——批量重构之后，即使一批文本全是空的，代码还是会尝试调用一次轻量模型。

原因：单条校验版本里"文本为空就不调用模型直接返回空结果"这条判断，在改造成批量接口时被漏掉了，批量版本原来的逻辑是"只要 texts 列表非空就调用"，没有考虑到列表非空但每一项内容都是空字符串的情况。

解决：在批量函数里补回这条判断，用 `if not any(texts): return rule_violations` 在真正发起模型调用之前拦一道，任何一条文本非空才会真的调用模型。

**问题二：给 `build_resume_draft` 加上护栏校验之后，两处已有测试的 `FakeLLMClient` 响应队列对不上号了**

现象：`test_build_resume_draft_k0_produces_no_suggestions`（`test_resume_tailor.py`）和 `test_tailor_draft_and_confirm_full_flow`（`test_dashboard.py`）都开始失败，报的是"响应队列已耗尽"或者拿到了形状不对的 JSON。

原因：`build_resume_draft` 内部新增了一次护栏校验调用，`FakeLLMClient` 是按调用顺序严格弹出预设响应的，新增的这次调用会插到原来两条测试准备好的响应序列中间，把后面的调用全部错位。

解决：给这两条测试各自的 `light_client` 响应队列里补一条 `{"results": [{"consistent": True}]}`，位置对应护栏校验实际发生的那次调用，并在代码里加注释说明为什么多了这一条，避免以后有人看不懂又删掉。这也是继续验证了此前 Phase 2 主体开发时踩过的同一类坑——`FakeLLMClient` 测试的响应队列必须和实际调用顺序完全对齐，新增任何一次调用都要回头检查所有复用这套响应队列的测试。

**问题三：Jinja2 模板里 `position.items` 被解析成了 dict 的内置 `.items()` 方法**

现象：PDF 模板渲染时报 `TypeError: 'builtin_function_or_method' object is not iterable`。

原因：传给模板的 `position` 是一个普通 dict，其中有一个键就叫 `items`（存这段职位下的贡献句列表）；Jinja2 的属性访问语法 `position.items` 在 dict 上会优先解析成 Python dict 自带的 `.items()` 方法，而不是去找字典里名为 `items` 的键，这是 Jinja2 属性访问对 dict 的一个通用行为（不限于这个字段名，任何撞上 dict 方法名的键都会有同样的问题）。

解决：改用下标访问 `position['items']`，绕开 Jinja2 的属性优先解析规则。记录下来提醒以后设计传给模板的数据结构时，尽量避免用 `items`/`keys`/`values` 这类和 dict 内置方法同名的键。

**问题四：`qa_bank` 按创建时间排序在同一秒内创建的多条记录时顺序不稳定**

现象：`test_list_qa_entries_orders_newest_first` 间歇性失败——本地测试环境跑得快，两条记录经常落在同一秒内创建。

原因：SQLite 的 `CURRENT_TIMESTAMP`/`func.now()` 精度只到秒级，同一秒内插入的多条记录 `created_at` 值完全相同，按这个字段排序时数据库不保证稳定顺序。

解决：改成按自增主键 `id.desc()` 排序——插入顺序和 `id` 递增顺序天然一致，比依赖时间戳精度可靠。这个坑记下来，以后任何"按插入顺序展示"的需求都优先考虑用自增 ID 排序而不是时间戳排序。

**观察到但暂不处理：全量测试跑起来比 Phase 2 之前明显变慢**

给 `confirm_and_finalize` 加上 WeasyPrint PDF 生成之后，全量测试从原来的几十秒涨到了 70-80 秒左右（很多测试路径会间接触发一次简历确认，也就间接触发一次 PDF 渲染），原因是字体加载和排版计算这类工作本身有固定开销，不是哪里写得低效。因为 PDF 渲染在真实使用场景里是用户点一次"确认"按钮才触发一次的低频操作，不是任何请求路径上的热点，所以这里选择先记录下这个性能特征、不做优化，如果后续 Phase 测试套件跑起来的耗时变成明显的开发体验问题，再回头考虑给测试单独配置跳过真实 PDF 渲染的选项。

### 验证结果

- 新增/扩展 4 个 pytest 文件：`test_resume_tailor.py` 新增 9 条护栏校验用例，新增 `test_resume_pdf.py`（6 条，含分页稳定性验证）、`test_qa_bank_service.py`（新增，10 条）、`test_profile_service.py` 新增约 25 条（冲突检测、冲突确认、CRUD 全覆盖），`test_dashboard.py` 新增 5 条端到端用例（合并冲突确认全流程、CRUD 路由全流程），加上 Phase 0/1/2 主体遗留的用例，`backend` 目录下共 **152 条 pytest 用例全部通过**，零回归。
- 先在云端沙盒环境跑通全部 152 条用例，再把新增/修改的 19 个文件同步到用户本机项目目录（`C:\liuzhibin\aI-agent\jobpilot`），逐文件核对 SHA-256 校验和确认全部 54 个相关文件（含未改动的）同步后完全一致，在设备侧独立 Linux 虚拟环境（`~/jobpilot_venv`，沿用 Phase 1/2 建立的隔离测试环境）里装上新增的 `weasyprint==70.0` 依赖、重新跑一遍全部 152 条用例，结果一致。

### 涉及文件

`docs/JobPilot_实施方案.md`（v3，第八节 Phase 2 清单全部标记完成 + 第六节合并交互决策记录更新）、`backend/requirements.txt`（新增 `weasyprint==70.0`）、`backend/app/services/resume_tailor.py`、`backend/app/services/resume_pdf.py`（新增）、`backend/app/services/qa_bank_service.py`（新增）、`backend/app/services/profile_service.py`、`backend/app/api/routes_dashboard.py`、`backend/app/templates/resume_styles/default.html`（新增）、`backend/app/templates/qa_bank.html`（新增）、`backend/app/templates/qa_bank_suggest.html`（新增）、`backend/app/templates/merge_conflicts.html`（新增）、`backend/app/templates/base.html`、`backend/app/templates/profile.html`、`backend/app/templates/position_detail.html`、`backend/app/templates/resume_result.html`、`backend/tests/test_resume_tailor.py`、`backend/tests/test_resume_pdf.py`（新增）、`backend/tests/test_qa_bank_service.py`（新增）、`backend/tests/test_profile_service.py`、`backend/tests/test_dashboard.py`

---

## Phase 3：插件抓取 LinkedIn 职位（2026-09-12）

### 目标

把 Phase 1 就设计好的插件配对鉴权基座真正用起来：在插件里实现 LinkedIn 职位详情页的解析，让用户不用再手动复制粘贴 JD 全文，一键就能把当前浏览的职位发进本地 App，走 Phase 1 已经跑通的入库/打分链路。按实施方案 v1 版本对这个 Phase 定的自测要求，重点不是"能不能抓到"，而是"抓不到的时候有没有老实认怂"——LinkedIn 的页面结构完全不受我们控制，随时可能改版，抓取逻辑必须经得起"选择器突然失效"这种情况，不能在识别失败时拿错误数据糊弄用户。

### 实现内容

- **后端新增插件专用接口 `POST /api/jobs`**（`backend/app/api/routes_extension.py`，新增路由文件）：复用 `require_paired_request` 这套 Phase 0 就定好的配对鉴权（校验 `X-JobPilot-Token` + Origin 必须是 `chrome-extension://` 开头），请求体校验后直接调用 Phase 1 的 `jd_ingest.create_jd` 入库，和 Dashboard 手动粘贴 JD 走的是完全同一条入库逻辑（同一个函数），新入库的 JD 仍然停在 `pending` 状态——插件这一步明确只负责"把 JD 送进来"，是否要花 LLM 配额去分析，仍然由用户在 Dashboard 里手动点"分析"决定，插件不越权替用户触发。描述为空（页面识别彻底失败）时返回 422，不允许塞一条空记录进库。响应体包含 `dashboard_url`，方便插件直接打开对应的详情页。
- **LinkedIn 抓取纯函数**（`extension/content_scripts/linkedin_parser.js`，新增）：`extractLinkedInJob(doc, locationHref)` 只读 DOM、不发网络请求，职位标题/公司名/地点+附加信息行/正文四类字段各自准备了 4-5 套按优先级排列的选择器（覆盖观察到的"新版详情页 `job-details-jobs-unified-top-card__*`"、"稍旧版 `jobs-unified-top-card__*`"、"更旧的公开职位页 `topcard__*`"三种常见布局），全部选择器都找不到就返回 `null`，绝不用错误的兜底值冒充真实数据。地点和附加信息行（"Toronto, ON · 2 weeks ago · 80 people clicked apply"这类）来自同一处 DOM，原文整行保留下来交给后端已有的 `parse_extra_meta_rules` 做规则解析。因为是纯函数、不依赖任何 `chrome.*` API，可以完全脱离真实浏览器用 jsdom 做单元测试。
- **内容脚本注入悬浮按钮**（`extension/content_scripts/linkedin.js`，新增）：`manifest.json` 新增 `content_scripts` 声明，匹配 `https://www.linkedin.com/jobs/*`，在页面右下角注入一个"发送到 JobPilot"悬浮按钮。点击时才调用 `extractLinkedInJob` 读取当前 DOM（不在脚本加载时就抓一次缓存起来）——LinkedIn 的职位列表页是单页应用，切换职位卡片通常不会触发页面刷新、也就不会重新执行内容脚本，把抓取放在点击那一刻，保证不管用户浏览过多少个职位，点击时读到的永远是当前这个职位的最新 DOM。按钮本身只做 DOM 读取和消息转发，不直接发网络请求（原因见下面"设计取舍"）。
- **background 转发抓取结果**（`extension/background/service_worker.js`）：新增 `jobpilot:send-job` 消息处理，用已经存好的配对 token 发起 `POST /api/jobs`，成功后 `chrome.tabs.create` 自动打开返回的 `dashboard_url`；对"还没配对"“连不上本地 App”“本地 App 返回错误”三种失败原因分别给出不同的提示文案，回传给内容脚本更新按钮状态，而不是一个笼统的"失败"。
- **侧边栏新增功能提示卡片**：只在"已连接"状态下显示，告诉用户"打开 LinkedIn 职位页会有一个按钮"，避免这个功能完全隐藏在用户不知道的地方。

### 设计取舍（有意简化，供后续 Phase 参考）

1. **抓取网络请求必须在 background 发起，不能在内容脚本里直接 fetch**：这不是新加的限制，是 Phase 0 配对鉴权设计（Origin 必须是 `chrome-extension://` 开头）的直接推论——内容脚本运行在页面自己的执行上下文里，发起的请求 Origin 头是 `https://www.linkedin.com`，天然过不了后端的 Origin 校验。这条记录进了第六节关键设计决策表，避免以后有人为了图方便在内容脚本里直接发请求，排查半天才发现是 Origin 被拒绝。
2. **插件只负责"送 JD 进来"，不自动触发分析**：一键发送后停在 `pending` 状态、跳转到 Dashboard 详情页，由用户自己点"分析"，而不是插件自动帮用户消耗一次 LLM 调用。这样即使用户还没配置好模型、或者只是想先看看抓取的内容对不对，也不会有一次"意外"的分析请求被发出去。
3. **按钮点击时才抓取，不做页面加载时的自动抓取或缓存**：一方面避免用户还没决定要不要发送、页面就已经做了一次没必要的抓取；另一方面天然解决了"LinkedIn 单页应用切换职位不重新执行内容脚本"这个问题，不需要额外写 SPA 路由监听逻辑。
4. **按钮的兜底重新注入用低频 `setInterval` 而不是 `MutationObserver`**：LinkedIn 页面本身会频繁增删 DOM 节点，监听整个 `body` 子树变化的 `MutationObserver` 在这种页面上开销不小，而这个按钮并不需要对页面变化做到毫秒级响应，用一个 3 秒一次的轻量检查换取更简单、更不容易引入额外性能问题的实现。
5. **选择器兜底策略是"抓不到就留空"而不是"抓不到就用页面标题之类的模糊替代"**：任何"看起来大概率对但不保证对"的兜底都不采用——比如公司名找不到时，不会退而求其次去猜测页面 `<title>` 里 `|` 分隔的某一段是公司名，因为一旦猜错，用户很容易信以为真直接拿去分析，比"明确留空、用户自己看到缺了什么去手动补"更糟。

### 遇到的问题与解决

**问题一：设计阶段就排除掉的一个方案——差点让内容脚本直接发起网络请求**

现象：写第一版设计时，最直接的想法是内容脚本抓完数据直接 `fetch(...)` 发给本地 App，逻辑上一步到位。

原因：没有立刻意识到内容脚本的请求 Origin 是页面自己的域名，会被后端 `require_paired_request` 的 Origin 校验拒绝——这条校验规则是 Phase 0 就写好的，但当时的场景只有 background 发起 WebSocket 连接，没有人在内容脚本里发过 REST 请求，这个隐含约束没有被显式记录下来。

解决：写代码之前先对照 `app/api/deps.py` 里 `_origin_allowed` 的实现过了一遍，确认了这一点，把网络请求这一步设计成"内容脚本只读 DOM、消息转发给 background，由 background 发起请求"，从一开始就避免了这个问题，没有走弯路重写。这个坑本身没有花时间踩，但值得记下来、写进第六节的决策表，防止以后加新功能时重新掉进去。

**问题二：第一版 fixture 里 "topcard__flavor-row" 元信息行没有真实的分隔符文本**

现象：写 `layout_public_topcard.html` 这份测试 fixture 时，最初用三个相邻的 `<span>` 标签分别装"Remote"、"1 week ago"、"25 people clicked apply"，中间没有任何分隔文本，`extractLinkedInJob` 解析出来的 `location` 字段是整行拼起来的文本，而不是期望的"Remote"。

原因：`_extractLocation` 是按 `·`/`•` 这类中点符号切分文本取第一段，这几个 `<span>` 之间在真实 LinkedIn 页面上是有一个字面的"·"分隔符渲染出来的，但我第一版手写的 fixture HTML 里漏掉了这个细节，导致测试用的模拟数据本身就不够真实，而不是解析逻辑有 bug。

解决：在 fixture 里把分隔符明确写成文本节点 `<span>Remote</span> · <span>1 week ago</span> · <span>25 people clicked apply</span>`，让它更贴近真实页面的渲染结果，测试才转绿。这条也提醒自己：写模拟 HTML fixture 时，容易只关注"该出现的文字都出现了"，却忽略了文字之间的分隔符/标点这类容易被解析逻辑依赖到的细节。

### 验证结果

- 后端新增 `tests/test_routes_extension.py`（6 条：鉴权拦截三种场景、正常入库并能在 Dashboard 详情页查到、空描述被拒绝、只传必填字段时其余字段保持 `None`），加上此前 Phase 0-2 遗留的用例，`backend` 目录下共 **158 条 pytest 用例全部通过**，零回归。
- 插件侧新增 `extension/content_scripts/linkedin_parser.test.js`（5 条，用 jsdom 加载新版/稍旧版/更旧公开职位页/完全无法识别布局四种模拟 LinkedIn 页面 HTML fixture，覆盖正常提取和字段缺失兜底两类场景），跑法是 `cd extension && npm install && npm test`，不需要真实 Chrome、不需要真实 LinkedIn 账号。
- 新增 `extension/scripts/check_permissions.js` 静态权限检查复跑一遍确认通过（`manifest.json` 新增了 `content_scripts` 和 `host_permissions`，没有引入需要额外声明却漏掉的 `chrome.*` API）。
- 先在云端沙盒环境跑通全部后端 pytest 和插件侧 Node 测试，再把改动同步到用户本机项目目录（`C:\liuzhibin\aI-agent\jobpilot`），逐文件 SHA-256 校验和确认同步无损坏，在设备侧独立 Linux 虚拟环境（`~/jobpilot_venv`）里重新跑一遍全部 pytest 用例，结果一致。真实 Chrome 加载插件、访问真实 LinkedIn 职位页点击"发送到 JobPilot"按钮这部分，云端沙盒没有图形界面、也没有 LinkedIn 账号，无法代为完成，验收清单写在 `README.md` 第五节，需要用户在自己电脑上手动过一遍。

### 涉及文件

`docs/JobPilot_实施方案.md`（v4，第八节 Phase 3 标记完成 + 第六节新增插件网络请求发起层的决策记录）、`README.md`（新增第五节：LinkedIn 抓取的测试方式和手动验收清单）、`backend/app/api/routes_extension.py`（新增）、`backend/app/main.py`（注册新路由）、`backend/tests/test_routes_extension.py`（新增）、`extension/manifest.json`（新增 `content_scripts` + `host_permissions`）、`extension/content_scripts/linkedin_parser.js`（新增）、`extension/content_scripts/linkedin.js`（新增）、`extension/content_scripts/linkedin_parser.test.js`（新增）、`extension/content_scripts/__fixtures__/`（新增，4 份模拟页面布局）、`extension/background/service_worker.js`、`extension/sidepanel/sidepanel.html`、`extension/sidepanel/sidepanel.js`、`extension/package.json`（新增，管理 jsdom 开发依赖）、`extension/package-lock.json`（新增）

---

## Phase 4：自动化填表（2026-09-12）

### 目标

在 Phase 3"插件把 JD 抓进来"的基础上，补上投递流程的另一端："插件把画像数据填进投递表单里"。编码开始前先针对实施方案 v1 遗留的两个开放问题（题库相似度检索用什么算法、提交按钮监听要不要联动 JD 状态推进）分别和用户确认了具体方案，再动手实现，避免把一个还没想清楚的设计直接写成代码。这个 Phase 比前几个 Phase 多了一层此前没有的风险：一旦自动填表把"工作授权""EEO 自报"这类涉及法律声明性质的字段填错，后果比"抓错一个职位标题"严重得多，所以专门为此设计了独立于字段映射结果之外的第二层防御。

### 实现内容

- **填表决策服务** `backend/app/services/autofill.py`（新增）：`build_autofill_plan(db, fields, light_client, jd_id)` 是整个 Phase 4 的决策核心，输入插件扫描到的一批表单字段（标签、控件类型、可选项），输出每个字段该填什么、要不要填。字段标签到画像字段的映射先走关键词直接匹配（覆盖姓名/邮箱/电话/领英/GitHub/学校/现居地等最常见字段，不消耗模型调用），匹配不上的标签批量丢给轻量模型做一次映射（同一批未命中字段合并成一次 `complete_json` 调用，沿用 Phase 2 起的批量调用惯例）。**合规安全限制**：选择类控件（select/radio/checkbox）只允许 `current_location`/`target_location` 两个客观字段通过白名单自动选择，同时叠加一层独立于字段映射结果的标签关键词兜底扫描（`_looks_like_compliance_label`，覆盖工作授权/签证/EEO/背景调查等中英文关键词），命中即强制跳过，两层防御任一触发就跳过，不依赖 LLM 输出是否"恰好没出错"。纯文本框转述用户自己的 `work_authorization` 画像字段不受这条限制。找不到画像字段映射、又被判断为主观问答题的字段，调用 `qa_bank_service.find_similar_answer` 检索题库。
- **题库本地相似度检索** `backend/app/services/qa_similarity.py`（新增）：字符 2/3-gram 特征哈希方案，用 `zlib.crc32`（标准库、跨进程确定性，区别于 Python 内置 `hash()` 的进程级随机化）把每个 n-gram 映射进一个 512 维向量的某个桶，L2 归一化后向量点积即为余弦相似度。不依赖任何模型 API，不需要新增模型配置字段，选字符 n-gram 而不是经典 TF-IDF 是因为后者的 IDF 项依赖整个语料库统计量，`qa_bank` 会随用户持续补充问答而变化，字符 n-gram 逐条独立计算不受影响。`qa_bank_service.py` 相应扩展：`add_qa_entry` 写入/更新时顺带计算并落库 embedding，新增 `backfill_qa_embeddings` 给 Phase 2 时代写入的历史空 embedding 记录补算，新增 `find_similar_answer(db, question_text)` 供自动填表调用，相似度低于经验阈值 0.3 时返回"没找到"而不是勉强凑一个不相关的答案。
- **插件专用接口新增四个**（`backend/app/api/routes_extension.py`）：`POST /api/autofill/plan`（调用 `build_autofill_plan`，`light_client` 允许未配置，未配置时只有关键词能命中的字段生效，不因为模型没配就整个功能不可用）、`POST /api/autofill/qa-save`（用户手打完一道题库里没有的问答题后回存，`source=application` 区别于建画像阶段的 `onboarding`）、`GET /api/autofill/resume-pdf/{jd_id}`（取这条 JD 最近一次生成的简历 PDF，供插件注入到 ATS 的简历上传控件）、`POST /api/jobs/{jd_id}/mark-applied`（投递页面提交按钮点击后推进状态）。
- **Dashboard 新增"去投递"/"手动标记为已投递"按钮**（`backend/app/templates/job_detail.html`）：点击"去投递"在新标签页打开 `jd.source_url`，同时用一个 `window.dispatchEvent` 的自定义事件通知 `dashboard_bridge.js` 这个内容脚本把"目标网站 hostname → 这条 JD 的 id"关系存进 `chrome.storage.local`（`background/service_worker.js` 新增 `rememberPendingApplication`/`lookupPendingApplication`，按 hostname 存、30 分钟过期）；"手动标记为已投递"是本次开发过程中发现的一个缺口补的兜底按钮，见下面"遇到的问题与解决"。
- **插件侧 ATS 平台识别与表单扫描**：`extension/content_scripts/form_scanner.js`（新增，通用表单扫描，标签解析优先级链路"`label[for]` → 包裹的 `<label>` → `aria-labelledby` → `aria-label` → `placeholder`"，都找不到就把这个字段排除在结果之外；扫描时给每个字段元素打上 `data-jobpilot-field-id` 属性，供之后回填阶段精确定位回同一个 DOM 元素）、`extension/content_scripts/ats_selectors.js`（新增，`detectAtsPlatform` 按 URL hostname 识别 Workday/Greenhouse/Lever，不可用时退化为扫描 DOM 特征；Workday 用 `data-automation-id` 优先当 field_id，Lever 用 `.application-label` 覆盖标签解析，Greenhouse 通用逻辑已够用）、`extension/content_scripts/ats_autofill.js`（新增，浏览器胶水代码：编排"扫描 → 发给本地 App 算计划 → 用原生 value setter 写回 DOM 并派发 input/change 事件让 React 类框架感知"，题库回存的 blur 提示，简历 PDF 的 `DataTransfer` 注入，提交按钮点击的捕获阶段监听）、`extension/content_scripts/dashboard_bridge.js`（新增，Dashboard 页面到插件存储的桥接）。`manifest.json` 新增 Workday/Greenhouse/Lever 三个 `host_permissions` 和两个 `content_scripts` 条目（ATS 页面一组、Dashboard 桥接一组）。

### 设计取舍（有意简化，供后续 Phase 参考）

1. **决策逻辑和 DOM 操作严格分层，延续 Phase 3 的原则**：`autofill.py` 只产出"这个字段该填什么"的结论，`ats_autofill.js` 只负责忠实执行，不在浏览器胶水代码里掺杂任何判断——这样合规安全限制只需要在一个地方维护和测试（`test_autofill.py`），不用担心插件侧会不会"自己多做了什么"。
2. **合规安全限制做两层独立防御，而不是只信任字段映射结果**：字段级白名单（只有 `current_location`/`target_location` 允许通过选择类控件自动选择）和标签关键词兜底扫描是完全独立的两条判断路径，任一触发都跳过。这是本项目一贯的"不让 AI 静默替用户做出有后果的决定"原则在这个 Phase 的具体体现——宁可多跳过几个本来可能安全的字段，也不能让一次模型判断失误变成一次错误的法律选择。
3. **题库相似度命中后直接回填原文，不再额外调用模型微调措辞**：v1 最初设想里有"轻量模型微调措辞"这一步，落地时去掉了——微调本身有把用户已经确认过的原意改走样的风险，而且省一次模型调用。
4. **ATS 选择器规则库明确是最佳努力，不是精确复刻**：云端开发环境没有真实 Workday/Greenhouse/Lever 账号，规则基于三家平台公开可观察、文档化程度较高的约定（Workday 的 `data-automation-id` 测试钩子、Lever 的 `.application-label` 写法）构造，jsdom 测试验证的是"规则本身的逻辑是对的"，不是"真实线上页面长这样"。这和 Phase 3 对待 LinkedIn 选择器的态度一致，验收清单里专门列了需要用户在真实页面上核对的部分。
5. **提交按钮点击只是"尽力而为"的信号，不代表投递真的成功**：没有办法可靠判断 ATS 后端是否真的接受了这次提交，这里只是捕获阶段监听一次点击动作，用户完全可以在没有点提交按钮的情况下自己把状态改成已投递（新增的手动按钮），也可以在插件误判的情况下自己纠正。

### 遇到的问题与解决

**问题一：`build_autofill_plan` 里用 `list.index()` 反查未命中字段的下标，在两个字段标签完全相同时会串位**

现象：写第一版实现时，未命中关键词匹配的字段先收集进一个 `unresolved` 列表统一批量调用 LLM，映射结果按下标对应回原字段时用了 `unresolved.index(sf)`。`ScannedField` 是一个字段值比较相等的 dataclass（没有显式关掉 `eq`），如果两个字段的标签、控件类型、options 恰好完全相同、只有 `field_id` 不同（测试里故意构造了这种情况来验证），`list.index()` 会返回第一个匹配项的下标，导致后一个字段被错误地映射成前一个字段的结果。

原因：`list.index()` 是按值比较查找，不是按对象身份查找，而 dataclass 默认按字段值实现 `__eq__`，这个坑不写专门的回归测试很容易被漏过。

解决：改成扫描阶段就显式建一份 `field_id -> 下标` 的映射（`unresolved_index_by_field_id`），后续查下标全部走这份映射，不再依赖 `list.index()`。补了一条专门的回归测试 `test_duplicate_labels_with_different_field_ids_resolved_independently`，故意构造两个标签完全相同的字段验证不会串位。

**问题二：`_selectOptions` 过滤下拉框占位选项时，只过滤了空文本，没过滤空 value**

现象：`select` 控件的占位提示项（比如 `<option value="">请选择</option>`）文本不为空（"请选择"这几个字是有内容的），第一版 `_selectOptions` 只按"文本是否为空"过滤，这个占位项就混进了返回的 options 列表里，写单元测试断言 options 列表精确匹配时立刻暴露出来。

解决：改成同时要求 `value !== ""`——几乎所有下拉框的约定都是用空 value 表示"还没选"，这类选项本身不是一个真实可选的答案。

**问题三：`form_scanner.js` 里用 `CSS.escape` 拼接属性选择器，在 Node/jsdom 测试环境下直接抛 `ReferenceError`**

现象：第一版实现里用 `` `label[for="${CSS.escape(el.id)}"]` `` 转义属性选择器里的特殊字符，这段代码本身在真实 Chrome 里没问题（`CSS` 是浏览器全局对象），但用 jsdom 在纯 Node 环境跑单元测试时，`CSS` 这个全局根本不存在，一执行到这行就抛 `ReferenceError: CSS is not defined`，所有测试直接报错退出，不是断言失败而是脚本崩溃。

原因：`CSS.escape` 是浏览器提供的全局，jsdom 模拟的是 DOM API 而不是完整的浏览器全局环境，`window.CSS` 不会自动出现在 Node 的全局作用域里（也没有在测试里显式引入 `jsdom.window` 的属性）。

解决：写一个不依赖浏览器全局的最小转义函数 `_escapeAttrValue`（只转义属性选择器里真正需要转义的引号和反斜杠），替换掉所有 `CSS.escape` 调用。`ats_autofill.js`（只在真实 Chrome 里运行，不会被 Node 测试 `require`）里保留了 `CSS.escape` 的用法，两处场景不同，不需要统一处理方式。

**问题四：写实施方案文档时，风险小节里声称"插件没识别到提交按钮时用户仍可以在 Dashboard 手动改状态"，但这个手动入口当时并不存在**

现象：写第九节风险小节草稿时，顺手写了一句"用户仍然可以在 Dashboard 手动把状态改成已投递"作为"提交按钮识别可能失效"这条风险的兜底说明，写完之后去 `routes_dashboard.py`/`job_detail.html` 核对，发现 Dashboard 上从来没有过这样一个手动改状态的入口——`JDStatus.APPLIED` 只在状态徽章的展示文案映射里出现过，没有任何路由或按钮能把状态真的改成它。

原因：写文档时凭"这种基础功能应该有"的直觉下笔，没有先去代码里确认。

解决：没有把这句话改成更谨慎的措辞回避问题，而是直接把这个真实存在的功能缺口补上——新增 `POST /dashboard/jobs/{jd_id}/mark-applied` 路由和 Dashboard 详情页的"手动标记为已投递"按钮（`test_dashboard.py` 新增 2 条用例），再回头确认文档里这句话是真的。这个坑提醒自己：写文档里任何一句"用户仍然可以……"这类兜底说明之前，先去代码里确认这条退路真的存在，不能凭直觉假设。

### 验证结果

- 后端新增/扩展 5 个 pytest 文件：`test_qa_similarity.py`（新增，8 条）、`test_qa_bank_service.py`（新增 8 条：embedding 落库、历史数据回填、相似度检索的命中/未命中/空题库/空问题各种场景）、`test_autofill.py`（新增，15 条，重点覆盖合规安全限制的多种触发路径、LLM 失败降级、批量调用去重）、`test_routes_extension.py`（新增 10 条：四个新接口的鉴权、正常路径、边界情况，含"LLM 已配置但仍然跳过合规字段"这条关键安全回归）、`test_dashboard.py`（新增 2 条：手动标记已投递）,加上此前 Phase 0-3 遗留的用例，`backend` 目录下共 **201 条 pytest 用例全部通过**，零回归。
- 插件侧新增 `extension/content_scripts/form_scanner.test.js`（16 条）、`extension/content_scripts/ats_selectors.test.js`（9 条），连同 Phase 3 遗留的 `linkedin_parser.test.js`（5 条），`npm test` 共 **30 条用例全部通过**；`ats_autofill.js`/`dashboard_bridge.js` 是纯浏览器胶水代码（DOM 操作、`chrome.*` 消息通信），和 Phase 3 的 `linkedin.js` 一样无法用 jsdom 做有意义的单元测试，依赖 README 的手动验收清单。
- `extension/scripts/check_permissions.js` 复跑确认通过（新增了 `data-automation-id` 相关 DOM 查询、`DataTransfer`/`File` 等 Web API，都不需要在 `permissions` 里额外声明；`chrome.storage`/`chrome.runtime`/`chrome.tabs` 三个命名空间已经在 Phase 0/3 声明过）。
- 先在云端沙盒环境跑通全部后端 pytest 和插件侧 Node 测试，再把改动同步到用户本机项目目录（`C:\liuzhibin\aI-agent\jobpilot`），逐文件 SHA-256 校验和确认同步无损坏，在设备侧独立 Linux 虚拟环境（`~/jobpilot_venv`）里重新跑一遍全部 pytest 用例，结果一致。真实 Chrome 加载插件、在真实 Workday/Greenhouse/Lever 投递页面上验证自动填表、简历 PDF 附件注入、提交按钮识别这几部分，云端沙盒没有图形界面也没有真实账号，无法代为完成，验收清单写在 `README.md` 新增章节，需要用户在自己电脑上手动过一遍。

### 涉及文件

`docs/JobPilot_实施方案.md`（v5，5.4 节改写为落地后的实际设计、第八节 Phase 4 标记完成、第六节新增三条决策记录、第九节补充 Phase 4 风险点）、`README.md`（新增章节：自动化填表的测试方式和手动验收清单）、`backend/app/services/autofill.py`（新增）、`backend/app/services/qa_similarity.py`（新增）、`backend/app/services/qa_bank_service.py`、`backend/app/api/routes_extension.py`、`backend/app/api/routes_dashboard.py`（新增手动标记已投递路由）、`backend/app/templates/job_detail.html`、`backend/tests/test_autofill.py`（新增）、`backend/tests/test_qa_similarity.py`（新增）、`backend/tests/test_qa_bank_service.py`、`backend/tests/test_routes_extension.py`、`backend/tests/test_dashboard.py`、`extension/manifest.json`（新增 ATS 三家 `host_permissions` + 两组 `content_scripts`）、`extension/content_scripts/form_scanner.js`（新增）、`extension/content_scripts/ats_selectors.js`（新增）、`extension/content_scripts/ats_autofill.js`（新增）、`extension/content_scripts/dashboard_bridge.js`（新增）、`extension/content_scripts/form_scanner.test.js`（新增）、`extension/content_scripts/ats_selectors.test.js`（新增）、`extension/background/service_worker.js`、`extension/package.json`

---

## Phase 4 补充：Windows 上 PDF 渲染依赖从"报错崩溃"到"自动安装"（2026-09-12）

### 目标

用户在自己的 Windows 机器上第一次本地体验时，`python -m app.main` 直接崩溃退出：`OSError: cannot load library 'libgobject-2.0-0'`。根因是 `resume_pdf.py` 在模块顶层 `from weasyprint import HTML`，而 WeasyPrint 是通过 cffi 调用系统级 Pango/GObject C 库的，`pip install weasyprint` 只装了 Python 这一层，Windows 上还需要单独装一份 GTK3 运行时——这个模块又被 `resume_tailor.py -> routes_dashboard.py -> app.main` 在顶层逐级 import，导致一个跟"能不能生成 PDF"这个可选产物完全无关的系统依赖缺失,直接拖垮了整个本地 App，画像、JD 打分、抓取、题库这些功能全都用不了。

这个 Phase 补充分两步走，对应用户两轮反馈：第一轮先把"崩溃"变成"运行时才检查、给一个信息明确的错误提示"（防止一个可选依赖缺失拖垮整个 App）；用户看到这个方案后明确反馈这仍然是"挖了个坑"——**如果这个依赖是必须的，就应该放进自动化流程里去装，而不是提示用户手动安装、或者不装就报错**。第二步才是这里要记录的重点：把 GTK3 Runtime 的安装本身也自动化。

### 实现内容

- **防御性/延迟 import**（`backend/app/services/resume_pdf.py`）：模块顶层的 `import weasyprint` 包一层 `try/except`，导入失败只记录"这次不可用"和原始异常，不让异常向上传播；真正调用 `render_resume_pdf_bytes` 时才检查，不可用就抛信息明确的 `PdfRenderingUnavailableError`，而不是一个不知所云的 `OSError`。`resume_tailor.confirm_and_finalize` 早就把 `save_resume_pdf` 包在 try/except 里，PDF 生成失败只是不写 `pdf_path`，不影响简历确认这个操作本身。
- **自动安装 GTK3 Runtime**（`backend/app/services/pdf_dependency_installer.py`，新增）：本地 App 每次启动时（`app/main.py` 的 `lifespan`）检查一次，`_WeasyPrintHTML is None` 且是 Windows 才会触发：先用 `ctypes.WinDLL("libgobject-2.0-0.dll")` 轻量探测（不直接 `import weasyprint`，避免它自己失败时往 stderr 打一大段排障文字），缺失就调用 GitHub API（`tschoonj/GTK-for-Windows-Runtime-Environment-Installer` 的 `releases/latest`）拿到最新安装包地址，下载后用 NSIS 的 `/S` 参数静默安装，不需要用户任何交互。安装失败（下载失败、安装程序超时、退出码非 0）会捕获成 `GtkAutoInstallError`，记日志 + 给一条带手动安装链接的提示，不会拖垮启动流程；连续失败时会在本地记一个时间戳，一小时内不重复尝试（避免网络有问题时每次启动都多等一次下载超时）。
- **`app/main.py` 接线**：新增 `_check_pdf_dependency`，在 `lifespan` 里、写完启动日志之后调用，把探测/安装结果落一行 `PDF 渲染依赖检查：...` 日志。新增 `Settings.skip_pdf_auto_install`（环境变量 `JOBPILOT_SKIP_PDF_AUTO_INSTALL`），`tests/conftest.py` 的 `isolated_home` fixture 里强制打开，保证 pytest 跑测试时绝不会意外触发真实的网络下载和外部安装程序执行。
- **面向用户的提示文案更新**：`resume_pdf.py` 里 `PdfRenderingUnavailableError` 的错误信息、`resume_result.html` 里 PDF 没生成成功时的提示，都改成指向启动日志里的"PDF 渲染依赖检查"这一行，而不是让用户自己去查 WeasyPrint 官方文档。`README.md` "一、启动本地 App" 章节新增一段，解释这行日志的几种可能内容分别该怎么处理。

### 设计取舍（有意简化，供后续 Phase 参考）

1. **只自动装 Windows 的 GTK3 Runtime，不管 Linux/macOS 缺的系统库**：Linux 的 `libpango` 之类只能走 `apt`，macOS 走 `brew`，这两者都需要 sudo/管理员交互、会改动系统级包管理器状态，不适合程序自己静默执行；而这两个平台的开发者本来也更容易自己用包管理器解决。Windows 的 GTK3 Runtime 恰好相反：有官方独立安装包、支持真正静默的 `/S` 参数、装到默认路径后 WeasyPrint 自己的 `ffi.py` 会自动发现，不需要改 PATH——这是唯一能在不需要用户交互、不需要 sudo 密码的前提下做到"真自动安装"的场景，也刚好是本项目实际踩到的那个 bug。
2. **自动装完之后，诚实地告诉用户"需要重启一次"，不假装当次就能用**：WeasyPrint 探测 DLL 目录的逻辑（`os.add_dll_directory`）只在"进程刚启动、还没 import 任何东西"的那一刻跑一次，当前这个已经在运行中的进程即使装好了 GTK3，大概率也看不到——这是 WeasyPrint 自己的实现方式决定的限制，不是这个自动化流程能绕开的，所以选择诚实反映（"已自动安装完成，请重启一次本地 App"）而不是误报成功或者误报失败。
3. **失败退避用本地时间戳文件，不用更复杂的机制**：一小时的固定退避足够避免"网络有问题时每次启动都多等一次超时"这个新问题，不需要指数退避之类更复杂的策略——这本来就是个低频场景（一台机器基本只会真正缺这个依赖一次）。

### 遇到的问题与解决

**问题一：`pytest.raises(PdfRenderingUnavailableError)` 抓不住 `importlib.reload` 之后模块自己抛出的异常实例**

现象：为了验证"这个模块被 import 的那一刻 `import weasyprint` 本身就抛异常"这个最贴近真实故障的场景，测试里用 `sys.modules['weasyprint'] = None` 强制下一次 import 失败、再 `importlib.reload(resume_pdf)`，断言重新加载之后调用 `render_resume_pdf_bytes` 会抛 `PdfRenderingUnavailableError`——但用文件顶部 reload 之前 `import` 进来的那个类引用去 `pytest.raises`，死活抓不住。

原因：`importlib.reload` 会重新执行整个模块的类定义，产生一个新的类对象，和 reload 之前导入进来的旧类对象不是同一个身份，`pytest.raises(旧引用)` 做的是 `isinstance` 检查，抓不住新类的实例。

解决：改用 `reloaded.PdfRenderingUnavailableError`（重新加载之后模块自己的类属性）而不是模块级别导入的旧引用。

**问题二：第一版"自动安装成功之后"的分支设计，把"这次探测还是失败"直接判定成安装失败**

现象：写完第一版 `ensure_pdf_dependency` 后自己测试时发现，`auto_install_gtk_runtime()` 没抛异常（意味着静默安装程序退出码是 0，系统层面已经装好了）之后，紧接着再探测一次 `probe_gtk_available()`，这次探测按前面"问题设计取舍 2"里说的原因大概率还是会返回 `False`——第一版代码把这种情况报成"安装程序已执行，但探测仍未通过"这样一句偏警告性质的话，读起来像是安装失败了，但实际上只是当前进程看不到而已，装到位这件事本身是成功的。

解决：把"探测是否立刻通过"和"安装本身是否成功"这两件事在代码里彻底分开——只要 `auto_install_gtk_runtime()` 没抛异常就记成功状态（不会触发失败退避），探测立刻通过就是"当次即可用"，探测没通过就明确说"已自动安装完成，请重启一次"，不再有一个"看起来像失败但其实是成功"的中间状态。写单测的时候把这条分支单独测出来（`test_ensure_pdf_dependency_success_reports_restart_needed`）才发现最初的用例断言本身就是照着"应该输出什么样的最终文案"这个错误假设写的,倒逼着回头改了代码逻辑而不是改测试断言去迁就它。

### 验证结果

- `backend/tests/test_pdf_dependency_installer.py`（新增，15 条）：覆盖平台判断、探测函数、`ensure_pdf_dependency` 的每一条状态机分支（已就绪 / 未启用自动安装 / 退避期内跳过重试 / 退避期过后重新尝试 / 自动安装成功且当次可用 / 自动安装成功但需要重启 / 自动安装失败）、GitHub release 资产解析、静默安装子进程调用的正常/超时/非零退出码路径——全部通过 monkeypatch 掉真实的网络请求和 `subprocess.run`，不会真的联网或者执行任何安装程序。
- `backend/tests/test_main_startup.py`（新增，3 条）：验证 `app/main.py` 新增的 `_check_pdf_dependency` 在 WeasyPrint 已可用时完全不触碰安装模块、在不可用时正确地把 `settings.skip_pdf_auto_install` 取反传给 `auto_install` 参数，以及完整走一遍 `TestClient` 生命周期确认不会意外触发真实安装逻辑。
- `backend/tests/test_resume_pdf.py`、`backend/tests/test_resume_tailor.py` 里此前为防御性 import 补的回归测试保持不变、全部通过。
- 全量跑 `backend` 目录下 pytest，**223 条用例全部通过**，零回归。
- 真实的"Windows 机器上从零开始、确实缺 GTK3、自动下载安装、重启一次之后 PDF 真的能生成"这条完整链路，云端沙盒和设备侧的 Linux 虚拟环境都无法端到端验证（本来就只在 Windows 上发生），需要用户在自己电脑上实际走一遍：删掉之前可能手动装过的 GTK3（如果装过的话）或者直接在一台干净的 Windows 环境上验证，重点看启动日志里 `PDF 渲染依赖检查：...` 这一行的内容是否符合预期、重启一次之后简历重制流程里"生成 PDF"是否真的能成功。

### 涉及文件

`backend/app/services/pdf_dependency_installer.py`（新增）、`backend/app/services/resume_pdf.py`（防御性 import + 错误提示文案）、`backend/app/main.py`（新增 `_check_pdf_dependency` 并接入 `lifespan`）、`backend/app/core/config.py`（新增 `skip_pdf_auto_install` 配置项）、`backend/app/templates/resume_result.html`（PDF 未生成时的提示文案）、`backend/tests/test_pdf_dependency_installer.py`（新增）、`backend/tests/test_main_startup.py`（新增）、`backend/tests/test_resume_pdf.py`、`backend/tests/test_resume_tailor.py`、`backend/tests/conftest.py`（新增 `JOBPILOT_SKIP_PDF_AUTO_INSTALL` 环境变量隔离）、`README.md`（"一、启动本地 App" 章节新增 Windows PDF 依赖自动安装说明）

---
