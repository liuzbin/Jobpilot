# JobPilot — Phase 0 + Phase 1

Phase 0 目标：本地 App（FastAPI + SQLite）能独立跑起来，数据库结构完整；Chrome 插件能检测到本地 App、完成配对、并维持心跳连接。这是后续所有功能（画像、打分、简历重制、自动填表）的地基。

Phase 1 目标：跑通"端到端打分闭环"——上传简历自动建立/补全画像、粘贴 JD 自动结构化解析、点一下"分析"得到一个可解释且**可复现**的匹配分数，全部通过本地浏览器打开的 Dashboard 页面操作，不需要用到插件。

## 目录结构

```
jobpilot/
├── backend/          # 本地 App（Python / FastAPI）
│   ├── app/
│   │   ├── core/     # 配置、数据库连接、配对 token、自动迁移、LLM 客户端、密钥加密存储
│   │   ├── models/   # SQLAlchemy 表结构
│   │   ├── services/ # 业务逻辑：简历解析、JD 解析、打分引擎、画像合并
│   │   ├── templates/# Dashboard 用的 Jinja2 模板
│   │   └── api/      # REST + WebSocket 路由 + Dashboard 路由
│   ├── alembic/      # 数据库迁移脚本
│   └── tests/        # pytest 自动化测试
└── extension/        # Chrome 插件（Manifest V3）
    ├── background/   # service worker：连接状态机、心跳
    └── sidepanel/    # 侧边栏 UI：配对、状态展示
```

## 一、启动本地 App

```bash
cd jobpilot/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

启动后会看到日志 `JobPilot local app ready. data dir = /Users/你的用户名/.jobpilot`，说明：

- 数据库文件、配对 token 都会自动创建在 `~/.jobpilot/` 目录下（Windows 下是用户目录的 `.jobpilot` 文件夹）。
- 本地服务只监听 `127.0.0.1:8756`，不会被局域网内其他设备访问到。
- 每次启动都会自动执行数据库迁移，不需要手动跑 `alembic upgrade head`。

## 二、运行自动化测试

```bash
cd jobpilot/backend
source .venv/bin/activate
python -m pytest -v
```

当前共 56 条用例：

1. **数据库迁移**（`test_migrations.py`）：从空目录跑 `alembic upgrade head` 能一次性建出全部 8 张业务表，重复运行不报错，并且不会误禁用 uvicorn/app 自己的日志 logger。
2. **配对握手**（`test_handshake.py`）：`/api/health` 无需鉴权即可探活；`/api/status` 在缺 token / 错误 token / 错误 Origin 时分别返回 401 或 403，只有 token 和 Origin 都正确才放行；`/ws/heartbeat` 的 ping/pong 在鉴权通过后正常工作。
3. **画像合并逻辑**（`test_profile_service.py`）：公司/项目按名称模糊匹配复用或新增、贡献句去重、简历抽取只填空字段不覆盖已有值。
4. **JD 规则解析**（`test_jd_ingest.py`）：LinkedIn 附加信息行（申请人数、发布时间、是否推广）的纯正则解析，多次调用结果完全一致。
5. **打分引擎**（`test_scoring.py`）：核心是"同样的输入无论调用多少次，输出必须完全一致"这条确定性断言，另外覆盖学历/清关不满足时的扣分、年限差扣分（含封顶）、加分技能（含封顶）、总分 clamp 到 [0,100] 等边界场景。
6. **Dashboard 路由**（`test_dashboard.py`）：画像编辑、简历上传解析、JD 新建、模型配置保存、一键分析（用 `FakeLLMClient` 模拟真实模型返回，不需要真实 API Key 和网络）、以及跨站请求防护。

## 三、检查插件权限声明（无需 Chrome，命令行即可）

```bash
cd jobpilot/extension
node scripts/check_permissions.js
```

这个脚本会扫描插件代码里用到的 `chrome.*` API，对照 `manifest.json` 里声明的 `permissions`，提前发现"用了某个 API 但忘记在 manifest 里声明权限"这类问题（第一版就因为用了 `chrome.alarms` 却忘记声明 `alarms` 权限，导致后台脚本一启动就抛异常、侧边栏一直卡在"检测中"）。正常应该输出"权限检查通过"。

## 四、加载 Chrome 插件（需要你在自己电脑上手动完成）

这一步需要在你本机的真实 Chrome 里操作，云端环境没有图形界面无法代为完成，请按下面步骤验收：

1. 打开 `chrome://extensions`，右上角打开"开发者模式"。
2. 点击"加载已解压的扩展程序"，选择 `jobpilot/extension` 目录。
   - 如果你之前已经加载过旧版本，请点这个插件卡片上的"重新加载"（一个圆形箭头图标），**不要只刷新侧边栏页面**——`manifest.json` 权限变化只有重新加载整个插件才会生效。
3. 确认本地 App 正在运行（第一步）。
4. 点击浏览器工具栏里的 JobPilot 图标，应该会打开侧边栏。
5. 如果侧边栏一直卡在某个状态没反应，打开 `chrome://extensions`，找到 JobPilot 卡片上的"service worker"或"检查视图"链接，点进去看控制台有没有报错——这是插件侧最直接的排查手段，以后遇到类似问题可以先看这里。

### Phase 0 验收清单

- [ ] 本地 App 未启动时，侧边栏显示"未连接"，并提示"还没检测到本地 App"。
- [ ] 启动本地 App 后（不用手动刷新），大约 1 分钟内或点击"重新检测"后，侧边栏状态变为"已检测到本地 App，待配对"。
- [ ] 点击"打开本地配对页面"，会新开一个标签页显示配对码（地址是 `http://127.0.0.1:8756/pair`）。
- [ ] 复制配对码粘贴到侧边栏输入框，点击"保存并连接"，状态应在几秒内变为"已连接"，并显示最近心跳时间，之后每 10 秒左右心跳时间会刷新一次。
- [ ] 手动关掉本地 App 进程（Ctrl+C），侧边栏应在心跳超时后（约 15-25 秒）变回"未连接"。
- [ ] 重新启动本地 App，侧边栏应自动重新连接（不需要重新输入配对码，因为 token 是持久化在两边的），除非你之前手动清过配对码或本地 App 的 `~/.jobpilot/pairing_token.json` 被删除重新生成过。
- [ ] 故意在侧边栏输入一个错误的配对码，应该看到"配对失败"提示，且不会无限重试同一个坏 token。

## 五、使用 Dashboard（画像 / JD 分析 / 模型配置）

Dashboard 是本地 App 自带的网页界面，启动本地 App 后直接在浏览器打开 `http://127.0.0.1:8756/dashboard/jobs` 即可，不需要用到 Chrome 插件。

1. **先配置模型**：打开"模型配置"页面（`/dashboard/models`），分别填写"轻量模型"和"重量模型"两个槽位的 Base URL（填到 `/v1` 这一级，比如 `https://api.openai.com/v1`）、模型名称、API Key。任意 OpenAI 兼容的 Chat Completions 接口都可以填，两个槽位可以指向完全不同的模型/服务商。API Key 会加密后存在本地，页面上不会明文回显。
2. **建立画像**：打开"画像"页面（`/dashboard/profile`），可以直接手动填写基本信息，也可以上传一份简历文件（支持 `.pdf`/`.docx`/`.txt`/`.md`），系统会用"轻量模型"自动抽取工作经历和基本信息合并进画像——已经手动填过的基本信息字段不会被覆盖，工作经历按公司/项目名称自动去重合并。
3. **粘贴 JD 并分析**：打开"职位"页面（`/dashboard/jobs`），点"新建"粘贴一段 JD 原文（公司、职位、地点等可选），保存后进入详情页点击"分析"，系统会用"轻量模型"结构化解析 JD、用"重量模型"判断技能契合度和硬性要求，最终分数由固定规则计算得出，多次点击"重新分析"同一份 JD 和画像，分数应该完全一致。

### Phase 1 验收清单

- [ ] 未配置任何模型时，上传简历或点击"分析"，应该看到"还没配置"这样的友好提示，而不是报错页面。
- [ ] 配置好轻量/重量两个模型槽位后，上传一份真实简历，能看到"解析完成：新增 X 家公司、Y 段经历、Z 条贡献句"的提示，画像页面能看到对应的工作经历树。
- [ ] 再次上传同一份（或高度相似的）简历，重复的经历/贡献句不应该被重复插入。
- [ ] 粘贴一段真实 JD 并分析，详情页能看到总分、扣分/加分明细、优势与劣势列表。
- [ ] 对同一份 JD 点击"重新分析"多次，总分和各项明细完全一致（这是打分引擎"确定性"要求的直接体现）。
- [ ] `python -m pytest -v` 全部 56 条用例通过。

以上全部打勾即代表 Phase 1 验收通过，可以进入 Phase 2（画像深化与简历重制）。

## 安全说明

- 配对 token 保存在本地文件 `~/.jobpilot/pairing_token.json`，不要把这个文件或其中的内容分享给别人。
- 模型的 API Key 经对称加密后保存在本地文件 `~/.jobpilot/secrets_store.json`，加密密钥单独存放在 `~/.jobpilot/secret.key`（权限限制为仅当前用户可读），同样不要分享这两个文件。
- 本地服务插件相关接口（`/api/*`、`/ws/*`）都同时校验"配对 token"和"请求来源（Origin 必须是 `chrome-extension://` 开头）"；Dashboard 相关接口（`/dashboard/*`）因为是用户直接用浏览器打开的普通页面，不走配对 token，但会拒绝来自其他站点的跨站请求。
- 目前插件侧 Origin 校验只要求前缀是 `chrome-extension://`，尚未锁定到具体插件 ID；插件正式打包发布、拿到固定 ID 后，建议通过环境变量 `JOBPILOT_ALLOWED_ORIGIN` 把它锁死到那一个具体来源，这个会在后续打磨阶段处理。
