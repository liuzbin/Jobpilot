/**
 * 内容脚本：注入到本地 Dashboard 页面（http://127.0.0.1:8756/*），负责把
 * "用户在 JD 详情页点了'去投递'"这件事从普通页面脚本桥接进插件自己的
 * 存储里，供之后在投递页面上关联"这个标签页对应哪条 JD"。
 *
 * 为什么需要这一层桥接：Dashboard 页面是本地 App 用 Jinja2 模板渲染出来的
 * 普通网页，运行在它自己的页面执行上下文里，没有、也不应该有 `chrome.*`
 * 扩展 API 的访问权限（这是浏览器扩展模型的基本隔离，普通网页不能随便
 * 调用扩展 API，否则任何网页都能读写用户所有插件的存储）。只有插件自己
 * 注入的内容脚本才能访问 `chrome.storage`，所以约定：Dashboard 页面的
 * "去投递"按钮点击后，只负责 `window.dispatchEvent` 一个普通的 DOM
 * CustomEvent（见 job_detail.html 内联脚本），这个内容脚本监听这个事件，
 * 再通过 `chrome.runtime.sendMessage` 把信息转发给 background service
 * worker 真正写入存储。
 *
 * 存储写入之后的读取方（ats_autofill.js，注入到 ATS 投递页面）按目标页面
 * 的 hostname 匹配这条"待投递意图"记录，具体的过期/清理策略、以及"用户
 * 不是从 Dashboard 点过去，直接自己打开投递页"时的降级行为（没有匹配记录
 * 就什么都不做，不报错也不提示）在 background/service_worker.js 里实现。
 */

(function () {
  window.addEventListener("jobpilot:start-application", function (event) {
    const detail = event.detail || {};
    const jdId = detail.jdId;
    const targetUrl = detail.targetUrl;
    if (!jdId || !targetUrl) return;

    let hostname;
    try {
      hostname = new URL(targetUrl).hostname;
    } catch (e) {
      return; // 目标地址解析不出来就放弃关联，用户仍然能正常打开新标签页投递
    }

    chrome.runtime.sendMessage({
      type: "jobpilot:start-application",
      payload: { jdId, hostname },
    });
  });
})();
