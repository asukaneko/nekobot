const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../nbot/web/static/js/nbot-methods.js'), 'utf8');
const sandbox = { window: {
    __nbotApi: {}, __nbotSocket: {}, location: { origin: 'https://panel.example' },
} };
vm.createContext(sandbox);
vm.runInContext(source + '\n;globalThis.methods = NbotMethods;', sandbox);
const methods = sandbox.methods;
const sent = [];
const frameWindow = { postMessage(payload, target) { sent.push({ payload, target }); } };
const page = {
    $refs: { pluginFrame: { contentWindow: frameWindow } },
    pluginBridgeQueue: [], pluginBridgeSeq: 0,
    processPluginBridge() { this.called = true; },
};
const data = { id: 'request', method: 'kv.get', params: { key: 'demo' } };
methods.onPluginMessage.call(page, { source: frameWindow, origin: 'null', data });
assert.equal(page.pluginBridgeQueue.length, 1);
assert.equal(page.called, true);
methods.onPluginMessage.call(page, { source: {}, origin: 'null', data });
methods.onPluginMessage.call(page, { source: frameWindow, origin: 'https://other.example', data });
assert.equal(page.pluginBridgeQueue.length, 1);
methods.postToPluginFrame.call(page, { ok: true, id: 'request' });
assert.equal(sent[0].target, '*');
assert.equal(sent[0].payload.id, 'request');
console.log('Plugin bridge: opaque frame accepted, foreign windows rejected, reply targeted to frame.');
