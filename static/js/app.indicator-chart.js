/* Prism - indicator-aware K-line chart renderer */
(function() {
  if (!window.App) return;

  function _dateLabel(ts) {
    var d = new Date((ts || 0) * 1000);
    return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
  }

  function _num(v, digits) {
    var n = Number(v);
    return isNaN(n) ? '-' : n.toFixed(digits == null ? 2 : digits);
  }

  function _ma(arr, n) {
    var r = [];
    for (var i = 0; i < arr.length; i++) {
      if (i < n - 1) { r.push(null); continue; }
      var s = 0;
      for (var j = 0; j < n; j++) s += +arr[i - j];
      r.push(+(s / n).toFixed(2));
    }
    return r;
  }

  App.renderKlineChart = function(container, kline) {
    if (typeof echarts === 'undefined') { container.innerHTML = '图表库未加载'; return; }
    if (this._klineChart) { try { this._klineChart.dispose(); } catch (e) {} this._klineChart = null; }
    var chart = echarts.init(container, null, {renderer: 'canvas'});
    this._klineChart = chart;
    this._lastKline = kline;

    var dates = [], ohlc = [], vols = [], closes = [], tsIdx = {}, dateIdx = {};
    for (var i = 0; i < kline.length; i++) {
      var k = kline[i];
      var ts = k.ts || k.timestamp || 0;
      var ds = _dateLabel(ts);
      dates.push(ds);
      dateIdx[ds] = i;
      tsIdx[ts] = i;
      ohlc.push([+k.open, +k.close, +k.low, +k.high]);
      vols.push(+k.volume || 0);
      closes.push(+k.close);
    }

    var ma5 = _ma(closes, 5), ma10 = _ma(closes, 10), ma20 = _ma(closes, 20), ma60 = _ma(closes, 60);
    var P = this._chartPalette();
    var overlays = this._indicatorOverlays || [];
    var markers = this._indicatorMarkers || [];
    var tracks = this._indicatorTracks || [];
    var zones = this._indicatorZones || [];
    var periodName = ({'1d':'日K', '1w':'周K', '1m':'月K'})[this._currentPeriod] || 'K线';
    var legendData = [periodName, 'MA5', 'MA10', 'MA20', 'MA60'];
    var overlayColors = ['#22c55e', '#fbbf24', '#60a5fa', '#f472b6', '#34d399'];
    var overlaySeries = [], overlayAt = {};

    for (var oi = 0; oi < overlays.length; oi++) {
      var ov = overlays[oi], arr = new Array(dates.length).fill(null);
      var pts = ov.points || [];
      for (var pi = 0; pi < pts.length; pi++) {
        var ix = tsIdx[pts[pi].t];
        if (ix == null) continue;
        arr[ix] = pts[pi].v;
        overlayAt[ix] = overlayAt[ix] || [];
        overlayAt[ix].push({name: ov.name, value: pts[pi].v});
      }
      var col = ov.color || overlayColors[oi % overlayColors.length];
      legendData.push(ov.name);
      overlaySeries.push({
        name: ov.name,
        type: 'line',
        data: arr,
        smooth: true,
        showSymbol: false,
        lineStyle: {width: 1.5, color: col},
        z: 6
      });
    }

    var eventByIdx = {}, markData = [];
    function addEvent(ev, fallbackName, fallbackColor) {
      var idx = tsIdx[ev.t];
      if (idx == null) return;
      eventByIdx[idx] = eventByIdx[idx] || [];
      eventByIdx[idx].push(ev);
      markData.push({
        coord: [dates[idx], (+kline[idx].low) * 0.99],
        value: ev.label || '▲',
        itemStyle: {color: ev.color || fallbackColor || P.up},
        tooltip: {formatter: esc(ev.reason || fallbackName || '指标触发')}
      });
    }
    for (var mi = 0; mi < markers.length; mi++) addEvent(markers[mi], markers[mi].name, markers[mi].color);

    var trackNames = [], signalData = [];
    for (var ti = 0; ti < tracks.length; ti++) {
      var tr = tracks[ti] || {};
      var tname = tr.name || '指标信号';
      if (trackNames.indexOf(tname) < 0) trackNames.push(tname);
      var evs = tr.events || [];
      for (var ei = 0; ei < evs.length; ei++) {
        var eidx = tsIdx[evs[ei].t];
        if (eidx == null) continue;
        eventByIdx[eidx] = eventByIdx[eidx] || [];
        eventByIdx[eidx].push(evs[ei]);
        signalData.push([
          dates[eidx],
          tname,
          evs[ei].strength || 60,
          evs[ei].label || '▲',
          evs[ei].reason || '',
          evs[ei].color || tr.color || '#22c55e'
        ]);
      }
    }

    var zoneData = [];
    for (var zi = 0; zi < zones.length; zi++) {
      var zs = tsIdx[zones[zi].start], ze = tsIdx[zones[zi].end];
      if (zs != null && ze != null) zoneData.push([{xAxis: dates[zs]}, {xAxis: dates[ze]}]);
    }
    var hasSignalTrack = signalData.length > 0;
    container.classList.toggle('has-indicator-track', hasSignalTrack);

    function tooltipFormatter(params) {
      params = Array.isArray(params) ? params : [params];
      var axis = params[0] && (params[0].axisValue || params[0].name);
      var idx = dateIdx[axis];
      if (idx == null && params[0]) idx = params[0].dataIndex;
      var row = kline[idx] || {};
      var previousClose = idx > 0 ? Number(kline[idx - 1].close) : Number(row.open || row.close);
      var close = Number(row.close);
      var changePct = previousClose ? (close - previousClose) / previousClose * 100 : 0;
      var amplitude = previousClose ? (Number(row.high) - Number(row.low)) / previousClose * 100 : 0;
      var lines = ['<b>' + esc(axis || '') + '</b>'];
      lines.push('开 ' + _num(row.open) + ' · 高 ' + _num(row.high) + ' · 低 ' + _num(row.low) + ' · 收 ' + _num(row.close));
      lines.push('<span style="color:' + (changePct >= 0 ? P.rise : P.fall) + '">涨跌 ' + (changePct >= 0 ? '+' : '') + _num(changePct) + '%</span> · 振幅 ' + _num(amplitude) + '%');
      lines.push('量 ' + (App._fmtVolume ? App._fmtVolume(row.volume) : _num(row.volume, 0)) + (row.turnover || row.amount ? ' · 额 ' + App._fmtVolume(row.turnover || row.amount) : ''));
      lines.push('MA5 ' + _num(ma5[idx]) + ' · MA10 ' + _num(ma10[idx]) + ' · MA20 ' + _num(ma20[idx]) + ' · MA60 ' + _num(ma60[idx]));
      var ovs = overlayAt[idx] || [];
      for (var oi2 = 0; oi2 < ovs.length; oi2++) lines.push(esc(ovs[oi2].name) + ' ' + _num(ovs[oi2].value));
      var evs2 = eventByIdx[idx] || [];
      for (var ei2 = 0; ei2 < evs2.length; ei2++) {
        lines.push('<span style="color:' + esc(evs2[ei2].color || '#22c55e') + '">' +
          esc(evs2[ei2].label || '▲') + ' ' + esc(evs2[ei2].reason || evs2[ei2].name || '指标触发') +
          ' · 强度 ' + esc(String(evs2[ei2].strength || '-')) + '</span>');
      }
      return lines.join('<br>');
    }

    var candleSeries = {
      name: periodName,
      type: 'candlestick',
      data: ohlc,
      itemStyle: {color: P.rise, color0: P.fall, borderColor: P.rise, borderColor0: P.fall}
    };
    if (kline.length) {
      var highIndex = 0, lowIndex = 0;
      for (var hi = 1; hi < kline.length; hi++) {
        if (Number(kline[hi].high) > Number(kline[highIndex].high)) highIndex = hi;
        if (Number(kline[hi].low) < Number(kline[lowIndex].low)) lowIndex = hi;
      }
      markData.push({coord:[dates[highIndex], Number(kline[highIndex].high)], value:'高 ' + _num(kline[highIndex].high), symbol:'circle', symbolSize:5, label:{position:'top', color:P.txt}});
      markData.push({coord:[dates[lowIndex], Number(kline[lowIndex].low)], value:'低 ' + _num(kline[lowIndex].low), symbol:'circle', symbolSize:5, label:{position:'bottom', color:P.txt}});
      var latestClose = Number(kline[kline.length - 1].close);
      var priorClose = kline.length > 1 ? Number(kline[kline.length - 2].close) : Number(kline[0].open);
      candleSeries.markLine = {
        symbol:['none','none'], silent:true,
        lineStyle:{type:'dashed', width:1},
        label:{show:true, position:'insideEndTop', color:P.txt, fontSize:9, formatter:function(p){ return p.name + ' ' + _num(p.value); }},
        data:[
          {name:'昨收', yAxis:priorClose, lineStyle:{color:P.cross, opacity:.65}},
          {name:'最新', yAxis:latestClose, lineStyle:{color:latestClose >= priorClose ? P.rise : P.fall, opacity:.8}}
        ]
      };
    }
    if (markData.length) candleSeries.markPoint = {data: markData, symbolSize: 30, label: {fontSize: 11, color: '#fff'}};
    if (zoneData.length) candleSeries.markArea = {silent: true, itemStyle: {color: 'rgba(96,165,250,.10)'}, data: zoneData};

    var grids = hasSignalTrack
      ? [{left: 54, right: 18, top: 28, height: '48%'}, {left: 54, right: 18, top: '65%', height: '11%'}, {left: 54, right: 18, top: '80%', height: '9%'}]
      : [{left: 54, right: 18, top: 28, height: '56%'}, {left: 54, right: 18, top: '74%', height: '15%'}];
    var xAxes = [
      {type: 'category', data: dates, boundaryGap: false, axisLine: {lineStyle: {color: P.axl}}, splitLine: {show: false}, axisLabel: {color: P.txt, fontSize: 9}},
      {type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLine: {show: false}, axisLabel: {show: false}, splitLine: {show: false}}
    ];
    var yAxes = [
      {scale: true, splitLine: {lineStyle: {color: P.grid}}, axisLabel: {color: P.txt, fontSize: 9}},
      {gridIndex: 1, splitNumber: 2, axisLabel: {show: false}, splitLine: {show: false}}
    ];
    var zoomAxis = [0, 1];
    if (hasSignalTrack) {
      xAxes.push({type: 'category', gridIndex: 2, data: dates, boundaryGap: false, axisLine: {show: false}, axisLabel: {show: false}, splitLine: {show: false}});
      yAxes.push({type: 'category', gridIndex: 2, data: trackNames, axisLabel: {color: P.txt, fontSize: 9}, axisLine: {show: false}, splitLine: {show: false}});
      zoomAxis.push(2);
      legendData.push('指标信号');
    }

    var series = [
      candleSeries,
      {name: 'MA5', type: 'line', data: ma5, smooth: true, showSymbol: false, lineStyle: {width: 1.3, color: P.ma1}, z: 5},
      {name: 'MA10', type: 'line', data: ma10, smooth: true, showSymbol: false, lineStyle: {width: 1.2, color: '#60a5fa'}, z: 5},
      {name: 'MA20', type: 'line', data: ma20, smooth: true, showSymbol: false, lineStyle: {width: 1.3, color: P.ma2}, z: 5},
      {name: 'MA60', type: 'line', data: ma60, smooth: true, showSymbol: false, lineStyle: {width: 1.2, color: '#64748b'}, z: 5},
      {name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
        data: vols.map(function(v, i) { return {value: v, itemStyle: {color: closes[i] >= +kline[i].open ? P.rise : P.fall}}; })}
    ].concat(overlaySeries);
    if (hasSignalTrack) {
      series.push({
        name: '指标信号',
        type: 'scatter',
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: signalData,
        symbolSize: function(v) { return Math.max(8, Math.min(18, 6 + (Number(v[2]) || 50) / 10)); },
        itemStyle: {color: function(p) { return p.data[5] || '#22c55e'; }},
        label: {show: true, formatter: function(p) { return p.data[3] || '▲'; }, position: 'top', fontSize: 10, color: P.tipTxt},
        tooltip: {formatter: function(p) { return '<b>' + esc(p.data[0]) + '</b><br>' + esc(p.data[4] || '指标触发') + '<br>强度 ' + esc(String(p.data[2] || '-')); }}
      });
    }

    chart.setOption({
      animation: false,
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        axisPointer: {type: 'cross', lineStyle: {color: P.cross, type: 'dashed'}},
        backgroundColor: P.tipBg,
        borderColor: P.tipBd,
        textStyle: {color: P.tipTxt, fontSize: 11},
        formatter: tooltipFormatter
      },
      axisPointer: {link: [{xAxisIndex: 'all'}]},
      legend: {data: legendData, selected:this._klineMaVisible || {}, top: 4, textStyle: {color: P.txt, fontSize: 10}, itemWidth: 12, itemHeight: 8},
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      dataZoom: [
        {type: 'inside', xAxisIndex: zoomAxis, start: 0, end: 100},
        {show: true, type: 'slider', xAxisIndex: zoomAxis, bottom: 2, height: 14, start: 0, end: 100, borderColor: P.zoomBd, fillerColor: P.zoomFill}
      ],
      series: series
    });

    var self = this;
    if (!this._klineResizeBound) {
      this._klineResizeBound = true;
      window.addEventListener('resize', function() { if (self._klineChart) { try { self._klineChart.resize(); } catch (e) {} } });
    }
  };
})();
