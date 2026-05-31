/* Goal Proactive Test Console — frontend logic */
(function () {
  'use strict';

  // ── State ──────────────────────────────────────────────────────
  var lastReport = null;
  var userId = '';
  var botId = '';

  // ── DOM refs ───────────────────────────────────────────────────
  function $(sel) { return document.querySelector(sel); }
  function $$(sel) { return document.querySelectorAll(sel); }

  var loginView = $('#loginView');
  var appView = $('#appView');
  var passwordInput = $('#passwordInput');
  var loginBtn = $('#loginBtn');
  var loginError = $('#loginError');
  var logoutBtn = $('#logoutBtn');

  // Top bar
  var barUser = $('#barUser');
  var barBot = $('#barBot');
  var barBridge = $('#barBridge');
  var barProactive = $('#barProactive');
  var barTodaySent = $('#barTodaySent');

  // Lab
  var runMode = $('#runMode');
  var impulseType = $('#impulseType');
  var debugNow = $('#debugNow');
  var simSilent = $('#simSilent');
  var simLastProactive = $('#simLastProactive');
  var userIdInput = $('#userId');
  var labSummary = $('#labSummary');

  // Buttons
  var btnCheck = $('#btnCheck');
  var btnImpulses = $('#btnImpulses');
  var btnDryRun = $('#btnDryRun');
  var btnRealSend = $('#btnRealSend');
  var btnCopyReport = $('#btnCopyReport');

  // Panels
  var eligPanel = $('#eligPanel');
  var eligResult = $('#eligResult');
  var impulsePanel = $('#impulsePanel');
  var impulseResult = $('#impulseResult');
  var chainPanel = $('#chainPanel');
  var chainResult = $('#chainResult');
  var historyPanel = $('#historyPanel');
  var historyResult = $('#historyResult');
  var runsResult = $('#runsResult');

  // Confirm overlay
  var confirmOverlay = null;

  // ══════════════════════════════════════════════════════════════
  // API helpers
  // ══════════════════════════════════════════════════════════════

  function api(method, path, body) {
    var opts = { method: method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    return fetch(path, opts).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t); });
      return r.json();
    });
  }

  function apiGet(path) { return api('GET', path); }
  function apiPost(path, body) { return api('POST', path, body); }

  function getUserId() { return userIdInput.value.trim() || userId; }

  function buildParams() {
    var p = {
      user_id: getUserId(),
      bot_id: botId,
      forced_impulse_type: impulseType.value,
    };
    if (debugNow.value) p.debug_now = debugNow.value + ':00+08:00';
    var s = parseInt(simSilent.value, 10);
    if (!isNaN(s) && s > 0) p.simulate_user_silent_seconds = s;
    s = parseInt(simLastProactive.value, 10);
    if (!isNaN(s) && s > 0) p.simulate_last_proactive_seconds = s;
    return p;
  }

  // ══════════════════════════════════════════════════════════════
  // Login
  // ══════════════════════════════════════════════════════════════

  function checkAuth() {
    return apiGet('/admin/auth/status').then(function (r) {
      if (r.authenticated) { showApp(); } else { showLogin(); }
    }).catch(function () { showLogin(); });
  }

  function showLogin() {
    loginView.style.display = 'flex';
    appView.style.display = 'none';
  }

  function showApp() {
    loginView.style.display = 'none';
    appView.style.display = '';
    loadBootstrap();
    loadRuns();
  }

  loginBtn.addEventListener('click', function () {
    var pw = passwordInput.value;
    if (!pw) { loginError.style.display = 'block'; loginError.textContent = '请输入密码'; return; }
    loginError.style.display = 'none';
    apiPost('/admin/auth/login', { password: pw }).then(function (r) {
      if (r.status === 'ok') { showApp(); } else { throw new Error('登录失败'); }
    }).catch(function (e) {
      loginError.style.display = 'block';
      loginError.textContent = '密码不对或网络错误';
    });
  });

  passwordInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') loginBtn.click();
  });

  logoutBtn.addEventListener('click', function () {
    apiPost('/admin/auth/logout').then(function () { showLogin(); }).catch(function () { showLogin(); });
  });

  // ══════════════════════════════════════════════════════════════
  // Bootstrap
  // ══════════════════════════════════════════════════════════════

  function loadBootstrap() {
    apiGet('/admin/proactive-test/bootstrap').then(function (r) {
      if (!r.ok) return;
      userId = r.current_user_id || '';
      botId = r.current_bot_id || 'default';
      barUser.textContent = 'user: ' + (userId || '—');
      barBot.textContent = 'bot: ' + botId;
      barBridge.textContent = 'bridge ' + (r.bridge.online ? 'online' : 'offline');
      barBridge.className = 'bar-tag ' + (r.bridge.online ? 'green' : 'red');
      barProactive.textContent = 'proactive ' + (r.features.proactive ? 'on' : 'off');
      barProactive.className = 'bar-tag ' + (r.features.proactive ? 'green' : 'red');
      barTodaySent.textContent = 'today ' + (r.proactive.today_real_send_count || 0);
      if (!r.bridge.online) {
        btnRealSend.disabled = true;
        btnRealSend.title = 'Bridge offline — real-send will fail';
      } else {
        btnRealSend.disabled = false;
        btnRealSend.title = '';
      }
    }).catch(function () {});
  }

  // ══════════════════════════════════════════════════════════════
  // v2.3 readability: translation tables
  // ══════════════════════════════════════════════════════════════

  var STAGE_LABELS = {
    bootstrap: '启动',
    eligibility: '资格检查',
    impulse_generation: '主动理由生成',
    impulse_filtering: '主动理由过滤',
    impulse_selection: '主动理由选择',
    proactive_context_build: '上下文构建',
    llm_step: '模型调用',
    model_tool_call: '工具调用',
    guard: '文案安全检查',
    send: '发送检查',
    history_write: '历史写入检查',
    terminal: '本轮结束状态'
  };

  var STAGE_STATUS_LABELS = {
    pass: '通过', ok: '通过', done: '完成', written: '已写入',
    fail: '未通过', block: '已拦截', empty: '无结果',
    simulated: '已模拟', skip: '已跳过', not_written: '未写入', skipped: '已跳过',
    end: '结束'
  };

  var REASON_LABELS = {
    'no_impulse_selected': '没有选中主动理由',
    'proactive feature is disabled': '主动消息功能未开启',
    'eligibility_failed': '资格检查未通过',
    'dry_run_never_writes_visible_history': 'Dry-run 不写入可见历史',
    'active_proactive_turn_running': '另一个主动回合正在执行',
    'scheduler_busy': '另一个主动回合正在执行',
    'no candidate passed filters': '所有候选被过滤（去重/过期/评分不达标）',
    'no candidate passed score threshold': '候选评分未达阈值',
    'no candidates generated': '未生成任何候选',
    'user_silent_window_not_met': '用户静默窗口未达标',
    'sleep_hours': '当前处于睡眠时段',
    'daily_limit_reached': '今日发送已达上限',
    'min_interval_not_met': '距上次主动消息太近'
  };

  var TS_LABELS = {
    'end_replied': '已回复',
    'end_no_reply': '未回复',
    'end_failed': '失败',
    'end_fallback_sent': '兜底已发送',
    'end_cancelled': '已取消（被新消息打断）',
    'end_superseded_before_send': '发送前被取代',
    'end_interrupted_during_reply_sequence': '回复序列中被中断',
    'end_max_limit': '达到最大限制',
    'end_protocol_error': '协议错误',
    'end_aborted_by_system': '被系统中止',
    'end_skipped_proactive': '主动跳过',
    'end_failed_no_fallback': '失败（已抑制兜底）',
    'end_proactive_guard_blocked': 'Guard 拦截',
    'end_proactive_max_reply_limit': '达到最大回复条数',
    'end_proactive_max_llm_steps': '达到最大 LLM 步数'
  };

  function tsLabel(ts) {
    if (!ts) return '—';
    var clean = String(ts).replace('TurnState.', '');
    return TS_LABELS[clean] || clean;
  }

  function reasonLabel(r) { return REASON_LABELS[r] || r || ''; }
  function stageLabel(s) { return STAGE_LABELS[s] || s; }
  function stageStatusLabel(s) { return STAGE_STATUS_LABELS[s] || s; }

  function _chainGuardBlocked(chain) {
    for (var i = 0; i < chain.length; i++) {
      if (chain[i].stage === 'guard') {
        var s = chain[i].status;
        return s === 'block' || s === 'fail';
      }
    }
    return false;
  }

  function _chainGuardExecuted(chain) {
    for (var i = 0; i < chain.length; i++) {
      if (chain[i].stage === 'guard') return true;
    }
    return false;
  }

  // ══════════════════════════════════════════════════════════════
  // Eligibility check (simplified display for check-only result)
  // ══════════════════════════════════════════════════════════════

  btnCheck.addEventListener('click', function () {
    resetPanels();
    setButtonLoading(btnCheck, true);
    apiPost('/admin/proactive-test/check', buildParams()).then(function (r) {
      renderEligibility(r);
      eligPanel.style.display = '';
    }).catch(function (e) {
      showLabSummary('fail', '检查失败: ' + e.message);
    }).finally(function () {
      setButtonLoading(btnCheck, false);
    });
  });

  function renderEligibility(r) {
    var checks = r.checks || [];
    var html = '<table class="checks-table"><thead><tr><th>检查项</th><th>结果</th><th>实际值</th><th>要求</th><th>原因</th></tr></thead><tbody>';
    for (var i = 0; i < checks.length; i++) {
      var c = checks[i];
      html += '<tr>';
      html += '<td>' + esc(c.name) + '</td>';
      html += '<td><span class="' + (c.passed ? 'pass-badge' : 'fail-badge') + '">' + (c.passed ? 'PASS' : 'FAIL') + '</span></td>';
      html += '<td>' + esc(String(c.actual)) + '</td>';
      html += '<td>' + esc(String(c.required)) + '</td>';
      html += '<td>' + (c.reason ? esc(c.reason) : '—') + '</td>';
      html += '</tr>';
    }
    html += '</tbody></table>';
    if (r.eligible) {
      html += '<p style="margin-top:8px;color:#2e7d32">eligible=true</p>';
      showLabSummary('pass', '资格检查通过 — 可以发起主动消息');
    } else {
      html += '<p style="margin-top:8px;color:#c62828">eligible=false — ' + esc(r.final_reason || '') + '</p>';
      showLabSummary('fail', '资格检查未通过: ' + (r.final_reason || '未知原因'));
    }
    eligResult.innerHTML = html;
  }

  // ══════════════════════════════════════════════════════════════
  // Impulse candidates
  // ══════════════════════════════════════════════════════════════

  btnImpulses.addEventListener('click', function () {
    resetPanels();
    setButtonLoading(btnImpulses, true);
    apiPost('/admin/proactive-test/impulses', buildParams()).then(function (r) {
      renderImpulses(r);
      impulsePanel.style.display = '';
    }).catch(function (e) {
      showLabSummary('fail', '候选生成失败: ' + e.message);
    }).finally(function () {
      setButtonLoading(btnImpulses, false);
    });
  });

  function renderImpulses(r) {
    var cands = r.candidates || [];
    var sel = r.selected;
    var html = '<p style="font-size:13px;color:#888;margin-bottom:8px">elegible=' + (r.eligible ? 'true' : 'false') + ', 共 ' + cands.length + ' 个候选</p>';
    for (var i = 0; i < cands.length; i++) {
      var c = cands[i];
      var isSel = sel && c.type === sel.impulse_type;
      html += '<div class="candidate-card' + (isSel ? ' selected' : '') + '">';
      html += '<span class="cand-type">' + esc(c.type) + '</span>';
      if (c.score !== null && c.score !== undefined) html += ' <span style="color:#888">score ' + c.score.toFixed(2) + '</span>';
      if (c.selected) html += ' <span style="color:#07c160">SELECTED</span>';
      if (c.reason) html += '<div class="cand-meta">' + esc(c.reason) + '</div>';
      html += '</div>';
    }
    if (cands.length === 0) html += '<p style="color:#888">无候选</p>';
    if (r.selection_reason) html += '<p style="color:#c62828;margin-top:8px">未选中原因: ' + esc(r.selection_reason) + '</p>';

    // Show filtering info
    if (r.filtering_dedup) {
      html += '<details style="margin-top:8px;font-size:12px"><summary>过滤详情</summary>';
      html += '<p>dedup: ' + JSON.stringify(r.filtering_dedup) + '</p>';
      html += '<p>expire: ' + JSON.stringify(r.filtering_expire) + '</p>';
      html += '<p>score: ' + JSON.stringify(r.filtering_score) + '</p>';
      html += '<p>threshold: ' + JSON.stringify(r.filtering_threshold) + '</p>';
      html += '</details>';
    }

    impulseResult.innerHTML = html;
    if (cands.length > 0 && sel) {
      showLabSummary('pass', '选中 ' + sel.impulse_type + ' 候选');
    } else {
      showLabSummary('skip', '无候选被选中');
    }
  }

  // ══════════════════════════════════════════════════════════════
  // Run (dry-run / real-send)
  // ══════════════════════════════════════════════════════════════

  btnDryRun.addEventListener('click', function () { runTest('dry_run', btnDryRun); });
  btnRealSend.addEventListener('click', function () { confirmRealSend(); });

  function confirmRealSend() {
    if (!confirmOverlay) {
      confirmOverlay = document.createElement('div');
      confirmOverlay.className = 'overlay';
      confirmOverlay.innerHTML = '<div class="overlay-card"><p style="color:#c62828;font-weight:600">确认发送真实微信消息？</p><p>此操作将调用真实微信发送接口，用户将收到一条主动消息。</p><div class="btn-row"><button id="confirmYes" class="btn btn-danger">确认发送</button><button id="confirmNo" class="btn">取消</button></div></div>';
      document.body.appendChild(confirmOverlay);
      $('#confirmNo').addEventListener('click', function () { confirmOverlay.classList.remove('show'); });
      $('#confirmYes').addEventListener('click', function () {
        confirmOverlay.classList.remove('show');
        runTest('real_send', btnRealSend);
      });
    }
    confirmOverlay.classList.add('show');
  }

  function runTest(mode, button) {
    resetPanels();
    setButtonLoading(button, true);
    var params = buildParams();
    params.mode = mode;
    apiPost('/admin/proactive-test/run', params).then(function (report) {
      lastReport = report;
      renderReport(report);
      btnCopyReport.style.display = '';
      loadRuns();
    }).catch(function (e) {
      showLabSummary('fail', '执行失败: ' + e.message);
    }).finally(function () {
      setButtonLoading(button, false);
    });
  }

  function renderReport(report) {
    var cls = _summaryClass(report);
    showLabSummary(cls, report.summary);

    // ── Layer 2: Core result cards ──────────────────────────────
    renderResultCards(report);

    // ── Layer 3: Generated messages ─────────────────────────────
    renderMessages(report);

    // ── Layer 4: Explanation (precheck + execution split) ───────
    renderExplain(report);

    // ── Layer 5: Engineering details (folded) ───────────────────
    renderDetails(report);

    // ── Layer 6: Suggestions ────────────────────────────────────
    renderSuggestions(report);

    // ── History consistency (separate section) ──────────────────
    renderHistoryConsistency(report);
  }

  function _summaryClass(report) {
    var ts = String(report.terminal_state || '');
    if (report.execution_skip_reason) return 'skip';
    if (ts.indexOf('REPLIED') >= 0) return 'pass';
    if (ts.indexOf('FAILED') >= 0 || ts.indexOf('BLOCKED') >= 0 || ts.indexOf('ERROR') >= 0) return 'fail';
    if (report.simulated_delivered_count > 0 || report.real_delivered_count > 0) return 'pass';
    return 'info';
  }

  // ── Layer 2: Core result cards ────────────────────────────────

  function renderResultCards(report) {
    var eligOk = report.precheck_passed;
    var eligFailed = report.precheck_eligibility && !report.precheck_eligibility.eligible;
    var guardBlocked = _chainGuardBlocked(report.decision_chain);
    var guardExecuted = report.guard_summary ? report.guard_summary.executed : _chainGuardExecuted(report.decision_chain);
    var guardOk = guardExecuted && !guardBlocked;
    var guardLabel = report.mode === 'dry_run' && !guardExecuted ? 'dry-run 已模拟'
                   : !guardExecuted ? '未执行'
                   : guardBlocked ? '拦截'
                   : '通过';
    var impulseLabel = report.actual_impulse_type || (report.selected_impulse || {}).impulse_type || '无';
    var msgTexts = report.generated_messages || [];
    var msgCount = msgTexts.length || report.simulated_delivered_count || report.real_delivered_count || 0;

    var sendLabel, sendClass;
    if (report.mode === 'dry_run') {
      sendLabel = 'dry-run 未真实发送';
      sendClass = 'rcard-yellow';
    } else if (report.real_send_attempted) {
      if (report.real_delivered_count > 0) {
        sendLabel = '真实发送成功（' + report.real_delivered_count + ' 条）';
        sendClass = 'rcard-pass';
      } else {
        sendLabel = '发送失败（0 条送达）';
        sendClass = 'rcard-fail';
      }
    } else {
      sendLabel = '未执行';
      sendClass = 'rcard-gray';
    }

    var hwLabel, hwClass;
    if (report.mode === 'dry_run') {
      hwLabel = '未写入（dry-run）';
      hwClass = 'rcard-yellow';
    } else if (report.history_written_count > 0) {
      hwLabel = '已写入 ' + report.history_written_count + ' 条';
      hwClass = 'rcard-pass';
    } else {
      hwLabel = '未写入';
      if (report.history_write && report.history_write.reason) {
        hwLabel += '（' + report.history_write.reason + '）';
      }
      hwClass = 'rcard-gray';
    }

    var cardsHtml = '<div class="result-cards">'
      + '<div class="rcard ' + (eligOk ? 'rcard-pass' : (eligFailed ? 'rcard-fail' : 'rcard-gray')) + '"><div class="rcard-label">资格检查</div><div class="rcard-value">' + (eligOk ? '通过' : (eligFailed ? '失败' : '—')) + '</div></div>'
      + '<div class="rcard ' + (impulseLabel !== '无' ? 'rcard-pass' : 'rcard-gray') + '"><div class="rcard-label">主动理由</div><div class="rcard-value">' + esc(impulseLabel) + '</div></div>'
      + '<div class="rcard ' + (msgCount > 0 ? 'rcard-pass' : 'rcard-gray') + '"><div class="rcard-label">生成消息</div><div class="rcard-value">' + msgCount + ' 条</div></div>'
      + '<div class="rcard ' + (guardOk ? 'rcard-pass' : (guardBlocked ? 'rcard-fail' : 'rcard-gray')) + '"><div class="rcard-label">Guard</div><div class="rcard-value">' + esc(guardLabel) + '</div></div>'
      + '<div class="rcard ' + sendClass + '"><div class="rcard-label">发送</div><div class="rcard-value">' + esc(sendLabel) + '</div></div>'
      + '<div class="rcard ' + hwClass + '"><div class="rcard-label">历史写入</div><div class="rcard-value">' + esc(hwLabel) + '</div></div>'
      + '</div>';

    var dest = $('#resultCards') || document.createElement('div');
    dest.innerHTML = cardsHtml;
    var panel = $('section[id="resultCardsPanel"]');
    if (panel) panel.style.display = '';
  }

  // ── Layer 3: Generated messages ───────────────────────────────

  function renderMessages(report) {
    var msgs = report.generated_messages || [];
    var html = '';
    if (msgs.length > 0) {
      html += '<p style="font-size:12px;color:#888;margin-bottom:8px">以下文案是模型实际生成的微信消息（' + (report.mode === 'dry_run' ? 'dry-run 模拟送达' : 'real-send 确认送达') + '）：</p>';
      for (var i = 0; i < msgs.length; i++) {
        html += '<div class="msg-bubble">' + esc(msgs[i]) + '</div>';
      }
    } else if (report.execution_skip_reason) {
      html += '<p class="dim">未生成消息（执行被跳过：' + esc(report.execution_skip_reason) + '）</p>';
    } else if (report.terminal_state) {
      html += '<p class="dim">未生成消息（模型调用了 skip_proactive 或 done(status=no_reply)，终端 ' + tsLabel(report.terminal_state) + '）</p>';
    } else {
      html += '<p class="dim">未生成消息（未执行 LLM 调用）</p>';
    }
    var dest = $('#messagesResult') || document.createElement('div');
    dest.innerHTML = html;
    var panel = $('section[id="messagesPanel"]');
    if (panel) panel.style.display = '';
  }

  // ── Layer 4: Explanation (precheck + execution split) ─────────

  function renderExplain(report) {
    var parts = [];
    parts.push('<div class="explain-section">');
    parts.push('<h3>预检查结果</h3>');
    parts.push(renderPrecheck(report));
    parts.push('<h3>正式执行结果</h3>');
    parts.push(renderExecution(report));
    parts.push('</div>');

    var dest = $('#explainResult') || document.createElement('div');
    dest.innerHTML = parts.join('');
    var panel = $('section[id="explainPanel"]');
    if (panel) panel.style.display = '';
  }

  function renderPrecheck(report) {
    var html = '<div class="explain-precheck">';
    var elig = report.precheck_eligibility;

    if (elig && elig.checks) {
      for (var i = 0; i < elig.checks.length; i++) {
        var c = elig.checks[i];
        var icon = c.passed ? '✓' : '✗';
        var cls = c.passed ? 'c-pass' : 'c-fail';
        html += '<div class="check-row"><span class="check-icon ' + cls + '">' + icon + '</span><span>' + esc(c.name) + '</span>';
        if (!c.passed && c.reason) html += '<span class="check-reason"> — ' + esc(reasonLabel(c.reason)) + '</span>';
        html += '</div>';
      }
      // Show impulse result
      if (report.precheck_selected_impulse) {
        var si = report.precheck_selected_impulse;
        html += '<div class="check-row"><span class="check-icon c-pass">✓</span><span>主动理由: <strong>' + esc(si.impulse_type || '') + '</strong></span></div>';
      } else if (elig.eligible) {
        html += '<div class="check-row"><span class="check-icon c-fail">✗</span><span>主动理由: 无候选被选中</span></div>';
        if (report.candidates && report.candidates.length > 0) {
          html += '<div class="check-detail">候选: ' + report.candidates.map(function(c){return c.type}).join(', ') + '</div>';
        }
      }
    } else if (report.execution_skip_reason) {
      html += '<p class="exec-skip">执行被跳过：' + esc(reasonLabel(report.execution_skip_reason)) + '</p>';
    }

    html += '</div>';
    return html;
  }

  function renderExecution(report) {
    var html = '<div class="explain-execution">';
    if (report.execution_skip_reason) {
      html += '<p class="exec-skip">执行被跳过：' + esc(reasonLabel(report.execution_skip_reason)) + '</p>';
    }
    if (report.terminal_state) {
      var tsClean = String(report.terminal_state).replace('TurnState.', '');
      var extra = '';
      if (tsClean === 'end_cancelled' || tsClean === 'END_CANCELLED') {
        extra = ' — 本轮被更新的用户消息打断。已发送的消息保留，未发送的文本丢弃。';
      }
      html += '<p>终端状态: <strong>' + tsLabel(report.terminal_state) + '</strong>' + extra + '</p>';
    }
    if (report.execution_result && report.execution_result.delivered_count !== undefined) {
      html += '<p>送达条数: ' + report.execution_result.delivered_count + '</p>';
    }
    if (report.turn_id) {
      html += '<p>turn_id: <code>' + esc(report.turn_id) + '</code></p>';
    }
    html += '</div>';
    return html;
  }

  // ── Layer 5: Engineering details (folded) ─────────────────────

  function renderDetails(report) {
    var parts = [];

    // Decision chain (folded by default)
    var chain = report.decision_chain || [];
    if (chain.length > 0) {
      var ch = '<details class="details-fold"><summary>决策链（工程详情）</summary>';
      for (var i = 0; i < chain.length; i++) {
        var s = chain[i];
        var sc = s.status === 'pass' || s.status === 'ok' || s.status === 'done' || s.status === 'written' ? 'st-green' :
                 s.status === 'fail' || s.status === 'block' || s.status === 'empty' ? 'st-red' :
                 s.status === 'simulated' || s.status === 'skip' || s.status === 'not_written' || s.status === 'skipped' ? 'st-yellow' : 'st-gray';
        ch += '<div class="chain-step">';
        ch += '<span class="stage-name">' + stageLabel(s.stage) + '</span>';
        ch += '<span class="stage-status ' + sc + '">' + stageStatusLabel(s.status) + '</span>';
        ch += '<span class="stage-reason">' + esc(reasonLabel(s.reason)) + '</span>';
        if (s.raw && Object.keys(s.raw).length > 0) {
          ch += '<span class="stage-raw" onclick="this.classList.toggle(\'open\')">raw <pre>' + esc(JSON.stringify(s.raw, null, 2)) + '</pre></span>';
        }
        ch += '</div>';
      }
      ch += '</details>';
      parts.push(ch);
    }

    // Candidates detail (folded)
    if (report.candidates && report.candidates.length > 0) {
      var cd = '<details class="details-fold"><summary>主动理由候选详情（系统为什么想主动发）</summary><pre style="font-size:11px">' + esc(JSON.stringify(report.candidates, null, 2)) + '</pre></details>';
      parts.push(cd);
    }

    // Full raw JSON (folded)
    parts.push('<details class="details-fold"><summary>完整 Raw JSON</summary><pre style="font-size:11px;max-height:400px;overflow:auto">' + esc(JSON.stringify(report, null, 2)) + '</pre></details>');

    var dest = $('#detailsResult') || document.createElement('div');
    dest.innerHTML = parts.join('<br>');
    var panel = $('section[id="detailsPanel"]');
    if (panel) panel.style.display = '';
  }

  // ── Layer 6: Suggestions ──────────────────────────────────────

  function renderSuggestions(report) {
    var suggestions = report.next_step_suggestions || [];
    // Frontend fallback: old runs without backend-generated suggestions
    if (suggestions.length === 0) {
      suggestions = _localSuggestions(report);
    }
    if (suggestions.length === 0) return;
    var html = '<div class="suggestion-box"><h3>下一步建议</h3><ul>';
    for (var i = 0; i < suggestions.length; i++) {
      html += '<li>' + esc(suggestions[i]) + '</li>';
    }
    html += '</ul></div>';
    var dest = $('#suggestionsContent') || document.createElement('div');
    dest.innerHTML = html;
    var panel = $('section[id="suggestionsPanel"]');
    if (panel) panel.style.display = '';
  }

  function _localSuggestions(report) {
    var s = [];
    var elig = report.precheck_eligibility;
    if (elig && elig.checks) {
      for (var i = 0; i < elig.checks.length; i++) {
        var c = elig.checks[i];
        if (c.name === 'features.proactive' && !c.passed) s.push('主动消息功能未开启：config.yaml 中 features.proactive 设为 true');
        if (c.name === 'user_silent_window' && !c.passed) s.push('用户静默窗口未达标：填写 simulate_user_silent_seconds=28800 模拟长时间静默');
        if (c.name === 'sleep_hours' && !c.passed) s.push('处于睡眠时段：设置 debug_now 为白天时间');
        if (c.name === 'bridge_online' && !c.passed) s.push('微信 bridge 离线：先部署/启动微信桥接');
      }
    }
    if (elig && elig.eligible && !report.selected_impulse) {
      s.push('没有选中主动理由：试试 forced_impulse_type=PRESENCE');
    }
    if (_chainGuardBlocked(report.decision_chain || [])) {
      s.push('文案被 guard 拦截：展开工程详情查看原因');
    }
    if (report.mode === 'dry_run' && (report.generated_messages || []).length > 0) {
      s.push('Dry-run 成功：检查上方文案后可执行 real-send');
    }
    return s;
  }

  // ── History consistency (separate section) ────────────────────

  function renderHistoryConsistency(report) {
    var parts = ['<h3>发送与历史一致性</h3><table class="checks-table"><tbody>'];
    parts.push(row('执行模式', report.mode === 'dry_run' ? 'dry-run（未真实发送）' : 'real-send（已真实发送）'));
    parts.push(row('是否调用真实微信', report.real_send_attempted ? '是' : '否（dry-run）'));
    parts.push(row('发送步骤是否执行', report.send && report.send.send_step_executed ? '是' : '否'));
    parts.push(row('真实送达条数', report.real_delivered_count || '—'));
    parts.push(row('模拟送达条数', report.simulated_delivered_count || '—'));
    parts.push(row('写入历史条数', report.history_written_count || (report.mode === 'dry_run' ? '未写入（dry-run）' : '0'));
    if (report.history_write && report.history_write.reason) parts.push(row('未写入原因', esc(report.history_write.reason)));
    if (report.error_code) parts.push(row('错误码', report.error_code));
    if (report.error_message) parts.push(row('错误信息', esc(report.error_message)));
    parts.push('</tbody></table>');

    var dest = $('#historyResult') || document.createElement('div');
    dest.innerHTML = parts.join('');
    var panel = $('section[id="historyPanel"]');
    if (panel) panel.style.display = '';
  }

  function row(label, value) {
    return '<tr><td style="color:#888;width:180px">' + esc(String(label)) + '</td><td>' + esc(String(value)) + '</td></tr>';
  }

  // ══════════════════════════════════════════════════════════════
  // Recent runs
  // ══════════════════════════════════════════════════════════════

  function loadRuns() {
    apiGet('/admin/proactive-test/runs?limit=20').then(function (r) {
      if (!r.ok) return;
      var items = r.items || [];
      if (items.length === 0) {
        runsResult.innerHTML = '<p style="color:#888">暂无记录</p>';
        return;
      }
      var html = '';
      for (var i = 0; i < items.length; i++) {
        var item = items[i];
        var modeLabel = item.mode === 'dry_run' ? '[dry]' : '[real]';
        var cls = item.send_simulated ? 'st-yellow' : 'st-green';
        html += '<div class="run-item" onclick="this.classList.toggle(\'open\')">';
        html += '<div class="run-header"><span>' + modeLabel + ' <span class="' + cls + '">' + esc(item.run_id || '') + '</span></span><span style="color:#888;font-size:12px">' + esc(item.terminal_state || '—') + '</span></div>';
        html += '<div class="run-detail"><pre style="font-size:11px;overflow-x:auto">' + esc(JSON.stringify(item, null, 2)) + '</pre></div>';
        html += '</div>';
      }
      runsResult.innerHTML = html;
    }).catch(function () {});
  }

  // ══════════════════════════════════════════════════════════════
  // Copy report
  // ══════════════════════════════════════════════════════════════

  btnCopyReport.addEventListener('click', function () {
    if (!lastReport) return;
    var text = JSON.stringify(lastReport, null, 2);
    navigator.clipboard.writeText(text).then(function () {
      btnCopyReport.textContent = '已复制';
      setTimeout(function () { btnCopyReport.textContent = '复制调试报告'; }, 2000);
    }).catch(function () {
      // Fallback
      var ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      btnCopyReport.textContent = '已复制';
      setTimeout(function () { btnCopyReport.textContent = '复制调试报告'; }, 2000);
    });
  });

  // ══════════════════════════════════════════════════════════════
  // Helpers
  // ══════════════════════════════════════════════════════════════

  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function resetPanels() {
    eligPanel.style.display = 'none';
    impulsePanel.style.display = 'none';
    chainPanel.style.display = 'none';
    historyPanel.style.display = 'none';
    labSummary.style.display = 'none';
    // New v2.3 panels
    hideIfExists('resultCardsPanel');
    hideIfExists('messagesPanel');
    hideIfExists('explainPanel');
    hideIfExists('detailsPanel');
    hideIfExists('suggestionsPanel');
    lastReport = null;
    btnCopyReport.style.display = 'none';
  }

  function hideIfExists(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  }

  function showLabSummary(cls, text) {
    labSummary.style.display = '';
    labSummary.className = 'summary ' + cls;
    labSummary.textContent = text;
  }

  function setButtonLoading(btn, loading) {
    btn.disabled = loading;
    btn.textContent = loading ? '...' : btn.getAttribute('data-original') || btn.textContent;
    if (!loading) btn.setAttribute('data-original', btn.textContent);
  }

  // Store original button texts
  [btnCheck, btnImpulses, btnDryRun, btnRealSend].forEach(function (btn) {
    btn.setAttribute('data-original', btn.textContent);
  });

  // ══════════════════════════════════════════════════════════════
  // Init
  // ══════════════════════════════════════════════════════════════
  checkAuth();
})();
