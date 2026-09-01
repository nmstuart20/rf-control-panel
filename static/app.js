const scenarioForm = document.querySelector('#scenario-form');
const scenarioSelect = document.querySelector('#scenario-select');
const scenarioArguments = document.querySelector('#scenario-arguments');
const runButton = document.querySelector('#run-scenario');
const hardware = document.querySelector('#hardware');
const stopButton = document.querySelector('#stop');
let active = false;
let scenarioCatalog = [];

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
  return data;
}

async function loadScenarios() {
  try {
    const data = await api('/api/scenarios');
    scenarioCatalog = data.scenarios;
    scenarioSelect.replaceChildren();
    for (const item of scenarioCatalog) {
      const option = document.createElement('option'); option.value = item.id; option.textContent = item.name || item.id;
      scenarioSelect.append(option);
    }
    scenarioSelect.disabled = !scenarioCatalog.length;
    renderScenario();
  } catch (error) {
    scenarioSelect.replaceChildren(new Option(error.message, ''));
  }
}

function renderScenario() {
  const scenario = scenarioCatalog.find(item => item.id === scenarioSelect.value);
  scenarioArguments.replaceChildren();
  document.querySelector('#scenario-equipment').textContent = scenario?.equipment?.length ? `Uses: ${scenario.equipment.join(', ')}` : '';
  for (const argument of scenario?.arguments || []) {
    const field = document.createElement('div'); field.className = 'field';
    const label = document.createElement('label'); label.htmlFor = `argument-${argument.id}`; label.textContent = argument.label || argument.id;
    const wrapper = document.createElement('div'); wrapper.className = 'input-with-unit';
    const input = document.createElement('input'); input.type = 'number'; input.id = `argument-${argument.id}`;
    input.name = argument.id; input.value = argument.default; input.required = true;
    if (argument.min !== undefined) input.min = argument.min;
    if (argument.max !== undefined) input.max = argument.max;
    input.step = argument.step ?? 'any';
    wrapper.append(input);
    if (argument.unit) { const unit = document.createElement('span'); unit.textContent = argument.unit; wrapper.append(unit); }
    field.append(label, wrapper); scenarioArguments.append(field);
  }
  runButton.disabled = !scenario || active;
}

scenarioSelect.addEventListener('change', renderScenario);
scenarioForm.addEventListener('submit', event => {
  event.preventDefault();
  const argumentsToSend = Object.fromEntries(new FormData(scenarioForm));
  start(scenarioSelect.value, argumentsToSend);
});

async function loadHardware() {
  try {
    const data = await api('/api/hardware');
    hardware.replaceChildren();
    for (const item of data.hardware) {
      const row = document.createElement('div'); row.className = 'hardware-item';
      const name = document.createElement('strong'); name.textContent = item.name;
      const status = document.createElement('span'); status.className = `status ${item.state}`;
      const statusLabels = {not_configured: 'Not configured', in_use: 'In use'};
      status.textContent = statusLabels[item.state] || item.state;
      row.append(name, status);
      if (item.state !== 'connected') {
        const detail = document.createElement('p'); detail.textContent = item.detail;
        row.append(detail);
      }
      hardware.append(row);
    }
    if (!data.hardware.length) {
      const notice = document.createElement('p'); notice.className = 'notice';
      notice.textContent = 'No hardware is listed in the loaded scenarios.';
      hardware.append(notice);
    }
  } catch (error) {
    hardware.replaceChildren();
    const notice = document.createElement('p'); notice.className = 'notice';
    notice.textContent = `Unable to check hardware: ${error.message}`;
    hardware.append(notice);
  }
}

async function start(id, argumentsToSend) {
  try { await api('/api/run', {method: 'POST', body: JSON.stringify({scenario_id: id, arguments: argumentsToSend})}); await poll(); }
  catch (error) { alert(error.message); }
}

stopButton.onclick = async () => {
  try { await api('/api/stop', {method: 'POST', body: '{}'}); await poll(); }
  catch (error) { alert(error.message); }
};

function render(run) {
  const running = run && ['starting', 'running', 'stopping'].includes(run.state);
  active = running;
  document.querySelectorAll('[data-run]').forEach(button => button.disabled = running);
  stopButton.disabled = !running || run.state === 'stopping';
  document.querySelector('#run-name').textContent = run ? run.scenario_name : 'No scenario running';
  const state = document.querySelector('#run-state'); state.textContent = run ? run.state : 'Idle'; state.className = `status ${run ? run.state : 'idle'}`;
  document.querySelector('#run-step').textContent = run?.current_step || 'Ready';
  document.querySelector('#run-time').textContent = run ? `Started ${new Date(run.started_at * 1000).toLocaleTimeString()}` : '—';
  const logs = document.querySelector('#logs');
  const next = run?.logs?.join('\n') || 'Waiting for a scenario…';
  if (logs.textContent !== next) { logs.textContent = next; logs.scrollTop = logs.scrollHeight; }
}

async function poll() {
  try { const data = await api('/api/status'); render(data.run); }
  catch (_) { /* The next successful poll will refresh the run state. */ }
}

loadScenarios(); loadHardware(); poll();
setInterval(poll, 1000);
setInterval(loadHardware, 10000);
