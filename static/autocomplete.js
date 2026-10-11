/* Drop address autocomplete — type-ahead suggestions like Uber/DoorDash.
 * Usage: attachAutocomplete(inputEl, onPick)
 *   onPick({label, lat, lng}) when the user picks a suggestion,
 *   onPick(null) when they type freely (coords cleared).
 * Picking a suggestion gives exact coords — no re-geocoding needed. */
function attachAutocomplete(input, onPick){
  if(!input || input._acAttached) return;
  input._acAttached = true;
  const wrap = input.parentNode;
  if(getComputedStyle(wrap).position === 'static') wrap.style.position = 'relative';
  const box = document.createElement('div');
  box.style.cssText = 'position:absolute;left:0;right:0;top:100%;z-index:99;display:none;'+
    'background:#151518;border:1px solid #3a3a40;border-radius:12px;margin-top:4px;'+
    'max-height:240px;overflow-y:auto;box-shadow:0 8px 30px rgba(0,0,0,.5)';
  wrap.appendChild(box);
  let timer = null, items = [], lastQ = '';

  input.setAttribute('autocomplete','off');
  input.addEventListener('input', ()=>{
    onPick(null);
    const q = input.value.trim();
    if(q.length < 3 || q === lastQ){ if(q.length<3) box.style.display='none'; return; }
    clearTimeout(timer);
    timer = setTimeout(async ()=>{
      try{
        const r = await (await fetch('/api/places/autocomplete?q='+encodeURIComponent(q))).json();
        items = r.suggestions || [];
        lastQ = q;
        if(!items.length){ box.style.display='none'; return; }
        box.innerHTML = items.map((it,i)=>
          `<div data-i="${i}" style="padding:11px 14px;cursor:pointer;color:#f5f0e6;`+
          `border-bottom:1px solid #222;font-size:.92rem">📍 ${it.label.replace(/</g,'&lt;')}</div>`
        ).join('');
        box.style.display = 'block';
        box.querySelectorAll('div[data-i]').forEach(el=>{
          el.onmouseenter = ()=>{ el.style.background='#222228'; };
          el.onmouseleave = ()=>{ el.style.background=''; };
          el.onclick = ()=>{
            const it = items[+el.dataset.i];
            input.value = it.label;
            lastQ = it.label;
            box.style.display = 'none';
            onPick({label: it.label, lat: it.lat, lng: it.lng});
          };
        });
      }catch(e){ box.style.display='none'; }
    }, 280);
  });
  // hide when tapping elsewhere
  document.addEventListener('click', e=>{
    if(!box.contains(e.target) && e.target !== input) box.style.display='none';
  });
  // hide on Escape
  input.addEventListener('keydown', e=>{ if(e.key==='Escape') box.style.display='none'; });
}
