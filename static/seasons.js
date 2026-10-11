/* Drop seasonal themes — the site dresses up for holidays and seasons.
   Applies site-wide: changes accent color, adds emoji to title, shows a festive banner. */
(function(){
  const now = new Date();
  const m = now.getMonth(); // 0=Jan
  const d = now.getDate();

  let theme = null;

  // Holiday + seasonal detection (order matters — specific dates first)
  if(m===9 && d>=25) theme={emoji:'🎃',name:'Halloween',acc:'#ff7518',bg:'linear-gradient(135deg,#1a0f00,#2a1505)'};
  else if(m===9) theme={emoji:'🎃',name:'Halloween',acc:'#ff7518',bg:'linear-gradient(135deg,#1a0f00,#2a1505)'};
  else if(m===10 && d<28) theme={emoji:'🦃',name:'Thanksgiving',acc:'#d4a017',bg:'linear-gradient(135deg,#1a1205,#2a1a05)'};
  else if(m===11) theme={emoji:'🎄',name:'Christmas',acc:'#22c55e',bg:'linear-gradient(135deg,#051a0a,#0a2a12)'};
  else if(m===0 && d===1) theme={emoji:'🎆',name:'New Year',acc:'#ffd700',bg:'linear-gradient(135deg,#0a0a1a,#1a1a2a)'};
  else if(m===0) theme={emoji:'❄️',name:'Winter',acc:'#7dd3fc',bg:'linear-gradient(135deg,#0a101a,#0f1a2a)'};
  else if(m===1 && d===14) theme={emoji:'💘',name:"Valentine's",acc:'#ec4899',bg:'linear-gradient(135deg,#1a0510,#2a0a18)'};
  else if(m===1) theme={emoji:'❄️',name:'Winter',acc:'#7dd3fc',bg:'linear-gradient(135deg,#0a101a,#0f1a2a)'};
  else if(m===2 && d===17) theme={emoji:'☘️',name:"St. Patrick's",acc:'#16a34a',bg:'linear-gradient(135deg,#051a0a,#0a2a12)'};
  else if(m===2 || m===3) theme={emoji:'🌸',name:'Spring',acc:'#f472b6',bg:'linear-gradient(135deg,#1a0a12,#2a0f1a)'};
  else if(m===4) theme={emoji:'🌷',name:'Spring',acc:'#f472b6',bg:'linear-gradient(135deg,#1a0a12,#2a0f1a)'};
  else if(m===5 || m===6) theme={emoji:'☀️',name:'Summer',acc:'#fbbf24',bg:'linear-gradient(135deg,#1a1505,#2a200a)'};
  else if(m===6 && d===4) theme={emoji:'🎆',name:'July 4th',acc:'#3b82f6',bg:'linear-gradient(135deg,#0a0a1a,#1a0f2a)'};
  else if(m===7) theme={emoji:'☀️',name:'Summer',acc:'#fbbf24',bg:'linear-gradient(135deg,#1a1505,#2a200a)'};
  else if(m===8) theme={emoji:'🍂',name:'Fall',acc:'#fb923c',bg:'linear-gradient(135deg,#1a0f05,#2a1808)'};

  if(!theme) return;

  // Apply accent color
  const st=document.createElement('style');
  st.id='season-theme';
  st.textContent=`:root{--acc:${theme.acc} !important;} .season-banner{background:${theme.bg};border:1px solid ${theme.acc};}`;
  document.head.appendChild(st);

  // Emoji in title
  if(!document.title.startsWith(theme.emoji)) document.title=theme.emoji+' '+document.title;

  // Festive banner at top (dismissible, once per season)
  const key='season-banner-'+theme.name;
  try{
    if(localStorage.getItem(key)) return;
    const b=document.createElement('div');
    b.className='season-banner';
    b.style.cssText='text-align:center;padding:8px 12px;font-size:.85rem;font-weight:600;cursor:pointer';
    b.innerHTML=`${theme.emoji} Happy ${theme.name} from Drop! ${theme.emoji} <span style="opacity:.6;font-weight:400">(tap to dismiss)</span>`;
    b.onclick=()=>{b.remove();try{localStorage.setItem(key,'1');}catch(e){}};
    if(document.body) document.body.prepend(b);
    else document.addEventListener('DOMContentLoaded',()=>document.body.prepend(b));
  }catch(e){}
})();
