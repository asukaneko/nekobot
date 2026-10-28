const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');
const state = new WeakMap();
class Chart {
    constructor(dom) { state.set(this, { disposed: false, updates: 0, dom }); }
    setOption() { state.get(this).updates += 1; }
    resize() { assert(state.has(this), 'chart instance must retain its original identity'); }
    isDisposed() { return state.get(this).disposed; }
    getDom() { return state.get(this).dom; }
}
const created = [];
const scope = { console, window: { __nbotApi: {}, __nbotSocket: {} },
    echarts: { init(dom) { const chart = new Chart(dom); created.push(chart); return chart; },
        graphic: { LinearGradient: class {} } },
};
vm.createContext(scope);
vm.runInContext(fs.readFileSync(path.join(root, 'nbot/web/static/vendor/vue.global.prod.js'), 'utf8'), scope);
vm.runInContext(fs.readFileSync(path.join(root, 'nbot/web/static/js/nbot-methods.js'), 'utf8') +
    '\n;window.methods = NbotMethods;', scope);
const methods = scope.window.methods;
const callbacks = [];
const page = scope.Vue.reactive({ trendChart: null, platformChart: null,
    $refs: { trendChart: {}, platformChart: {} },
    $nextTick(callback) { const pending = Promise.resolve().then(callback); callbacks.push(pending); },
    fetchTrendData: async () => ({ times: ['12:00'], values: [1] }),
    fetchPlatformData: async () => [], selectedPeriod: 'day',
    updateTrendChart: methods.updateTrendChart, updatePlatformChart: methods.updatePlatformChart,
});
(async () => {
    await methods.initCharts.call(page);
    while (callbacks.length) await callbacks.shift();
    assert.equal(page.trendChart, created[0]);
    assert.equal(page.platformChart, created[1]);
    assert.equal(state.get(created[0]).updates, 1);
    assert.equal(state.get(created[1]).updates, 1);
    let resolve;
    page.fetchTrendData = () => new Promise(done => { resolve = done; });
    const pending = methods.updateTrendChart.call(page);
    page.trendChart = null;
    state.get(created[0]).disposed = true;
    resolve({ times: [], values: [] });
    await pending;
    assert.equal(state.get(created[0]).updates, 1);
    console.log('Dashboard charts: raw Vue instances and page-switch cancellation passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
