// -- Auth token helper (shared across all IIFEs) --
var _tk=new URLSearchParams(location.search).get('token')||'';
function apiUrl(p){return p+(_tk?(p.indexOf('?')>=0?'&':'?')+'token='+encodeURIComponent(_tk):'');}

var _tabHidden=document.hidden||false;
var _activeDashTab='portfolio';
document.addEventListener('visibilitychange',function(){_tabHidden=document.hidden;});
function isTabActive(tabName){return !_tabHidden && _activeDashTab===tabName;}
var _initialLoadDone=false;

(function(){
  // -- Tab switching (lazy-load) --
  var _oppsLoaded=false,_watchLoaded=false;
  document.querySelectorAll('.tab').forEach(function(tab){
    tab.addEventListener('click', function(){
      document.querySelectorAll('.tab').forEach(function(t){ t.classList.remove('active'); t.setAttribute('aria-selected','false'); });
      document.querySelectorAll('.tab-content').forEach(function(c){ c.classList.remove('active'); });
      tab.classList.add('active');
      tab.setAttribute('aria-selected','true');
      document.getElementById('tab-'+tab.dataset.tab).classList.add('active');
      _activeDashTab=tab.dataset.tab;
      localStorage.setItem('activeTab', tab.dataset.tab);
      // Start/stop polling based on active tab
      if(tab.dataset.tab==='opportunities'){if(!_oppsLoaded){loadOpportunities();_oppsLoaded=true;}startOppsPolling();}else{stopOppsPolling();}
      if(tab.dataset.tab==='watchlist'){if(!_watchLoaded){loadWatchlist();_watchLoaded=true;}startWatchPolling();}else{stopWatchPolling();}
      // strategy tab — no lazy-load needed
    });
  });
  // Restore saved tab on load
  var savedTab = localStorage.getItem('activeTab');
  if(savedTab){
    _activeDashTab=savedTab;
    var t = document.querySelector('.tab[data-tab="'+savedTab+'"]');
    if(t) t.click();
  }

  // -- Balance toggle --
  var cb=document.getElementById('bal-cb');
  // Migration: rename localStorage key
  if(localStorage.getItem('polybond-bal')!==null){
    localStorage.setItem('polybonds-bal',localStorage.getItem('polybond-bal'));
    localStorage.removeItem('polybond-bal');
  }
  var saved=localStorage.getItem('polybonds-bal');
  if(saved==='shown'){cb.checked=true;document.body.classList.remove('bal-hidden');}
  cb.addEventListener('change',function(){
    document.body.classList.toggle('bal-hidden',!cb.checked);
    localStorage.setItem('polybonds-bal',cb.checked?'shown':'hidden');
  });

  // -- ET clock --
  function updateClock(){
    var now=new Date();
    var et=now.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
    document.getElementById('header-clock').textContent=et+' ET';
  }
  updateClock();setInterval(updateClock,1000);

  // -- Copy toast --
  var toastTimer=null;
  function showCopyToast(msg,isError){
    var t=document.getElementById('copy-toast');
    t.textContent=msg||'Copied to clipboard';
    if(isError){t.style.borderColor='var(--red, #c0392b)';t.style.color='var(--red, #c0392b)';}
    else{t.style.borderColor='';t.style.color='';}
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer=setTimeout(function(){t.classList.remove('show');},1500);
  }

  // -- Wallet copy + modal --
  var walletAddr=window.DASHBOARD_CONFIG.walletAddress;
  var copyBtn=document.getElementById('wallet-copy-btn');
  if(copyBtn){
    copyBtn.addEventListener('click',function(){
      if(navigator.clipboard){
        navigator.clipboard.writeText(walletAddr).then(function(){
          showCopyToast('Copied!');
        }).catch(function(){ showCopyToast('Copy failed',true); });
      }else{ showCopyToast('Copy not available (HTTPS required)',true); }
    });
  }
  var qrBtn=document.getElementById('wallet-qr-btn');
  var walletModal=document.getElementById('wallet-modal');
  var walletModalClose=document.getElementById('wallet-modal-close');
  if(qrBtn&&walletModal&&walletModalClose){
    function closeWallet(){walletModal.classList.remove('active');}
    qrBtn.addEventListener('click',function(){walletModal.classList.add('active');});
    walletModalClose.addEventListener('click',closeWallet);
    walletModal.addEventListener('click',function(e){if(e.target===walletModal)closeWallet();});
    document.addEventListener('keydown',function(e){if(e.key==='Escape'&&walletModal.classList.contains('active'))closeWallet();});
  }
  var fullAddr=document.getElementById('wallet-full-addr');
  if(fullAddr){
    fullAddr.addEventListener('click',function(){
      if(navigator.clipboard){
        navigator.clipboard.writeText(walletAddr).then(function(){showCopyToast('Address copied!');}).catch(function(){showCopyToast('Copy failed',true);});
      }else{ showCopyToast('Copy not available (HTTPS required)',true); }
    });
  }

  // -- Helpers --
  window.fetchWithTimeout=fetchWithTimeout;
  function fetchWithTimeout(url,opts,ms){
    ms=ms||window.DASHBOARD_CONFIG.fetchTimeoutMs;
    var ctrl=new AbortController();
    var tid=setTimeout(function(){ctrl.abort();},ms);
    opts=opts||{};
    var ext=opts.signal;
    if(ext){ext.addEventListener('abort',function(){ctrl.abort();});}
    opts.signal=ctrl.signal;
    return fetch(url,opts).finally(function(){clearTimeout(tid);});
  }
  function showConfirm(title,msg,okText,okClass,callback){
    var ov=document.getElementById('generic-confirm-overlay');
    var okBtn=document.getElementById('generic-confirm-ok');
    var canBtn=document.getElementById('generic-confirm-cancel');
    document.getElementById('generic-confirm-title').textContent=title;
    document.getElementById('generic-confirm-msg').textContent=msg;
    okBtn.textContent=okText;okBtn.className=okClass||'btn-confirm-on';
    ov.classList.add('active');
    function cleanup(){ov.classList.remove('active');okBtn.onclick=null;canBtn.onclick=null;ov.onclick=null;document.removeEventListener('keydown',escHandler);}
    function escHandler(e){if(e.key==='Escape')cleanup();}
    document.addEventListener('keydown',escHandler);
    canBtn.onclick=cleanup;
    ov.onclick=function(e){if(e.target===ov)cleanup();};
    okBtn.onclick=function(){cleanup();callback();};
  }
  function htmlEscape(s){
    if(!s)return '';
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  function relTime(dateStr){
    if(!dateStr)return '\u2014';
    var d=new Date(dateStr);
    if(isNaN(d.getTime()))return '\u2014';
    var now=new Date();
    var diff=d-now;
    var abs=Math.abs(diff);
    var days=Math.floor(abs/86400000);
    var hours=Math.floor((abs%86400000)/3600000);
    var mins=Math.floor((abs%3600000)/60000);
    if(diff>0){
      if(days>0)return days+'d '+hours+'h left';
      if(hours>0)return hours+'h left';
      if(mins>0)return mins+'m left';
      return '<1m left';
    }else{
      if(days>1)return 'expired';
      if(days>0)return days+'d ago';
      if(hours>0)return hours+'h ago';
      if(mins>0)return mins+'m ago';
      return '<1m ago';
    }
  }
  function updateFooterTimestamp(){
    var frEl=document.getElementById('footer-rendered');
    if(frEl)frEl.textContent='Data as of '+new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',timeZone:'America/New_York',hour12:false})+' ET';
  }
  function pnlClass(v){return v>0?'pnl-positive':v<0?'pnl-negative':'';}
  function sideClass(o){return o==='No'?'side-sell':'side-buy';}
  function truncate(s,n){return s&&s.length>n?s.substring(0,n)+'...':s||'\u2014';}
  function fmtMoney(v){
    var n=Number(v);
    if(isNaN(n))return '\u2014';
    var sign=n<0?'-':'';
    return sign+'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  }
  function N(v){return v||0;}
  function polyLink(r,text){
    if(!r.slug)return text;
    var url='https://polymarket.com/event/'+(r.event_slug||r.slug)+(r.event_slug&&r.event_slug!==r.slug?'/'+r.slug:'');
    return '<a href="'+url+'" target="_blank" rel="noopener noreferrer" style="color:var(--accent);text-decoration:none">'+text+'</a>';
  }
  function sortData(data,key,asc){
    data.sort(function(a,b){
      var va=a[key],vb=b[key];
      if(va==null&&vb==null)return 0;
      if(va==null)return asc?-1:1;
      if(vb==null)return asc?1:-1;
      if(typeof va==='string')return asc?va.localeCompare(vb):vb.localeCompare(va);
      return asc?(va-vb):(vb-va);
    });
  }
  function errorHtml(msg,retryFn){
    return '<div class="error-state">'+htmlEscape(msg||'Failed to load data')+'<br><button class="retry-btn" data-retry="'+htmlEscape(retryFn)+'">Retry</button></div>';
  }
  // Delegated click handler for retry buttons (XSS-safe: no inline onclick)
  document.addEventListener('click',function(e){
    var btn=e.target.closest('.retry-btn[data-retry]');
    if(!btn)return;
    var fn=btn.dataset.retry;
    if(fn&&typeof window[fn]==='function')window[fn]();
  });

  // -- Equity chart (update pattern, no destroy/recreate) --
  var equityChart=null;
  var yieldChart=null;
  var chartGradient=null;
  var _chartLoading=false;
  var _chartAbort=null;
  var _chartDays=7;
  var _activeChartTab='equity';
  function setChartRange(days,btn){
    _chartDays=days;
    document.querySelectorAll('.range-day-btn').forEach(function(b){b.classList.remove('active');b.disabled=false;});
    btn.classList.add('active');
    btn.disabled=true;
    if(_chartAbort){_chartAbort.abort();_chartAbort=null;}
    _chartLoading=false;
    loadEquityChart();
  }
  window.setChartRange=setChartRange;
  function switchChartTab(tab){
    _activeChartTab=tab;
    document.getElementById('chart-tab-equity').classList.toggle('active',tab==='equity');
    document.getElementById('chart-tab-yield').classList.toggle('active',tab==='yield');
    document.getElementById('equity-chart').style.display=tab==='equity'?'':'none';
    document.getElementById('yield-chart').style.display=tab==='yield'?'':'none';
    if(tab==='yield'&&yieldChart)yieldChart.resize();
    if(tab==='equity'&&equityChart)equityChart.resize();
  }
  window.switchChartTab=switchChartTab;
  function loadEquityChart(){
    if(_initialLoadDone&&!isTabActive("portfolio"))return;
    if(_chartLoading)return;
    _chartLoading=true;
    if(_chartAbort){_chartAbort.abort();}
    _chartAbort=new AbortController();
    fetchWithTimeout(apiUrl('/api/bonds/equity-curve?days='+_chartDays),{signal:_chartAbort.signal}).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      // Clean up any previous overlays on successful API response
      var wrap=document.getElementById('chart-wrap');
      var ov=wrap?wrap.querySelector('.error-state'):null;
      if(ov)ov.remove();
      var nd=wrap?wrap.querySelector('.no-data-msg'):null;
      if(nd)nd.remove();
      if(!Array.isArray(data)||!data.length){
        if(equityChart){equityChart.data.labels=[];equityChart.data.datasets.forEach(function(ds){ds.data=[];});equityChart.update('none');}
        if(yieldChart){yieldChart.data.labels=[];yieldChart.data.datasets.forEach(function(ds){ds.data=[];});yieldChart.update('none');}
        if(wrap&&!wrap.querySelector('.no-data-msg')){
          var msg=document.createElement('div');msg.className='no-data-msg';
          msg.style.cssText='position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--text-muted)';
          msg.textContent='No equity data yet';wrap.style.position='relative';wrap.appendChild(msg);
        }
        return;
      }
      var labels=data.map(function(d){return d.ts;});
      var values=data.map(function(d){return d.equity;});
      var cashValues=data.map(function(d){return d.cash;});
      // Invested line removed — equity already includes it
      var yieldValues=data.map(function(d){return d['yield']||0;});
      if(equityChart){
        equityChart.data.labels=labels;
        equityChart.data.datasets[0].data=values;
        equityChart.data.datasets[1].data=cashValues;
        // dataset[2] (Invested) removed
        equityChart.update('none');
      }else{
        var ctx=document.getElementById('equity-chart').getContext('2d');
        chartGradient=ctx.createLinearGradient(0,0,0,(wrap&&wrap.offsetHeight)||260);
        var _cs=getComputedStyle(document.documentElement);
        var _arb=_cs.getPropertyValue('--accent-rgb').trim();
        var _ac=_cs.getPropertyValue('--accent').trim();
        chartGradient.addColorStop(0,'rgba('+_arb+',0.2)');
        chartGradient.addColorStop(1,'rgba('+_arb+',0.01)');
        equityChart=new Chart(ctx,{
          type:'line',
          data:{labels:labels,datasets:[
            {label:'Equity',data:values,borderColor:_ac,backgroundColor:chartGradient,fill:true,tension:0.3,pointRadius:0,borderWidth:2},
            {label:'Cash',data:cashValues,borderColor:'rgba(100,180,100,0.6)',borderDash:[5,3],fill:false,tension:0.3,pointRadius:0,borderWidth:1.5},
            // Invested dataset removed
          ]},
          options:{responsive:true,maintainAspectRatio:false,animation:false,
            plugins:{legend:{display:true,labels:{color:'#888',font:{family:'DM Sans',size:11},usePointStyle:true,pointStyle:'line'}},tooltip:{mode:'index',intersect:false,backgroundColor:'#141414',titleColor:'#e0e0e0',bodyColor:'#e0e0e0',borderColor:'#222',borderWidth:1,callbacks:{label:function(ctx){if(document.body.classList.contains('bal-hidden'))return ctx.dataset.label+': ***';return ctx.dataset.label+': $'+Number(ctx.parsed.y).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}}}},
            scales:{x:{ticks:{color:'#888',font:{family:'DM Sans',size:11},maxTicksLimit:8},grid:{color:'rgba(34,34,34,0.5)'}},
                    y:{ticks:{color:'#888',font:{family:'SF Mono',size:11},callback:function(v){if(document.body.classList.contains('bal-hidden'))return '***';return '$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:0,maximumFractionDigits:0});}},grid:{color:'rgba(34,34,34,0.5)'}}}
          }
        });
      }
      // -- Yield chart (same labels, separate canvas) --
      if(yieldChart){
        yieldChart.data.labels=labels;
        yieldChart.data.datasets[0].data=yieldValues;
        yieldChart.update('none');
      }else{
        var yCtx=document.getElementById('yield-chart').getContext('2d');
        var yGrad=yCtx.createLinearGradient(0,0,0,(wrap&&wrap.offsetHeight)||260);
        var _cs2=getComputedStyle(document.documentElement);
        var _arb2=_cs2.getPropertyValue('--accent-rgb').trim();
        var _ac2=_cs2.getPropertyValue('--accent').trim();
        yGrad.addColorStop(0,'rgba('+_arb2+',0.2)');
        yGrad.addColorStop(1,'rgba('+_arb2+',0.01)');
        yieldChart=new Chart(yCtx,{
          type:'line',
          data:{labels:labels,datasets:[
            {label:'Ann. Yield',data:yieldValues,borderColor:_ac2,backgroundColor:yGrad,fill:true,tension:0.3,pointRadius:0,borderWidth:2}
          ]},
          options:{responsive:true,maintainAspectRatio:false,animation:false,
            plugins:{legend:{display:true,labels:{color:'#888',font:{family:'DM Sans',size:11},usePointStyle:true,pointStyle:'line'}},tooltip:{mode:'index',intersect:false,backgroundColor:'#141414',titleColor:'#e0e0e0',bodyColor:'#e0e0e0',borderColor:'#222',borderWidth:1,callbacks:{label:function(ctx){return ctx.dataset.label+': '+(ctx.parsed.y||0).toFixed(2)+'%';}}}},
            scales:{x:{ticks:{color:'#888',font:{family:'DM Sans',size:11},maxTicksLimit:8},grid:{color:'rgba(34,34,34,0.5)'}},
                    y:{ticks:{color:'#888',font:{family:'SF Mono',size:11},callback:function(v){return v.toFixed(1)+'%';}},grid:{color:'rgba(34,34,34,0.5)'}}}
          }
        });
      }
    }).catch(function(e){
      if(e&&e.name==='AbortError')return;
      var wrap=document.getElementById('chart-wrap');
      if(wrap&&!wrap.querySelector('.error-state')){
        var ov=document.createElement('div');
        ov.className='error-state';
        ov.style.cssText='position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:var(--surface);z-index:1';
        ov.innerHTML='Chart data unavailable<br><button class="retry-btn" onclick="loadEquityChart()">Retry</button>';
        wrap.style.position='relative';
        wrap.appendChild(ov);
      }
    }).finally(function(){_chartLoading=false;_chartAbort=null;document.querySelectorAll('.range-day-btn').forEach(function(b){b.disabled=false;});});
  }
  loadEquityChart();
  setInterval(loadEquityChart,window.DASHBOARD_CONFIG.equityPollMs);

  // -- Value flash on change --
  var _prevKpiVals={};
  function flashIfChanged(id,newVal){
    if(_prevKpiVals[id]!==undefined&&_prevKpiVals[id]!==newVal){
      var el=document.getElementById(id);
      if(el){el.classList.remove('value-flash');void el.offsetWidth;el.classList.add('value-flash');}
    }
    _prevKpiVals[id]=newVal;
  }

  // -- KPI auto-refresh (always update text, CSS handles blur) --
  function refreshKPIs(){
    if(_tabHidden)return;
    fetchWithTimeout(apiUrl('/api/bonds/overview')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(d){
      if(d.error)return;
      var netPnl=(d.realized_pnl||0)+(d.unrealized_pnl||0);
      var wOC=d.wallet_usdc_onchain!=null?d.wallet_usdc_onchain:0;
      var wEx=d.wallet_usdc!=null?d.wallet_usdc:0;
      var wPol=d.wallet_pol!=null?d.wallet_pol:0;
      var totalEquity=wEx+d.invested+N(d.unrealized_pnl);
      flashIfChanged('kpi-wallet',fmtMoney(totalEquity));
      document.getElementById('kpi-wallet').innerHTML='<span class="bal-val">'+fmtMoney(totalEquity)+'</span>';
      document.getElementById('kpi-wallet-sub').textContent=fmtMoney(wEx)+' cash \u00b7 '+fmtMoney(d.invested)+' invested \u00b7 '+fmtMoney(wOC)+' on-chain \u00b7 '+wPol.toFixed(4)+' POL';
      flashIfChanged('kpi-pnl',netPnl.toFixed(2));
      var pnlPct=totalEquity>0?(netPnl/totalEquity*100):0;
      document.getElementById('kpi-pnl').innerHTML='<span class="bal-val">'+(netPnl>=0?'+$':'-$')+Math.abs(netPnl).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+'</span> <span style="font-size:0.65em;opacity:0.7">('+( pnlPct>=0?'+':'')+pnlPct.toFixed(2)+'%)</span>';
      document.getElementById('kpi-pnl').className='value '+pnlClass(netPnl);
      var rpnl=d.realized_pnl||0;var upnl=d.unrealized_pnl||0;
      document.getElementById('kpi-pnl-sub').textContent=(rpnl>=0?'+$':'-$')+Math.abs(rpnl).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+' realized \u00b7 '+(upnl>=0?'+$':'-$')+Math.abs(upnl).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+' unrealized';
      document.getElementById('kpi-winrate').textContent=Math.round((d.win_rate||0)*100)+'%';
      document.getElementById('kpi-yield').textContent=((d.annualized_yield||0)*100).toFixed(1)+'%';
      document.getElementById('kpi-positions').textContent=d.position_count||0;
      var recEl=document.getElementById('kpi-record');
      recEl.textContent=(d.wins||0)+'W / '+(d.losses||0)+'L';
      recEl.className='value '+((d.wins||0)>(d.losses||0)?'pnl-positive':(d.losses||0)>(d.wins||0)?'pnl-negative':'');
      var dailyEl=document.getElementById('kpi-daily-orders');if(dailyEl){var filled=d.daily_orders_filled||0;dailyEl.textContent=filled+'/'+(d.daily_orders_max||0);}
      var ddEl=document.getElementById('kpi-drawdown');var ddPct=d.drawdown_pct||0;if(ddEl){ddEl.textContent=ddPct.toFixed(1)+'%';ddEl.className='value '+(ddPct>window.DASHBOARD_CONFIG.haltDrawdownPct?'pnl-negative':ddPct>window.DASHBOARD_CONFIG.drawdownWarnPct?'pnl-warn':'');}
      var _haltPct=window.DASHBOARD_CONFIG.haltDrawdownPct;
      var ddBar=document.getElementById('kpi-drawdown-bar');if(ddBar){ddBar.style.width=Math.min(100,ddPct/_haltPct*100)+'%';ddBar.className='drawdown-fill '+(ddPct>_haltPct*0.75?'dd-danger':ddPct>_haltPct*0.25?'dd-warn':'dd-ok');}
      document.getElementById('header-positions').textContent=(d.position_count||0)+' positions';
      document.getElementById('header-wallet').innerHTML='<span class="bal-val">'+fmtMoney(wOC)+'</span><span style="color:var(--text-muted);font-size:0.75rem;margin:0 4px">USDC</span><span style="color:var(--text-muted);font-size:0.75rem;margin-right:4px">|</span>'+wPol.toFixed(4)+'<span style="color:var(--text-muted);font-size:0.75rem;margin-left:4px">POL</span>';
      updateFooterTimestamp();
    }).catch(function(err){
      console.warn('KPI refresh failed:', err);
    });
  }
  setInterval(refreshKPIs,window.DASHBOARD_CONFIG.kpiPollMs);

  // -- Open Positions (sortable with age) --
  var _posSortKey='unrealized_pnl';
  var _posSortAsc=true;
  var _posData=[];
  function posAge(openedAt){
    if(!openedAt)return {text:'\u2014',cls:'',hours:0};
    var d=new Date(openedAt);if(isNaN(d.getTime()))return {text:'\u2014',cls:'',hours:0};
    var h=Math.max(0,(Date.now()-d.getTime())/3600000);
    var cls=h<24?'age-fresh':h<72?'age-mature':'age-stale';
    if(h<1)return {text:Math.round(h*60)+'m',cls:cls,hours:h};
    if(h<24)return {text:Math.round(h)+'h',cls:cls,hours:h};
    return {text:Math.floor(h/24)+'d '+Math.round(h%24)+'h',cls:cls,hours:h};
  }
  function pnlBar(v,maxV){
    if(!v||!maxV)return '';
    var w=Math.min(40,Math.max(2,Math.round(Math.abs(v)/maxV*40)));
    return '<span class="pnl-bar '+(v>=0?'pnl-bar-pos':'pnl-bar-neg')+'" style="width:'+w+'px"></span>';
  }
  function renderPositions(rows){
    var el=document.getElementById('positions-table');
    document.getElementById('positions-count').textContent=rows.length;
    if(!rows.length){el.innerHTML='<div class="empty-state">No open positions \u2014 scanner will find opportunities.</div>';return;}
    var maxPnl=Math.max.apply(null,rows.map(function(r){return Math.abs(N(r.unrealized_pnl))||1;}));
    var cols=[{label:'',key:null},{label:'Market',key:'question'},{label:'Side',key:'outcome'},{label:'Entry',key:'entry_price',num:true},{label:'Now',key:'current_price',num:true},{label:'Yield',key:'annualized_yield',num:true},{label:'Cost',key:'cost_basis',num:true},{label:'Shares',key:'shares',num:true},{label:'P&L',key:'unrealized_pnl',num:true},{label:'Age',key:'_age_hours',num:true},{label:'Expires',key:'end_date'},{label:'',key:null}];
    var html='<div class="table-wrap"><table class="portfolio-sortable" id="pos-tbl"><thead><tr>';
    cols.forEach(function(c){
      var arrow='',ariaSort='';
      if(c.key){
        if(_posSortKey===c.key){arrow=' <span class="sort-arrow active">'+(_posSortAsc?'\u25B2':'\u25BC')+'</span>';ariaSort=' aria-sort="'+(_posSortAsc?'ascending':'descending')+'"';}
        else{arrow=' <span class="sort-arrow">\u25BC</span>';}
      }
      html+='<th'+(c.num?' class="num"':'')+(c.key?' data-sort="'+c.key+'"':'')+ariaSort+'>'+c.label+arrow+'</th>';
    });
    html+='</tr></thead><tbody>';
    rows.forEach(function(r,idx){
      var qText=htmlEscape(truncate(r.question,60));
      var qFull=htmlEscape(r.question||'');
      var posStatus=r.status||'open';
      var statusDot='<span class="status-dot status-dot-'+posStatus+'"></span>';
      html+='<tr class="pos-row-clickable" data-pidx="'+idx+'">';
      html+='<td style="width:20px;padding-right:0">'+statusDot+'</td>';
      html+='<td title="'+qFull+'"><span class="market-name">'+polyLink(r,qText)+'</span></td>';
      html+='<td class="'+sideClass(r.outcome)+'">'+htmlEscape(r.outcome)+'</td>';
      html+='<td class="num">'+N(r.entry_price).toFixed(3)+'</td>';
      html+='<td class="num">'+N(r.current_price).toFixed(3)+'</td>';
      html+='<td class="num accent-gold">'+(N(r.annualized_yield)*100).toFixed(1)+'%</td>';
      html+='<td class="num"><span class="bal-val">'+fmtMoney(N(r.cost_basis))+'</span></td>';
      html+='<td class="num">'+N(r.shares).toFixed(1)+'</td>';
      var upnl=N(r.unrealized_pnl);
      html+='<td class="num '+pnlClass(upnl)+'"><span class="bal-val">'+(upnl>=0?'+$':'-$')+Number(Math.abs(upnl)).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+'</span>'+pnlBar(upnl,maxPnl)+'</td>';
      var age=posAge(r.opened_at);
      html+='<td class="num"><span class="age-badge '+age.cls+'">'+age.text+'</span></td>';
      html+='<td class="td-muted">'+relTime(r.end_date)+'</td>';
      html+='<td>'+(posStatus==='exiting'?'<span class="pos-badge pos-badge-exiting">EXITING\u2026</span>':'<button class="btn-action btn-exit" data-market="'+htmlEscape(r.market_id)+'" data-token="'+htmlEscape(r.token_id)+'">Exit</button>')+'</td></tr>';
      // Expandable detail row
      var pLink=r.slug?'https://polymarket.com/event/'+(r.event_slug||r.slug)+(r.event_slug&&r.event_slug!==r.slug?'/'+r.slug:''):'';
      html+='<tr class="pos-expand-row" data-pidx="'+idx+'"><td colspan="'+cols.length+'">';
      html+='<div class="pos-detail-grid">';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Market ID</div><div class="pos-detail-value" style="font-size:0.7rem;word-break:break-all">'+htmlEscape(r.market_id||'')+'</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Token ID</div><div class="pos-detail-value" style="font-size:0.7rem;word-break:break-all">'+htmlEscape(r.token_id||'').substring(0,20)+'...</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Entry Price</div><div class="pos-detail-value">$'+N(r.entry_price).toFixed(4)+'</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Current Price</div><div class="pos-detail-value">$'+N(r.current_price).toFixed(4)+'</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Cost Basis</div><div class="pos-detail-value">'+fmtMoney(N(r.cost_basis))+'</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Ann. Yield</div><div class="pos-detail-value accent-gold">'+(N(r.annualized_yield)*100).toFixed(2)+'%</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Opened</div><div class="pos-detail-value">'+(r.opened_at?new Date(r.opened_at).toLocaleString('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'\u2014')+'</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">End Date</div><div class="pos-detail-value">'+(r.end_date?new Date(r.end_date).toLocaleString('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',year:'numeric'}):'\u2014')+'</div></div>';
      if(pLink)html+='<div class="pos-detail-item"><div class="pos-detail-label">Market Link</div><div class="pos-detail-value"><a href="'+pLink+'" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">View on Polymarket \u2197</a></div></div>';
      html+='</div></td></tr>';
    });
    html+='</tbody></table></div>';
    el.innerHTML=html;attachScrollFade(el);
    // Click to expand
    el.querySelectorAll('.pos-row-clickable').forEach(function(tr){
      tr.addEventListener('click',function(){
        var idx=tr.dataset.pidx;
        var detail=el.querySelector('.pos-expand-row[data-pidx="'+idx+'"]');
        if(detail)detail.classList.toggle('expanded');
      });
    });
    var thead=document.querySelector('#pos-tbl thead');
    if(thead)thead.onclick=function(e){
      var th=e.target.closest('th');var key=th?th.dataset.sort:null;
      if(key){
        if(_posSortKey===key){_posSortAsc=!_posSortAsc;}else{_posSortKey=key;_posSortAsc=(key==='question'||key==='outcome');}
        _posData.forEach(function(r){r._age_hours=posAge(r.opened_at).hours;});
        sortData(_posData,_posSortKey,_posSortAsc);
        renderPositions(_posData);
      }
    };
  }
  function loadPositions(){
    if(_initialLoadDone&&!isTabActive("portfolio"))return;
    fetchWithTimeout(apiUrl('/api/bonds/positions')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      if(data.error){document.getElementById('positions-table').innerHTML=errorHtml(data.error,'loadPositions');document.getElementById('positions-count').textContent='error';return;}
      updateFooterTimestamp();
      _posData=Array.isArray(data)?data:[];
      _posData.forEach(function(r){r._age_hours=posAge(r.opened_at).hours;});
      sortData(_posData,_posSortKey,_posSortAsc);
      renderPositions(_posData);
      // Update avg yield/days from positions (cash/invested updated from KPI refresh)
      var ayEl=document.getElementById('kpi-avg-yield');
      var adEl=document.getElementById('kpi-avg-days');
      if(_posData.length>0){
        var weightedYield=0,totalCost=0,totalDays=0,countDays=0;
        _posData.forEach(function(r){
          var cb=N(r.cost_basis);
          weightedYield+=N(r.annualized_yield)*cb;
          totalCost+=cb;
          if(r.end_date){var d=new Date(r.end_date);if(!isNaN(d.getTime())){var dl=Math.max(0,(d-Date.now())/86400000);totalDays+=dl;countDays++;}}
        });
        if(ayEl)ayEl.textContent=(totalCost>0?(weightedYield/totalCost*100):0).toFixed(1)+'%';
        if(adEl)adEl.textContent=countDays>0?(totalDays/countDays).toFixed(0)+'d':'\u2014';
      } else {
        if(ayEl)ayEl.textContent='\u2014';
        if(adEl)adEl.textContent='\u2014';
      }
    }).catch(function(){
      document.getElementById('positions-table').innerHTML=errorHtml('Failed to load positions','loadPositions');
      document.getElementById('positions-count').textContent='error';
    });
  }
  loadPositions();
  setInterval(loadPositions,window.DASHBOARD_CONFIG.positionsPollMs);

  // -- Pending Orders --
  function loadPendingOrders(){
    if(_initialLoadDone&&!isTabActive("portfolio"))return;
    fetchWithTimeout(apiUrl('/api/bonds/orders')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      var panel=document.getElementById('pending-orders-panel');
      var el=document.getElementById('pending-orders-table');
      if(!Array.isArray(data)){panel.style.display='none';return;}
      var rows=data.filter(function(r){return r.status==='pending'||r.status==='open';});
      document.getElementById('pending-orders-count').textContent=rows.length;
      if(!rows.length){panel.style.display='none';panel.classList.remove('has-orders');return;}
      panel.style.display='';panel.classList.add('has-orders');
      var html='<div class="table-wrap"><table><thead><tr><th>Market</th><th>Side</th><th class="num">Price</th><th class="num">Cost</th><th class="num">Shares</th><th>Age</th><th></th></tr></thead><tbody>';
      rows.forEach(function(r){
        var qText=htmlEscape(truncate(r.question||'',60));
        var qFull=htmlEscape(r.question||'');
        html+='<tr><td title="'+qFull+'"><span class="market-name">'+polyLink(r,qText)+'</span></td>';
        html+='<td class="'+sideClass(r.outcome)+'">'+htmlEscape(r.outcome||r.side||'buy')+'</td>';
        html+='<td class="num">'+N(r.price).toFixed(3)+'</td>';
        html+='<td class="num"><span class="bal-val">'+fmtMoney(N(r.size))+'</span></td>';
        html+='<td class="num">'+N(r.shares).toFixed(1)+'</td>';
        var oAge=posAge(r.created_at);html+='<td class="td-muted">'+oAge.text+'</td>';
        html+='<td><button class="btn-action btn-cancel-order" data-oid="'+(Number(r.id)||0)+'" data-clob="'+htmlEscape(r.clob_order_id||'')+'">Cancel</button></td></tr>';
      });
      html+='</tbody></table></div>';
      el.innerHTML=html;attachScrollFade(el);
    }).catch(function(){
      var panel=document.getElementById('pending-orders-panel');
      if(panel.style.display!=='none'){
        document.getElementById('pending-orders-table').innerHTML=errorHtml('Failed to load orders','loadPendingOrders');
        document.getElementById('pending-orders-count').textContent='error';
      }
    });
  }
  loadPendingOrders();
  setInterval(loadPendingOrders,window.DASHBOARD_CONFIG.ordersPollMs);

  // -- Resolved History (sortable) --
  var _histSortKey='closed_at';
  var _histSortAsc=false;
  var _histData=[];
  function renderHistory(rows){
    var el=document.getElementById('history-table');
    document.getElementById('history-count').textContent=rows.length;
    if(!rows.length){el.innerHTML='<div class="empty-state">No resolved positions yet.</div>';return;}
    var maxPnl=Math.max.apply(null,rows.map(function(r){return Math.abs(N(r.realized_pnl))||1;}));
    var cols=[{label:'Market',key:'question'},{label:'Side',key:'outcome'},{label:'Entry',key:'entry_price',num:true},{label:'Cost',key:'cost_basis',num:true},{label:'P&L',key:'realized_pnl',num:true},{label:'Result',key:'status'},{label:'Closed',key:'closed_at'}];
    var html='<div class="table-wrap"><table class="portfolio-sortable" id="hist-tbl"><thead><tr>';
    cols.forEach(function(c){
      var arrow='';
      if(c.key){
        if(_histSortKey===c.key){arrow=' <span class="sort-arrow active">'+(_histSortAsc?'\u25B2':'\u25BC')+'</span>';}
        else{arrow=' <span class="sort-arrow">\u25BC</span>';}
      }
      html+='<th'+(c.num?' class="num"':'')+(c.key?' data-sort="'+c.key+'"':'')+'>'+c.label+arrow+'</th>';
    });
    html+='</tr></thead><tbody>';
    rows.forEach(function(r){
      var isWin=r.status==='resolved_win';
      var isExit=r.status==='exited';
      var rpnl=N(r.realized_pnl);
      var qText=htmlEscape(truncate(r.question,60));
      var qFull=htmlEscape(r.question||'');
      html+='<tr><td title="'+qFull+'"><span class="market-name">'+polyLink(r,qText)+'</span></td>';
      html+='<td class="'+sideClass(r.outcome)+'">'+htmlEscape(r.outcome)+'</td>';
      html+='<td class="num">'+N(r.entry_price).toFixed(3)+'</td>';
      html+='<td class="num"><span class="bal-val">'+fmtMoney(N(r.cost_basis))+'</span></td>';
      html+='<td class="num '+pnlClass(rpnl)+'"><span class="bal-val">'+(rpnl>=0?'+$':'-$')+Number(Math.abs(rpnl)).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+'</span>'+pnlBar(rpnl,maxPnl)+'</td>';
      var badgeClass=isWin?'badge-ok':isExit?'badge-warn':'badge-error';
      var badgeText=isWin?'WIN':isExit?'EXITED':'LOSS';
      html+='<td><span class="badge '+badgeClass+'">'+badgeText+'</span></td>';
      html+='<td class="td-muted">'+relTime(r.closed_at)+'</td></tr>';
    });
    html+='</tbody></table></div>';
    el.innerHTML=html;attachScrollFade(el);
    var thead=document.querySelector('#hist-tbl thead');
    if(thead)thead.onclick=function(e){
      var th=e.target.closest('th');var key=th?th.dataset.sort:null;
      if(key){
        if(_histSortKey===key){_histSortAsc=!_histSortAsc;}else{_histSortKey=key;_histSortAsc=(key==='question'||key==='outcome');}
        sortData(_histData,_histSortKey,_histSortAsc);
        renderHistory(_histData);
      }
    };
  }
  function loadHistory(){
    if(_initialLoadDone&&!isTabActive("portfolio"))return;
    fetchWithTimeout(apiUrl('/api/bonds/history')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      var el=document.getElementById('history-table');
      if(data.error){el.innerHTML=errorHtml(data.error,'loadHistory');document.getElementById('history-count').textContent='error';return;}
      _histData=Array.isArray(data)?data:[];
      sortData(_histData,_histSortKey,_histSortAsc);
      renderHistory(_histData);
    }).catch(function(){
      document.getElementById('history-table').innerHTML=errorHtml('Failed to load history','loadHistory');
      document.getElementById('history-count').textContent='error';
    });
  }
  loadHistory();
  setInterval(loadHistory,window.DASHBOARD_CONFIG.historyPollMs);
  _initialLoadDone=true;

  // -- Opportunities (sortable) --
  var _oppsData=[];
  var _oppsSortKey='opportunity_score';
  var _oppsSortAsc=false;
  var _oppsColWidth=null;
  var _oppsResizing=false;
  var _oppsLastJson='';

  function bar(v){var nv=Math.min(1,Math.max(0,N(v)));var w=Math.max(2,Math.round(nv*60));var cls=nv<0.3?'factor-bar factor-bar-dim':nv>=0.7?'factor-bar factor-bar-strong':'factor-bar';return '<span class="factor-track"><span class="'+cls+'" style="width:'+w+'px"></span></span><span class="factor-val">'+nv.toFixed(2)+'</span>';}

  function renderOppRow(r, isBuyable, eid, isLast){
    var cls=eid?' class="event-child'+(isBuyable?' event-best':'')+(isLast?' event-last':'')+'" data-eid="'+eid+'" style="display:none"':'';
    var qText=htmlEscape(r.question||'\u2014');
    var qFull=htmlEscape(r.question||'');
    var html='<tr'+cls+'><td title="'+qFull+'"><span class="market-name">'+polyLink(r,qText)+'</span></td>';
    html+='<td class="'+sideClass(r.outcome)+'">'+htmlEscape(r.outcome)+'</td>';
    html+='<td class="num">'+N(r.price).toFixed(3)+'</td>';
    html+='<td class="num" style="color:var(--text-muted)">'+N(r.days_remaining).toFixed(1)+'</td>';
    html+='<td class="num" style="color:var(--text-muted)">'+N(r.spread).toFixed(3)+'</td>';
    html+='<td class="num" style="color:var(--text-muted)">$'+(N(r.volume)/1e6).toFixed(1)+'M</td>';
    html+='<td class="num">'+(N(r.annualized_yield)*100).toFixed(1)+'%</td>';
    var sc=N(r.opportunity_score);
    html+='<td class="num" style="font-weight:700;color:'+(sc>=0.01?'var(--accent)':'var(--text-muted)')+'">'+sc.toFixed(4)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.yield_score)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.liquidity_score)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.time_value)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.exit_liquidity)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.market_quality)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.spread_efficiency)+'</td>';
    html+='<td class="num"><span class="bal-val">'+(r.computed_size?fmtMoney(r.computed_size):'\u2014')+'</span></td>';
    var canBuy=isBuyable&&r.computed_size&&r.computed_size>=window.DASHBOARD_CONFIG.minBuyableUsd;
    html+='<td>'+(canBuy?'<button class="btn-action btn-buy" data-market="'+htmlEscape(r.market_id)+'" data-token="'+htmlEscape(r.token_id)+'" data-outcome="'+htmlEscape(r.outcome)+'">Buy</button>':'<span class="td-muted" title="Edge too small">\u2014</span>')+'</td></tr>';
    return html;
  }

  function toggleEventRows(headerRow){
    var eid=headerRow.dataset.eid;
    var children=document.querySelectorAll('.event-child[data-eid="'+eid+'"]');
    var visible=children.length&&children[0].style.display!=='none';
    children.forEach(function(tr){tr.style.display=visible?'none':'';});
    var chev=headerRow.querySelector('.event-chevron');
    if(chev)chev.textContent=visible?'\u25B6':'\u25BC';
    headerRow.classList.toggle('expanded',!visible);
  }
  window.toggleEventRows=toggleEventRows;

  function renderOpportunities(rows){
    if(_oppsResizing)return;  // Don't re-render during active column drag
    var el=document.getElementById('opportunities-table');
    if(!rows.length){el.innerHTML='<div class="empty-state">No bond candidates found.</div>';return;}

    // 1. Group by event_slug (fallback to market_id for ungrouped)
    var groups={},order=[];
    rows.forEach(function(r){
      var key=r.event_slug||r.market_id;
      if(!groups[key]){groups[key]={title:r.event_title||r.question,rows:[]};order.push(key);}
      groups[key].rows.push(r);
    });

    // 2. Sort within each group by opportunity_score desc
    order.forEach(function(k){
      groups[k].rows.sort(function(a,b){return N(b.opportunity_score)-N(a.opportunity_score);});
    });

    // 3. Sort groups by best member's current sort key
    order.sort(function(a,b){
      var aVal=groups[a].rows[0],bVal=groups[b].rows[0];
      var ak=typeof aVal[_oppsSortKey]==='string'?aVal[_oppsSortKey]:N(aVal[_oppsSortKey]);
      var bk=typeof bVal[_oppsSortKey]==='string'?bVal[_oppsSortKey]:N(bVal[_oppsSortKey]);
      if(ak<bk)return _oppsSortAsc?-1:1;
      if(ak>bk)return _oppsSortAsc?1:-1;
      return 0;
    });

    // 4. Render
    var cols=[
      {label:'Market',key:'question'},{label:'Side',key:'outcome'},
      {label:'Price',key:'price',num:true},{label:'Days',key:'days_remaining',num:true},{label:'Bid-Ask',key:'spread',num:true},{label:'Vol',key:'volume',num:true},
      {label:'Yield',key:'annualized_yield',num:true},
      {label:'Score',key:'opportunity_score',num:true},{label:'Yield Score',key:'yield_score',num:true,factor:true,title:'tanh(yield / scale)'},
      {label:'Liquidity',key:'liquidity_score',num:true,factor:true,title:'tanh(depth / scale)'},{label:'Time',key:'time_value',num:true,factor:true,title:'exp(-days / tau)'},
      {label:'Exit Liq',key:'exit_liquidity',num:true,factor:true,title:'Exit liquidity (bid depth)'},{label:'Mkt Qual',key:'market_quality',num:true,factor:true,title:'Volume & spread quality'},
      {label:'Spread',key:'spread_efficiency',num:true,factor:true,title:'1 - spread/price'},{label:'Order $',key:'computed_size',num:true}
    ];
    var html='<div class="table-wrap"><table id="opps-tbl"><thead><tr>';
    cols.forEach(function(c,idx){
      var arrow='';
      if(_oppsSortKey===c.key){arrow=' <span class="sort-arrow active">'+(_oppsSortAsc?'\u25B2':'\u25BC')+'</span>';}
      else{arrow=' <span class="sort-arrow">\u25BC</span>';}
      var thStyle=idx===0?' style="min-width:150px"':'';
      var resizeEl=idx===0?'<span class="col-resize-handle"></span>':'';
      html+='<th'+(c.num?' class="num"':'')+' data-sort="'+c.key+'"'+(c.title?' title="'+c.title+'"':'')+thStyle+'>'+c.label+arrow+resizeEl+'</th>';
    });
    html+='<th style="min-width:50px"></th>';
    html+='</tr></thead><tbody>';

    order.forEach(function(key){
      var g=groups[key];
      var best=g.rows[0];
      if(g.rows.length===1){
        // Single market — render inline, no accordion
        html+=renderOppRow(best,true);
      }else{
        // Multi-market event — accordion header
        var eid='evt-'+key.replace(/[^a-z0-9]/gi,'').slice(0,40);
        html+='<tr class="event-header" data-eid="'+eid+'" onclick="toggleEventRows(this)">';
        html+='<td><div class="event-td-inner"><span class="event-title">'+htmlEscape(g.title||'\u2014')+'</span>';
        html+=' <span class="event-count">'+g.rows.length+'</span>';
        html+=' <span class="event-chevron">\u25B6</span></div></td>';
        html+='<td class="td-muted">\u2014</td>';
        html+='<td class="num">'+N(best.price).toFixed(3)+'</td>';
        html+='<td class="num" style="color:var(--text-muted)">'+N(best.days_remaining).toFixed(1)+'</td>';
        html+='<td class="num" style="color:var(--text-muted)">'+N(best.spread).toFixed(3)+'</td>';
        html+='<td class="num" style="color:var(--text-muted)">$'+(N(best.volume)/1e6).toFixed(1)+'M</td>';
        html+='<td class="num">'+(N(best.annualized_yield)*100).toFixed(1)+'%</td>';
        var bsc=N(best.opportunity_score);
        html+='<td class="num" style="font-weight:700;color:'+(bsc>=0.01?'var(--accent)':'var(--text-muted)')+'">'+bsc.toFixed(4)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.yield_score)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.liquidity_score)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.time_value)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.exit_liquidity)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.market_quality)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.spread_efficiency)+'</td>';
        html+='<td class="num">'+(best.computed_size?fmtMoney(best.computed_size):'\u2014')+'</td>';
        html+='<td><span class="td-muted">\u2014</span></td></tr>';

        // Child rows (hidden by default)
        g.rows.forEach(function(r,i){
          html+=renderOppRow(r,i===0,eid,i===g.rows.length-1);
        });
      }
    });
    html+='</tbody></table></div>';

    // Save UI state before rebuild
    var expandedEids=[];
    document.querySelectorAll('.event-header.expanded').forEach(function(h){expandedEids.push(h.dataset.eid);});
    var prevWrap=el.querySelector('.table-wrap');
    var prevScrollLeft=prevWrap?prevWrap.scrollLeft:0;

    if(window._oppsResizeCleanup){window._oppsResizeCleanup();window._oppsResizeCleanup=null;}
    el.innerHTML=html;attachScrollFade(el);

    // Restore scroll position
    var newWrap=el.querySelector('.table-wrap');
    if(newWrap&&prevScrollLeft)newWrap.scrollLeft=prevScrollLeft;

    // Restore column width
    if(_oppsColWidth){
      var cw=_oppsColWidth+'px';
      var firstTh=document.querySelector('#opps-tbl th:first-child');
      if(firstTh){firstTh.style.width=cw;firstTh.style.minWidth=cw;firstTh.style.maxWidth=cw;}
      document.querySelectorAll('#opps-tbl td:first-child').forEach(function(td){
        td.style.width=cw;td.style.minWidth=cw;td.style.maxWidth=cw;
      });
    }

    // Column resize handle for Market column
    var resizeHandle=document.querySelector('#opps-tbl .col-resize-handle');
    if(resizeHandle){
      var th=resizeHandle.parentElement;
      var _resizing=false;
      resizeHandle.addEventListener('mousedown',function(e){
        e.preventDefault();e.stopPropagation();
        _resizing=true;_oppsResizing=true;
        var startX=e.pageX,startW=th.offsetWidth;
        var tds=document.querySelectorAll('#opps-tbl td:first-child');
        resizeHandle.classList.add('active');
        function onMove(ev){
          var w=Math.max(150,startW+(ev.pageX-startX));
          _oppsColWidth=w;
          th.style.width=w+'px';th.style.minWidth=w+'px';th.style.maxWidth=w+'px';
          tds.forEach(function(td){
            td.style.width=w+'px';td.style.minWidth=w+'px';td.style.maxWidth=w+'px';
          });
        }
        function onUp(){
          resizeHandle.classList.remove('active');
          document.removeEventListener('mousemove',onMove);
          document.removeEventListener('mouseup',onUp);
          _oppsResizing=false;
          setTimeout(function(){_resizing=false;},0);
        }
        document.addEventListener('mousemove',onMove);
        document.addEventListener('mouseup',onUp);
        window._oppsResizeCleanup=function(){
          resizeHandle.classList.remove('active');
          document.removeEventListener('mousemove',onMove);
          document.removeEventListener('mouseup',onUp);
          _oppsResizing=false;
        };
      });
      th.addEventListener('click',function(e){
        if(_resizing){e.stopPropagation();e.preventDefault();}
      },true);
    }

    // Restore expanded accordions
    expandedEids.forEach(function(eid){
      var h=document.querySelector('.event-header[data-eid="'+eid+'"]');
      if(h)toggleEventRows(h);
    });

    // Update badge with event count
    document.getElementById('opps-count').textContent=order.length+' events, '+rows.length+' candidates';

    var thead=document.querySelector('#opps-tbl thead');
    if(thead)thead.onclick=function(e){
      var th=e.target.closest('th');
      var key=th?th.dataset.sort:null;
      if(key)sortOpportunities(key);
    };
  }

  function sortOpportunities(key){
    if(_oppsSortKey===key){_oppsSortAsc=!_oppsSortAsc;}
    else{_oppsSortKey=key;_oppsSortAsc=(key==='question'||key==='outcome');}
    renderOpportunities(_oppsData);
  }

  var _oppsLoading=false;
  function loadOpportunities(){
    if(_initialLoadDone&&!isTabActive("opportunities"))return;
    if(_oppsLoading)return;
    _oppsLoading=true;
    fetchWithTimeout(apiUrl('/api/bonds/opportunities')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      if(data.error){document.getElementById('opportunities-table').innerHTML=errorHtml(data.error,'loadOpportunities');document.getElementById('opps-count').textContent='error';return;}
      var newJson=JSON.stringify(data);
      if(newJson===_oppsLastJson)return;
      _oppsLastJson=newJson;
      _oppsData=Array.isArray(data)?data:[];
      _oppsData=_oppsData.filter(function(r){return r.opportunity_score>0;});
      renderOpportunities(_oppsData);
    }).catch(function(){
      document.getElementById('opportunities-table').innerHTML=errorHtml('Failed to load opportunities','loadOpportunities');
      document.getElementById('opps-count').textContent='error';
    }).finally(function(){_oppsLoading=false;});
  }
  var _oppsInterval=null;
  function startOppsPolling(){if(!_oppsInterval)_oppsInterval=setInterval(loadOpportunities,window.DASHBOARD_CONFIG.oppsPollMs);}
  function stopOppsPolling(){if(_oppsInterval){clearInterval(_oppsInterval);_oppsInterval=null;}}

  // -- Watchlist (sortable) --
  var _watchData=[];
  var _watchSortKey='alert_intensity';
  var _watchSortAsc=false;

  function renderWatchlist(rows){
    var el=document.getElementById('watchlist-table');
    if(!rows.length){el.innerHTML='<div class="empty-state">No crypto/DeFi markets tracked yet.</div>';return;}
    var cols=[
      {label:'Market',key:'question'},{label:'Price',key:'current_price',num:true},
      {label:'EWMA',key:'ewma_price',num:true},{label:'Z-Score',key:'z_score',num:true},
      {label:'Intensity',key:'alert_intensity',num:true},{label:'Volume',key:'volume',num:true},
      {label:'Last Alert',key:'last_alerted_at'},
      {label:'Expires',key:'end_date'},
      {label:'Position',key:null},{label:'Trade',key:null}
    ];
    var html='<div class="table-wrap"><table id="watch-tbl"><thead><tr>';
    cols.forEach(function(c){
      var arrow='';
      if(c.key){
        if(_watchSortKey===c.key){arrow=' <span class="sort-arrow active">'+(_watchSortAsc?'\u25B2':'\u25BC')+'</span>';}
        else{arrow=' <span class="sort-arrow">\u25BC</span>';}
      }
      html+='<th'+(c.num?' class="num"':'')+(c.key?' data-sort="'+c.key+'"':'')+'>'+c.label+arrow+'</th>';
    });
    html+='</tr></thead><tbody>';
    function wBar(v){var nv=Math.min(1,Math.abs(N(v)));var w=Math.max(2,Math.round(nv*60));var cls=nv<0.3?'factor-bar factor-bar-dim':nv>=0.7?'factor-bar factor-bar-strong':'factor-bar';return '<span class="factor-track"><span class="'+cls+'" style="width:'+w+'px"></span></span><span class="factor-val">'+nv.toFixed(2)+'</span>';}
    rows.forEach(function(r){
      var qText=htmlEscape(truncate(r.question,60));
      var qFull=htmlEscape(r.question||'');
      html+='<tr><td title="'+qFull+'"><span class="market-name">'+polyLink(r,qText)+'</span></td>';
      html+='<td class="num">'+N(r.current_price).toFixed(3)+'</td>';
      html+='<td class="num" style="color:var(--text-muted)">'+N(r.ewma_price).toFixed(3)+'</td>';
      var absZ=Math.abs(N(r.z_score));
      html+='<td class="num '+(absZ>2?'pnl-negative':absZ>1?'pnl-warn':'')+'">'+N(r.z_score).toFixed(2)+'</td>';
      html+='<td class="num factor-cell">'+wBar(r.alert_intensity)+'</td>';
      html+='<td class="num">$'+Math.round(N(r.volume)).toLocaleString('en-US')+'</td>';
      html+='<td class="td-muted">'+(r.last_alerted_at?relTime(r.last_alerted_at):'\u2014')+'</td>';
      html+='<td class="td-muted">'+relTime(r.end_date)+'</td>';

      // Position column
      var posHtml='\u2014';
      var py=r.position_yes, pn=r.position_no;
      if(py){
        posHtml='<span class="pos-badge pos-badge-'+(py.status==='exiting'?'exiting':'open')+'">Yes '+py.status+'</span>';
        posHtml+=' <span class="'+(py.pnl>=0?'pnl-positive':'pnl-negative')+'" style="font-size:0.75rem">'+(py.pnl>=0?'+$':'-$')+Math.abs(py.pnl).toFixed(2)+'</span>';
      }
      if(pn){
        if(py)posHtml+='<br>';
        posHtml+='<span class="pos-badge pos-badge-'+(pn.status==='exiting'?'exiting':'open')+'">No '+pn.status+'</span>';
        posHtml+=' <span class="'+(pn.pnl>=0?'pnl-positive':'pnl-negative')+'" style="font-size:0.75rem">'+(pn.pnl>=0?'+$':'-$')+Math.abs(pn.pnl).toFixed(2)+'</span>';
      }
      html+='<td style="font-size:0.75rem">'+posHtml+'</td>';

      // Trade column
      var mid=r.market_id;
      var tradeHtml='';
      if(py&&py.status==='open'){
        tradeHtml+='<button class="trade-btn trade-btn-exit" data-mid="'+htmlEscape(mid)+'" data-action="sell" data-side="Yes">Exit Yes</button> ';
      } else if(py&&py.status==='exiting'){
        tradeHtml+='<span style="color:var(--yellow);font-size:0.7rem">Exiting...</span> ';
      } else {
        tradeHtml+='<button class="trade-btn trade-btn-yes" data-mid="'+htmlEscape(mid)+'" data-action="buy" data-side="Yes">Buy Yes</button> ';
      }
      if(pn&&pn.status==='open'){
        tradeHtml+='<button class="trade-btn trade-btn-exit" data-mid="'+htmlEscape(mid)+'" data-action="sell" data-side="No">Exit No</button>';
      } else if(pn&&pn.status==='exiting'){
        tradeHtml+='<span style="color:var(--yellow);font-size:0.7rem">Exiting...</span>';
      } else {
        tradeHtml+='<button class="trade-btn trade-btn-no" data-mid="'+htmlEscape(mid)+'" data-action="buy" data-side="No">Buy No</button>';
      }
      html+='<td style="white-space:nowrap">'+tradeHtml+'</td></tr>';
    });
    html+='</tbody></table></div>';
    el.innerHTML=html;attachScrollFade(el);
    var thead=document.querySelector('#watch-tbl thead');
    if(thead)thead.onclick=function(e){
      var th=e.target.closest('th');
      var key=th?th.dataset.sort:null;
      if(key)sortWatchlist(key);
    };
  }

  function sortWatchlist(key){
    if(_watchSortKey===key){_watchSortAsc=!_watchSortAsc;}
    else{_watchSortKey=key;_watchSortAsc=(key==='question');}
    sortData(_watchData,_watchSortKey,_watchSortAsc);
    renderWatchlist(_watchData);
  }

  var _pendingTrades=new Set();
  function tradeToggle(marketId, action, side, btn) {
    var key=marketId+':'+side;
    if(_pendingTrades.has(key)) return;
    var isBuy=action==='buy';
    var title=isBuy?'Place Buy Order?':'Exit Position?';
    var msg=isBuy
        ? 'Buy '+side+'? (Kelly-sized limit order)'
        : 'Exit '+side+' position? (Limit sell at best bid)';
    showConfirm(title,msg,isBuy?'Buy':'Exit',isBuy?'btn-confirm-on':'btn-confirm-off',function(){
      _pendingTrades.add(key);
      if(btn){btn.classList.add('loading');btn.textContent=isBuy?'Placing...':'Exiting...';}
      fetchWithTimeout(apiUrl('/api/watchlist/trade'), {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({market_id:marketId, action:action, side:side})
      }).then(function(r){
          if(!r.ok) return r.json().catch(function(){throw new Error('HTTP '+r.status)}).then(function(d){throw new Error(d.error||'HTTP '+r.status)});
          return r.json();
      }).then(function(d){
          showCopyToast(isBuy
              ? 'Order: $'+(d.size_usd||0).toFixed(2)+' @ '+(d.price||0).toFixed(3)
              : 'Exit @ '+(d.price||0).toFixed(3));
      }).catch(function(e){
          showCopyToast('Error: '+e.message,true);
          if(btn){btn.classList.remove('loading');btn.textContent=isBuy?(side==='Yes'?'Buy Yes':'Buy No'):(side==='Yes'?'Exit Yes':'Exit No');}
      }).finally(function(){
          _pendingTrades.delete(key);
          setTimeout(loadWatchlist, 1000);
      });
    });
  }
  window.tradeToggle = tradeToggle;

  // One-time delegated click handler for trade buttons (avoids accumulation on re-render)
  (function(){
    var wc=document.getElementById('tab-watchlist');
    if(wc)wc.addEventListener('click',function(e){
      var btn=e.target.closest('.trade-btn');
      if(!btn)return;
      tradeToggle(btn.dataset.mid,btn.dataset.action,btn.dataset.side,btn);
    });
  })();

  function loadWatchlist(){
    if(_initialLoadDone&&!isTabActive("watchlist"))return;
    fetchWithTimeout(apiUrl('/api/watchlist/crypto')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      if(data.error){document.getElementById('watchlist-table').innerHTML=errorHtml(data.error,'loadWatchlist');document.getElementById('watchlist-count').textContent='error';return;}
      _watchData=Array.isArray(data)?data:[];
      document.getElementById('watchlist-count').textContent=_watchData.length+' markets';
      sortData(_watchData,_watchSortKey,_watchSortAsc);
      renderWatchlist(_watchData);
    }).catch(function(){
      document.getElementById('watchlist-table').innerHTML=errorHtml('Failed to load watchlist','loadWatchlist');
      document.getElementById('watchlist-count').textContent='error';
    });
  }
  var _watchInterval=null;
  function startWatchPolling(){if(!_watchInterval)_watchInterval=setInterval(loadWatchlist,window.DASHBOARD_CONFIG.watchlistPollMs);}
  function stopWatchPolling(){if(_watchInterval){clearInterval(_watchInterval);_watchInterval=null;}}

  // -- Exit position action --
  function exitPosition(marketId,tokenId,btn){
    showConfirm('Exit Position?','A sell order will be placed at best bid.','Exit','btn-confirm-off',function(){
      if(btn)btn.disabled=true;
      fetchWithTimeout(apiUrl('/api/bonds/positions/close'),{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({market_id:marketId,token_id:tokenId})
      }).then(function(r){if(!r.ok) throw new Error('HTTP '+r.status);return r.json()}).then(function(d){
        if(d.ok){showCopyToast('Exit order placed');loadPositions();loadPendingOrders();}
        else{showCopyToast('Error: '+(d.error||'Unknown'),true);}
      }).catch(function(e){showCopyToast('Error: '+e.message,true);}).finally(function(){if(btn)btn.disabled=false;});
    });
  }

  // -- Cancel order action --
  function cancelOrder(orderId,clobOrderId,btn){
    showConfirm('Cancel Order?','This will cancel the pending order on the exchange.','Cancel Order','btn-confirm-off',function(){
      if(btn)btn.disabled=true;
      fetchWithTimeout(apiUrl('/api/bonds/orders/cancel'),{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({order_id:orderId,clob_order_id:clobOrderId})
      }).then(function(r){if(!r.ok) throw new Error('HTTP '+r.status);return r.json()}).then(function(d){
        if(d.ok){showCopyToast('Order cancelled');loadPendingOrders();}
        else{showCopyToast('Error: '+(d.error||'Unknown'),true);}
      }).catch(function(e){showCopyToast('Error: '+e.message,true);}).finally(function(){if(btn)btn.disabled=false;});
    });
  }

  // -- Buy opportunity action --
  function buyOpportunity(marketId,tokenId,outcome,btn){
    showConfirm('Place Buy Order?','Place a buy order for '+outcome+'?','Buy','btn-confirm-on',function(){
      if(btn)btn.disabled=true;
      fetchWithTimeout(apiUrl('/api/bonds/opportunities/buy'),{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({market_id:marketId,token_id:tokenId,outcome:outcome})
      }).then(function(r){if(!r.ok) throw new Error('HTTP '+r.status);return r.json()}).then(function(d){
        if(d.ok){showCopyToast('Buy order placed: $'+(d.size_usd||0).toFixed(2)+' @ '+(d.price||0).toFixed(3));loadOpportunities();loadPendingOrders();}
        else{showCopyToast('Error: '+(d.error||'Unknown'),true);}
      }).catch(function(e){showCopyToast('Error: '+e.message,true);}).finally(function(){if(btn)btn.disabled=false;});
    });
  }

  // Delegated click handlers for action buttons (no inline onclick)
  document.addEventListener('click',function(e){
    var exitBtn=e.target.closest('.btn-exit[data-market]');
    if(exitBtn){e.stopPropagation();exitPosition(exitBtn.dataset.market,exitBtn.dataset.token,exitBtn);return;}
    var cancelBtn=e.target.closest('.btn-cancel-order[data-oid]');
    if(cancelBtn){cancelOrder(Number(cancelBtn.dataset.oid),cancelBtn.dataset.clob,cancelBtn);return;}
    var buyBtn=e.target.closest('.btn-buy[data-market]');
    if(buyBtn){buyOpportunity(buyBtn.dataset.market,buyBtn.dataset.token,buyBtn.dataset.outcome,buyBtn);return;}
  });

  // Expose functions for retry buttons (onclick runs in global scope)
  window.loadPositions=loadPositions;
  window.loadHistory=loadHistory;
  window.loadOpportunities=loadOpportunities;
  window.loadWatchlist=loadWatchlist;
  window.loadEquityChart=loadEquityChart;
  window.exitPosition=exitPosition;
  window.cancelOrder=cancelOrder;
  window.buyOpportunity=buyOpportunity;
})();

// -- Bot on/off toggle --
(function(){
  var btn=document.getElementById('bot-toggle-btn');
  var statusEl=document.getElementById('bot-toggle-status');
  var overlay=document.getElementById('bot-confirm-overlay');
  var titleEl=document.getElementById('bot-confirm-title');
  var msgEl=document.getElementById('bot-confirm-msg');
  var okBtn=document.getElementById('bot-confirm-ok');
  var cancelBtn=document.getElementById('bot-confirm-cancel');
  var currentState=null;
  function updateUI(en){
    currentState=en;
    btn.classList.toggle('on',en);
    btn.setAttribute('aria-pressed',en?'true':'false');
    statusEl.textContent=en?'ON':'OFF';
    statusEl.className='bot-toggle-status '+(en?'on':'off');
  }
  function fetchStatus(){
    if(_tabHidden)return;
    fetchWithTimeout(apiUrl('/api/trading/status')).then(function(r){return r.json()})
      .then(function(d){
        updateUI(d.trading_enabled);
        var banner=document.getElementById('trading-paused-banner');
        if(banner)banner.style.display=d.trading_enabled?'none':'block';
      }).catch(function(){});
  }
  btn.addEventListener('click',function(){
    if(currentState===null)return;
    var ns=!currentState;
    titleEl.textContent=ns?'Enable Trading?':'Disable Trading?';
    msgEl.textContent=ns
      ?'The bot will resume placing new orders on the next scan cycle.'
      :'The bot will stop placing new orders. Existing positions will continue to be monitored.';
    okBtn.textContent=ns?'Yes, Enable':'Yes, Disable';
    okBtn.className=ns?'btn-confirm-on':'btn-confirm-off';
    overlay.classList.add('active');
    okBtn.onclick=function(){
      overlay.classList.remove('active');
      btn.disabled=true;
      fetchWithTimeout(apiUrl('/api/trading/toggle'),{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({enabled:ns})
      }).then(function(r){return r.json()})
        .then(function(d){updateUI(d.trading_enabled);btn.disabled=false;})
        .catch(function(){btn.disabled=false;});
    };
  });
  cancelBtn.addEventListener('click',function(){overlay.classList.remove('active')});
  overlay.addEventListener('click',function(e){if(e.target===overlay)overlay.classList.remove('active')});
  fetchStatus();
  setInterval(fetchStatus,window.DASHBOARD_CONFIG.tradingStatusPollMs);
})();
var _scrollFadeObserver=window.ResizeObserver?new ResizeObserver(function(entries){
  entries.forEach(function(entry){
    var tw=entry.target;
    tw.classList.toggle('scrolled',tw.scrollLeft<tw.scrollWidth-tw.clientWidth-2);
  });
}):null;
var _scrollFadeSet=typeof WeakSet!=='undefined'?new WeakSet():null;
function attachScrollFade(container){
  var root=container||document;
  var wraps=root.classList&&root.classList.contains('table-wrap')?[root]:root.querySelectorAll('.table-wrap');
  wraps.forEach(function(tw){
    if(_scrollFadeSet&&_scrollFadeSet.has(tw))return;
    if(_scrollFadeSet)_scrollFadeSet.add(tw);
    var update=function(){tw.classList.toggle('scrolled',tw.scrollLeft<tw.scrollWidth-tw.clientWidth-2);};
    tw.addEventListener('scroll',update);
    if(_scrollFadeObserver)_scrollFadeObserver.observe(tw);
    update();
  });
}

