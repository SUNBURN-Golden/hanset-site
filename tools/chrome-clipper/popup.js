const status = document.querySelector('#status');
const originSelect = document.getElementById('origin');
for (const pattern of chrome.runtime.getManifest().optional_host_permissions) {
  if (!pattern.startsWith('https://') || pattern.slice(0, -2).includes('*')) continue;
  const option = document.createElement('option');
  option.value = option.textContent = pattern.slice(0, -2);
  originSelect.append(option);
}
for (const action of ['clip', 'resume', 'retry', 'approve', 'submit']) {
  document.getElementById(action).addEventListener('click', async () => {
    try {
      const origin = document.getElementById('origin').value;
      const origins = ['http://127.0.0.1/*'];
      if (action === 'approve') origins.push(origin + '/*');
      if (!await chrome.permissions.request({origins})) { status.textContent = '권한이 거절되어 중단했습니다.'; return; }
      const result = await chrome.runtime.sendMessage({action, origin});
      status.textContent = result.state;
    } catch { status.textContent = '중단됨. 로컬 작업기와 권한을 확인하세요.'; }
  });
}
document.querySelector('#alarms').addEventListener('click', async () => {
  if (await chrome.permissions.request({permissions: ['alarms']})) {
    await chrome.alarms.create('deliver', {periodInMinutes: 1});
    status.textContent = '저장된 자료만 자동 재전송합니다.';
  }
});
