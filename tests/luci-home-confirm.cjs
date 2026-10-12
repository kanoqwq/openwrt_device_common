// Exercise the production click handlers without issuing real control RPCs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,
  '../luci-app-t551m/htdocs/luci-static/resources/view/mu300/home.js'), 'utf8');
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
function fixture(on = false) {
  const buttons = {}, confirmations = [], requests = [];
  const root = {isConnected: true, querySelector(id) {
    return buttons[id] ||= {disabled: false, addEventListener() {}};
  }};
  const M = {
    confirmBox(...args) { const d = deferred(); confirmations.push({args, ...d}); return d.promise; },
    callAct(...args) { const d = deferred(); requests.push({args, ...d}); return d.promise; },
    busy() {}, toast() {},
  };
  const view = new Function('view', 'M', 'L', source)({extend: x => x}, M,
    {resolveDefault: p => p.catch(() => undefined)});
  view.refreshLock = () => {};
  view.wire(root);
  view.lastInfo = {wan: {up: on}, wifi: {up: on}};
  view.lastCell = {cfun: on ? 1 : 0};
  return {view, root, confirmations, requests,
    click: name => buttons['#mud-btn-' + name].onclick()};
}
async function flush() { await Promise.resolve(); await Promise.resolve(); }
(async () => {
  for (const on of [false, true]) {
    for (const [button, op, arg] of [
      ['data', 'data', on ? 'down' : 'up'], ['wifi', 'wifi', on ? 'off' : 'on'],
      ['radio', 'radio', on ? 'off' : 'on'], ['modem', 'modem-reset', null],
      ['reboot', 'reboot', null], ['android', 'os', 'android'],
    ]) {
      const f = fixture(on);
      let done = f.click(button);
      assert.equal(f.requests.length, 0, 'no request before confirmation');
      assert.equal(f.confirmations.length, 1);
      f.click(button); f.click('reboot');
      assert.equal(f.confirmations.length, 1, 'only one pending dialog');
      f.confirmations[0].resolve(false); await done;
      assert.equal(f.requests.length, 0, 'cancel must do nothing');
      done = f.click(button); f.confirmations[1].resolve(true); await flush();
      assert.deepEqual(f.requests[0].args, [op, arg]);
      f.click(button); assert.equal(f.confirmations.length, 2, 'pending RPC blocks repeats');
      f.requests[0].resolve({ok: true}); await done;
      assert.equal(f.view._quickPending, false);
    }
  }
  for (const leave of ['unload', 'detach']) {
    const f = fixture(); const done = f.click('reboot');
    if (leave === 'unload') f.view.unload(); else f.root.isConnected = false;
    f.confirmations[0].resolve(true); await done;
    assert.equal(f.requests.length, 0, 'leaving the page cancels control');
  }
  const f = fixture(); f.root.querySelector('#mud-btn-data').disabled = true;
  f.click('data'); assert.equal(f.confirmations.length, 0);
  console.log('PASS: all six controls, both toggle directions, cancel, duplicate clicks, pending RPC, page exit, disabled control');
})().catch(error => {console.error(error); process.exitCode = 1;});
