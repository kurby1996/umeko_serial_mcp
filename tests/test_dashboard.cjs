// Bounded-rendering regression checks using a small DOM model; no serial access.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('src/umeko_serial_mcp/dashboard.html', 'utf8');
const fullScript = html.match(/<script>([\s\S]*?)<\/script>/)[1].replaceAll('{WS_PORT}', '8081');
new vm.Script(fullScript); // Syntax-check the entire dashboard, not only extracted functions.
class Element {
    constructor(fragment = false) { this.children = []; this.fragment = fragment; }
    appendChild(node) {
        if (node.fragment) { for (const child of [...node.children]) this.appendChild(child); }
        else { this.children.push(node); node.parent = this; }
    }
    remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
    set innerHTML(value) { this.children = []; this.html = value; }
    get innerHTML() { return this.html || this.textContent || ''; }
    get childElementCount() { return this.children.length; }
    get firstElementChild() { return this.children[0]; }
}
const context = vm.createContext({ assert, console, Element,
    document: { createElement: () => new Element(), createDocumentFragment: () => new Element(true) },
    performance: { now: () => 0 },
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {}, clearTimeout: () => {},
});
const functions = html.slice(html.indexOf('        function escapeHtml('), html.indexOf('        // ---------- 导出 TXT'));
vm.runInContext(`
    const LOG_STORE_MAX = 2000, LOG_PENDING_MAX = 1000;
    const logContainer = new Element(), logCountLabel = new Element();
    const logStore = [], pendingLogs = [];
    let seenSeq = new Set(), skippedLogCount = 0, clearedThroughSeq = 0;
    let logRenderFrame = null, displayCount = 0, lastLogSource = null;
    let followLogs = true, pausedNewCount = 0, logScrollProgrammatic = false;
    let logSearchRefreshTimer = null, logSearchMatches = [], logSearchIndex = -1;
    let scrolls = 0;
    const showTimestampEl = { checked: true }, showSourceEl = { checked: true };
    const rxDisplayMode = { value: 'text' };
    function scrollLogToBottom() { scrolls++; }
    function scheduleLogSearchRefresh() {}
    function updateFollowHint() {}
    function refreshLogSearch() {}
    function clearLogSearchHighlights() {}
    function updateLogSearchStatus() {}
    ${functions}
    for (let i = 1; i <= 20000; i++) appendLogEntry('HARDWARE', 'line ' + i, i, 1, false, '41');
    assert.equal(pendingLogs.length, 1000);
    assert.equal(skippedLogCount, 19000);
    assert.equal(logContainer.childElementCount, 0);
    while (pendingLogs.length) flushPendingLogs();
    assert.equal(logStore.at(-1).seq, 20000);
    assert.equal(scrolls, 5); // One scroll per batch, not one per line.
    for (let group = 0; group < 8; group++) {
        for (let i = 1; i <= 500; i++) appendLogEntry('HARDWARE', 'line', 20000 + group * 500 + i, 1, false, '41');
        while (pendingLogs.length) flushPendingLogs();
    }
    assert.equal(logStore.length, 2000);
    assert.equal(logContainer.childElementCount, 2000);
    rebuildLogView();
    assert.equal(logContainer.childElementCount, 2000);
    appendLogEntry('HARDWARE', 'duplicate', 24000, 1);
    flushPendingLogs();
    assert.equal(logStore.length, 2000);
    assert.equal(logStore.at(-1).msg, 'line');
    appendLogEntry('HARDWARE', 'queued before clear', 24001, 1);
    clearedThroughSeq = 24001;
    clearLogView();
    appendLogEntry('HARDWARE', 'stale in flight', 24001, 1);
    assert.equal(pendingLogs.length, 0);
    appendLogEntry('HARDWARE', 'after clear', 24002, 1);
    flushPendingLogs();
    assert.equal(logContainer.childElementCount, 1);
    assert.equal(logStore[0].msg, 'after clear');
    appendLogEntry('HARDWARE', 'newer queued', 24003, 1);
    applyLogClear(24001); // A delayed HTTP response after the WS clear event.
    assert.equal(logStore[0].msg, 'after clear');
    assert.equal(pendingLogs.length, 1);
    while (pendingLogs.length) flushPendingLogs();
    assert.equal(logStore.at(-1).msg, 'newer queued');
    applyLogClear(24001); // Duplicate clear events must preserve new records.
    assert.equal(logStore.length, 2);
    followLogs = false;
    appendLogEntry('HARDWARE', 'paused', 24004, 1);
    flushPendingLogs();
    assert.equal(pausedNewCount, 1);
    console.log('Dashboard: burst, batching, bounds, rebuild, duplicate, clear and paused-follow checks passed.');
`, context);
