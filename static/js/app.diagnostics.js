/* Prism - privacy-aware client anomaly detection */
Object.assign(App, {
  _diagnosticRecent: {},
  _diagnosticTimer: null,

  _initDiagnostics: function() {
    if (this._diagnosticTimer) return;
    var self = this;
    window.addEventListener('error', function(event) {
      var source = '';
      try { source = event.filename ? new URL(event.filename, location.href).pathname : ''; } catch (e) {}
      self._captureDiagnostic('js_error', event.message || 'JavaScript error', {
        severity:'error', component:source || 'window', stack:event.error && event.error.stack || '',
        context:{source:source, line:event.lineno || 0, column:event.colno || 0}
      });
    });
    window.addEventListener('unhandledrejection', function(event) {
      var reason = event.reason || {};
      self._captureDiagnostic('promise_rejection', reason.message || 'Unhandled promise rejection', {
        severity:'error', component:'promise', stack:reason.stack || ''
      });
    });
    setTimeout(function() {
      self._scanUiDiagnostics();
      self._diagnosticTimer = setInterval(function() { self._scanUiDiagnostics(); }, 30000);
    }, 12000);
  },

  _diagnosticVersion: function() {
    var tag = document.getElementById('versionTag');
    return (this._releaseData && this._releaseData.version) || (tag && tag.textContent) || '';
  },

  _captureDiagnostic: function(kind, message, options) {
    options = options || {};
    if (!this.user || !kind || !message) return;
    var key = [kind, options.component || '', String(message).slice(0, 180), this._diagnosticVersion()].join('|');
    var now = Date.now();
    if (now - Number(this._diagnosticRecent[key] || 0) < 10 * 60 * 1000) return;
    this._diagnosticRecent[key] = now;
    var context = Object.assign({
      mode:this._mode || 'chat',
      route:location.pathname,
      viewport:window.innerWidth + 'x' + window.innerHeight
    }, options.context || {});
    var csrf = this._getCookie('csrf_token');
    fetch('/api/diagnostics/client', {
      method:'POST',
      headers:{
        'Content-Type':'application/json',
        'X-Requested-With':'XMLHttpRequest',
        'X-CSRF-Token':csrf || ''
      },
      body:JSON.stringify({
        kind:kind,
        severity:options.severity || 'error',
        component:options.component || '',
        page:location.pathname,
        message:String(message).slice(0, 2000),
        stack:String(options.stack || '').slice(0, 8000),
        version:this._diagnosticVersion(),
        context:context
      })
    }).catch(function() {});
  },

  _scanUiDiagnostics: function() {
    var self = this;
    var artifact = null;
    try {
      var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
      var node;
      while ((node = walker.nextNode())) {
        if (!node.nodeValue || node.nodeValue.indexOf('[object Object]') < 0) continue;
        var parent = node.parentElement;
        if (!parent || parent.closest('script,style,pre,code,.release-card') || !parent.offsetParent) continue;
        artifact = parent;
        break;
      }
    } catch (e) {}
    if (artifact) {
      this._captureDiagnostic('render_object_artifact', 'Visible [object Object] placeholder detected', {
        severity:'error', component:artifact.id || artifact.className || 'content',
        context:{selector:artifact.id ? '#' + artifact.id : String(artifact.className || '').slice(0, 120)}
      });
    }

    var chart = document.getElementById('marketKlineChart');
    if (chart && chart.offsetParent && !chart.querySelector('canvas,svg')) {
      this._captureDiagnostic('chart_blank', 'K-line container is visible but has no rendered canvas', {
        severity:'warning', component:'marketKlineChart', context:{selector:'#marketKlineChart'}
      });
    }

    var panels = ['analysisPanelInner','vibePanelInner','reportsPanelInner','industryPanelInner','adminPanelInner'];
    for (var i = 0; i < panels.length; i++) {
      var panel = document.getElementById(panels[i]);
      if (!panel || !panel.offsetParent) continue;
      if (!panel.textContent.trim() && !panel.querySelector('canvas,svg,iframe')) {
        self._captureDiagnostic('blank_panel', 'Active application panel rendered blank', {
          severity:'warning', component:panels[i], context:{selector:'#' + panels[i]}
        });
      }
    }
  }
});
