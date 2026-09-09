"""Exercise the actual dashboard script with a held registry fetch and fake time."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_slow_registry_does_not_accumulate_dashboard_polls():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the dashboard polling test")
    template = Path("app/web/templates/admin_sync.html").read_text()
    script = template.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
let now = 0, next = 0, registryCalls = 0, statusCalls = 0, release;
let locked = true, rejectStatus = false;
const timers = new Map();
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {textContent:'', innerHTML:'',
    classList:{toggle(){}}, addEventListener(){}, appendChild(){}});
  return elements.get(id);
}
const context = vm.createContext({console, Date,
  document:{getElementById:element, createElement:()=>element('new')},
  setTimeout(fn, ms){const id=++next; timers.set(id,{fn, at:now+ms}); return id;},
  clearTimeout(id){timers.delete(id);},
  setInterval(){throw new Error('Overlapping interval polling is forbidden');},
  fetch: async (url, options) => {
    if (url === '/api/sync/status') {
      statusCalls++;
      if (rejectStatus) throw new Error('transient failure');
      return {ok:true,json:async()=>({locked})};
    }
    assert.equal(url, '/api/admin/registry');
    assert.equal(options.headers['X-Agnes-Registry-Poll'], '1');
    registryCalls++;
    await new Promise(resolve => {release=resolve;});
    return {ok:true,json:async()=>({tables:[]})};
  }
});
const flush = async()=>{for(let i=0;i<20;i++) await Promise.resolve();};
async function advance(ms) {
  now += ms;
  for (const [id,t] of [...timers]) if(t.at <= now){timers.delete(id); t.fn();}
  await flush();
}
(async()=>{
  vm.runInContext(SCRIPT,context);
  await flush();
  assert.equal(registryCalls,1);
  for(let i=0;i<10;i++) {
    await advance(3000);
    await vm.runInContext('checkStatus()',context); // e.g. a manual trigger
  }
  assert.equal(statusCalls,1);
  assert.equal(registryCalls,1);
  assert.equal(timers.size,0);
  release(); await flush();
  assert.equal(timers.size,1);
  locked=false;
  await advance(3000);
  assert.equal(registryCalls,2); // final refresh after sync completion
  release(); await flush();
  assert.equal(element('status-label').textContent,'Idle');
  rejectStatus=true;
  await advance(5000);
  assert.equal(timers.size,1); // transient failures do not stop the chain
  rejectStatus=false;
  await advance(5000);
  assert.equal(registryCalls,2);
  assert.equal(timers.size,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-e", "const SCRIPT=" + json.dumps(script) + ";\n" + harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
