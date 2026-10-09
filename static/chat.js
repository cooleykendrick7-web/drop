/* Drop support chat — floating widget with instant answers + message-to-Ken */
(function(){
  const FAQ = [
    {k:["fee","cost","delivery charge","how much","price of delivery"],
     a:"Delivery is a flat <b>$3.99</b> — no surge pricing, no hidden fees. You see the full total before you pay."},
    {k:["how long","fast","time","when will","arrive","fresher","fresh"],
     a:"Ken is your driver and he heads out as soon as your order is confirmed — most orders arrive in <b>30–45 minutes</b>, straight from the restaurant to your door."},
    {k:["where","deliver","area","bismarck","zone"],
     a:"We deliver all across <b>Bismarck, ND</b>. Put in your address at checkout and Ken will get it to you."},
    {k:["track","where is my","status","order status"],
     a:"You can follow your order live on the tracking page — it updates every few seconds from confirmed to delivered. Your tracking link was shown right after checkout."},
    {k:["pay","payment","card","secure","stripe","safe"],
     a:"Checkout is handled securely by <b>Stripe</b> — we never see or store your card number."},
    {k:["who","driver","ken"],
     a:"Your driver is <b>Ken</b> — a local, not a random gig driver. One person handling your food from pickup to doorstep."},
    {k:["hour","open","close","when"],
     a:"Restaurants show <b>OPEN</b> or <b>CLOSED</b> right on the list. Ken delivers whenever the restaurants are open."},
    {k:["refund","wrong","cold","missing","late","problem","issue","complaint","bad"],
     a:"Sorry about that — let's get Ken on it. Tap <b>Message Ken</b> below, include your order number if you have it, and he'll make it right."},
    {k:["account","sign up","log in","login"],
     a:"No account needed — just pick your food, check out, and track it. Simple."},
    {k:["contact","email","phone number","call you","reach you","talk to someone","human"],
     a:"You can reach us anytime at <b>wearedropservicesllc@gmail.com</b> or <b>(262) 977-7869</b> — or tap <b>Message Ken</b> and he'll get back to you."},
  ];

  function answer(q){
    q = q.toLowerCase();
    let best = null, bestScore = 0;
    for(const f of FAQ){
      let s = 0;
      for(const kw of f.k){ if(q.includes(kw)) s += kw.length; }
      if(s > bestScore){ bestScore = s; best = f; }
    }
    return best ? best.a
      : "Good question — I don't have that one yet. Tap <b>Message Ken</b> and he'll get back to you personally.";
  }

  /* queue status bar — shows live wait info on every customer page */
  async function mountQueueBar(){
    if(document.getElementById('queuebar') || location.pathname.startsWith('/dash')) return;
    try{
      const q = await (await fetch('/api/queue')).json();
      window.DROP_QUEUE = q;
      const bar = el(q.accepting
        ? `<div id="queuebar" style="text-align:center;padding:8px 12px;font-size:.82rem;font-weight:600;background:rgba(0,200,100,.12);color:#4ade80;border-bottom:1px solid var(--line)">🟢 Taking orders · about ${q.estimated_mins} min right now${q.active>0?` (${q.active} ahead of you)`:''}</div>`
        : `<div id="queuebar" style="text-align:center;padding:10px 12px;font-size:.85rem;font-weight:700;background:rgba(255,80,80,.14);color:#ff8080;border-bottom:1px solid var(--line)">🔴 At capacity right now — check back soon!</div>`);
      document.body.prepend(bar);
    }catch(e){}
  }

  function el(html){
    const d = document.createElement('div');
    d.innerHTML = html.trim();
    return d.firstChild;
  }

  function mount(){
    if(document.getElementById('dropchat')) return;
    mountQueueBar();
    const root = el(`<div id="dropchat">
      <button id="dc-btn" aria-label="Chat with us">💬</button>
      <div id="dc-panel" style="display:none">
        <div id="dc-head"><img src="/static/logo-mark.jpg" alt=""><div><b>Drop Support</b><span>Typically replies fast</span></div><button id="dc-x">✕</button></div>
        <div id="dc-msgs"></div>
        <div id="dc-quick"></div>
        <div id="dc-form" style="display:none">
          <input id="dc-name" placeholder="Your name">
          <input id="dc-phone" placeholder="Phone (optional)" inputmode="tel">
          <input id="dc-order" placeholder="Order # (if you have one)" inputmode="numeric">
          <textarea id="dc-msg" rows="3" placeholder="What's going on?"></textarea>
          <button class="btn" id="dc-send">Send to Ken</button>
        </div>
        <div id="dc-inputrow">
          <input id="dc-in" placeholder="Ask a question…">
          <button id="dc-go">➤</button>
        </div>
      </div>
    </div>`);
    document.body.appendChild(root);

    const msgs = document.getElementById('dc-msgs');
    const quick = document.getElementById('dc-quick');
    function bot(html){
      msgs.appendChild(el(`<div class="dc-bubble bot">${html}</div>`));
      msgs.scrollTop = msgs.scrollHeight;
    }
    function user(html){
      msgs.appendChild(el(`<div class="dc-bubble me">${html}</div>`));
      msgs.scrollTop = msgs.scrollHeight;
    }
    function ask(q){
      user(q.replace(/</g,'&lt;'));
      setTimeout(()=>bot(answer(q)), 350);
    }

    const QUICKS = ["Delivery fee?","How fast?","Track my order","Problem with order","Message Ken"];
    quick.innerHTML = '';
    QUICKS.forEach(q=>{
      const b = el(`<button>${q}</button>`);
      b.onclick = ()=>{
        if(q === "Message Ken"){
          document.getElementById('dc-form').style.display = 'block';
          document.getElementById('dc-inputrow').style.display = 'none';
          quick.style.display = 'none';
          bot("Tell Ken what's up — he'll see it in his dashboard and get back to you.");
        } else ask(q);
      };
      quick.appendChild(b);
    });

    document.getElementById('dc-btn').onclick = ()=>{
      const p = document.getElementById('dc-panel');
      const open = p.style.display !== 'none';
      p.style.display = open ? 'none' : 'flex';
      if(!open && !msgs.children.length){
        bot("Hey! I'm the Drop assistant 👋 Ask me about delivery, timing, or tracking — or tap <b>Message Ken</b> if something needs a human.");
      }
    };
    document.getElementById('dc-x').onclick = ()=>{
      document.getElementById('dc-panel').style.display = 'none';
    };
    function send(){
      const v = document.getElementById('dc-in').value.trim();
      if(!v) return;
      document.getElementById('dc-in').value = '';
      ask(v);
    }
    document.getElementById('dc-go').onclick = send;
    document.getElementById('dc-in').addEventListener('keydown', e=>{
      if(e.key === 'Enter') send();
    });
    document.getElementById('dc-send').onclick = async ()=>{
      const name = document.getElementById('dc-name').value.trim();
      const phone = document.getElementById('dc-phone').value.trim();
      const order = document.getElementById('dc-order').value.trim();
      const message = document.getElementById('dc-msg').value.trim();
      if(!name || !message){ bot("Please add your <b>name</b> and a <b>message</b> so Ken knows who to help."); return; }
      try{
        const r = await fetch('/api/support/ticket',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({name, phone, order_id:order, message})});
        const j = await r.json();
        if(j.ok){
          document.getElementById('dc-form').style.display = 'none';
          bot(`Got it, ${name.replace(/</g,'&lt;')} — your message is with Ken (ticket <b>#${j.ticket_id}</b>). He'll reach out at the phone you gave.`);
        } else bot("Hmm, that didn't send — try again in a moment.");
      }catch(e){ bot("Hmm, that didn't send — try again in a moment."); }
    };
  }

  if(document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
