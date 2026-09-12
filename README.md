# JobPilot

本地优先的求职助手：Chrome 插件 + 本地常驻 App（FastAPI + SQLite），不依赖任何云端服务器。当前进度见 `docs/DEVELOPMENT_LOG.md`（每个 Phase 的实现过程、踩坑与解决）和 `docs/JobPilot_实施方案.md`（分阶段实施计划的唯一依据）。下面这份 README 主要是"怎么把本地 App 和插件跑起来、怎么验收"的操作手册，按 Phase 0 的顺序写的，后面几个 Phase 新增的验证步骤追加在对应小节里。

## 目录结构

```
jobpilot/
├── backend/                     # 本地 App（Python / FastAPI）
│   ├── app/
│   │   ├── core/                # 配置、数据库连接、配对 token、自动迁移
│   │   ├── models/               # SQLAlchemy 表结构
│   │   ├── services/             # 画像/JD/打分/简历重制等业务逻辑
│   │   └── api/                  # REST + WebSocket 路由（routes_system 配对与插件专用接口，routes_dashboard 本地浏览器页面）
│   ├── alembic/                  # 数据库迁移脚本
│   └── tests/                    # pytest 自动化测试
└── extension/                    # Chrome 插件（Manifest V3）
    ├── background/                # service worker：连接状态机、心跳、转发插件抓取到的 JD 给本地 App
    ├── content_scripts/           # 注入到 LinkedIn 职位页的抓取脚本（Phase 3）+ 对应的 jsdom 回归测试
    └── sidepanel/                 # 侧边栏 UI：配对、状态展示
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

Phase 0 阶段是 12 条用例，覆盖数据库迁移和配对握手两类验收点；后面每个 Phase 都在原有基础上继续补充，覆盖范围随功能一起长（画像/JD 解析、打分引擎、简历重制、事实护栏校验、PDF 渲染、插件专用 API 等），具体每个 Phase 新增了什么测试、当时的用例总数是多少，见 `docs/DEVELOPMENT_LOG.md` 对应章节的"验证结果"小节——这里不再逐个 Phase 重复维护一份可能过时的数字，跑一遍上面的命令看实际输出的用例总数和是否全部通过就是最新的真实状态。

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

### 验收清单

- [ ] 本地 App 未启动时，侧边栏显示"未连接"，并提示"还没检测到本地 App"。
- [ ] 启动本地 App 后（不用手动刷新），大约 1 分钟内或点击"重新检测"后，侧边栏状态变为"已检测到本地 App，待配对"。
- [ ] 点击"打开本地配对页面"，会新开一个标签页显示配对码（地址是 `http://127.0.0.1:8756/pair`）。
- [ ] 复制配对码粘贴到侧边栏输入框，点击"保存并连接"，状态应在几秒内变为"已连接"，并显示最近心跳时间，之后每 10 秒左右心跳时间会刷新一次。
- [ ] 手动关掉本地 App 进程（Ctrl+C），侧边栏应在心跳超时后（约 15-25 秒）变回"未连接"。
- [ ] 重新启动本地 App，侧边栏应自动重新连接（不需要重新输入配对码，因为 token 是持久化在两边的），除非你之前手动清过配对码或本地 App 的 `~/.jobpilot/pairing_token.json` 被删除重新生成过。
- [ ] 故意在侧边栏输入一个错误的配对码，应该看到"配对失败"提示，且不会无限重试同一个坏 token。

以上全部打勾即代表 Phase 0 验收通过，可以进入 Phase 1（端到端打分闭环：画像上传解析、JD 结构化、打分引擎、最简 Dashboard）。

## 五、抓取 LinkedIn 职位（Phase 3）

打开一个 LinkedIn 职位详情页，页面右下角会出现一个"发送到 JobPilot"悬浮按钮，点一下就会把这条职位的公司、岗位、正文、地点、"X people clicked apply"这类附加信息一起发给本地 App，自动打开对应的 Dashboard 详情页——接下来的分析、简历重制流程和手动粘贴 JD 完全一样，走的是同一条入库/打分链路。

### 不需要 Chrome 的回归测试

LinkedIn 的页面结构不受我们控制、也会不定期调整，抓取逻辑单独抽成一个不依赖浏览器的纯函数（`extension/content_scripts/linkedin_parser.js`），用 [jsdom](https://github.com/jsdom/jsdom) 模拟几种常见的 LinkedIn 页面布局（新版详情页、稍旧版、更旧的公开职位页、完全无法识别的布局）跑单元测试，可以在没有 Chrome、没有真实 LinkedIn 账号的情况下随时验证：

```bash
cd jobpilot/extension
npm install       # 第一次跑之前装一下开发依赖（只有 jsdom，运行插件本身不需要）
npm test          # 等价于 node content_scripts/linkedin_parser.test.js
```

正常应该看到 5 条用例全部 `ok`。如果哪天发现插件在真实 LinkedIn 页面上抓不到某个字段了，优先的排查方式是：把当时页面的 HTML 存一份到 `extension/content_scripts/__fixtures__/`，在 `linkedin_parser.test.js` 里加一条新用例复现问题，再去 `linkedin_parser.js` 里补一条新的选择器分支——而不是直接改代码之后祈祷它在真实页面上碰巧好使。

### 需要在真实 Chrome + 真实 LinkedIn 页面上手动验收的部分

云端环境没有图形界面、也没有 LinkedIn 账号，下面这些必须你在自己电脑上手动确认：

- [ ] 按上面"四、加载 Chrome 插件"重新加载一次插件（`manifest.json` 新增了 `content_scripts` 和 `host_permissions`，必须整个重新加载才会生效，不能只刷新侧边栏）。
- [ ] 在已连接状态下打开侧边栏，应该能看到新的"抓取 LinkedIn 职位"提示卡片。
- [ ] 访问几个不同的真实 LinkedIn 职位详情页（`linkedin.com/jobs/view/...`），页面右下角应该都能看到"发送到 JobPilot"悬浮按钮。
- [ ] 点击按钮，按钮文案应该依次变化："发送中…" → "已发送，正在打开…"，同时自动新开一个标签页跳转到本地 Dashboard 对应 JD 的详情页，公司名、职位名、正文都应该和 LinkedIn 页面上看到的一致。
- [ ] 在 LinkedIn 的职位列表页里，不刷新页面、直接点击切换到另一个职位卡片，再点按钮，应该发送的是切换后这个新职位的信息（验证"单页应用切换职位不需要刷新页面"这条设计）。
- [ ] 故意在还没完成插件配对的状态下点击按钮，按钮应该提示"还没有完成配对，请先在侧边栏完成配对再试一次"，而不是无提示地失败。
- [ ] 打开一个明显不是职位详情页的 LinkedIn 页面（比如个人主页），确认按钮没有出现（`content_scripts` 的 `matches` 规则只匹配 `linkedin.com/jobs/*`）。

以上全部打勾即代表 Phase 3 验收通过。

## 安全说明

- 配对 token 保存在本地文件 `~/.jobpilot/pairing_token.json`，不要把这个文件或其中的内容分享给别人。
- 本地服务的所有需要鉴权的接口都同时校验"配对 token"和"请求来源（Origin 必须是 `chrome-extension://` 开头）"，防止你打开的其他网页伪装成插件向本地 App 发请求。
- 目前 Origin 校验只要求前缀是 `chrome-extension://`，尚未锁定到具体插件 ID；插件正式打包发布、拿到固定 ID 后，建议通过环境变量 `JOBPILOT_ALLOWED_ORIGIN` 把它锁死到那一个具体来源，这个会在后续打磨阶段处理。
