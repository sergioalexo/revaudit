// Click any <code> token (part numbers, filenames, patterns) to copy it.
// navigator.clipboard needs a secure context - fine over https or localhost,
// blocked on a plain-http LAN IP, so there is an execCommand fallback.
(function(){
  function copy(text){
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function(){ return legacy(text); });
    }
    return legacy(text);
  }
  function legacy(text){
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    try { document.execCommand('copy'); } catch(e){}
    document.body.removeChild(ta);
    return Promise.resolve();
  }
  document.addEventListener('click', function(e){
    var el = e.target.closest && e.target.closest('code');
    if (!el) return;
    var txt = (el.textContent || '').trim();
    if (!txt) return;
    copy(txt).then(function(){
      el.classList.add('copied');
      setTimeout(function(){ el.classList.remove('copied'); }, 900);
    });
  });
})();
