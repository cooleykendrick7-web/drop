// Drop Live Location + Help Agent — loaded on every page
(function(){
  // ========== ALWAYS-LIVE LOCATION ==========
  window.DropLoc = {lat: 0, lng: 0, city: '', state: '', active: false};

  function saveLoc(lat, lng){
    window.DropLoc.lat = lat;
    window.DropLoc.lng = lng;
    window.DropLoc.active = true;
    try{
      localStorage.setItem('droploc', JSON.stringify({lat, lng, t: Date.now()}));
    }catch(e){}
    // Notify any listeners
    window.dispatchEvent(new CustomEvent('droploc', {detail: {lat, lng}}));
  }

  function startWatching(){
    if(!navigator.geolocation) { tryIP(); return; }
    // Continuous watch — always live
    try{
      navigator.geolocation.watchPosition(
        pos => {
          saveLoc(pos.coords.latitude, pos.coords.longitude);
          // Reverse geocode to city (throttled: max once per 5 min)
          const last = parseInt(localStorage.getItem('droploc_geo_t')||'0');
          if(Date.now() - last > 300000){
            localStorage.setItem('droploc_geo_t', Date.now().toString());
            fetch(`/api/nearest-city?lat=${pos.coords.latitude}&lng=${pos.coords.longitude}`)
              .then(r=>r.json()).then(d=>{
                if(d.detected_city){
                  window.DropLoc.city = d.detected_city;
                  if(d.match){ window.DropLoc.city = d.match.city; window.DropLoc.state = d.match.state; }
                }
              }).catch(()=>{});
          }
        },
        err => { tryIP(); },
        {enableHighAccuracy: true, timeout: 15000, maximumAge: 30000}
      );
    }catch(e){ tryIP(); }
  }

  function tryIP(){
    // Fallback: IP-based location
    fetch('/api/ip-location').then(r=>r.json()).then(d=>{
      if(d.lat && d.lng){
        saveLoc(d.lat, d.lng);
        window.DropLoc.city = d.city||'';
        window.DropLoc.state = d.state||'';
      }
    }).catch(()=>{});
  }

  // Restore last known location immediately
  try{
    const saved = JSON.parse(localStorage.getItem('droploc')||'null');
    if(saved && saved.lat){
      window.DropLoc.lat = saved.lat;
      window.DropLoc.lng = saved.lng;
      window.DropLoc.active = true;
    }
  }catch(e){}

  // Start watching on page load
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', startWatching);
  } else {
    startWatching();
  }

  // ========== FLOATING AI HELP AGENT ==========
  function createHelpWidget(){
    if(document.getElementById('drop-help')) return;

    const btn = document.createElement('button');
    btn.id = 'drop-help';
    btn.innerHTML = '💬';
    btn.title = 'Need help?';
    btn.style.cssText = 'position:fixed;bottom:20px;right:20px;width:56px;height:56px;border-radius:50%;background:linear-gradient(135deg,#ff6b35,#f7931e);border:none;font-size:24px;cursor:pointer;z-index:9999;box-shadow:0 4px 16px rgba(0,0,0,0.3);color:white;';
    
    const panel = document.createElement('div');
    panel.id = 'drop-help-panel';
    panel.style.cssText = 'display:none;position:fixed;bottom:86px;right:20px;width:320px;max-height:440px;background:#1a1a1a;border-radius:16px;z-index:9999;box-shadow:0 8px 32px rgba(0,0,0,0.5);overflow:hidden;flex-direction:column;border:1px solid #333;';
    panel.innerHTML = `
      <div style="padding:12px 16px;background:linear-gradient(135deg,#ff6b35,#f7931e);color:white;font-weight:bold;display:flex;justify-content:space-between;align-items:center;">
        <span>🤖 Drop Helper</span>
        <button id="drop-help-close" style="background:none;border:none;color:white;font-size:18px;cursor:pointer;">✕</button>
      </div>
      <div id="drop-help-msgs" style="flex:1;overflow-y:auto;padding:12px;min-height:200px;max-height:300px;"></div>
      <div style="padding:12px;border-top:1px solid #333;display:flex;gap:8px;">
        <input id="drop-help-input" placeholder="Ask anything…" style="flex:1;padding:8px 12px;border-radius:20px;border:1px solid #444;background:#222;color:white;outline:none;">
        <button id="drop-help-send" style="padding:8px 16px;border-radius:20px;background:#ff6b35;border:none;color:white;cursor:pointer;font-weight:bold;">Send</button>
      </div>`;

    document.body.appendChild(btn);
    document.body.appendChild(panel);

    const msgs = document.getElementById('drop-help-msgs');
    function addMsg(text, me){
      const d = document.createElement('div');
      d.style.cssText = `margin:8px 0;padding:8px 12px;border-radius:12px;max-width:85%;${me?'background:#ff6b35;color:white;margin-left:auto;':'background:#2a2a2a;color:#eee;'}`;
      d.textContent = text;
      msgs.appendChild(d);
      msgs.scrollTop = msgs.scrollHeight;
    }

    btn.onclick = ()=>{
      const open = panel.style.display !== 'none';
      panel.style.display = open ? 'none' : 'flex';
      if(!open && !msgs.children.length){
        addMsg("Hi! I'm the Drop helper. Having trouble? Ask me anything about ordering, tracking, or your account.");
      }
    };
    document.getElementById('drop-help-close').onclick = ()=>{ panel.style.display='none'; };

    async function send(){
      const input = document.getElementById('drop-help-input');
      const q = input.value.trim();
      if(!q) return;
      addMsg(q, true);
      input.value = '';
      addMsg('Thinking…', false);
      try{
        const r = await(await fetch('/api/help/ask', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({question: q, page: location.pathname})
        })).json();
        msgs.removeChild(msgs.lastChild);
        addMsg(r.answer || "Sorry, I couldn't help with that. Try contacting support.");
      }catch(e){
        msgs.removeChild(msgs.lastChild);
        addMsg("Connection issue. Please try again.");
      }
    }
    document.getElementById('drop-help-send').onclick = send;
    document.getElementById('drop-help-input').onkeypress = e=>{ if(e.key==='Enter') send(); };
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', createHelpWidget);
  } else {
    createHelpWidget();
  }
})();
