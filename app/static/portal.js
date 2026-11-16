// Account panels share the active conversation state and API client in app.js.
function portalError(message) {
  $('portal-error').textContent = message;
  $('portal-error').hidden = !message;
}

function stamp(at) {
  return new Date(at * 1000).toLocaleString();
}

function portalButton(label, fn, cls = 'secondary') {
  const b = textNode('button', label, cls);
  b.type = 'button';
  b.onclick = fn;
  return b;
}

function verifyFromPortal() {
  $('account-dialog').close();
  $('lookup-panel').open = true;
  $('lookup-name').focus();
}

function openPortal(tab) {
  portalTab = tab;
  $('account-dialog').scrollTop = 0;
  if (!$('account-dialog').open) $('account-dialog').showModal();
  portalError('');
  renderPortal();
  loadAccount();
}
async function loadAccount() {
  if (!state?.verified || accountLoading) return;
  accountLoading = true;
  const key = `${state.session_id}:${state.authorization_epoch}`,
    version = state.version;
  try {
    const result = await api(`/v1/sessions/${state.session_id}/account`);
    if (state.verified && key === `${state.session_id}:${state.authorization_epoch}` && state.version ===
      version) {
      accountData = result;
      renderPortal();
    }
  } catch (e) {
    accountData = null;
    if ($('account-dialog').open) portalError(
      `Could not load your account (${e.message}). Verify again if your session has expired.`);
  } finally {
    accountLoading = false;
  }
}

function renderPortal() {
  $('account-auth').textContent = state.verified ?
    'Identity verified · New conversations, tabs, claims, history and email share this authorization until logout or expiry.' :
    'Verify once in chat or with the identity form to unlock your account across conversations and tabs.';
  const key = `${state.session_id}:${state.authorization_epoch}:${state.verified}`;
  if (accountKey !== key) {
    accountKey = key;
    accountData = null;
    supportRendered = '';
    supportNotes.clear();
    supportPending.clear();
    enquiryPending.clear();
    enquiryDrafts.clear();
    inquiriesRendered = '';
    claimOptions = '';
    $('inquiry-form').reset();
    inquiryCreatePending = null;
    if ($('account-dialog').open) loadAccount();
  }
  const tickets = state.verified ? (state.handoffs || []) : [],
    open = tickets.filter(t => !['CANCELLED', 'SIMULATED_CLOSED', 'CLOSED'].includes(t.status));
  $('support-count').textContent = open.length ? `(${open.length})` : '';
  $('support-receipt').hidden = !tickets.length || tickets.some(t => t.can_update);
  $('support-receipt').textContent = '';
  if (tickets.length) $('support-receipt').textContent =
    `${tickets[0].id} · ${tickets[0].label} · You can keep chatting. View Human requests for updates.`;
  const title = {
    claims: 'My claims',
    support: 'Human requests',
    email: 'Optional conversation copy',
    enquiries: 'Email enquiry'
  };
  $('account-title').textContent = title[portalTab];
  document.querySelector('[data-panel="email"]').hidden = portalTab !== 'email';
  document.querySelectorAll('[data-panel]').forEach(b => b.setAttribute('aria-current', String(b.dataset
    .panel === portalTab)));
  ['claims', 'support', 'email', 'enquiries'].forEach(tab => $(`panel-${tab}`).hidden = tab !== portalTab);
  $('account-profile').textContent = accountData ?
    `${state.caller?.role === "representative" ? "Policyholder: " : ""}${accountData.profile.name} · ${accountData.profile.policy_number}` : '';
  $('case-search').hidden = !state.verified;
  $('account-claims').replaceChildren();
  if (!state.verified) {
    $('account-claims').append(textNode('p',
      'Verify any three identity fields to see your claims. A policy or claim number is not required.',
      'empty'), portalButton('Verify my identity', verifyFromPortal));
  } else if (!accountData) {
    $('account-claims').append(textNode('p', 'Loading your account…', 'empty'), portalButton('Refresh claims',
      loadAccount));
  } else if (!accountData.claims.length) {
    $('account-claims').append(textNode('p',
      'No claims were found in the available records for your verified account. A representative can help check records elsewhere.',
      'empty'), portalButton('Request human help', () => submit('human')));
  } else accountData.claims.forEach(c => {
    const card = textNode('article', '', 'portal-card');
    card.append(textNode('h3', `${c.case_id} · ${c.case_type}`), textNode('p',
      `Status: ${c.status} · Created: ${c.created_at}`, 'claim-meta'));
    if (c.denial_reason) card.append(textNode('p', c.denial_reason));
    if (c.documents_needed?.length) card.append(textNode('p', 'Documents requested: ' + c.documents_needed
      .join(', ')));
    if (c.appeal_deadline) card.append(textNode('p',
      `Recorded appeal deadline: ${c.appeal_deadline}${c.appeal_deadline<state.business_date?' · date has passed; ask a representative about available options.':''}`
    ));
    card.append(portalButton('View & discuss this claim', () => {
      $('account-dialog').close();
      submit('form', {
        case_id: c.case_id
      });
    }));
    const linked = (accountData.inquiries || []).filter(q => q.case_id === c.case_id);
    if (linked.length) card.append(portalButton(`Email enquiries (${linked.length})`, () => {
      openPortal('enquiries');
      $('inquiry-case').value = c.case_id;
    }));
    const requests = tickets.filter(t => t.case_id === c.case_id);
    if (requests.length) card.append(portalButton(`Human requests (${requests.length})`, () => openPortal(
      'support')));
    $('account-claims').append(card);
  });
  $('account-source').textContent = accountData?.source_note || '';
  const serial = JSON.stringify(tickets);
  if (serial !== supportRendered) {
    supportRendered = serial;
    $('support-list').replaceChildren();
    if (!tickets.length) $('support-list').append(textNode('p',
      'No human-help requests yet. Create one to receive a tracking reference.', 'empty'));
    tickets.forEach(renderTicket);
  }
  $('email-empty').hidden = !!state.draft;
  $('email-empty').replaceChildren();
  if (!state.draft) {
    const ready = state.verified && ['PROCESS_CASE', 'POST_PROCESS'].includes(state.phase);
    $('email-empty').append(textNode('p', ready ?
      'Prepare an email draft from this consultation. It will not be sent until you confirm.' :
      'Verify your identity and discuss a claim first. Your summary will include its recorded status and next steps.',
      'empty'), portalButton(ready ? 'Write my email summary' : state.verified ? 'Choose a claim' :
      'Verify my identity', () => ready ? submit('email_prepare') : state.verified ? openPortal(
        'claims') : verifyFromPortal()));
  }
  renderInquiries();
  $('email-history').replaceChildren();
  if (!state.verified) $('email-history').append(textNode('p',
    'Verify your identity to see saved email records.', 'empty'));
  else if (!accountData?.emails.length) $('email-history').append(textNode('p',
    'No saved email operations yet. Drafts only appear above until you choose Send.', 'empty'));
  else accountData.emails.forEach(mail => {
    const detail = document.createElement('details');
    detail.className = 'portal-card';
    detail.append(textNode('summary',
      `${mail.case_id||'Consultation'} · ${mail.status.replaceAll('_',' ')} · ${stamp(mail.updated_at)}`
    ), textNode('p', mail.subject), textNode('pre', mail.body, 'mail-history-body'));
    $('email-history').append(detail);
  });
}

function renderTicket(t) {
  const card = textNode('article', '', 'portal-card');
  card.id = 'ticket-' + t.id;
  card.append(textNode('h3', t.id), textNode('p', `${t.label}${t.case_id?' · '+t.case_id:''}`, 'claim-meta'),
    textNode('p', `Updated: ${stamp(t.updated_at)} · Response time: not configured`, 'source-note'));
  if (t.details_locked) {
    card.append(textNode('p',
        'Verify the original account to read this request’s private details and feedback.', 'empty'),
      portalButton('Verify my identity', verifyFromPortal));
  } else {
    const timeline = textNode('ol', '', 'ticket-timeline');
    t.timeline.forEach(item => {
      const li = document.createElement('li');
      li.append(textNode('small', `${stamp(item.at)} · ${item.actor.replaceAll('_',' ')}`), textNode('p',
        item.text));
      timeline.append(li);
    });
    const details = document.createElement('details');
    details.append(textNode('summary', 'Request context'));
    const summary = t.summary || {};
    details.append(textNode('p', summary.verified_identity ? `Verified party: ${summary.verified_identity.party_id}` : 'Identity has not been verified yet.'));
    details.append(textNode('p', `Reason: ${(summary.handoff_reason || summary.reason || 'Customer requested help').replaceAll('_', ' ').toLowerCase()}`));
    details.append(textNode('p', `Unresolved: ${(summary.open_questions || []).join('; ') || 'Customer requests representative assistance.'}`));
    details.append(textNode('p', (summary.discussion || []).join('\n')));
    card.append(details, timeline);
    if (t.can_update && t.channel === 'MOCK') {
      const label = textNode('label', 'Add a follow-up (saved in this demo only)');
      const input = document.createElement('textarea');
      input.maxLength = 1000;
      input.rows = 3;
      input.value = supportNotes.get(t.id) || '';
      input.setAttribute('aria-label', 'Follow-up for ' + t.id);
      input.oninput = () => supportNotes.set(t.id, input.value);
      label.append(input);
      card.append(label);
      const actions = textNode('div', '', 'portal-actions');
      actions.append(portalButton('Save follow-up', () => supportUpdate(t, 'note', input.value)),
        portalButton('Cancel request', () => supportUpdate(t, 'cancel')));
      card.append(actions);
      if (t.channel === 'MOCK') {
        const simulation = document.createElement('details');
        simulation.className = 'demo-controls';
        simulation.append(textNode('summary', 'Demo controls · simulate staff updates'), textNode('p',
          'Use these controls to test receiving feedback. They do not contact a real representative.'));
        if (t.status === 'SIMULATED_RECORDED') simulation.append(portalButton('Simulate acknowledgement',
          () => supportUpdate(t, 'demo_acknowledge')));
        if (['SIMULATED_RECORDED', 'SIMULATED_ACKNOWLEDGED'].includes(t.status)) simulation.append(
          portalButton('Simulate representative reply', () => supportUpdate(t, 'demo_reply')));
        if (t.status === 'SIMULATED_RESPONDED') simulation.append(portalButton('Simulate closure', () =>
          supportUpdate(t, 'demo_close')));
        card.append(simulation);
      }
    }
  }
  $('support-list').append(card);
}
async function supportUpdate(t, action, text = '') {
  if (action === 'note' && !text.trim()) {
    portalError('Enter a follow-up message first.');
    return;
  }
  const prior = supportPending.get(t.id);
  if (prior?.running) return;
  const same = prior?.payload.action === action && prior.payload.text === text;
  const payload = same ? prior.payload : {
    session_id: state.session_id,
    message_id: requestId(),
    expected_revision: t.revision,
    action,
    text
  };
  supportPending.set(t.id, {
    payload,
    running: true
  });
  portalError('');
  try {
    await api(`/v1/handoffs/${t.id}/${action==='cancel'?'cancel':'updates'}`, {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    supportPending.delete(t.id);
    if (action === 'note') supportNotes.delete(t.id);
    await refreshSupport();
    return true;
  } catch (e) {
    if (e.status && e.status < 500) supportPending.delete(t.id);
    else supportPending.set(t.id, {
      payload,
      running: false
    });
    error(`Update not confirmed (${e.message}). Your note is preserved. Refresh progress and retry.`);
  }
}
async function refreshSupport() {
  const sid = state.session_id;
  try {
    const fresh = await api(`/v1/sessions/${sid}`);
    if (sid === state.session_id) {
      applyState(fresh);
      render();
    }
  } catch (e) {
    portalError(`Could not refresh progress (${e.message}).`);
  }
}
$('review-summary').onclick = () => openPortal('email');
$('inquiry-open').onclick = () => openPortal('enquiries');
$('my-claims').onclick = () => openPortal('claims');
$('support-open').onclick = () => openPortal('support');
$('account-close').onclick = () => $('account-dialog').close();
document.querySelectorAll('[data-panel]').forEach(b => b.onclick = () => openPortal(b.dataset.panel));
$('portal-human').onclick = () => submit('human');
$('support-refresh').onclick = refreshSupport;
$('ticket-search').onsubmit = async e => {
  e.preventDefault();
  portalError('');
  const id = $('ticket-id').value.trim();
  if (!id) return;
  try {
    const t = await api(`/v1/handoffs/${encodeURIComponent(id)}?session_id=${state.session_id}`);
    await refreshSupport();
    const card = $('ticket-' + t.id);
    card?.scrollIntoView({
      block: 'center'
    });
    card?.classList.add('ticket-found');
  } catch (err) {
    portalError(
      'No accessible request found for that reference. Check the number and verify the original account.'
    );
  }
};
$('account-dialog').addEventListener('close', () => {
  $('message').focus();
});
$('chat-mode-toggle').onclick = () => submit('resume');
$('wait-human').onclick = () => submit('wait_human');


function renderInquiries() {
  $('inquiry-form').hidden = !state.verified;
  $('inquiry-search').hidden = !state.verified;
  $('inquiry-gate').replaceChildren();
  if (!state.verified) $('inquiry-gate').append(textNode('p',
    'Verify your identity to send or view an enquiry saved to your account.', 'empty'), portalButton(
    'Verify my identity', verifyFromPortal));
  const cases = accountData?.claims || [],
    options = JSON.stringify(cases.map(c => c.case_id));
  if (options !== claimOptions) {
    claimOptions = options;
    const select = $('inquiry-case'),
      old = select.value;
    select.replaceChildren();
    const general = textNode('option', 'General account enquiry');
    general.value = '';
    select.append(general);
    cases.forEach(c => {
      const o = textNode('option', `${c.case_id} · ${c.case_type} · ${c.status}`);
      o.value = c.case_id;
      select.append(o);
    });
    select.value = cases.some(c => c.case_id === old) ? old : cases.some(c => c.case_id === state
      .selected_case_id) ? state.selected_case_id : '';
  }
  const inquiries = state.verified ? (accountData?.inquiries || []) : [],
    serial = JSON.stringify(inquiries);
  if (serial === inquiriesRendered) return;
  inquiriesRendered = serial;
  $('inquiry-list').replaceChildren();
  if (!inquiries.length) $('inquiry-list').append(textNode('p', state.verified ?
    'No enquiries yet. Send the form above to create a tracking reference.' :
    'Your enquiry history is protected until verification.', 'empty'));
  inquiries.forEach(q => {
    const card = textNode('article', '', 'portal-card');
    card.id = 'inquiry-' + q.id;
    card.append(textNode('h3', `${q.id} · ${q.case_id||'Account enquiry'}`), textNode('p', q.status
      .replaceAll('_', ' ') + ' · ' + stamp(q.updated_at), 'claim-meta'), textNode('p', 'To: ' + q
      .recipient), textNode('strong', q.subject), textNode('p', q.body));
    const list = textNode('ol', '', 'ticket-timeline');
    q.timeline.forEach(m => {
      const li = document.createElement('li');
      li.append(textNode('small', stamp(m.at) + ' · ' + m.actor.replaceAll('_', ' ')), textNode('p', m
        .text));
      list.append(li);
    });
    card.append(list);
    if (q.status === 'QUEUED') card.append(portalButton('Cancel pending enquiry', () => inquiryUpdate(q,
      'cancel')));
    if (['SIMULATED_SENT', 'SIMULATED_REPLIED'].includes(q.status)) {
      const input = document.createElement('textarea');
      input.rows = 3;
      input.maxLength = 1000;
      input.setAttribute('aria-label', 'Email follow-up for ' + q.id);
      input.value = enquiryDrafts.get(q.id) || '';
      input.oninput = () => enquiryDrafts.set(q.id, input.value);
      card.append(input, portalButton('Send follow-up · simulated', () => inquiryUpdate(q, 'followup',
        input.value)), portalButton('Close enquiry', () => inquiryUpdate(q, 'close')));
      if (q.status === 'SIMULATED_SENT') {
        const demo = document.createElement('details');
        demo.className = 'demo-controls';
        demo.append(textNode('summary', 'Demo controls'), portalButton('Simulate official reply', () =>
          inquiryUpdate(q, 'demo_reply')));
        card.append(demo);
      }
    }
    $('inquiry-list').append(card);
  });
}
$('inquiry-form').onsubmit = async e => {
  e.preventDefault();
  if (inquiryBusy) return;
  const values = {
    case_id: $('inquiry-case').value || null,
    subject: $('inquiry-subject').value.trim(),
    body: $('inquiry-body').value.trim()
  };
  if (!values.subject || !values.body) {
    portalError('Enter a subject and message.');
    return;
  }
  const same = inquiryCreatePending && JSON.stringify(inquiryCreatePending.values) === JSON.stringify(
    values);
  const payload = same ? inquiryCreatePending.payload : {
    message_id: requestId(),
    ...values
  };
  inquiryCreatePending = {
    payload,
    values
  };
  inquiryBusy = true;
  $('inquiry-submit').disabled = true;
  portalError('');
  try {
    const q = await api(`/v1/sessions/${state.session_id}/inquiries`, {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    inquiryCreatePending = null;
    if ($('inquiry-subject').value.trim() === values.subject) $('inquiry-subject').value = '';
    if ($('inquiry-body').value.trim() === values.body) $('inquiry-body').value = '';
    await loadAccount();
    $('inquiry-reference').value = q.id;
    const card = $('inquiry-' + q.id);
    card?.scrollIntoView({
      block: 'center'
    });
    card?.classList.add('ticket-found');
  } catch (e) {
    if (e.status && e.status < 500) inquiryCreatePending = null;
    portalError(
      `Enquiry not confirmed (${e.message}). Your draft is preserved. Retry with the same content if the outcome is uncertain.`
    );
  } finally {
    inquiryBusy = false;
    $('inquiry-submit').disabled = false;
  }
};
async function inquiryUpdate(q, action, text = '') {
  if (action === 'followup' && !text.trim()) {
    portalError('Enter a follow-up message.');
    return;
  }
  let prior = enquiryPending.get(q.id);
  if (prior?.running) return;
  const payload = prior && prior.payload.action === action && prior.payload.text === text ? prior.payload : {
    message_id: requestId(),
    expected_revision: q.revision,
    action,
    text
  };
  enquiryPending.set(q.id, {
    payload,
    running: true
  });
  portalError('');
  try {
    await api(`/v1/sessions/${state.session_id}/inquiries/${q.id}`, {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    enquiryPending.delete(q.id);
    if (action === 'followup') enquiryDrafts.delete(q.id);
    await loadAccount();
  } catch (e) {
    if (e.status && e.status < 500) enquiryPending.delete(q.id);
    else enquiryPending.set(q.id, {
      payload,
      running: false
    });
    portalError(`Update not confirmed (${e.message}). Your draft is preserved; refresh and retry.`);
    await loadAccount();
  }
}
$('inquiry-search').onsubmit = async e => {
  e.preventDefault();
  portalError('');
  const id = $('inquiry-reference').value.trim();
  try {
    await api(`/v1/sessions/${state.session_id}/inquiries/${encodeURIComponent(id)}`);
    await loadAccount();
    const card = $('inquiry-' + id);
    card?.scrollIntoView({
      block: 'center'
    });
    card?.classList.add('ticket-found');
  } catch {
    portalError('No accessible enquiry found. Check the reference and verify the original account.');
  }
};



$('case-search').onsubmit = async event => {
  event.preventDefault();
  const value = $('case-reference').value.trim().toUpperCase();
  if (!/^(?:POL|CL)-\d+$/.test(value)) {
    portalError('Enter a policy number or claim ID.');
    return;
  }
  $('account-dialog').close();
  await submit('form', value.startsWith('POL-') ? {
    policy_number: value
  } : {
    case_id: value
  });
};
start();
