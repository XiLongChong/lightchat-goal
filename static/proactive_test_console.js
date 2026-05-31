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
  // Eligibility check
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
    // Summary
    var cls = 'info';
    if (report.terminal_state && report.terminal_state.indexOf('REPLIED') >= 0) cls = 'pass';
    else if (report.terminal_state && (report.terminal_state.indexOf('FAILED') >= 0 || report.terminal_state.indexOf('BLOCKED') >= 0)) cls = 'fail';
    else if (report.execution_skip_reason) cls = 'skip';
    showLabSummary(cls, report.summary);

    // Precheck vs execution
    var precheckHtml = '';
    if (report.precheck_eligibility) {
      precheckHtml += '<h3 style="font-size:13px;margin-bottom:4px">Precheck (预检)</h3>';
      precheckHtml += '<p style="font-size:13px">eligibility: <span class="' + (report.precheck_passed ? 'pass-badge' : 'fail-badge') + '">' + (report.precheck_passed ? 'passed' : 'failed') + '</span></p>';
      if (report.precheck_selected_impulse) {
        precheckHtml += '<p style="font-size:13px">selected impulse: ' + esc(report.precheck_selected_impulse.impulse_type || '—') + '</p>';
      }
    }
    if (report.execution_result) {
      precheckHtml += '<h3 style="font-size:13px;margin:8px 0 4px">Execution (执行结果)</h3>';
      precheckHtml += '<p style="font-size:13px">terminal: ' + esc(report.terminal_state || '—') + '</p>';
      precheckHtml += '<p style="font-size:13px">turn_id: ' + esc(report.turn_id || '—') + '</p>';
    }
    if (report.execution_skip_reason) {
      precheckHtml += '<p style="color:#f57f17;font-size:13px">skip reason: ' + esc(report.execution_skip_reason) + '</p>';
    }
    if (precheckHtml) {
      historyPanel.style.display = '';
      historyResult.innerHTML = precheckHtml;
    }

    // History consistency
    var hc = '<h3 style="font-size:13px;margin-bottom:4px">发送与历史一致性</h3>';
    hc += '<table class="checks-table"><tbody>';
    hc += row('real_send_attempted', report.real_send_attempted ? 'YES' : 'NO');
    hc += row('send_simulated', report.send_simulated ? 'YES' : 'NO');
    hc += row('real_delivered_count', report.real_delivered_count);
    hc += row('simulated_delivered_count', report.simulated_delivered_count);
    hc += row('history_written_count', report.history_written_count);
    if (report.history_write && report.history_write.reason) hc += row('未写入原因', report.history_write.reason);
    if (report.error_code) hc += row('error_code', report.error_code);
    if (report.error_message) hc += row('error_message', report.error_message);
    hc += '</tbody></table>';
    historyResult.innerHTML = (historyResult.innerHTML || '') + hc;

    // Decision chain
    var chain = report.decision_chain || [];
    if (chain.length > 0) {
      chainPanel.style.display = '';
      var ch = '';
      for (var i = 0; i < chain.length; i++) {
        var s = chain[i];
        var sc = s.status === 'pass' || s.status === 'ok' || s.status === 'done' || s.status === 'written' ? 'st-green' :
                 s.status === 'fail' || s.status === 'block' || s.status === 'empty' ? 'st-red' :
                 s.status === 'simulated' || s.status === 'skip' || s.status === 'not_written' ? 'st-yellow' : 'st-gray';
        ch += '<div class="chain-step">';
        ch += '<span class="stage-name">' + esc(s.stage) + '</span>';
        ch += '<span class="stage-status ' + sc + '">' + esc(s.status) + '</span>';
        ch += '<span class="stage-reason">' + esc(s.reason || '') + '</span>';
        if (s.raw && Object.keys(s.raw).length > 0) {
          ch += '<span class="stage-raw" onclick="this.classList.toggle(\'open\')">raw <pre>' + esc(JSON.stringify(s.raw, null, 2)) + '</pre></span>';
        }
        ch += '</div>';
      }
      chainResult.innerHTML = ch;
    }
  }

  function row(label, value) {
    return '<tr><td style="color:#888">' + esc(String(label)) + '</td><td>' + esc(String(value)) + '</td></tr>';
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
    lastReport = null;
    btnCopyReport.style.display = 'none';
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
