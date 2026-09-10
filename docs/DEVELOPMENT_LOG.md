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
