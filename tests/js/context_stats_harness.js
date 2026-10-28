/**
 * 上下文统计回归脚本（Node 运行）
 *
 * 复现并防止两个历史问题：
 *  1. 空会话：详情列表在「有 → 无」之间循环（contextBreakdown 被反复清空）；
 *  2. 有对话：消息数/字符数每 2s 在 ±1 之间跳动（本地估算与服务端统计互相覆盖）。
 *
 * 用法：node tests/js/context_stats_harness.js
 * 退出码 0 = 通过。
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const repoRoot = path.resolve(__dirname, "..", "..");
const sourcePath = process.env.NBOT_METHODS_PATH
    || path.join(repoRoot, "nbot", "web", "static", "js", "nbot-methods.js");
const source = fs.readFileSync(sourcePath, "utf8");

const requests = [];
let serverPayload = {
    success: true,
    total: { chars: 0, tokens: 0, messages: 0 },
    categories: {
        system_prompt: { label: "系统提示词", chars: 0, tokens: 0, messages: 0 },
        user_messages: { label: "用户消息", chars: 0, tokens: 0, messages: 0 },
        assistant_messages: { label: "助手消息", chars: 0, tokens: 0, messages: 0 },
    },
    max_context_length: 1000,
};

const noop = () => {};
const fakeElement = () => ({
    style: {},
    classList: { add: noop, remove: noop, contains: () => false, toggle: noop },
    addEventListener: noop,
    removeEventListener: noop,
    appendChild: noop,
    setAttribute: noop,
    getAttribute: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    focus: noop,
    click: noop,
    innerHTML: "",
});

const sandbox = {
    console,
    setTimeout: (fn) => {
        Promise.resolve().then(fn);
        return 0;
    },
    clearTimeout: noop,
    setInterval: () => 0,
    clearInterval: noop,
    window: {
        __nbotApi: {
            get: async (url) => {
                requests.push(url);
                return { data: serverPayload };
            },
            post: async () => ({ data: {} }),
        },
        __nbotSocket: { on: noop, emit: noop },
        __nbotCopyCodeBlock: noop,
        __nbotConnectSocketWithAuth: noop,
        addEventListener: noop,
        location: { pathname: "/", search: "" },
    },
    document: {
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: noop,
        removeEventListener: noop,
        createElement: fakeElement,
        head: { appendChild: noop },
        body: { classList: { add: noop, remove: noop }, appendChild: noop },
        documentElement: { setAttribute: noop, removeAttribute: noop, style: { setProperty: noop } },
    },
    localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
    sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
    navigator: { userAgent: "node-harness", clipboard: { writeText: async () => {} } },
    MutationObserver: class {
        observe() {}
        disconnect() {}
    },
    marked: { setOptions: noop, parse: (text) => text },
    hljs: { getLanguage: () => false, highlightAuto: () => ({ value: "" }) },
    Chart: class {
        constructor() {}
        destroy() {}
        update() {}
    },
    URL: { createObjectURL: () => "blob:mock", revokeObjectURL: noop },
    Blob: class {},
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    alert: noop,
    confirm: () => true,
};
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(source + "\n;globalThis.__methods = NbotMethods;", sandbox);

const methods = sandbox.__methods;
if (!methods || typeof methods.updateContextStats !== "function") {
    console.error("FAIL: 未能从 nbot-methods.js 取到 updateContextStats");
    process.exit(1);
}

const flush = async (times = 4) => {
    for (let i = 0; i < times; i += 1) {
        await new Promise((resolve) => setImmediate(resolve));
    }
};

const makeVm = (session, messages, aiConfig) => {
    const vmObj = Object.create(methods);
    vmObj.currentSession = session;
    vmObj.currentMessages = messages;
    vmObj.aiConfig = aiConfig || { max_context_length: 1000 };
    vmObj.contextCharCount = 0;
    vmObj.contextTokenEstimate = 0;
    vmObj.contextUsage = 0;
    vmObj.contextMessageCount = 0;
    vmObj.contextBreakdown = null;
    vmObj._contextStatsSignature = "";
    vmObj._contextStatsSessionId = "";
    vmObj.getContextStatMessages = methods.getContextStatMessages;
    return vmObj;
};

const results = [];
const check = (name, ok, detail) => {
    results.push({ name, ok, detail });
    if (!ok) console.error(`FAIL: ${name}${detail ? " — " + detail : ""}`);
};

(async () => {
    // ---- 场景 A：空会话（无系统提示词、无消息），反复 update 不应来回清空 ----
    requests.length = 0;
    serverPayload = {
        success: true,
        total: { chars: 0, tokens: 0, messages: 0 },
        categories: {
            system_prompt: { label: "系统提示词", chars: 0, tokens: 0, messages: 0 },
            user_messages: { label: "用户消息", chars: 0, tokens: 0, messages: 0 },
        },
        max_context_length: 1000,
    };
    const emptyVm = makeVm({ id: "empty-1", system_prompt: "" }, [], { max_context_length: 1000 });
    for (let i = 0; i < 6; i += 1) {
        emptyVm.updateContextStats();
        await flush();
    }
    check("空会话只请求一次", requests.length === 1, `requests=${requests.length}`);
    check("空会话 breakdown 保持存在", emptyVm.contextBreakdown !== null);
    check("空会话行列表为空", emptyVm.contextBreakdownRows().length === 0);

    // ---- 场景 B：有对话，重复 update 不应让数字跳动 ----
    requests.length = 0;
    serverPayload = {
        success: true,
        total: { chars: 123, tokens: 45, messages: 3 },
        categories: {
            system_prompt: { label: "系统提示词", chars: 40, tokens: 20, messages: 1 },
            user_messages: { label: "用户消息", chars: 20, tokens: 10, messages: 1 },
            assistant_messages: { label: "助手消息", chars: 63, tokens: 15, messages: 1 },
        },
        max_context_length: 1000,
    };
    const session = { id: "sess-1", system_prompt: "系统提示词" };
    const messages = [
        { id: "m1", role: "system", content: "系统提示词" },
        { id: "m2", role: "user", content: "你好" },
        { id: "m3", role: "assistant", content: "你好呀" },
    ];
    const chatVm = makeVm(session, messages, { max_context_length: 1000 });
    const seenCounts = new Set();
    for (let i = 0; i < 6; i += 1) {
        chatVm.updateContextStats();
        await flush();
        seenCounts.add(chatVm.contextMessageCount);
    }
    check("有对话只请求一次", requests.length === 1, `requests=${requests.length}`);
    check("消息数稳定为服务端值", chatVm.contextMessageCount === 3, `count=${chatVm.contextMessageCount}`);
    check("消息数没有出现多个取值", seenCounts.size === 1, `values=${[...seenCounts].join(",")}`);
    check("字符数来自服务端", chatVm.contextCharCount === 123);
    check("占比行按 tokens 过滤", chatVm.contextBreakdownRows().length === 3);

    // ---- 场景 C：消息变化后才重新统计 ----
    chatVm.currentMessages = messages.concat([{ id: "m4", role: "user", content: "再说一句" }]);
    serverPayload = {
        success: true,
        total: { chars: 200, tokens: 60, messages: 4 },
        categories: {
            system_prompt: { label: "系统提示词", chars: 40, tokens: 20, messages: 1 },
            user_messages: { label: "用户消息", chars: 120, tokens: 30, messages: 2 },
            assistant_messages: { label: "助手消息", chars: 40, tokens: 10, messages: 1 },
        },
        max_context_length: 1000,
    };
    chatVm.updateContextStats();
    await flush();
    check("消息变化后重新统计", requests.length === 2, `requests=${requests.length}`);
    check("新消息数已应用", chatVm.contextMessageCount === 4, `count=${chatVm.contextMessageCount}`);

    // ---- 场景 D：切换会话时清零再取新值 ----
    requests.length = 0;
    serverPayload = {
        success: true,
        total: { chars: 10, tokens: 5, messages: 1 },
        categories: { system_prompt: { label: "系统提示词", chars: 10, tokens: 5, messages: 1 } },
        max_context_length: 1000,
    };
    chatVm.currentSession = { id: "sess-2", system_prompt: "另一个角色" };
    chatVm.currentMessages = [{ id: "x1", role: "system", content: "另一个角色" }];
    chatVm.updateContextStats();
    check("切换会话先清零", chatVm.contextMessageCount === 0, `count=${chatVm.contextMessageCount}`);
    await flush();
    check("切换会话后取到新值", chatVm.contextMessageCount === 1, `count=${chatVm.contextMessageCount}`);
    check("切换会话只请求一次", requests.length === 1, `requests=${requests.length}`);

    const failed = results.filter((item) => !item.ok);
    if (failed.length) {
        console.error(`\n${failed.length} 项失败 / ${results.length} 项`);
        process.exit(1);
    }
    console.log(`全部通过（${results.length} 项断言）`);
})();
