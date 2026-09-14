/**
 * JobPilot 内容脚本：注入到 LinkedIn 职位页面，负责响应侧边栏发来的"抓取当前
 * 职位信息"请求。
 *
 * 抓取逻辑本身是纯函数 `extractLinkedInJob`（定义在同目录 `linkedin_parser.js`，
 * 已经用 jsdom 跑过多种页面布局的回归测试，见 `linkedin_parser.test.js`）；
 * 这个文件只负责浏览器环境里的胶水代码：监听侧边栏发来的消息、抓取当前
 * DOM、把结果返回给侧边栏。
 *
 * 用户反馈之前"悬浮在页面右下角的按钮"这个方案不够可靠：LinkedIn 是单页
 * 应用，会频繁大范围重渲染 DOM，按钮偶尔会被顶掉、被其他元素盖住、或者在
 * 某些页面布局下位置不对；纯靠低频轮询重新注入(`setInterval`)只能算一种
 * "亡羊补牢"，没办法从根上解决"按钮有时候就是不出现"这个问题。所以现在改成
 * 把"发送到 JobPilot"这个入口固定挪到侧边栏里（见 `sidepanel/sidepanel.js`），
 * 侧边栏是插件自己的独立 UI，不会被页面的重渲染影响到；这个内容脚本只需要
 * 老老实实回答"你抓到当前职位信息了吗"这一个问题就够了。
 *
 * 之所以"真正发 HTTP 请求给本地 App"这一步还是放在 background 而不是这里
 * 直接 fetch、也不是放在侧边栏直接 fetch：内容脚本/侧边栏运行在各自的执行
 * 上下文里，只有从插件自己的 background 发起的请求才会带上稳定的插件 Origin，
 * 过后端 `require_paired_request` 的 Origin 校验。这也是 Phase 0 就定下的
 * 配对鉴权设计的直接推论，不是这里额外加的限制。
 *
 * LinkedIn 的职位列表/详情页是单页应用，切换到不同职位卡片通常不会整页刷新，
 * 所以这里刻意不在脚本加载时就抓一次数据缓存起来，而是把抓取放到收到消息的
 * 那一刻——不管用户之前在同一个标签页里看过多少个不同职位，侧边栏每次点击
 * "发送到 JobPilot"时读到的永远是当前这个职位的最新 DOM。
 */

(function () {
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "jobpilot:extract-current-job") return undefined; // 不是发给这个监听器的消息,交给其他监听器处理

    const job = extractLinkedInJob(document, window.location.href);
    if (!job.description_raw) {
      sendResponse({ ok: false, error: "没有在这个页面里抓到职位正文，试试手动粘贴 JD" });
      return false;
    }
    sendResponse({ ok: true, job });
    return false; // 上面这几行都是同步执行完的,不需要保持消息通道开着等异步结果
  });
})();
