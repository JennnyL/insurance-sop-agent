const $ = id => document.getElementById(id);
const phases = ['VERIFY_ID', 'RESOLVE_INTENT', 'PROCESS_CASE', 'POST_PROCESS'];
const labels = ['Verify identity', 'Find your claim', 'Work through it', 'Your next steps'];
const sample =
  'I’m the policyholder. My name is Margaret Chen, policy POL-9921. I’m calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.';
let state, busy = false,
  pending = null,
  lastMessages = '',
  polling = false;
let lastInlineOffer = null;
let generation = 0,
  active = null,
  controlBusy = false,
  controlPending = null;
let portalTab = 'claims',
  accountData = null,
  accountKey = '',
  accountLoading = false,
  supportRendered = '',
  noteOffer = null;
let chatMode = 'ai',
  liveTicketId = null;
const supportNotes = new Map(),
  supportPending = new Map(),
  enquiryDrafts = new Map(),
  enquiryPending = new Map();
let inquiryCreatePending = null,
  inquiryBusy = false,
  inquiriesRendered = '',
  claimOptions = '';

const api = httpJSON;

function textNode(tag, text, cls) {
  const el = document.createElement(tag);
  el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

function error(message) {
  $('error').textContent = message;
  $('error').hidden = !message;
  if ($('account-dialog').open) portalError(message);
}

function render() {
  if (!state) return;
  const index = phases.indexOf(state.phase);
  document.querySelectorAll('.steps li').forEach((el, i) => {
    el.classList.toggle('active', i === index);
    el.classList.toggle('done', i < index || state.lifecycle === 'COMPLETED');
    el.querySelector('.step-number').textContent = i < index || state.lifecycle === 'COMPLETED' ? '✓' :
      `0${i+1}`;
  });
  $('phase-name').textContent = labels[index];
  $('lifecycle').textContent = state.lifecycle.replaceAll('_', ' ');
  $('gate-status').textContent = state.verified ? (state.caller?.role === 'representative' ? `${state.caller.name} · authorized representative for ${state.data_subject?.name || 'policyholder'}` : 'Identity verified') : 'Identity gate is locked';
  $('gate-icon').textContent = state.verified ? '✓' : '○';
  $('gate-icon').parentElement.classList.toggle('verified', state.verified);
  $('identity-progress').textContent = state.verified ? `${state.identity_count} distinct fields verified` :
    `${state.identity_count} of 3 identity fields collected`;
  $('chat-status').textContent = ['ACTIVE', 'COMPLETED'].includes(state.lifecycle) ?
    'Here to help you move forward' : state.lifecycle === 'HANDOFF_PENDING' ?
    'Human request recorded · simulated' : 'Consultation ' + state.lifecycle.toLowerCase();
  const tickets = (state.handoffs || []).filter(t => t.can_update);
  const live = tickets.find(t => t.id === state.handoff?.id) || tickets[0];
  chatMode = state.support_mode === 'wait' && (live || state.anonymous_support_active) ? 'wait' : 'ai';
  liveTicketId = live?.id || null;
  $('human-chat-bar').hidden = !live;
  if (!live) $('human-chat-label').textContent = '';
  if (live) {
    $('human-chat-label').textContent = `${live.id} · ${live.label} · AI assistant · ${chatMode === 'wait' ? 'Wait only: system acknowledgments' : 'AI answers enabled'}`;
    $('chat-mode-toggle').disabled = busy || controlBusy;
    $('wait-human').disabled = busy || controlBusy || chatMode === 'wait';
  }
  const visibleMessages = [...state.messages];
  visibleMessages.sort((a, b) => (a.at || 0) - (b.at || 0));
  const serialized = JSON.stringify([visibleMessages, state.draft, state.email_operation, state.lifecycle]);
  if (serialized !== lastMessages) {
    const newSummary = state.draft?.id && state.draft.id !== lastInlineOffer;
    lastInlineOffer = state.draft?.id || null;
    const nearBottom = $('messages').scrollHeight - $('messages').scrollTop - $('messages').clientHeight <
      100;
    $('messages').replaceChildren();
    visibleMessages.forEach(m => {
      const item = textNode('div', '', `message ${m.role}`);
      item.append(textNode('div', m.role === 'assistant' ? 'Harbor AI assistant' : m.role ===
        'representative' ? `${m.name||'Representative'} · Human` : m.role === 'system' ?
        'System event' : 'You', 'message-label'), textNode('div', m.text, 'bubble'));
      $('messages').append(item);
    });
    if (state.draft) {
      const card = textNode('article', '', 'summary-card chat-summary');
      card.id = 'chat-summary';
      card.append(textNode('h3', state.draft.subject), textNode('p', 'To ' + (state.draft.recipient_label || 'registered email') + ': ' + state.draft.recipient), textNode('pre', state.draft.body));
      if (!state.email_operation && state.lifecycle === 'ACTIVE') {
        const actions = textNode('div', '', 'summary-actions');
        for (const [label, action] of [['Send this summary', 'email_send'], ['Skip email', 'email_skip']]) {
          const button = textNode('button', label, 'secondary');
          button.onclick = () => submit(action);
          button.disabled = busy;
          actions.append(button);
        }
        card.append(actions);
      }
      card.append(textNode('footer', state.draft.footer, 'source-note'));
      $('messages').append(card);
    }
    lastMessages = serialized;
    if (newSummary || nearBottom || !busy) requestAnimationFrame(() => {
      const card = $('chat-summary');
      $('messages').scrollTop = newSummary && card ? $('messages').scrollTop + card.getBoundingClientRect().top - $('messages').getBoundingClientRect().top : $('messages').scrollHeight;
    });
  }
  $('memory').replaceChildren();
  const entries = Object.entries(state.memory.hints);
  if (entries.length) {
    const dl = document.createElement('dl');
    entries.forEach(([k, v]) => {
      const row = textNode('div', '', 'memory-row');
      row.append(textNode('dt', k.replaceAll('_', ' ')), textNode('dd', k === 'month' ? new Date(2026,
        Number(v.value) - 1, 1).toLocaleString('en', {
        month: 'long'
      }) : v.value));
      row.title = `User reported · source ${v.source_turn}`;
      dl.append(row);
    });
    $('memory').append(dl);
  } else $('memory').append(textNode('p',
    'Useful details will appear here, even when shared before verification.', 'empty'));
  const caseHistory = state.memory.case_history;
  if (caseHistory?.turn_count) $('memory').append(textNode('p',
    `${caseHistory.case_id} · ${caseHistory.turn_count} saved turns · ${caseHistory.discussed_topics.filter(t => !['unknown', 'verification'].includes(t)).map(t => ({denial: 'denial reason', amounts: 'recorded amounts', deadline: 'appeal deadline', status: 'claim status', documents: 'required documents'}[t] || t.replaceAll('_', ' '))).join(', ')}`,
    'source-note'));
  if (state.verified && state.memory.recent_cases?.length) {
    const recent = textNode('div', '', 'recent-cases');
    state.memory.recent_cases.forEach(id => {
      const b = textNode('button', id, 'secondary');
      b.disabled = busy;
      b.onclick = () => {
        closeInspector();
        submit('form', {
          case_id: id
        });
      };
      recent.append(b);
    });
    $('memory').append(recent);
  }
  $('sources').replaceChildren();
  if (!state.verified) $('sources').append(textNode('p', 'Claim data is locked until verification.',
    'empty'));
  else if (!state.evidence.length) {
    if (state.selected_case_id) $('sources').append(textNode('span', state.selected_case_id, 'source-chip'));
    $('sources').append(textNode('p', state.selected_case_id ? 'Case selected. Ask a question to view supporting records.' : 'Identity verified. Waiting for a claim to be resolved.', 'empty'));
  } else {
    if (state.selected_case_id) $('sources').append(textNode('span', state.selected_case_id, 'source-chip'));
    [...new Set(state.evidence.map(x => x.source))].forEach(source => $('sources').append(textNode('span',
      source.replace('.json', '').replaceAll('_', ' '), 'source-chip')));
  }
  $('source-note').textContent = state.evidence.length ?
    'Demo records · source update time not provided. Created dates are claim creation dates.' : '';
  $('clock-note').textContent =
    `Business date: ${state.business_date}${state.historical_clock?' · historical demo clock':''}. Verification uses real time.`;
  $('summary').hidden = !state.draft;
  if (!state.draft) {
    $('summary-body').textContent = '';
    $('summary-footer').textContent = '';
    $('recipient').textContent = '';
    $('summary-subject').textContent = '';
    $('email-note').value = '';
    noteOffer = null;
  }
  if (state.draft) {
    $('recipient').textContent = 'To ' + (state.draft.recipient_label || 'registered email') + ': ' + state.draft.recipient;
    $('summary-body').textContent = state.draft.body;
    $('summary-footer').textContent = state.draft.footer;
    $('summary-subject').textContent = 'Subject: ' + state.draft.subject;
    if (noteOffer !== state.draft.id) {
      $('email-note').value = state.draft.note || '';
      noteOffer = state.draft.id;
    }
    $('note-editor').hidden = state.lifecycle !== 'ACTIVE' || !!state.email_operation;
    $('email-save').disabled = busy;
    $('email-unsaved').hidden = $('email-note').value === (state.draft.note || '');
    const op = state.email_operation;
    $('consent-actions').hidden = !!op || state.lifecycle !== 'ACTIVE';
    $('email-status').textContent = op ? ({
      QUEUED: state.draft.paused ? 'Email paused. Confirm “yes” to resume this unchanged summary.' : 'Queued for the simulated outbox…',
      SIMULATED_RECORDED: 'Recorded in the demo outbox. No real email was sent.',
      CANCELLED: 'Pending email cancelled.'
    } [op.status] || op.status) : 'Optional · registered address only · no verification details included';
    $('email-cancel').hidden = !op || op.status !== 'QUEUED';
  }
  $('review-summary').hidden = !state.draft;
  $('finish').hidden = state.phase !== 'PROCESS_CASE' || state.lifecycle !== 'ACTIVE';
  $('case-picker').replaceChildren();
  $('case-picker').hidden = !state.case_picker && !state.available_claims?.length;
  const choices = state.case_picker?.matches || state.available_claims || [];
  if (choices.length || state.case_picker) {
    const heading = state.caller?.role === 'representative' && state.data_subject?.name
      ? `${state.data_subject.name}’s claims`
      : 'Choose a claim on your verified account:';
    $('case-picker').append(textNode('p', heading));
    if (state.case_picker?.notice) $('case-picker').append(textNode('p', state.case_picker.notice));
    choices.forEach(c => {
      const b = textNode('button', `${c.case_id} · ${c.case_type} · ${c.status} · ${c.created_at}`, 'secondary');
      b.onclick = () => submit('form', {case_id: c.case_id});
      b.disabled = busy;
      $('case-picker').append(b);
    });
  }
  $('suggestions').replaceChildren();
  const suggestions = state.handoff_note_offer ? [['Add question to request', 'handoff_note_accept'], ['Do not add', 'handoff_note_decline']] : state.claim_confirmation ? [['Yes, this claim', 'confirm_claim']] : state.representative_consent?.status === 'pending' ? [['Check policyholder consent · simulated', 'consent_check'], ['Simulate pending / timeout', 'consent_timeout']] : state.closing_pending ? [['No, that’s all', 'closing_no']] :
    state.phase === 'POST_PROCESS' && state.draft && !state.email_operation && state.lifecycle === 'ACTIVE' ?
      [] :
    state.lifecycle !== 'ACTIVE' ? [['Start a new conversation', 'reset']] :
    state.phase === 'VERIFY_ID' ? [['Why verify my identity?', null], ['I don’t want to share my SSN.', null]] :
    state.phase === 'PROCESS_CASE' ? (state.next_steps?.actions || []).map(item => [item.label, item.action === 'question' ? null : item.action]) : [];
  suggestions.forEach(([label, action]) => {
    const b = textNode('button', label);
    b.type = 'button';
    b.disabled = busy;
    b.onclick = () => action === 'closing_no' ? (fill('No, nothing else.'), submit()) : action ? submit(action) : fill(label);
    $('suggestions').append(b);
  });
  if (state.anonymous_support_active) {
    for (const [label, action] of [['Continue with AI', 'resume'], ['Wait for human only', 'wait_human']]) {
      const b = textNode('button', label);
      b.disabled = busy;
      b.onclick = () => submit(action);
      $('suggestions').append(b);
    }
  }
  if (state.candidates.length) state.candidates.forEach(c => {
    const b = textNode('button', `${c.case_id} · ${c.created_at}`);
    b.disabled = busy;
    b.onclick = () => fill(`I mean ${c.case_id}.`);
    $('suggestions').append(b);
  });
  $('message').disabled = !['ACTIVE', 'COMPLETED'].includes(state.lifecycle);
  $('message').maxLength = 4000;
  $('message').placeholder = chatMode === 'wait' ? 'Add a note · confirmation only…' :
    'Ask about your insurance claim…';
  $('send').disabled = controlBusy || !['ACTIVE', 'COMPLETED'].includes(state.lifecycle);
  $('lookup-panel').hidden = state.verified;
  $('sample').hidden = state.verified;
  $('lookup-submit').disabled = controlBusy || !['ACTIVE', 'COMPLETED'].includes(state.lifecycle);
  $('lookup-note').textContent = state.verified ?
    'Your identity is verified. Enter a policy number to list its claims, or a claim ID to switch. Changing identity details will require verification again.' :
    'Provide any three identity fields, or combine this form with details already shared in chat. SSN is optional. A claim ID alone does not unlock records.';
  $('send').setAttribute('aria-label', busy ? 'Send and interrupt reply' : 'Send message');
  $('stop-reply').hidden = !busy && !controlPending;
  $('stop-reply').disabled = controlBusy;
  $('reset').disabled = controlBusy;
  $('email-cancel').disabled = controlBusy;
  $('composer-note').textContent = busy ? 'Send a new message to interrupt this reply.' :
    'Enter to send · Shift + Enter for a new line';
  $('finish').disabled = busy;
  $('email-send').disabled = busy || !!state.draft && $('email-note').value !== (state.draft.note || '');
  $('email-skip').disabled = busy;
  $('human').disabled = controlBusy || state.lifecycle === 'EXPIRED';
  $('portal-human').disabled = $('human').disabled;
  browserSession.setItem('sop-channel', JSON.stringify({
    sid: state.session_id,
    mode: chatMode,
    ticket: liveTicketId
  }));
  renderPortal();
}

function closeInspector() {
  $('inspector').classList.remove('show');
  $('inspect-toggle').setAttribute('aria-expanded', 'false');
}

function fill(text) {
  if (window.matchMedia('(max-width:900px)').matches) closeInspector();
  $('message').value = text;
  $('message').dispatchEvent(new Event('input'));
  $('message').focus();
}

function applyState(fresh) {
  if (!state || (fresh.session_id === state.session_id && fresh.version >= state.version)) {
    if (state && (fresh.authorization_epoch !== state.authorization_epoch || fresh.verified !== state.verified)) {
      $('events').replaceChildren();
      $('metrics').textContent = '';
      $('human-chat-label').textContent = '';
      $('support-receipt').textContent = '';
    }
    state = fresh;
    lastMessages = '';
  }
}
async function diagnostics() {
  if (!state) return;
  const sid = state.session_id,
    epoch = state.authorization_epoch,
    ticket = generation;
  try {
    const d = await api(`/v1/sessions/${sid}/trace`);
    if (ticket !== generation || sid !== state.session_id || epoch !== state.authorization_epoch) return;
    $('metrics').textContent =
      `${d.metrics.completed_turns} completed turns · ${d.metrics.model_calls} model calls · ${d.metrics.input_tokens+d.metrics.output_tokens} reported tokens${d.metrics.usage_unknown_calls ? ` · usage unknown for ${d.metrics.usage_unknown_calls} calls` : ''}`;
    $('events').replaceChildren();
    d.events.slice(-25).reverse().forEach(e => {
      const li = textNode('li', e.kind);
      li.append(textNode('span',
        `v${e.version} · ${Object.entries(e.details).filter(([k]) => !['input_tokens','output_tokens','latency_ms'].includes(k)).map(([k,v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : v}`).join(' · ')}`
      ));
      $('events').append(li);
    });
  } catch {}
}
async function stopCurrent(action = 'interrupt') {
  if (!state || controlBusy) return false;
  const target = active?.payload.message_id || pending?.message_id || controlPending?.target_message_id ||
    null;
  const payload = controlPending?.action === action ? {
    ...controlPending
  } : {
    message_id: requestId(),
    expected_version: state.version,
    action,
    target_message_id: target
  };
  const draft = $('message').value;
  const ticket = ++generation;
  active?.controller.abort();
  active = null;
  pending = null;
  busy = true;
  controlBusy = true;
  controlPending = payload;
  error('');
  lastMessages = '';
  render();
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const fresh = await api(`/v1/sessions/${state.session_id}/messages`, {
          method: 'POST',
          body: JSON.stringify(payload)
        });
        if (ticket !== generation) return false;
        applyState(fresh);
        controlPending = null;
        if (action === 'reset' && $('message').value === draft) $('message').value = '';
        return true;
      } catch (e) {
        if (attempt === 0 && e.message === 'VERSION_CONFLICT') {
          applyState(await api(`/v1/sessions/${state.session_id}`));
          payload.expected_version = state.version;
          continue;
        }
        throw e;
      }
    }
  } catch (e) {
    if (e.status && e.status < 500) controlPending = null;
    error(
      `Could not confirm the interruption (${e.message}). Your draft is saved; review the current state and try again.`
    );
    try {
      applyState(await api(`/v1/sessions/${state.session_id}`));
    } catch {}
    return false;
  } finally {
    if (ticket === generation) {
      busy = false;
      controlBusy = false;
      render();
      diagnostics();
      if ($('account-dialog').open) loadAccount();
      if (state.lifecycle === 'ACTIVE' && !$('account-dialog').open) $('message').focus();
    }
  }
}
async function submit(action = 'message', form = null, extra = {}) {
  if (!state || controlBusy) return;
  const text = action === 'message' ? $('message').value.trim() : '';
  if (action === 'message' && !text) return;
  if (['reset', 'cancel', 'email_cancel', 'interrupt'].includes(action)) {
    await stopCurrent(action);
    return;
  }
  // New input explicitly supersedes an unfinished request. Same-message retries
  // retain their idempotency key when the previous outcome is uncertain.
  const samePending = pending && pending.text === text && pending.action === action && (action !==
      'email_edit' || pending.email_note === extra.email_note) && JSON.stringify(pending.form || null) ===
    JSON.stringify(form);
  if (busy || controlPending || (pending && !samePending)) {
    if (!await stopCurrent()) return;
  }
  const ticket = ++generation;
  const controller = new AbortController();
  const payload = pending && samePending ? {
    ...pending,
    expected_version: state.version
  } : {
    message_id: requestId(),
    expected_version: state.version,
    text,
    action,
    form,
    offer_id: ['email_send', 'email_edit'].includes(action) ? state.draft?.id : null,
    ...extra
  };
  pending = payload;
  active = {
    payload,
    controller
  };
  busy = true;
  error('');
  render();
  if (action === 'message') {
    const item = textNode('div', '', 'message user pending');
    item.append(textNode('div', 'You', 'message-label'), textNode('div', text, 'bubble'));
    $('messages').append(item);
    // Clear only what was submitted now; later typing belongs to a new draft.
    if ($('message').value.trim() === text) $('message').value = '';
    $('message').style.height = 'auto';
  }
  const typing = textNode('div', '', 'message typing');
  typing.append(textNode('div', 'Working through your message…', 'bubble'));
  $('messages').append(typing);
  $('messages').scrollTop = $('messages').scrollHeight;
  try {
    const fresh = await api(`/v1/sessions/${state.session_id}/messages`, {
      method: 'POST',
      body: JSON.stringify(payload),
      signal: controller.signal
    });
    if (ticket !== generation) return;
    applyState(fresh);
    pending = null;
    if (state.draft && action === 'email_edit') openPortal('email');
    if (action === 'form') {
      // Clear only submitted values; preserve edits typed while the request ran.
      Object.entries(form).forEach(([key, value]) => {
        const field = $('lookup-form').elements.namedItem(key);
        if (field && field.value.trim() === value) field.value = '';
      });
      $('lookup-panel').open = false;
    }
  } catch (e) {
    if (ticket !== generation) return;
    if (action === 'message' && !$('message').value) $('message').value = text;
    error(e.message === 'VERSION_CONFLICT' ?
      'This session changed in another tab. Your text is preserved. Review the updated state, then send again.' :
      e.message === 'OFFER_EXPIRED' ?
      'This summary offer expired. Ask to finish again to review a fresh summary.' :
      `Could not complete that request (${e.message}). Your text is preserved; retry when ready.`);
    try {
      const fresh = await api(`/v1/sessions/${state.session_id}`);
      if (ticket === generation) applyState(fresh);
    } catch {}
    if (ticket === generation && e.status && e.status < 500 && e.message !== 'TURN_IN_PROGRESS') pending =
      null;
  } finally {
    typing.remove();
    if (ticket === generation) {
      active = null;
      busy = false;
      render();
      diagnostics();
      if ($('account-dialog').open) loadAccount();
      if (state.lifecycle === 'ACTIVE' && !$('account-dialog').open) $('message').focus();
    }
  }
}
async function start() {
  try {
    const config = await api('/v1/config');
    $('mode').textContent = config.model_mode === 'openai' ? 'LIVE MODEL' : 'OFFLINE DEMO';
    $('mode').title = config.model_name;
    $('model-disclosure').textContent = config.model_mode === 'openai' ?
      'AI answers from your conversation and verified records. Please use synthetic data only.' :
      'Limited scripted walkthrough. Set a server API token to enable AI-written conversational answers.';
    const saved = browserSession.getItem('sop-session');
    if (saved) {
      try {
        state = await api(`/v1/sessions/${saved}`);
      } catch (failure) {
        if (![401, 403, 404, 410].includes(failure.status)) throw failure;
      }
    }
    if (!state) state = await api('/v1/sessions', {
      method: 'POST',
      body: '{}'
    });
    browserSession.setItem('sop-session', state.session_id);
    render();
    startup.ready();
    diagnostics();
  } catch (e) {
    startup.failed(`Unable to open the demo: ${e.message}. Use Retry connection above.`);
    error(`Unable to open the demo: ${e.message}. Reload to try again.`);
    $('messages').replaceChildren();
  }
}
$('composer').addEventListener('submit', e => {
  e.preventDefault();
  submit();
});
$('lookup-form').addEventListener('submit', e => {
  e.preventDefault();
  const form = Object.fromEntries([...new FormData(e.currentTarget)].map(([key, value]) => [key, value
    .trim()
  ]).filter(([, value]) => value));
  if (!Object.keys(form).length) {
    error('Enter identity details or a claim ID.');
    return;
  }
  submit('form', form);
});
$('lookup-sample').onclick = () => {
  const values = {
    name: 'Margaret Chen',
    dob: '1985-03-15',
    ssn_last4: '4472',
    case_id: 'CL-2048'
  };
  Object.entries(values).forEach(([key, value]) => {
    $('lookup-form').elements.namedItem(key).value = value;
  });
};
$('lookup-close').onclick = () => {
  $('lookup-panel').open = false;
  $('message').focus();
};
$('message').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    submit();
  }
});
$('message').addEventListener('input', () => {
  $('message').style.height = 'auto';
  $('message').style.height = Math.min($('message').scrollHeight, 120) + 'px';
});
$('stop-reply').onclick = () => submit('interrupt');
$('email-save').onclick = () => submit('email_edit', null, {
  email_note: $('email-note').value
});
$('email-note').oninput = () => {
  $('email-unsaved').hidden = $('email-note').value === (state.draft?.note || '');
  $('email-send').disabled = busy || !$('email-unsaved').hidden;
};
$('sample').onclick = () => {
  chatMode = 'ai';
  fill(sample);
  render();
};
$('human').onclick = () => submit('human');
$('finish').onclick = () => submit('finish');
$('reset').onclick = () => submit('reset');
$('logout').onclick = async () => {
  if (controlBusy) return;
  ++generation;
  active?.controller.abort();
  active = null;
  busy = false;
  pending = null;
  try {
    await api('/v1/auth/logout', {method: 'POST', body: '{}'});
    applyState(await api(`/v1/sessions/${state.session_id}`));
    accountData = null;
    $('message').value = '';
    $('lookup-form').reset();
    render();
    diagnostics();
  } catch (e) { error(`Could not confirm logout: ${e.message}`); }
};
$('email-send').onclick = () => submit('email_send');
$('email-skip').onclick = () => submit('email_skip');
$('email-cancel').onclick = () => submit('email_cancel');
$('inspect-close').onclick = closeInspector;
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeInspector();
});
$('inspect-toggle').onclick = () => {
  const on = $('inspector').classList.toggle('show');
  $('inspect-toggle').setAttribute('aria-expanded', String(on));
};
setInterval(async () => {
  if (!state || busy || polling) return;
  polling = true;
  const ticket = generation,
    sid = state.session_id;
  try {
    const fresh = await api(`/v1/sessions/${sid}`);
    if (ticket === generation && !busy && (fresh.version > state.version || JSON.stringify(fresh
          .handoffs) !== JSON.stringify(state.handoffs) || fresh.verified !== state.verified || JSON
        .stringify(fresh.support_availability) !== JSON.stringify(state.support_availability))) {
      applyState(fresh);
      render();
      diagnostics();
      if ($('account-dialog').open) loadAccount();
    }
    if ($('account-dialog').open && portalTab === 'enquiries') loadAccount();
  } catch {} finally {
    polling = false;
  }
}, 1500);


// Only real interaction renews idle authentication; background polling cannot.
let lastActivityPing = 0;

function recordActivity(event) {
  if (!event.isTrusted || !state?.verified || Date.now() - lastActivityPing < 60000) return;
  lastActivityPing = Date.now();
  api(`/v1/sessions/${state.session_id}/activity`, {
      method: 'POST',
      body: '{}'
    })
    .then(result => {
      if (!result.verified) refreshSupport();
    })
    .catch(() => {});
}
document.addEventListener('click', recordActivity, {
  passive: true
});
document.addEventListener('keydown', recordActivity, {
  passive: true
});
