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
    ├── background/                # service worker：连接状态机、心跳、转发插件抓取到的 JD 给本地 App、自动化填表相关的网络调用
    ├── content_scripts/           # 注入到 LinkedIn 职位页（Phase 3）和 Workday/Greenhouse/Lever 投递页（Phase 4）的脚本 + 对应的 jsdom 回归测试
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
- **Windows 用户会额外看到一行 `PDF 渲染依赖检查：...` 日志**：简历重制功能里"生成 PDF"这一步依赖 WeasyPrint，而 WeasyPrint 需要系统级的 GTK3 Runtime，`pip install -r requirements.txt` 装不出这个系统级依赖。这一步不需要你手动处理——本地 App 每次启动时会自己探测，缺失时自动下载并静默安装 GTK3 Runtime，你只需要看这行日志里的提示：
  - 如果说"已就绪"或"当次即可使用"：不用做任何事。
  - 如果说"已自动安装完成，请重启一次本地 App"：按提示重新执行一次 `python -m app.main` 即可（这是 WeasyPrint 自身的限制——它探测系统依赖的逻辑只在进程刚启动时跑一次，装好之后必须换一个新进程才能生效，重启一次就好，不需要手动改任何 PATH/环境变量）。
  - 如果说"自动安装失败"：日志里会附一个手动安装的链接，装完后重启本地 App。
  - Markdown/结构化数据这些简历重制的核心产物完全不受这个依赖影响，只有"下载 PDF"这一项功能会受影响。

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

## 六、自动化填表（Phase 4）

在已知的三家投递系统（Workday、Greenhouse、Lever）的投递页面上，插件会在页面右下角注入两个悬浮按钮："JobPilot 自动填表"和"附加简历 PDF"。点"自动填表"会扫描当前页面的表单字段，发给本地 App 算出每个字段该填什么，再把结果写回页面；点"附加简历 PDF"会把这条 JD 最近一次生成的简历 PDF 自动放进简历上传控件。另外，Dashboard 的 JD 详情页新增了"去投递"按钮：点击会在新标签页打开投递链接，同时让插件记住"接下来这个标签页对应这条 JD"，之后在投递页面上点击提交按钮，只要还是这个标签页，就会自动把这条 JD 的状态推进成"已投递"；如果你不是从这个按钮点过去（比如自己手动打开的投递页），插件不会做任何自动状态更新，你可以用详情页上的"手动标记为已投递"按钮自己同步状态。

**合规安全说明**：工作授权、签证担保、EEO 自报（性别/种族/退伍军人身份/残障状态）、犯罪记录/背景调查这类涉及法律声明性质的字段，插件不会自动帮你选（select/radio/checkbox 这类控件），需要你自己手动选择——这是有意为之的限制，不是 bug，详见 `docs/JobPilot_实施方案.md` 5.4 节的说明。

### 不需要 Chrome 的回归测试

和 Phase 3 的 LinkedIn 抓取一样，表单扫描和 ATS 平台识别的核心逻辑抽成了不依赖浏览器的纯函数，用 jsdom 做单元测试：

```bash
cd jobpilot/extension
npm install       # 第一次跑之前装一下开发依赖
npm test          # 依次跑 linkedin_parser / form_scanner / ats_selectors 三组测试
```

正常应该看到三组测试全部通过（`form_scanner.test.js` 16 条，覆盖各种标签关联方式和 select/radio/checkbox 字段提取；`ats_selectors.test.js` 9 条，覆盖平台识别和 Workday/Greenhouse/Lever 各自的标签解析规则）。后端的填表决策逻辑（`build_autofill_plan`）和合规安全限制在 `backend/tests/test_autofill.py` 里用 pytest 覆盖，随 `python -m pytest` 一起跑。

`ats_autofill.js`（悬浮按钮、DOM 填值、简历 PDF 注入、提交按钮监听）和 `dashboard_bridge.js`（"去投递"关联桥接）是纯浏览器胶水代码，依赖真实 DOM 事件和 `chrome.*` 消息通信，和 Phase 3 的 `linkedin.js` 一样无法用 jsdom 做有意义的单元测试，需要下面的手动验收。

如果哪天发现某个 ATS 平台上抓不到某个字段的标签、或者字段类型判断错了，优先的排查方式和 LinkedIn 抓取一样：把当时页面对应部分的 HTML 结构记录下来，在 `form_scanner.test.js` 或 `ats_selectors.test.js` 里加一条新用例复现问题，再去 `form_scanner.js`/`ats_selectors.js` 里补一条新的选择器/标签解析分支。

### 需要在真实 Chrome + 真实 ATS 页面上手动验收的部分

云端环境没有图形界面、也没有 Workday/Greenhouse/Lever 的真实测试账号，规则库是基于三家平台公开可观察到的约定构造的最佳努力实现，下面这些必须你在自己电脑上手动确认：

- [ ] 按上面"四、加载 Chrome 插件"重新加载一次插件（`manifest.json` 新增了 ATS 三家的 `host_permissions` 和 `content_scripts`，必须整个重新加载才会生效）。
- [ ] 找一个真实的 Workday/Greenhouse/Lever 投递页面打开，页面右下角应该能看到"JobPilot 自动填表"和"附加简历 PDF"两个悬浮按钮。
- [ ] 先在画像页面填好姓名/邮箱/电话/领英链接等基本信息，点击"JobPilot 自动填表"，按钮文案应该依次变化"扫描中…" → "计算填表方案中…" → "已填 N 个字段"，对应的输入框应该被正确填上；如果页面是 React/Vue 这类框架驱动的，确认填进去的值真的被框架感知到了（比如触发了页面自己的实时校验提示消失），而不是看起来填上了、提交时却读到空值。
- [ ] 找一个涉及"工作授权"/"是否需要担保"这类问题的单选/下拉字段，确认插件没有自动帮你选，需要你自己手动选择。
- [ ] 找一道需要主观作答的问答题（比如"为什么想加入我们"），如果题库里已经有相似的问题，确认能被自动填上；如果没有，自己写完答案后失去焦点，应该弹出"要不要存进题库"的确认框，确认后下次遇到类似问题能被检索到。
- [ ] 在 JD 已经生成过简历的情况下，点击"附加简历 PDF"，确认简历上传控件里出现了对应的 PDF 文件。
- [ ] 从 Dashboard JD 详情页点击"去投递"打开投递页，完成表单（不需要真的投递成功，找到提交按钮点一下即可，如果会触发真实投递请谨慎，可以用一个不重要的测试职位或者提交前及时关闭标签页），确认 Dashboard 对应 JD 的状态变成了"已投递"。
- [ ] 不通过"去投递"按钮、自己直接打开一个投递页面，点击提交按钮，确认没有任何 JD 状态被意外更新（验证"没有关联记录就完全不做任何事"这条设计）。
- [ ] 在 JD 详情页，点击"手动标记为已投递"，确认状态立刻变成"已投递"，按钮本身也随之消失。

以上全部打勾即代表 Phase 4 验收通过。

## 安全说明

- 配对 token 保存在本地文件 `~/.jobpilot/pairing_token.json`，不要把这个文件或其中的内容分享给别人。
- 本地服务的所有需要鉴权的接口都同时校验"配对 token"和"请求来源（Origin 必须是 `chrome-extension://` 开头）"，防止你打开的其他网页伪装成插件向本地 App 发请求。
- 目前 Origin 校验只要求前缀是 `chrome-extension://`，尚未锁定到具体插件 ID；插件正式打包发布、拿到固定 ID 后，建议通过环境变量 `JOBPILOT_ALLOWED_ORIGIN` 把它锁死到那一个具体来源，这个会在后续打磨阶段处理。
