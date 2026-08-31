const scenarios = document.querySelector('#scenarios');
const stopButton = document.querySelector('#stop');
let active = false;

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
  return data;
}

async function loadScenarios() {
  try {
    const data = await api('/api/scenarios');
    scenarios.innerHTML = '';
    for (const item of data.scenarios) {
      const card = document.createElement('article');
      card.className = 'card';
      const name = document.createElement('h2'); name.textContent = item.name || item.id;
      const desc = document.createElement('p'); desc.textContent = item.description || '';
      const equipment = document.createElement('div'); equipment.className = 'equipment'; equipment.textContent = (item.equipment || []).join(' · ');
      const button = document.createElement('button'); button.textContent = 'Run scenario'; button.dataset.run = item.id;
      button.onclick = () => start(item.id);
      card.append(name, desc, equipment, button); scenarios.append(card);
    }
  } catch (error) { scenarios.innerHTML = `<div class="empty">${error.message}</div>`; }
}

async function start(id) {
  if (!confirm('Start this RF scenario? Confirm the RF path and attenuation are safe.')) return;
  try { await api('/api/run', {method: 'POST', body: JSON.stringify({scenario_id: id})}); await poll(); }
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
  document.querySelector('#connection').textContent = 'Server online';
  document.querySelector('#connection').className = 'status running';
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
  catch (_) { document.querySelector('#connection').textContent = 'Server offline'; document.querySelector('#connection').className = 'status failed'; }
}

loadScenarios(); poll(); setInterval(poll, 1000);
