/* Weather Advisory frontend. No framework, no innerHTML: every string from the server is set with textContent. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const TICKS = { Favourable: '#6fd3b0', Note: '#9db8d8', Caution: '#f2c879', Warning: '#f09a6b', Danger: '#f07272' };
  const STEPS = [
    ['understand', 'Reading your question'], ['geocode', 'Locating the place'], ['fetch_forecast', 'Fetching the live forecast'],
    ['evaluate', 'Checking the policies'], ['compose', 'Writing the answer'],
  ];
  const CATEGORY_NAMES = {
    regional_hazard: 'Regional hazards', outdoor_exercise: 'Outdoor exercise', travel: 'Travel',
    vulnerable_groups: 'Vulnerable groups', leisure: 'Leisure',
  };
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

  const thread = $('thread'), form = $('composer'), input = $('q'), sendBtn = $('send');
  const drawer = $('drawer'), scrim = $('scrim');
  let byId = {}, busy = false, turns = 0, lastFocus = null;
  let sessionId = sessionStorage.getItem('wa-session') || newSession();

  function newSession() {
    const raw = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
    const id = raw.replace(/[^A-Za-z0-9_-]/g, '');
    sessionStorage.setItem('wa-session', id);
    return id;
  }

  /* ---------- policies: rail + drawer ---------- */
  async function loadPolicies() {
    let policies;
    try {
      const res = await fetch('/api/policies');
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not load policies');
      policies = await res.json();
    } catch (err) {
      $('policyList').append(el('p', 'rail-note', 'Policies could not be loaded: ' + err.message));
      return;
    }
    byId = Object.fromEntries(policies.map((p) => [p.id, p]));
    $('policyCount').textContent = '(' + policies.length + ')';
    const list = $('policyList');
    list.replaceChildren();
    for (const cat of [...new Set(policies.map((p) => p.category))]) {
      const group = el('section', 'policy-group');
      group.append(el('h3', '', CATEGORY_NAMES[cat] || cat.replace(/_/g, ' ')));
      for (const p of policies.filter((x) => x.category === cat)) {
        const btn = el('button', 'policy');
        btn.type = 'button';
        btn.style.setProperty('--tick', TICKS[p.severity]);
        btn.append(el('span', 'tick'), el('span', 'pid', p.id), el('span', 'ptitle', p.title));
        btn.addEventListener('click', () => openDrawer(p.id, btn));
        group.append(btn);
      }
      list.append(group);
    }
  }

  function openDrawer(id, trigger) {
    const p = byId[id];
    if (!p) return;
    lastFocus = trigger || document.activeElement;
    drawer.style.setProperty('--tick', TICKS[p.severity]);
    const body = $('drawerBody');
    body.replaceChildren();
    const title = el('h2', '', p.title);
    title.id = 'drawerTitle';
    const meta = el('div', 'meta');
    meta.append(el('span', 'tag', p.id), el('span', 'tag sev', p.severity), el('span', 'tag', CATEGORY_NAMES[p.category] || p.category));
    if (p.lead) meta.append(el('span', 'tag', 'Leads every answer'));
    body.append(title, meta);
    const section = (heading, content) => body.append(el('h4', '', heading), content);
    section('Applies to', el('p', '', p.applies_to));
    section('What it says (verbatim)', el('p', '', p.advice));
    section('Triggers when', el('pre', '', p.rule.join('\n')));
    if (p.rationale) section('Why this threshold', el('p', '', p.rationale));
    drawer.setAttribute('aria-hidden', 'false');
    scrim.hidden = false;
    $('drawerClose').focus();
  }

  function closeDrawer() {
    drawer.setAttribute('aria-hidden', 'true');
    scrim.hidden = true;
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
  }

  const setRail = (open) => document.body.classList.toggle('rail-open', open);

  /* ---------- a conversation turn ---------- */
  function scrollToEnd() {
    thread.scrollTo({ top: thread.scrollHeight, behavior: reduceMotion ? 'auto' : 'smooth' });
  }

  function words(text) {
    const frag = document.createDocumentFragment();
    text.split(/(\s+)/).forEach((tok, i) => {
      if (!tok.trim()) return frag.append(tok);
      const w = el('span', 'w', tok);
      w.style.setProperty('--i', Math.min(i, 90));
      frag.append(w);
    });
    return frag;
  }

  function workingList() {
    const ul = el('ol', 'working');
    ul.setAttribute('aria-label', 'Progress');
    for (const [node, label] of STEPS) {
      const li = el('li', '', label);
      li.dataset.node = node;
      ul.append(li);
    }
    ul.firstChild.classList.add('now');
    return ul;
  }

  function markStep(ul, node) {
    const items = [...ul.children];
    const idx = items.findIndex((li) => li.dataset.node === node);
    if (idx < 0) return;
    items.forEach((li, i) => {
      li.classList.toggle('done', i <= idx);
      li.classList.toggle('now', i === idx + 1);
    });
  }

  function noteFor(data) {
    if (data.policies.length) return data.policies.length === 1 ? 'Policy-backed answer' : data.policies.length + ' policies apply';
    return { no_sop: 'No policy triggered', out_of_scope: 'Not covered by any policy', ask_location: 'One detail missing' }[data.outcome]
      || 'No forecast was produced';
  }

  function render(data, bulletin) {
    bulletin.replaceChildren();
    document.body.dataset.mood = data.mood;
    const head = el('div', 'sevline');
    head.append(el('span', 'sevword', data.label), el('span', 'sevnote', noteFor(data)));
    const prose = el('p', 'prose');
    prose.append(reduceMotion ? document.createTextNode(data.prose) : words(data.prose));
    bulletin.append(head, prose);

    if (data.facts.length) {
      const dl = el('dl', 'ledger');
      for (const f of data.facts) {
        const row = el('div', 'row');
        const dd = el('dd', '', f.value);
        if (f.unit) dd.append(el('small', '', f.unit));
        row.append(el('dt', '', f.label), dd, el('div', 'win', f.window));
        dl.append(row);
      }
      bulletin.append(dl);
    }
    if (data.policies.length) {
      const ul = el('ul', 'cites');
      for (const p of data.policies) {
        const li = el('li');
        const b = el('button', 'cite', p.id);
        b.type = 'button';
        b.setAttribute('aria-label', 'Open policy ' + p.id);
        b.addEventListener('click', () => openDrawer(p.id, b));
        li.append(b, el('span', 'ctitle', p.title), el('span', 'csev', p.severity));
        ul.append(li);
      }
      bulletin.append(ul);
    } else if (data.checked.length) {
      bulletin.append(el('p', 'src', 'Checked ' + data.checked.join(', ') + '. None of their conditions is met.'));
    }
    if (data.source) {
      bulletin.append(el('p', 'src', `Open-Meteo forecast · ${data.source.place} · ${data.source.lat}, ${data.source.lon} · fetched ${data.source.fetched} local`));
    }
    const details = el('details', 'trace');
    details.append(el('summary', '', 'How this answer was produced'));
    const steps = el('ol', 'steps');
    for (const s of data.trace) steps.append(el('li', /failed|failure/.test(s) ? 'bad' : '', s.replace(/\(.*$/, '')));
    details.append(steps);
    bulletin.append(details);
  }

  async function ask(question) {
    question = question.trim();
    if (!question || busy) return;
    busy = true;
    sendBtn.disabled = true;
    input.value = '';
    $('hello')?.remove();
    turns += 1;

    const turn = el('article', 'turn');
    const q = el('p', 'q');
    q.append(el('span', 'qn', String(turns).padStart(2, '0')), el('span', '', question));
    const bulletin = el('div', 'bulletin');
    const working = workingList();
    bulletin.append(working);
    turn.append(q, bulletin);
    thread.append(turn);
    scrollToEnd();

    try {
      const res = await fetch('/api/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, question }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(typeof detail.detail === 'string' ? detail.detail : 'The service is unavailable (' + res.status + ').');
      }
      const reader = res.body.getReader(), decoder = new TextDecoder();
      let buffer = '', finished = false;
      const handle = (line) => {
        if (!line.trim()) return;
        const evt = JSON.parse(line);
        if (evt.type === 'step') markStep(working, evt.node);
        else if (evt.type === 'result') { render(evt, bulletin); finished = true; }
        else if (evt.type === 'error') throw new Error(evt.message);
      };
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        lines.forEach(handle);
      }
      handle(buffer);
      if (!finished) throw new Error('The answer was cut short. Please try again.');
    } catch (err) {
      bulletin.replaceChildren(el('p', 'notice', err.message || 'Something went wrong. Please try again.'));
      document.body.dataset.mood = 'fault';
    } finally {
      busy = false;
      sendBtn.disabled = false;
      scrollToEnd();
      input.focus();
    }
  }

  /* ---------- wiring ---------- */
  form.addEventListener('submit', (e) => { e.preventDefault(); ask(input.value); });
  document.querySelectorAll('#starters button').forEach((b) => b.addEventListener('click', () => ask(b.textContent)));
  $('drawerClose').addEventListener('click', closeDrawer);
  scrim.addEventListener('click', closeDrawer);
  $('railOpen').addEventListener('click', () => setRail(true));
  $('railClose').addEventListener('click', () => setRail(false));
  $('newSession').addEventListener('click', () => {
    if (busy) return;
    newSession();
    location.reload(); // simplest correct reset: fresh session id, empty thread, idle sky
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeDrawer(); setRail(false); }
    if (e.key === '/' && document.activeElement !== input && !e.metaKey && !e.ctrlKey) { e.preventDefault(); input.focus(); }
  });

  loadPolicies();
})();
