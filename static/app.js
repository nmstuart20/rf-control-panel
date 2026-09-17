const ACTIVE_STATES = ['starting', 'running', 'stopping', 'cleanup'];
const RUN_STATE_LABELS = {
  starting: 'Starting', running: 'Running', stopping: 'Stopping',
  cleanup: 'Cleanup', stopped: 'Stopped', completed: 'Completed',
  failed: 'Failed', interrupted: 'Interrupted',
};
const RUN_STATE_TONES = {
  starting: 'warn', running: 'info', stopping: 'warn', cleanup: 'warn',
  stopped: 'idle', completed: 'ok', failed: 'bad', interrupted: 'bad',
};
const SAFE_STATE_LABELS = {
  pending: 'Not reached', ok: 'Confirmed', degraded: 'Partly confirmed',
  failed: 'NOT CONFIRMED', skipped: 'Not configured',
};
const HARDWARE_LABELS = {
  connected: 'Connected', disconnected: 'Disconnected',
  not_configured: 'Not configured', in_use: 'In use', unknown: 'Checking…',
};
const HARDWARE_TONES = {connected: 'ok', disconnected: 'bad', not_configured: 'warn', in_use: 'idle', unknown: 'idle'};

const el = id => document.getElementById(id);
const scenarioList = el('scenario-list');
const scenarioForm = el('scenario-form');
const scenarioArguments = el('scenario-arguments');
const runButton = el('run-scenario');
const stopButton = el('stop');
const logsBox = el('logs');
const logEmpty = el('log-empty');
const confirmDialog = el('confirm');
const rfSwitchAuthDialog = el('rf-switch-auth');
const scenarioPickerPanel = el('scenario-picker-panel');
const addScenarioButton = el('add-scenario');
const hardwarePickerPanel = el('hardware-picker-panel');
const addHardwareButton = el('add-hardware');
const newScenarioHardware = el('new-scenario-hardware');
const addScenarioForm = el('add-scenario-form');
const newScenarioArguments = el('new-scenario-arguments');
const newScenarioSteps = el('new-scenario-steps');
const newScenarioStopSteps = el('new-scenario-stop-steps');
const addHardwareForm = el('add-hardware-form');
const addHardwareSubmit = el('add-hardware-submit');
const safeStateButton = el('safe-state');
const historyList = el('history-list');
const historyDetail = el('history-detail');

let scenarios = [];
let selectedId = null;
let hardware = new Map();
let hardwareOptions = [];
let currentRun = null;
let locked = false;
let stopPending = false;
let logView = {runId: null, rendered: 0};
let stepTracker = {runId: null, index: -1};
let activeTab = 'scenarios';
let safeStatePending = false;
let selectedHistoryId = null;
let rfSwitchUnlocked = true;
let editorFieldSequence = 0;

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
  return data;
}

function showError(node, message) {
  node.textContent = message;
  node.hidden = false;
}

function clearError(node) {
  node.textContent = '';
  node.hidden = true;
}

function setState(node, tone, label) {
  node.className = `state ${tone}`;
  node.textContent = label;
}

function selectedScenario() {
  return scenarios.find(item => item.id === selectedId) || null;
}

/* Scenario catalog ------------------------------------------------------- */

async function loadScenarios() {
  try {
    const data = await api('/api/scenarios');
    scenarios = data.scenarios || [];
    clearError(el('catalog-error'));
    renderScenarioList();
    selectScenario(scenarios.some(item => item.id === selectedId) ? selectedId : scenarios[0]?.id ?? null);
    if (currentRun) renderSteps(currentRun); // A poll may have landed before the catalog did.
  } catch (error) {
    scenarioList.replaceChildren();
    showError(el('catalog-error'), `Unable to load scenarios: ${error.message}`);
  }
}

function renderScenarioList() {
  scenarioList.replaceChildren();
  if (!scenarios.length) {
    const option = document.createElement('option');
    option.textContent = 'No scenarios are defined';
    option.value = '';
    scenarioList.append(option);
    return;
  }
  for (const scenario of scenarios) {
    const option = document.createElement('option');
    option.value = scenario.id;
    option.textContent = scenario.name || scenario.id;
    scenarioList.append(option);
  }
  scenarioList.value = selectedId || scenarios[0].id;
}

function selectTab(tab) {
  activeTab = tab;
  const leftTab = tab === 'add-scenario' ? 'scenarios' : tab === 'add-hardware' ? 'hardware' : tab;
  for (const name of ['scenarios', 'hardware', 'rf-switch', 'history', 'add-scenario', 'add-hardware']) {
    const button = el(`tab-${name}`);
    const panel = el(`panel-${name}`);
    const selected = name === tab;
    if (button) {
      const leftSelected = name === leftTab;
      button.classList.toggle('active', leftSelected);
      button.setAttribute('aria-selected', String(leftSelected));
    }
    panel.hidden = !selected;
  }
  el('run-section').hidden = tab !== 'scenarios';
  if (tab === 'history') loadHistory();
  scenarioPickerPanel.classList.toggle('is-visible', leftTab === 'scenarios');
  scenarioPickerPanel.setAttribute('aria-hidden', String(leftTab !== 'scenarios'));
  hardwarePickerPanel.classList.toggle('is-visible', leftTab === 'hardware');
  hardwarePickerPanel.setAttribute('aria-hidden', String(leftTab !== 'hardware'));
}

async function requestRfSwitchAccess() {
  if (rfSwitchAuthDialog.open) return;
  clearError(el('rf-switch-auth-error'));
  el('rf-switch-password').value = '';
  rfSwitchAuthDialog.showModal();
  el('rf-switch-password').focus();
}

async function unlockRfSwitch(event) {
  event.preventDefault();
  clearError(el('rf-switch-auth-error'));
  try {
    await api('/api/rf-switch/access', {
      method: 'POST',
      body: JSON.stringify({password: el('rf-switch-password').value}),
    });
    rfSwitchUnlocked = true;
    rfSwitchAuthDialog.close();
    selectTab('rf-switch');
  } catch (error) {
    showError(el('rf-switch-auth-error'), error.message);
  }
}

function selectScenario(id) {
  selectedId = id;
  scenarioList.value = id || '';
  renderConfiguration();
}

/* Scenario configuration ------------------------------------------------- */

function renderConfiguration() {
  const scenario = selectedScenario();
  el('config').hidden = !scenario;
  clearError(el('run-error'));
  if (!scenario) return;

  el('config-name').textContent = scenario.name || scenario.id;
  const description = el('config-description');
  description.textContent = scenario.description || '';
  description.hidden = !scenario.description;

  renderScenarioHardware();
  renderArguments(scenario.arguments || []);
  runButton.disabled = locked;
}

function renderArguments(definitions) {
  scenarioArguments.replaceChildren();
  el('parameters-heading').hidden = !definitions.length;
  for (const argument of definitions) {
    const field = document.createElement('div');
    field.className = 'field';

    const label = document.createElement('label');
    label.htmlFor = `argument-${argument.id}`;
    label.textContent = argument.label || argument.id;

    const wrapper = document.createElement('div');
    wrapper.className = 'input-unit';

    const input = document.createElement('input');
    input.type = 'number';
    input.id = `argument-${argument.id}`;
    input.name = argument.id;
    input.value = argument.default;
    input.required = true;
    input.disabled = locked;
    if (argument.min !== undefined) input.min = argument.min;
    if (argument.max !== undefined) input.max = argument.max;
    input.step = argument.step ?? 'any';
    wrapper.append(input);

    if (argument.unit) {
      const unit = document.createElement('span');
      unit.className = 'unit';
      unit.textContent = argument.unit;
      wrapper.append(unit);
    }
    field.append(label, wrapper);
    scenarioArguments.append(field);
  }
}

/* Hardware --------------------------------------------------------------- */

async function loadHardware() {
  try {
    const data = await api('/api/hardware');
    hardware = new Map((data.hardware || []).map(item => [item.name, item]));
  } catch (error) {
    hardware = new Map();
  }
  renderHardwareStatus();
  renderScenarioHardware();
}

async function loadHardwareOptions() {
  try {
    const data = await api('/api/hardware/options');
    hardwareOptions = data.hardware || [];
    renderHardwareOptions(hardwareOptions.map(name => ({name})));
  } catch (_) {
    if (!hardwareOptions.length && hardware.size) {
      hardwareOptions = [...hardware.keys()];
      renderHardwareOptions(hardwareOptions.map(name => ({name})));
    }
  }
}

function renderScenarioHardware() {
  const list = el('config-hardware');
  const names = selectedScenario()?.equipment || [];
  list.replaceChildren();
  if (!names.length) {
    const item = document.createElement('li');
    item.className = 'detail';
    item.textContent = 'This scenario does not declare any equipment.';
    list.append(item);
    return;
  }
  for (const name of names) {
    const check = hardware.get(name);
    const item = document.createElement('li');

    const label = document.createElement('span');
    label.textContent = name;

    const status = document.createElement('span');
    const state = check?.state ?? 'unknown';
    setState(status, HARDWARE_TONES[state] || 'idle', HARDWARE_LABELS[state] || state);

    item.append(label, status);
    list.append(item);
  }
}

function renderHardwareStatus() {
  const list = el('hardware-status');
  const items = [...hardware.values()];
  list.replaceChildren();
  if (!items.length) {
    const item = document.createElement('li');
    item.className = 'detail';
    item.textContent = 'No hardware configured.';
    list.append(item);
    return;
  }
  for (const check of items) {
    const item = document.createElement('li');

    const label = document.createElement('span');
    label.textContent = check.name;

    const status = document.createElement('span');
    const state = check.state || 'unknown';
    setState(status, HARDWARE_TONES[state] || 'idle', HARDWARE_LABELS[state] || state);

    item.append(label, status);
    if (/modem/i.test(check.name) && check.tx_state) {
      const tx = document.createElement('span');
      tx.className = `tx-status ${check.tx_state === 'on' ? 'on' : 'off'}`;
      tx.textContent = `TX ${check.tx_state.toUpperCase()}`;
      item.append(tx);
    }
    list.append(item);
  }
}

function renderHardwareOptions(items) {
  const selected = new Set(
    [...newScenarioHardware.querySelectorAll('input:checked')].map(input => input.value),
  );
  newScenarioHardware.replaceChildren();
  if (!items.length) {
    const message = document.createElement('p');
    message.className = 'subtle';
    message.textContent = 'No hardware checks are configured.';
    newScenarioHardware.append(message);
    return;
  }
  for (const item of items) {
    const label = document.createElement('label');
    label.className = 'hardware-choice';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = 'equipment';
    input.value = item.name;
    input.checked = selected.has(item.name);
    const text = document.createElement('span');
    text.textContent = item.name;
    label.append(input, text);
    newScenarioHardware.append(label);
  }
}

/* Scenario editor -------------------------------------------------------- */

function templateElement(id) {
  return el(id).content.firstElementChild.cloneNode(true);
}

function connectEditorLabels(root) {
  for (const field of root.querySelectorAll('.field')) {
    const label = field.querySelector('label');
    const input = field.querySelector('input, select, textarea');
    if (!label || !input) continue;
    input.id = `scenario-editor-field-${++editorFieldSequence}`;
    label.htmlFor = input.id;
  }
}

function updateEditorEmptyStates() {
  el('arguments-empty').hidden = Boolean(newScenarioArguments.children.length);
  el('stop-steps-empty').hidden = Boolean(newScenarioStopSteps.children.length);
  updateStepControls(newScenarioSteps, 'Step');
  updateStepControls(newScenarioStopSteps, 'Cleanup step');
}

function updateStepControls(container, label) {
  const cards = [...container.children];
  cards.forEach((card, index) => {
    card.querySelector('h4').textContent = `${label} ${index + 1}`;
    card.querySelector('.move-up').disabled = index === 0;
    card.querySelector('.move-down').disabled = index === cards.length - 1;
  });
}

function addArgumentEditor() {
  const card = templateElement('argument-editor-template');
  connectEditorLabels(card);
  card.querySelector('.remove-item').addEventListener('click', () => {
    card.remove();
    updateEditorEmptyStates();
  });
  newScenarioArguments.append(card);
  updateEditorEmptyStates();
  card.querySelector('[data-field="id"]').focus();
}

function addCommandPart(card, value = '') {
  const row = templateElement('command-part-template');
  const input = row.querySelector('input');
  input.value = value;
  row.querySelector('.remove-inline').addEventListener('click', () => {
    row.remove();
    updateCommandPartControls(card);
  });
  card.querySelector('.command-parts').append(row);
  updateCommandPartControls(card);
  return input;
}

function updateCommandPartControls(card) {
  const rows = card.querySelectorAll('.command-part');
  for (const button of card.querySelectorAll('.command-part .remove-inline')) {
    button.disabled = rows.length === 1;
  }
}

function addEnvironmentPart(card) {
  const row = templateElement('environment-part-template');
  row.querySelector('.remove-inline').addEventListener('click', () => row.remove());
  card.querySelector('.environment-parts').append(row);
  row.querySelector('.environment-name').focus();
}

function addStepEditor(container, cleanup = false) {
  const card = templateElement('step-editor-template');
  connectEditorLabels(card);
  if (cleanup) card.querySelector('.background-choice').remove();
  card.querySelector('.remove-item').addEventListener('click', () => {
    card.remove();
    updateEditorEmptyStates();
  });
  card.querySelector('.move-up').addEventListener('click', () => {
    if (card.previousElementSibling) container.insertBefore(card, card.previousElementSibling);
    updateEditorEmptyStates();
  });
  card.querySelector('.move-down').addEventListener('click', () => {
    if (card.nextElementSibling) container.insertBefore(card.nextElementSibling, card);
    updateEditorEmptyStates();
  });
  card.querySelector('.add-command-part').addEventListener('click', () => addCommandPart(card).focus());
  card.querySelector('.add-environment').addEventListener('click', () => addEnvironmentPart(card));
  container.append(card);
  addCommandPart(card);
  updateEditorEmptyStates();
  card.querySelector('[data-field="name"]').focus();
}

function slugify(value) {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

function resetScenarioEditor() {
  addScenarioForm.reset();
  el('new-scenario-id').dataset.touched = '';
  newScenarioArguments.replaceChildren();
  newScenarioSteps.replaceChildren();
  newScenarioStopSteps.replaceChildren();
  clearError(el('add-scenario-error'));
  renderHardwareOptions(hardwareOptions.map(name => ({name})));
  addStepEditor(newScenarioSteps);
}

function openScenarioEditor() {
  if (!newScenarioSteps.children.length) resetScenarioEditor();
  selectTab('add-scenario');
  el('new-scenario-name').focus();
}

function optionalNumber(card, field) {
  const value = card.querySelector(`[data-field="${field}"]`).value;
  return value === '' ? undefined : Number(value);
}

function readArgument(card) {
  const argument = {
    id: card.querySelector('[data-field="id"]').value.trim(),
    label: card.querySelector('[data-field="label"]').value.trim(),
    type: card.querySelector('[data-field="type"]').value,
    default: Number(card.querySelector('[data-field="default"]').value),
  };
  for (const field of ['min', 'max', 'step']) {
    const value = optionalNumber(card, field);
    if (value !== undefined) argument[field] = value;
  }
  const unit = card.querySelector('[data-field="unit"]').value.trim();
  if (unit) argument.unit = unit;
  return argument;
}

function readStep(card) {
  const step = {
    name: card.querySelector('[data-field="name"]').value.trim(),
    command: [...card.querySelectorAll('.command-part input')].map(input => input.value.trim()),
  };
  const background = card.querySelector('[data-field="background"]');
  if (background?.checked) step.background = true;
  const environment = Object.create(null);
  for (const row of card.querySelectorAll('.environment-part')) {
    const name = row.querySelector('.environment-name').value.trim();
    if (Object.hasOwn(environment, name)) throw new Error(`Environment variable ${name} is repeated in ${step.name}.`);
    environment[name] = row.querySelector('.environment-value').value;
  }
  if (Object.keys(environment).length) step.environment = environment;
  return step;
}

function scenarioFromEditor() {
  const scenario = {
    id: el('new-scenario-id').value.trim(),
    name: el('new-scenario-name').value.trim(),
    equipment: [...newScenarioHardware.querySelectorAll('input:checked')].map(input => input.value),
    arguments: [...newScenarioArguments.children].map(readArgument),
    steps: [...newScenarioSteps.children].map(readStep),
  };
  const description = el('new-scenario-description').value.trim();
  if (description) scenario.description = description;
  const stopSteps = [...newScenarioStopSteps.children].map(readStep);
  if (stopSteps.length) scenario.stop_steps = stopSteps;
  if (!scenario.equipment.length) throw new Error('Select at least one configured hardware item.');
  if (!scenario.steps.length) throw new Error('Add at least one scenario step.');
  return scenario;
}

async function createScenario(event) {
  event.preventDefault();
  clearError(el('add-scenario-error'));
  const button = el('create-scenario');
  button.disabled = true;
  button.textContent = 'Creating…';
  try {
    const scenario = scenarioFromEditor();
    await api('/api/scenarios', {method: 'POST', body: JSON.stringify({scenario})});
    selectedId = scenario.id;
    resetScenarioEditor();
    await loadScenarios();
    selectTab('scenarios');
  } catch (error) {
    showError(el('add-scenario-error'), `Unable to create scenario: ${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = 'Create scenario';
  }
}

/* Starting and stopping -------------------------------------------------- */

function formValues() {
  return Object.fromEntries(new FormData(scenarioForm));
}

function formatNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString('en-US', {maximumFractionDigits: 6}) : String(value);
}

function openConfirmation() {
  const scenario = selectedScenario();
  if (!scenario) return;
  const values = formValues();

  el('confirm-title').textContent = `Run ${scenario.name || scenario.id}?`;

  const params = el('confirm-params');
  params.replaceChildren();
  for (const argument of scenario.arguments || []) {
    const term = document.createElement('dt');
    term.textContent = argument.label || argument.id;
    const value = document.createElement('dd');
    value.textContent = `${formatNumber(values[argument.id])}${argument.unit ? ` ${argument.unit}` : ''}`;
    params.append(term, value);
  }
  params.hidden = !(scenario.arguments || []).length;

  const equipment = el('confirm-equipment');
  equipment.replaceChildren();
  for (const name of scenario.equipment || []) {
    const item = document.createElement('li');
    const check = hardware.get(name);
    item.textContent = check ? `${name} — ${HARDWARE_LABELS[check.state] || check.state}` : name;
    equipment.append(item);
  }
  if (!(scenario.equipment || []).length) {
    const item = document.createElement('li');
    item.className = 'subtle';
    item.textContent = 'None declared.';
    equipment.append(item);
  }
  confirmDialog.returnValue = ''; // Never inherit an earlier confirmation.
  confirmDialog.showModal();
}

async function startRun() {
  const scenario = selectedScenario();
  if (!scenario) return;
  clearError(el('run-error'));
  runButton.disabled = true;
  try {
    await api('/api/run', {
      method: 'POST',
      body: JSON.stringify({scenario_id: scenario.id, arguments: formValues()}),
    });
    await poll();
  } catch (error) {
    showError(el('run-error'), `Run request failed: ${error.message}`);
    runButton.disabled = locked;
  }
}

async function stopRun() {
  if (stopPending) return;
  stopPending = true;
  stopButton.disabled = true;
  stopButton.textContent = 'Stopping…';
  clearError(el('stop-error'));
  try {
    await api('/api/stop', {method: 'POST', body: '{}'});
    await poll();
  } catch (error) {
    showError(el('stop-error'), `Stop request failed: ${error.message}`);
  } finally {
    stopPending = false;
  }
}

async function requestSafeState() {
  if (safeStatePending) return;
  safeStatePending = true;
  safeStateButton.disabled = true;
  clearError(el('stop-error'));
  try {
    await api('/api/safe-state', {method: 'POST', body: '{}'});
    await poll();
  } catch (error) {
    showError(el('stop-error'), `All stop failed: ${error.message}`);
  } finally {
    safeStatePending = false;
    safeStateButton.disabled = false;
  }
}

/* Run status ------------------------------------------------------------- */

function setLocked(value) {
  locked = value;
  scenarioList.disabled = value;
  for (const input of scenarioArguments.querySelectorAll('input')) input.disabled = value;
  runButton.disabled = value || !selectedScenario();
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const parts = [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60];
  return parts.map(part => String(part).padStart(2, '0')).join(':');
}

function renderRun(run) {
  const previous = currentRun;
  currentRun = run;
  const running = Boolean(run) && ACTIVE_STATES.includes(run.state);
  setLocked(running);
  if (previous && run && previous.id === run.id
      && ACTIVE_STATES.includes(previous.state) && !running && activeTab === 'history') {
    loadHistory();
  }

  stopButton.disabled = !running || stopPending || run.state === 'stopping';
  stopButton.textContent = stopPending || run?.state === 'stopping' ? 'Stopping…' : 'Stop scenario';

  const state = run ? run.state : 'idle';
  setState(el('run-state'), run ? RUN_STATE_TONES[state] || 'idle' : 'idle', run ? RUN_STATE_LABELS[state] || state : 'Idle');
  el('run-name').textContent = run ? run.scenario_name : 'No scenario has been run yet.';

  renderSteps(run);
  renderMeta(run);
  renderLogs(run);
}

function renderSteps(run) {
  const list = el('run-steps');
  const names = run ? scenarios.find(item => item.id === run.scenario_id)?.step_names || [] : [];
  list.hidden = !names.length;
  list.replaceChildren();
  if (!names.length) return;

  if (stepTracker.runId !== run.id) stepTracker = {runId: run.id, index: -1};
  const current = run.current_step ? names.indexOf(run.current_step) : -1;
  if (current >= 0) stepTracker.index = current;
  // After a reload the tracker is empty, so fall back to the last "Step N:" log line.
  const last = Math.max(stepTracker.index, lastLoggedStepIndex(run));

  names.forEach((name, index) => {
    const status = stepStatus(run, index, current, last);
    const item = document.createElement('li');
    item.className = `step ${status}`;

    const icon = document.createElement('span');
    icon.className = 'step-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = {done: '✓', active: '●', failed: '✕', pending: '○'}[status];

    const label = document.createElement('span');
    label.className = 'step-name';
    label.textContent = name;

    const readable = document.createElement('span');
    readable.className = 'sr-only';
    readable.textContent = ` — ${status === 'active' ? 'in progress' : status}`;

    item.append(icon, label, readable);
    list.append(item);
  });
}

function lastLoggedStepIndex(run) {
  const lines = run.logs || [];
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const match = /^\[\d{2}:\d{2}:\d{2}\] Step (\d+):/.exec(lines[index]);
    if (match) return Number(match[1]) - 1;
  }
  return -1;
}

function stepStatus(run, index, current, last) {
  if (run.state === 'completed') return 'done';
  if (current >= 0) return index < current ? 'done' : index === current ? 'active' : 'pending';
  if (index < last) return 'done';
  if (index === last) return run.state === 'failed' ? 'failed' : 'pending';
  return 'pending';
}

function renderMeta(run) {
  el('run-meta').hidden = !run;
  if (!run) return;
  el('run-started').textContent = new Date(run.started_at * 1000).toLocaleTimeString();
  el('run-elapsed-label').textContent = run.finished_at ? 'Duration' : 'Elapsed';
  updateElapsed();
  const showExit = run.exit_code !== null && run.exit_code !== undefined && !ACTIVE_STATES.includes(run.state);
  el('run-exit-field').hidden = !showExit;
  if (showExit) el('run-exit').textContent = String(run.exit_code);
  renderSafeState(run);
}

// Whether the transmitters were confirmed off matters more than the exit code,
// so an unconfirmed safe state is called out rather than left in the metadata.
function renderSafeState(run) {
  const safeState = run.safe_state || 'pending';
  const finished = !ACTIVE_STATES.includes(run.state);
  el('run-safe-state-field').hidden = !finished || safeState === 'skipped';
  el('run-safe-state').textContent = SAFE_STATE_LABELS[safeState] || safeState;

  const warning = el('run-safe-state-warning');
  const messages = [];
  if (finished && safeState === 'failed') {
    messages.push('Hardware may still be transmitting.');
  }
  if (finished && safeState === 'degraded') {
    messages.push('An optional safe state step could not be reached. Confirm that device by hand.');
  }
  if (run.force_killed) {
    messages.push('A command had to be killed, so its own hardware cleanup did not run.');
  }
  if (messages.length) {
    showError(warning, messages.join(' '));
  } else {
    clearError(warning);
  }
}

/* Run history ------------------------------------------------------------ */

async function loadHistory() {
  clearError(el('history-error'));
  try {
    const data = await api('/api/runs');
    renderHistoryList(data.runs || []);
  } catch (error) {
    historyList.replaceChildren();
    showError(el('history-error'), `Unable to load run history: ${error.message}`);
  }
}

function renderHistoryList(runs) {
  historyList.replaceChildren();
  if (!runs.length) {
    const item = document.createElement('li');
    item.className = 'detail';
    item.textContent = 'No runs have been archived yet.';
    historyList.append(item);
    historyDetail.hidden = true;
    return;
  }
  for (const run of runs) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'history-entry';
    button.classList.toggle('selected', run.id === selectedHistoryId);

    const name = document.createElement('span');
    name.className = 'history-name';
    name.textContent = run.scenario_name || run.scenario_id;

    const when = document.createElement('span');
    when.className = 'history-when';
    when.textContent = new Date(run.started_at * 1000).toLocaleString();

    const state = document.createElement('span');
    setState(state, RUN_STATE_TONES[run.state] || 'idle', RUN_STATE_LABELS[run.state] || run.state);

    button.append(name, when, state);
    if (run.safe_state === 'failed' || run.force_killed) {
      const flag = document.createElement('span');
      flag.className = 'state bad';
      flag.textContent = 'Check hardware';
      button.append(flag);
    }
    button.addEventListener('click', () => showHistoryEntry(run.id));
    item.append(button);
    historyList.append(item);
  }
}

async function showHistoryEntry(runId) {
  selectedHistoryId = runId;
  clearError(el('history-error'));
  try {
    const data = await api(`/api/runs/${encodeURIComponent(runId)}`);
    renderHistoryEntry(data.run);
  } catch (error) {
    historyDetail.hidden = true;
    showError(el('history-error'), `Unable to load run ${runId}: ${error.message}`);
    return;
  }
  for (const button of historyList.querySelectorAll('.history-entry')) {
    button.classList.remove('selected');
  }
  loadHistory();
}

function renderHistoryEntry(run) {
  historyDetail.hidden = false;
  el('history-detail-name').textContent = run.scenario_name || run.scenario_id;
  setState(
    el('history-detail-state'),
    RUN_STATE_TONES[run.state] || 'idle',
    RUN_STATE_LABELS[run.state] || run.state,
  );

  const meta = el('history-detail-meta');
  meta.replaceChildren();
  const duration = run.finished_at ? formatDuration(run.finished_at - run.started_at) : '—';
  const entries = [
    ['Started', new Date(run.started_at * 1000).toLocaleString()],
    ['Duration', duration],
    ['Exit code', run.exit_code === null || run.exit_code === undefined ? '—' : String(run.exit_code)],
    ['Safe state', SAFE_STATE_LABELS[run.safe_state] || run.safe_state || '—'],
  ];
  if (run.force_killed) entries.push(['Force killed', 'Yes — cleanup did not run']);
  for (const [term, value] of entries) {
    const row = document.createElement('div');
    const dt = document.createElement('dt');
    dt.textContent = term;
    const dd = document.createElement('dd');
    dd.textContent = value;
    row.append(dt, dd);
    meta.append(row);
  }

  const args = el('history-detail-arguments');
  args.replaceChildren();
  const argumentEntries = Object.entries(run.arguments || {});
  args.hidden = !argumentEntries.length;
  el('history-detail-no-arguments').hidden = Boolean(argumentEntries.length);
  for (const [name, value] of argumentEntries) {
    const dt = document.createElement('dt');
    dt.textContent = name;
    const dd = document.createElement('dd');
    dd.textContent = value;
    args.append(dt, dd);
  }

  const steps = el('history-detail-steps');
  steps.replaceChildren();
  for (const step of run.steps || []) {
    const item = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'step-name';
    name.textContent = step.name || 'Step';
    const command = document.createElement('code');
    command.className = 'history-command';
    command.textContent = (step.command || []).join(' ');
    item.append(name, command);
    if (step.background) {
      const badge = document.createElement('span');
      badge.className = 'state idle';
      badge.textContent = 'Background';
      item.append(badge);
    }
    steps.append(item);
  }

  const logs = el('history-detail-logs');
  logs.replaceChildren();
  const lines = run.logs || [];
  if (!lines.length) {
    const empty = document.createElement('p');
    empty.className = 'log-empty';
    empty.textContent = 'No output was recorded.';
    logs.append(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const line of lines) fragment.append(logLine(line));
  logs.append(fragment);
}

function updateElapsed() {
  if (!currentRun) return;
  const end = currentRun.finished_at ?? Date.now() / 1000;
  el('run-elapsed').textContent = formatDuration(end - currentRun.started_at);
}

/* Logs ------------------------------------------------------------------- */

function logLine(text) {
  const line = document.createElement('div');
  line.className = 'log-line';
  const match = /^(\[\d{2}:\d{2}:\d{2}\] )([\s\S]*)$/.exec(text);
  const body = match ? match[2] : text;
  if (match) {
    const stamp = document.createElement('span');
    stamp.className = 'log-time';
    stamp.textContent = match[1];
    line.append(stamp);
  }
  const rest = document.createElement('span');
  rest.className = logTone(body);
  rest.textContent = body;
  line.append(rest);
  return line;
}

function logTone(text) {
  if (/\b(error|traceback|failed|fatal)\b/i.test(text)) return 'log-error';
  if (/\bwarn(ing)?\b/i.test(text)) return 'log-warn';
  if (/^(Starting |Step \d+:|Scenario )/.test(text)) return 'log-note';
  return '';
}

function isScrolledToBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 24;
}

function renderLogs(run) {
  const lines = run?.logs || [];
  if (logView.runId !== (run?.id ?? null) || lines.length < logView.rendered) {
    logView = {runId: run?.id ?? null, rendered: 0};
    logsBox.replaceChildren(logEmpty);
    logEmpty.hidden = false;
  }
  if (lines.length === logView.rendered) return;
  const atBottom = isScrolledToBottom(logsBox);
  const fragment = document.createDocumentFragment();
  for (let index = logView.rendered; index < lines.length; index += 1) fragment.append(logLine(lines[index]));
  logsBox.append(fragment);
  logView.rendered = lines.length;
  logEmpty.hidden = true;
  if (atBottom) logsBox.scrollTop = logsBox.scrollHeight;
}

/* Wiring ----------------------------------------------------------------- */

scenarioForm.addEventListener('submit', event => {
  event.preventDefault();
  openConfirmation();
});

confirmDialog.addEventListener('close', () => {
  if (confirmDialog.returnValue === 'run') startRun();
});

stopButton.addEventListener('click', stopRun);
safeStateButton.addEventListener('click', requestSafeState);
el('refresh-history').addEventListener('click', loadHistory);

el('clear-logs').addEventListener('click', () => {
  logsBox.replaceChildren(logEmpty);
  logEmpty.hidden = false;
});

scenarioList.addEventListener('change', () => selectScenario(scenarioList.value || null));
addScenarioButton.addEventListener('click', openScenarioEditor);
addHardwareButton.addEventListener('click', () => selectTab('add-hardware'));
addScenarioForm.addEventListener('submit', createScenario);
el('add-argument').addEventListener('click', addArgumentEditor);
el('add-step').addEventListener('click', () => addStepEditor(newScenarioSteps));
el('add-stop-step').addEventListener('click', () => addStepEditor(newScenarioStopSteps, true));
el('cancel-add-scenario').addEventListener('click', () => {
  resetScenarioEditor();
  selectTab('scenarios');
});
el('new-scenario-name').addEventListener('input', event => {
  const idInput = el('new-scenario-id');
  if (!idInput.dataset.touched) idInput.value = slugify(event.target.value);
});
el('new-scenario-id').addEventListener('input', event => {
  event.target.dataset.touched = 'true';
});
function updateAddHardwareButton() {
  addHardwareSubmit.disabled = !addHardwareForm.checkValidity();
}
addHardwareForm.addEventListener('input', updateAddHardwareButton);
addHardwareForm.addEventListener('change', updateAddHardwareButton);
updateAddHardwareButton();

for (const tab of ['scenarios', 'hardware', 'rf-switch', 'history']) {
  el(`tab-${tab}`).addEventListener('click', () => selectTab(tab));
}
el('rf-switch-auth-form').addEventListener('submit', unlockRfSwitch);
el('rf-switch-auth-cancel').addEventListener('click', () => rfSwitchAuthDialog.close());

async function poll() {
  try {
    el('run-section').hidden = activeTab !== 'scenarios';
    const data = await api('/api/status');
    renderRun(data.run);
  } catch (_) {
    /* The next successful poll refreshes the run state. */
  }
}

resetScenarioEditor();
loadScenarios();
loadHardwareOptions();
loadHardware();
poll();
setInterval(poll, 1000);
setInterval(loadHardware, 10000);
setInterval(loadHardwareOptions, 30000);
setInterval(updateElapsed, 1000);
