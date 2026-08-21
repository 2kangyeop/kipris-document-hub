const token = document.querySelector('meta[name="kipris-token"]').content;
const $ = (id) => document.getElementById(id);
const state = {
  selectedFolder: '',
  selectedFiles: new Set(),
  currentPdfUrl: '',
  lastRunning: false,
  mergeOrder: [],
  logHiddenBefore: 0,
};
let statusPollTimer = null;

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set('X-KIPRIS-Token', token);
  if (options.body && typeof options.body !== 'string' && !(options.body instanceof ArrayBuffer)) {
    headers.set('Content-Type', 'application/json');
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  let payload = null;
  try { payload = await response.json(); } catch (_) { /* non-json response */ }
  if (!response.ok) throw new Error(payload?.detail || `요청 실패: HTTP ${response.status}`);
  return payload;
}

let toastTimer;
function toast(message, error = false) {
  const node = $('toast');
  node.textContent = message;
  node.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = 'toast'; }, 3500);
}

function openModal(id) {
  const modal = $(id);
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
}

function closeModal(id) {
  const modal = $(id);
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
}

function formatBytes(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function loadConfig() {
  const config = await api('/api/config');
  $('outputDir').value = config.output_dir;
  $('allowInsecureTls').checked = config.allow_insecure_tls;
  $('keyState').textContent = config.has_api_key
    ? (config.api_key_from_environment ? '환경변수 AccessKey 사용 중' : 'AccessKey 안전하게 저장됨')
    : 'AccessKey가 저장되지 않음';
}

async function saveSettings() {
  try {
    const result = await api('/api/config', {
      method: 'POST',
      body: {
        api_key: $('apiKey').value,
        output_dir: $('outputDir').value,
        allow_insecure_tls: $('allowInsecureTls').checked,
      },
    });
    $('apiKey').value = '';
    $('keyState').textContent = result.has_api_key ? 'AccessKey 안전하게 저장됨' : 'AccessKey가 저장되지 않음';
    closeModal('settingsModal');
    toast('설정을 저장했습니다.');
    await refreshFolders();
  } catch (error) { toast(error.message, true); }
}

async function deleteKey() {
  if (!confirm('Windows 자격 증명 저장소의 AccessKey를 삭제할까요?')) return;
  try {
    await api('/api/config/key', { method: 'DELETE' });
    $('keyState').textContent = 'AccessKey가 저장되지 않음';
    $('apiKey').value = '';
    toast('저장된 AccessKey를 삭제했습니다.');
  } catch (error) { toast(error.message, true); }
}

async function chooseOutput() {
  try {
    const result = await api('/api/choose-output', { method: 'POST' });
    $('outputDir').value = result.path;
  } catch (error) { toast(error.message, true); }
}

async function importNumberFile(file) {
  if (!file) return;
  try {
    const suffixIndex = file.name.lastIndexOf('.');
    const suffix = suffixIndex >= 0 ? file.name.slice(suffixIndex).toLowerCase() : '';
    const headers = new Headers({ 'X-KIPRIS-Token': token, 'X-File-Suffix': suffix });
    const response = await fetch('/api/input-file', {
      method: 'POST', headers, body: await file.arrayBuffer(),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '파일을 읽지 못했습니다.');
    $('numbers').value = result.numbers;
    toast(`${result.count}개 문헌번호를 불러왔습니다.`);
  } catch (error) { toast(error.message, true); }
  $('numberFile').value = '';
}

async function startDownload() {
  try {
    await api('/api/download', {
      method: 'POST',
      body: {
        numbers: $('numbers').value,
        output_dir: $('outputDir').value,
        want_publication: $('wantPublication').checked,
        want_admin: $('wantAdmin').checked,
        google_patents: $('googlePatents').checked,
        allow_insecure_tls: $('allowInsecureTls').checked,
      },
    });
    state.logHiddenBefore = 0;
    state.lastRunning = true;
    toast('다운로드를 시작했습니다.');
    scheduleStatusPoll(0);
  } catch (error) { toast(error.message, true); }
}

async function cancelDownload() {
  try {
    await api('/api/cancel', { method: 'POST' });
    toast('중지를 요청했습니다. 현재 문서 처리 후 종료합니다.');
  } catch (error) { toast(error.message, true); }
}

function renderStatus(result) {
  const fill = $('progressFill');
  fill.style.width = `${result.percent}%`;
  fill.classList.toggle('running', result.running);
  $('progressLabel').textContent = `${result.completed} / ${result.total} (${result.percent}%)`;
  const badge = $('statusBadge');
  badge.textContent = result.status;
  badge.classList.toggle('running', result.running);
  $('startButton').disabled = result.running;
  $('cancelButton').disabled = !result.running;
  $('settingsButton').disabled = result.running;
  $('browseButton').disabled = result.running;
  $('deleteFolderButton').disabled = result.running;
  $('deleteFilesButton').disabled = result.running;
  const logs = result.logs.slice(state.logHiddenBefore);
  $('logView').textContent = logs.length ? logs.join('\n') : '진행 기록이 없습니다.';
  $('logView').scrollTop = $('logView').scrollHeight;
}

async function pollStatus() {
  if (statusPollTimer) {
    clearTimeout(statusPollTimer);
    statusPollTimer = null;
  }
  let nextDelay = 5000;
  try {
    const result = await api('/api/status');
    renderStatus(result);
    nextDelay = result.running ? 800 : 4000;
    if (state.lastRunning && !result.running) {
      await refreshFolders(result.preview_folder || '', result.preview_file || '');
      toast(result.files ? `${result.files}개 파일 처리가 완료되었습니다.` : '저장된 파일이 없습니다.', !result.files);
    }
    state.lastRunning = result.running;
  } catch (_) { /* server may be closing */ }
  statusPollTimer = setTimeout(pollStatus, nextDelay);
}

function scheduleStatusPoll(delay = 0) {
  if (statusPollTimer) clearTimeout(statusPollTimer);
  statusPollTimer = setTimeout(pollStatus, delay);
}

async function refreshFolders(preferred = '', autoOpenFile = '') {
  try {
    const result = await api('/api/folders');
    $('rootLabel').textContent = result.root;
    $('folderCount').textContent = result.folders.length;
    const list = $('folderList');
    list.innerHTML = '';
    list.classList.toggle('empty-list', !result.folders.length);
    if (!result.folders.length) {
      list.textContent = '폴더가 없습니다.';
      state.selectedFolder = '';
      renderFiles([]);
      return;
    }
    for (const folder of result.folders) {
      const button = document.createElement('button');
      button.className = `folder-item${folder.name === state.selectedFolder ? ' active' : ''}`;
      const name = document.createElement('span');
      name.textContent = folder.name;
      const count = document.createElement('small');
      count.textContent = folder.pdf_count;
      button.append(name, count);
      button.addEventListener('click', () => selectFolder(folder.name));
      list.append(button);
    }
    const target = preferred || (result.folders.some((f) => f.name === state.selectedFolder) ? state.selectedFolder : result.folders[0].name);
    await selectFolder(target, autoOpenFile);
  } catch (error) { toast(error.message, true); }
}

async function selectFolder(folder, autoOpenFile = '') {
  state.selectedFolder = folder;
  state.selectedFiles.clear();
  document.querySelectorAll('.folder-item').forEach((node) => node.classList.toggle('active', node.firstChild.textContent === folder));
  try {
    const result = await api(`/api/files?folder=${encodeURIComponent(folder)}`);
    renderFiles(result.files);
    if (autoOpenFile && result.files.length) {
      const preferred = result.files.find((file) => file.name === autoOpenFile)
        || [...result.files].sort((left, right) => right.modified - left.modified)[0];
      const row = [...document.querySelectorAll('.file-item')]
        .find((node) => node.dataset.name === preferred.name);
      if (row) previewPdf(preferred.name, row);
    }
  } catch (error) { toast(error.message, true); }
}

function renderFiles(files) {
  $('fileCount').textContent = files.length;
  const list = $('fileList');
  list.innerHTML = '';
  list.classList.toggle('empty-list', !files.length);
  if (!files.length) {
    list.textContent = state.selectedFolder ? 'PDF 파일이 없습니다.' : '폴더를 선택하세요.';
    clearViewer();
    return;
  }
  for (const file of files) {
    const row = document.createElement('div');
    row.className = 'file-item';
    row.dataset.name = file.name;
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = state.selectedFiles.has(file.name);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.selectedFiles.add(file.name); else state.selectedFiles.delete(file.name);
    });
    const name = document.createElement('button');
    name.className = 'file-name';
    name.textContent = file.name;
    name.title = file.name;
    name.addEventListener('click', () => previewPdf(file.name, row));
    const size = document.createElement('small');
    size.textContent = formatBytes(file.size);
    row.append(checkbox, name, size);
    list.append(row);
  }
}

function previewPdf(filename, row) {
  document.querySelectorAll('.file-item').forEach((node) => node.classList.remove('active'));
  row.classList.add('active');
  const base = `/api/pdf?folder=${encodeURIComponent(state.selectedFolder)}&file=${encodeURIComponent(filename)}&token=${encodeURIComponent(token)}`;
  state.currentPdfUrl = `${base}#view=FitV`;
  $('pdfViewer').src = state.currentPdfUrl;
  $('pdfViewer').style.display = 'block';
  $('viewerEmpty').style.display = 'none';
  $('viewerTitle').textContent = filename;
  $('viewerHint').textContent = `${state.selectedFolder} · 브라우저 PDF 뷰어`;
  $('newWindowButton').disabled = false;
}

function clearViewer() {
  state.currentPdfUrl = '';
  $('pdfViewer').removeAttribute('src');
  $('pdfViewer').style.display = 'none';
  $('viewerEmpty').style.display = 'flex';
  $('viewerTitle').textContent = '선택된 PDF 없음';
  $('viewerHint').textContent = 'PDF 파일을 선택하면 높이 맞춤으로 표시됩니다.';
  $('newWindowButton').disabled = true;
}

function selectedFileNames() {
  return [...state.selectedFiles];
}

function selectAllFiles() {
  const checkboxes = [...document.querySelectorAll('.file-item input[type=checkbox]')];
  const shouldSelect = checkboxes.some((box) => !box.checked);
  state.selectedFiles.clear();
  for (const box of checkboxes) {
    box.checked = shouldSelect;
    if (shouldSelect) state.selectedFiles.add(box.closest('.file-item').dataset.name);
  }
}

function openMergeDialog() {
  const files = selectedFileNames();
  if (files.length < 2) return toast('합칠 PDF를 두 개 이상 선택해 주세요.', true);
  state.mergeOrder = [...files];
  $('mergeName').value = `${state.selectedFolder}_병합문서.pdf`;
  renderMergeList();
  openModal('mergeModal');
}

function moveMergeItem(index, direction) {
  const next = index + direction;
  if (next < 0 || next >= state.mergeOrder.length) return;
  [state.mergeOrder[index], state.mergeOrder[next]] = [state.mergeOrder[next], state.mergeOrder[index]];
  renderMergeList();
}

function renderMergeList() {
  const list = $('mergeList');
  list.innerHTML = '';
  state.mergeOrder.forEach((filename, index) => {
    const row = document.createElement('div');
    row.className = 'merge-row';
    const number = document.createElement('strong'); number.textContent = index + 1;
    const name = document.createElement('span'); name.textContent = filename; name.title = filename;
    const up = document.createElement('button'); up.textContent = '↑'; up.disabled = index === 0; up.addEventListener('click', () => moveMergeItem(index, -1));
    const down = document.createElement('button'); down.textContent = '↓'; down.disabled = index === state.mergeOrder.length - 1; down.addEventListener('click', () => moveMergeItem(index, 1));
    row.append(number, name, up, down);
    list.append(row);
  });
}

async function runMerge() {
  try {
    const result = await api('/api/merge', {
      method: 'POST',
      body: { folder: state.selectedFolder, files: state.mergeOrder, output_name: $('mergeName').value },
    });
    closeModal('mergeModal');
    await selectFolder(state.selectedFolder);
    toast(`${result.file} 생성 완료 · ${result.pages}페이지`);
  } catch (error) { toast(error.message, true); }
}

async function extractText() {
  const files = selectedFileNames();
  if (!files.length) return toast('텍스트를 추출할 PDF를 선택해 주세요.', true);
  try {
    $('extractButton').disabled = true;
    const result = await api('/api/extract', { method: 'POST', body: { folder: state.selectedFolder, files } });
    $('extractedText').value = result.text;
    $('textStats').textContent = `${files.length}개 파일 · ${result.characters.toLocaleString()}자`;
    openModal('textModal');
  } catch (error) { toast(error.message, true); }
  finally { $('extractButton').disabled = false; }
}

async function extractExternalText() {
  const button = $('externalExtractButton');
  try {
    button.disabled = true;
    const selection = await api('/api/choose-external-pdfs', { method: 'POST' });
    if (!selection.count) return;
    toast(`${selection.count}개 외부 PDF를 선택했습니다. 텍스트를 추출합니다.`);
    const result = await api('/api/extract-external', {
      method: 'POST', body: { ids: selection.files.map((file) => file.id) },
    });
    $('extractedText').value = result.text;
    $('textStats').textContent = `외부 PDF ${result.files.length}개 · ${result.characters.toLocaleString()}자`;
    openModal('textModal');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function copyText() {
  try {
    await navigator.clipboard.writeText($('extractedText').value);
    toast('추출 텍스트를 클립보드에 복사했습니다.');
  } catch (_) {
    $('extractedText').select();
    document.execCommand('copy');
    toast('추출 텍스트를 복사했습니다.');
  }
}

async function openFolder() {
  try {
    await api('/api/open-folder', { method: 'POST', body: { folder: state.selectedFolder } });
  } catch (error) { toast(error.message, true); }
}

async function deleteSelectedFiles() {
  const files = selectedFileNames();
  if (!files.length) return toast('삭제할 PDF를 선택해 주세요.', true);
  const label = files.length === 1 ? files[0] : `${files.length}개 PDF`;
  if (!confirm(`${label}를 Windows 휴지통으로 보낼까요?`)) return;
  try {
    clearViewer();
    await new Promise((resolve) => setTimeout(resolve, 120));
    const result = await api('/api/delete-files', {
      method: 'POST', body: { folder: state.selectedFolder, files },
    });
    clearViewer();
    state.selectedFiles.clear();
    await selectFolder(state.selectedFolder);
    if (result.failures.length) {
      toast(`${result.deleted.length}개 삭제, ${result.failures.length}개 실패`, true);
    } else {
      toast(`${result.deleted.length}개 PDF를 휴지통으로 보냈습니다.`);
    }
  } catch (error) { toast(error.message, true); }
}

async function deleteSelectedFolder() {
  if (!state.selectedFolder) return toast('삭제할 문헌 폴더를 선택해 주세요.', true);
  if (!confirm(`문헌 폴더 “${state.selectedFolder}”와 폴더 안의 모든 파일을 Windows 휴지통으로 보낼까요?`)) return;
  const deletedFolder = state.selectedFolder;
  try {
    clearViewer();
    await new Promise((resolve) => setTimeout(resolve, 120));
    await api('/api/delete-folder', { method: 'POST', body: { folder: deletedFolder } });
    state.selectedFolder = '';
    state.selectedFiles.clear();
    clearViewer();
    await refreshFolders();
    toast(`${deletedFolder} 폴더를 휴지통으로 보냈습니다.`);
  } catch (error) { toast(error.message, true); }
}

async function shutdown() {
  if (!confirm('KIPRIS Document Hub를 종료할까요?')) return;
  try {
    await api('/api/shutdown', { method: 'POST' });
    window.close();
    document.body.innerHTML = '<div style="display:grid;place-items:center;height:100%;background:#07101a;color:#dce9f8;font:16px Malgun Gothic">프로그램이 종료되었습니다. 이 창을 닫아 주세요.</div>';
  } catch (error) { toast(error.message, true); }
}

function bindEvents() {
  $('settingsButton').addEventListener('click', () => openModal('settingsModal'));
  $('saveSettingsButton').addEventListener('click', saveSettings);
  $('deleteKeyButton').addEventListener('click', deleteKey);
  $('browseButton').addEventListener('click', chooseOutput);
  $('numberFile').addEventListener('change', (event) => importNumberFile(event.target.files[0]));
  $('clearNumbersButton').addEventListener('click', () => {
    $('numbers').value = '';
    $('numberFile').value = '';
    $('numbers').focus();
    toast('문헌번호 입력란을 지웠습니다.');
  });
  $('startButton').addEventListener('click', startDownload);
  $('cancelButton').addEventListener('click', cancelDownload);
  $('refreshButton').addEventListener('click', () => refreshFolders(state.selectedFolder));
  $('openFolderButton').addEventListener('click', openFolder);
  $('deleteFolderButton').addEventListener('click', deleteSelectedFolder);
  $('selectAllButton').addEventListener('click', selectAllFiles);
  $('mergeButton').addEventListener('click', openMergeDialog);
  $('runMergeButton').addEventListener('click', runMerge);
  $('extractButton').addEventListener('click', extractText);
  $('externalExtractButton').addEventListener('click', extractExternalText);
  $('deleteFilesButton').addEventListener('click', deleteSelectedFiles);
  $('copyTextButton').addEventListener('click', copyText);
  $('clearLogButton').addEventListener('click', () => { $('logView').textContent = ''; state.logHiddenBefore = Number.MAX_SAFE_INTEGER; });
  $('fitButton').addEventListener('click', () => { if (state.currentPdfUrl) $('pdfViewer').src = state.currentPdfUrl.split('#')[0] + '#view=FitV'; });
  $('newWindowButton').addEventListener('click', () => { if (state.currentPdfUrl) window.open(state.currentPdfUrl, '_blank', 'noopener'); });
  $('shutdownButton').addEventListener('click', shutdown);
  document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => closeModal(button.dataset.close)));
  document.querySelectorAll('.modal').forEach((modal) => modal.addEventListener('click', (event) => { if (event.target === modal) closeModal(modal.id); }));
}

async function initialize() {
  bindEvents();
  try {
    await loadConfig();
    await refreshFolders();
    await pollStatus();
  } catch (error) { toast(error.message, true); }
}

initialize();
