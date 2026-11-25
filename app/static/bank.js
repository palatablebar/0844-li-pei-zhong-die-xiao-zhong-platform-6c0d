(function () {
  var root = document.getElementById('bank-workbench');
  if (!root) return;

  var lastTransactionId = null;

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }
  function money(cents) {
    var value = Number(cents || 0);
    if (!Number.isFinite(value)) value = 0;
    return '¥' + (value / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function setNotice(text, isError) {
    var node = $('bank-notice');
    node.textContent = text || '';
    node.classList.toggle('error', !!isError);
  }
  async function request(url, options) {
    var response = await fetch(url, Object.assign(
      { headers: { 'Content-Type': 'application/json' } },
      options || {}
    ));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || data.code || ('HTTP ' + response.status));
    return data;
  }
  function txnMap(state) {
    return (state.transactions || []).reduce(function (map, txn) {
      map[txn.id] = txn;
      return map;
    }, {});
  }
  function renderPayables(state) {
    var rows = state.payables || [];
    $('bank-payable-count').textContent = rows.length + ' 笔';
    if (!rows.length) {
      $('bank-payable-body').innerHTML = '<tr><td colspan="6" class="empty">暂无数据，可载入演示场景。</td></tr>';
      return;
    }
    var txns = txnMap(state);
    $('bank-payable-body').innerHTML = rows.map(function (p) {
      var refs = (p.allocations || []).map(function (a) {
        var txnId = a.bank_transaction_id || a.transaction_id || '-';
        var replay = txns[txnId] && Number(txns[txnId].replay_count || 0) > 0 ? ' <span class="duplicate-badge">幂等重试×' + txns[txnId].replay_count + '</span>' : '';
        return '<div class="txn-ref">' + esc(txnId) + replay + '</div>';
      }).join('') || '<span class="hint">—</span>';
      return '<tr data-testid="payable-row" data-payable-id="' + esc(p.payable_id) + '" data-status="' + esc(p.status) + '">' +
        '<td><strong>' + esc(p.payable_id) + '</strong></td>' +
        '<td class="num" data-amount-cents="' + esc(p.amount_cents) + '">' + money(p.amount_cents) + '</td>' +
        '<td class="num">' + money(p.settled_amount) + '</td>' +
        '<td class="num">' + money(p.remaining_amount) + '</td>' +
        '<td><span class="bank-status ' + esc(p.status) + '">' + esc(p.status_label || p.status) + '</span></td>' +
        '<td>' + refs + '</td>' +
      '</tr>';
    }).join('');
  }
  function candidateButtons(item) {
    var ids = item.possible_payable_ids && item.possible_payable_ids.length ? item.possible_payable_ids : (item.candidate_payable_ids || []);
    if (item.status === 'CLAIMED') return '<p class="hint">已认领并结清。</p>';
    return '<div class="candidate-buttons">' + ids.map(function (id) {
      return '<button type="button" data-claim-suspense="' + esc(item.id) + '" data-payable="' + esc(id) + '">认领给 ' + esc(id) + '</button>';
    }).join('') + '</div>';
  }
  function renderSuspense(state) {
    var items = state.suspense || [];
    var held = items.filter(function (x) { return x.status !== 'CLAIMED'; });
    $('bank-hanging-count').textContent = held.length + ' 笔挂账 / 共 ' + items.length + ' 笔记录';
    if (!items.length) {
      $('bank-suspense-list').innerHTML = '<p class="empty">暂无挂账。</p>';
      return;
    }
    $('bank-suspense-list').innerHTML = items.map(function (item) {
      var candidates = (item.candidate_sets || []).map(function (set) { return set.join(' + '); }).join('；');
      return '<article class="suspense-card ' + esc((item.status || 'HELD').toLowerCase()) + '" data-testid="suspense-row" data-suspense-id="' + esc(item.id) + '" data-status="' + esc(item.status) + '">' +
        '<div class="suspense-title"><span>' + esc(item.bank_transaction_id) + (item.payable_id ? ' → ' + esc(item.payable_id) : '') + '</span>' +
        '<span class="suspense-amount" data-amount-cents="' + esc(item.amount_cents) + '">' + money(item.amount_cents) + '</span></div>' +
        '<div class="suspense-meta"><strong>' + esc(item.status_label || '挂账中') + '</strong> · ' + esc(item.reason_text || item.reason) + '<br>' +
        (candidates ? '可能归属组合：' + esc(candidates) + '<br>' : '') +
        (item.common_payable_ids && item.common_payable_ids.length ? '已确认共同份额：' + esc(item.common_payable_ids.join(', ')) + '<br>' : '') +
        '挂账号：' + esc(item.id) + '</div>' +
        candidateButtons(item) +
      '</article>';
    }).join('');
  }
  function renderAllocations(state) {
    var rows = state.allocations || [];
    $('bank-allocation-count').textContent = rows.length;
    if (!rows.length) {
      $('bank-allocation-body').innerHTML = '<tr><td colspan="6" class="empty">暂无销账。</td></tr>';
      return;
    }
    var txns = txnMap(state);
    $('bank-allocation-body').innerHTML = rows.slice().reverse().map(function (a) {
      var txnId = a.bank_transaction_id || a.transaction_id;
      var txn = txns[txnId] || {};
      var idempotent = Number(txn.replay_count || 0) > 0
        ? '<span class="duplicate-badge">重试 ' + txn.replay_count + ' 次，未重复</span>'
        : (txn.duplicate ? '<span class="duplicate-badge">幂等返回</span>' : '首次销账');
      return '<tr data-allocation-id="' + esc(a.id) + '">' +
        '<td class="txn-ref">' + esc(txnId) + '</td>' +
        '<td><strong>' + esc(a.payable_id) + '</strong></td>' +
        '<td class="num">' + money(a.amount_cents) + '</td>' +
        '<td>' + esc(a.source || '') + '</td>' +
        '<td>' + esc(a.settlement_date || a.settled_on || '') + '</td>' +
        '<td>' + idempotent + '</td>' +
      '</tr>';
    }).join('');
  }
  function render(state) {
    var summary = state.summary || state.totals || {};
    $('bank-settled-count').textContent = summary.settled_count || 0;
    $('bank-settled-ids').textContent = (state.settled_payables || []).map(function (p) { return p.payable_id; }).join('、') || '暂无已结应付';
    var hanging = summary.hanging_total || 0;
    var hangingNode = $('bank-hanging-total');
    hangingNode.textContent = money(summary.hanging_total_cents || hanging);
    hangingNode.setAttribute('data-hanging-cents', String(summary.hanging_total_cents || hanging || 0));
    $('bank-outstanding-total').textContent = money(summary.outstanding_total || 0);
    $('bank-open-count').textContent = (summary.open_count || 0) + ' 笔待结 / ' + (summary.partial_count || 0) + ' 笔部分';
    $('bank-replay-count').textContent = '重试 ' + (summary.replay_count || 0) + ' 次，未重复销账';
    renderPayables(state);
    renderSuspense(state);
    renderAllocations(state);
    if (state.transaction && state.transaction.id) lastTransactionId = state.transaction.id;
    if (!lastTransactionId && state.transactions && state.transactions.length) {
      lastTransactionId = state.transactions[state.transactions.length - 1].id;
    }
  }
  async function refresh() {
    try {
      var state = await request('/api/bank/state');
      render(state);
    } catch (error) { setNotice(error.message, true); }
  }

  $('bank-payable-form').addEventListener('submit', async function (event) {
    event.preventDefault();
    var form = new FormData(event.currentTarget);
    var payload = {
      payable_id: form.get('payable_id').trim(),
      amount_cents: Number(form.get('amount_cents')),
      status: form.get('status')
    };
    try {
      setNotice('正在登记应付…');
      var state = await request('/api/bank/payables', { method: 'POST', body: JSON.stringify(payload) });
      event.currentTarget.reset();
      setNotice('应付已加入台账。');
      render(state);
    } catch (error) { setNotice(error.message, true); }
  });

  $('bank-txn-form').addEventListener('submit', async function (event) {
    event.preventDefault();
    var form = new FormData(event.currentTarget);
    var ids = String(form.get('payable_ids') || '').split(',').map(function (x) { return x.trim(); }).filter(Boolean);
    var payload = {
      bank_transaction_id: form.get('bank_transaction_id').trim(),
      amount_cents: Number(form.get('amount_cents')),
      memo: form.get('memo') || ''
    };
    if (ids.length) payload.payable_ids = ids;
    try {
      setNotice('正在处理到账…');
      var state = await request('/api/bank/transactions', { method: 'POST', body: JSON.stringify(payload) });
      lastTransactionId = payload.bank_transaction_id;
      render(state);
      var txn = state.transaction || {};
      setNotice('流水 ' + payload.bank_transaction_id + ' 已处理：销账 ' +
        ((txn.allocation_ids || []).length) + ' 笔，挂账 ' + ((txn.suspense_ids || []).length) + ' 笔。');
    } catch (error) { setNotice(error.message, true); }
  });

  $('bank-retry-last').addEventListener('click', async function () {
    if (!lastTransactionId) {
      var state = await request('/api/bank/state');
      var txns = state.transactions || [];
      if (!txns.length) return setNotice('还没有可重试的银行流水。', true);
      lastTransactionId = txns[txns.length - 1].id;
    }
    try {
      var result = await request('/api/bank/transactions/' + encodeURIComponent(lastTransactionId) + '/retry', { method: 'POST' });
      render(result);
      setNotice('同一流水重试完成：返回首次结果，未新增销账。');
    } catch (error) { setNotice(error.message, true); }
  });

  root.addEventListener('click', async function (event) {
    var button = event.target.closest('[data-claim-suspense]');
    if (!button) return;
    var suspenseId = button.getAttribute('data-claim-suspense');
    var payableId = button.getAttribute('data-payable');
    try {
      var state = await request('/api/bank/suspense/' + encodeURIComponent(suspenseId) + '/claim', {
        method: 'POST', body: JSON.stringify({ payable_id: payableId })
      });
      render(state);
      setNotice('挂账份额已认领给 ' + payableId + '。');
    } catch (error) { setNotice(error.message, true); }
  });

  ['first', 'late', 'all'].forEach(function (through) {
    var button = document.querySelector('[data-demo="' + through + '"]');
    if (!button) return;
    button.addEventListener('click', async function () {
      try {
        setNotice('正在载入演示场景…');
        var state = await request('/api/bank/demo', { method: 'POST', body: JSON.stringify({ through: through }) });
        lastTransactionId = 'BK-20261125-001';
        render(state);
        setNotice('演示数据已载入。可点击“重放最近流水”验证幂等。');
      } catch (error) { setNotice(error.message, true); }
    });
  });
  $('bank-refresh').addEventListener('click', refresh);
  $('bank-reset').addEventListener('click', async function () {
    try {
      var state = await request('/api/bank/reset', { method: 'POST' });
      lastTransactionId = null;
      render(state);
      setNotice('银行对账工作台已重置。');
    } catch (error) { setNotice(error.message, true); }
  });

  if (window.location.pathname === '/bank' && root.scrollIntoView) {
    root.scrollIntoView({ block: 'start' });
  }
  refresh();
})();
