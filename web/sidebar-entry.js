// Compatibility layer for the existing compiled Vue admin; no compressed bundle edits.
(() => {
  const id = 'sky-agent-menu-entry';
  function ensureEntry() {
    const existing = document.getElementById(id);
    const loggedIn = document.cookie.split(';').some(part => part.trim().startsWith('token='));
    if (!loggedIn || location.hash.startsWith('#/login')) { if (existing) existing.remove(); return; }
    const menu = document.querySelector('.sidebar-container .el-menu');
    if (!menu || existing) return;
    const item = document.createElement('li');
    item.id = id;
    item.className = 'el-menu-item';
    item.setAttribute('role', 'none');
    const link = document.createElement('a');
    link.href = '/assistant/';
    link.setAttribute('role', 'menuitem');
    link.title = '经营助手';
    const icon = document.createElement('span');
    icon.className = 'agent-entry-icon';
    icon.textContent = '✦';
    icon.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.className = 'agent-entry-label';
    label.textContent = '经营助手';
    link.append(icon, label);
    item.append(link);
    menu.append(item);
  }
  const style = document.createElement('style');
  style.textContent = '#sky-agent-menu-entry{padding:0!important}#sky-agent-menu-entry a{display:flex;align-items:center;gap:12px;padding:0 28px;color:inherit;text-decoration:none;min-height:56px}#sky-agent-menu-entry a:focus-visible{outline:2px solid #ffc600;outline-offset:-3px}.agent-entry-icon{font-size:22px;color:#ffc600;min-width:20px}.hideSidebar #sky-agent-menu-entry a{padding:0;justify-content:center}.hideSidebar .agent-entry-label{display:none}';
  document.head.append(style);
  let scheduled = false;
  const observer = new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; ensureEntry(); });
  });
  observer.observe(document.body, { childList: true, subtree: true });
  window.addEventListener('hashchange', ensureEntry);
  window.addEventListener('focus', ensureEntry);
  ensureEntry();
})();
