// navigator.clipboard is undefined on a plain-http LAN address
// (not a secure context), so fall back to execCommand there
function copyShareUrl(){
  const el = document.getElementById('shareUrl');
  el.select();
  const done = () => {
    const c = document.getElementById('copied');
    c.style.display = 'inline';
    setTimeout(() => c.style.display = 'none', 1500);
  };
  const legacy = () => {
    try { document.execCommand('copy'); } catch(e) {}
    done();
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(el.value).then(done, legacy);
  } else {
    legacy();
  }
}
