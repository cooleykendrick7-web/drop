// Drop seasonal themes — auto-changes with seasons & holidays
(function(){
  const now = new Date();
  const m = now.getMonth() + 1; // 1-12
  const d = now.getDate();

  // returns [themeName, emoji, accentColor, accentGlow]
  function getTheme(){
    // Halloween
    if(m===10) return ['halloween','🎃','#ff6a00','rgba(255,106,0,.35)'];
    // Thanksgiving
    if(m===11) return ['thanksgiving','🦃','#c2702e','rgba(194,112,46,.35)'];
    // Christmas
    if(m===12 && d<=26) return ['christmas','🎄','#e63946','rgba(230,57,70,.35)'];
    // New Year
    if((m===12&&d>26)||(m===1&&d<=5)) return ['newyear','🎆','#f6e27a','rgba(246,226,122,.35)'];
    // Valentine's
    if(m===2 && d<=14) return ['valentine','💘','#ff4d6d','rgba(255,77,109,.35)'];
    // St. Patrick's
    if(m===3 && d>=10 && d<=17) return ['stpatricks','🍀','#2d9a4e','rgba(45,154,78,.35)'];
    // July 4th
    if(m===7 && d<=7) return ['july4','🇺🇸','#3a86ff','rgba(58,134,255,.35)'];
    // Winter
    if(m===1||m===2) return ['winter','❄️','#7dd3fc','rgba(125,211,252,.35)'];
    // Spring
    if(m>=3&&m<=5) return ['spring','🌸','#86efac','rgba(134,239,172,.35)'];
    // Summer
    if(m>=6&&m<=8) return ['summer','☀️','#fbbf24','rgba(251,191,36,.35)'];
    // Fall
    return ['fall','🍂','#fb923c','rgba(251,146,60,.35)'];
  }

  const [name, emoji, accent, glow] = getTheme();
  document.documentElement.setAttribute('data-season', name);

  // inject seasonal CSS overrides
  const css = `
    :root{ --acc: ${accent} !important; }
    .seasonal-badge{
      position:fixed;top:10px;right:10px;z-index:9999;
      background:rgba(0,0,0,.7);border:1px solid ${accent};
      border-radius:20px;padding:4px 12px;font-size:.8rem;
      color:${accent};pointer-events:none;
    }
  `;
  const st = document.createElement('style');
  st.textContent = css;
  document.head.appendChild(st);

  // seasonal badge (subtle, top-right)
  window.addEventListener('DOMContentLoaded', ()=>{
    const b = document.createElement('div');
    b.className = 'seasonal-badge';
    b.textContent = emoji+' '+name.charAt(0).toUpperCase()+name.slice(1);
    document.body.appendChild(b);
    setTimeout(()=>b.remove(), 5000); // shows for 5 sec then fades
  });
})();
