# JobPilot — Phase 0：本地 App + 插件基座

本阶段目标：本地 App（FastAPI + SQLite）能独立跑起来，数据库结构完整；Chrome 插件能检测到本地 App、完成配对、并维持心跳连接。这是后续所有功能（画像、打分、简历重制、自动填表）的地基。

## 目录结构

```
jobpilot/
├── backend/          # 本地 App（Python / FastAPI）
│   ├── app/
│   │   ├── core/     # 配置、数据库连接、配对 token、自动迁移
│   │   ├── models/   # SQLAlchemy 表结构
│   │   └── api/      # REST + WebSocket 路由
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

当前共 12 条用例，覆盖两类验收点：

1. **数据库迁移**：从空目录跑 `alembic upgrade head` 能一次性建出全部 8 张业务表，重复运行不报错，并且不会误禁用 uvicorn/app 自己的日志 logger（这条是踩过坑之后专门补的回归测试）。
2. **配对握手**：`/api/health` 无需鉴权即可探活；`/api/status` 在缺 token / 错误 token / 错误 Origin 时分别返回 401 或 403，只有 token 和 Origin 都正确才放行；`/ws/heartbeat` 的 ping/pong 在鉴权通过后正常工作，鉴权失败时握手被拒绝。

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

## 安全说明

- 配对 token 保存在本地文件 `~/.jobpilot/pairing_token.json`，不要把这个文件或其中的内容分享给别人。
- 本地服务的所有需要鉴权的接口都同时校验"配对 token"和"请求来源（Origin 必须是 `chrome-extension://` 开头）"，防止你打开的其他网页伪装成插件向本地 App 发请求。
- 目前 Origin 校验只要求前缀是 `chrome-extension://`，尚未锁定到具体插件 ID；插件正式打包发布、拿到固定 ID 后，建议通过环境变量 `JOBPILOT_ALLOWED_ORIGIN` 把它锁死到那一个具体来源，这个会在后续打磨阶段处理。
