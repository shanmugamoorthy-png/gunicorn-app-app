import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

CLIENT_ID = os.environ.get("DHAN_CLIENT_ID", "")
ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
BASE = "https://api.dhan.co/v2"
IST = timezone(timedelta(hours=5, minutes=30))
SEGMENT = "IDX_I"

SYMBOLS = {
    "NIFTY": {"id": "13", "name": "NIFTY 50", "step": 50, "span": 1000, "min_abs": 20000},
    "BANKNIFTY": {"id": "25", "name": "BANK NIFTY", "step": 100, "span": 2500, "min_abs": 5000},
    "SENSEX": {"id": "51", "name": "SENSEX", "step": 100, "span": 3000, "min_abs": 3000},
}

# OI change tracking (in memory, per symbol + expiry)
MIN_PCT = 2.0     # log a change only if >= 2% vs baseline
BASE_AGE = 300    # compare against snapshot ~5 min old
_state = {}
_lock = threading.Lock()
_cache = {}


def headers():
    return {
        "access-token": ACCESS_TOKEN,
        "client-id": CLIENT_ID,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def err_json(e):
    """Turn an exception into a short, readable message for the page."""
    code, msg = "error", str(e)[:250]
    if not CLIENT_ID or not ACCESS_TOKEN:
        code = "token"
        msg = "Dhan keys are missing. Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Render > Environment."
    elif isinstance(e, requests.HTTPError) and e.response is not None:
        s = e.response.status_code
        if s in (401, 403):
            code = "token"
            msg = ("Dhan token expired or invalid. Generate a new token and update "
                   "DHAN_ACCESS_TOKEN in Render > Environment, then redeploy.")
        elif s == 429:
            code = "rate"
            msg = "Dhan rate limit reached. Wait a few seconds; the page retries by itself."
        else:
            msg = f"Dhan API error {s}: {e.response.text[:200]}"
    elif isinstance(e, (requests.ConnectionError, requests.Timeout)):
        code = "network"
        msg = "Cannot reach the Dhan API right now."
    return jsonify({"error": msg, "code": code}), 500


def get_cfg():
    sym = request.args.get("symbol", "NIFTY").upper()
    return sym, SYMBOLS.get(sym, SYMBOLS["NIFTY"])


def fetch_candles(cfg, interval, days):
    now = datetime.now(IST)
    if interval == "D":
        url = f"{BASE}/charts/historical"
        body = {
            "securityId": cfg["id"],
            "exchangeSegment": SEGMENT,
            "instrument": "INDEX",
            "expiryCode": 0,
            "fromDate": (now - timedelta(days=days)).strftime("%Y-%m-%d"),
            "toDate": now.strftime("%Y-%m-%d"),
        }
    else:
        url = f"{BASE}/charts/intraday"
        body = {
            "securityId": cfg["id"],
            "exchangeSegment": SEGMENT,
            "instrument": "INDEX",
            "interval": interval,
            "fromDate": (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15:00"),
            "toDate": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
    r = requests.post(url, json=body, headers=headers(), timeout=20)
    r.raise_for_status()
    d = r.json()
    ts = d.get("timestamp", [])
    out = []
    for i, t in enumerate(ts):
        out.append(
            {
                "time": int(t) + 19800,  # chart shows UTC, shift to IST
                "open": d["open"][i],
                "high": d["high"][i],
                "low": d["low"][i],
                "close": d["close"][i],
            }
        )
    return out


def track_changes(key, rows, min_abs):
    """Compare current OI with a ~5 min old snapshot and log big adds/unwinds."""
    now = time.time()
    cur = {r["strike"]: (r["ce_oi"], r["pe_oi"]) for r in rows}
    with _lock:
        st = _state.setdefault(key, {"hist": deque(), "events": [], "last": {}})
        base = None
        for t, snap in st["hist"]:
            if now - t >= BASE_AGE:
                base = snap  # newest snapshot that is old enough
        if base:
            stamp = datetime.now(IST).strftime("%H:%M")
            for strike, (ce, pe) in cur.items():
                if strike not in base:
                    continue
                for side, new, old in (("CE", ce, base[strike][0]), ("PE", pe, base[strike][1])):
                    if old <= 0:
                        continue
                    delta = new - old
                    pct = delta / old * 100
                    if abs(delta) < min_abs or abs(pct) < MIN_PCT:
                        continue
                    k = (strike, side)
                    if now - st["last"].get(k, 0) < BASE_AGE:
                        continue
                    st["last"][k] = now
                    st["events"].insert(
                        0,
                        {
                            "time": stamp,
                            "ts": int(now),
                            "strike": strike,
                            "side": side,
                            "type": "add" if delta > 0 else "unwind",
                            "delta": delta,
                            "pct": round(pct, 1),
                        },
                    )
            del st["events"][80:]
        st["hist"].append((now, cur))
        while st["hist"] and now - st["hist"][0][0] > 900:
            st["hist"].popleft()
        return list(st["events"])


def get_expiries(cfg):
    r = requests.post(
        f"{BASE}/optionchain/expirylist",
        json={"UnderlyingScrip": int(cfg["id"]), "UnderlyingSeg": SEGMENT},
        headers=headers(), timeout=20,
    )
    r.raise_for_status()
    return r.json()["data"]


def fetch_option_chain(symbol, cfg, expiry_req):
    expiries = cached(("exp", symbol), 600, lambda: get_expiries(cfg))
    expiry = expiry_req if expiry_req in expiries else expiries[0]
    r = requests.post(
        f"{BASE}/optionchain",
        json={"UnderlyingScrip": int(cfg["id"]), "UnderlyingSeg": SEGMENT, "Expiry": expiry},
        headers=headers(), timeout=20,
    )
    r.raise_for_status()
    data = r.json()["data"]
    spot = data["last_price"]
    rows = []
    for strike, leg in data["oc"].items():
        ce, pe = leg.get("ce", {}), leg.get("pe", {})
        rows.append(
            {
                "strike": float(strike),
                "ce_oi": ce.get("oi", 0),
                "pe_oi": pe.get("oi", 0),
                "ce_prev": ce.get("previous_oi", 0),
                "pe_prev": pe.get("previous_oi", 0),
            }
        )
    rows.sort(key=lambda x: x["strike"])
    atm = min(rows, key=lambda x: abs(x["strike"] - spot))["strike"]
    rows = [x for x in rows if abs(x["strike"] - atm) <= cfg["span"]]

    tot_ce = sum(x["ce_oi"] for x in rows)
    tot_pe = sum(x["pe_oi"] for x in rows)
    pcr = round(tot_pe / tot_ce, 2) if tot_ce else 0

    best, pain_strike = None, None
    for s in rows:
        pain = sum(max(0, s["strike"] - x["strike"]) * x["ce_oi"] +
                   max(0, x["strike"] - s["strike"]) * x["pe_oi"] for x in rows)
        if best is None or pain < best:
            best, pain_strike = pain, s["strike"]

    events = track_changes((symbol, expiry), rows, cfg["min_abs"])
    return {
        "symbol": symbol, "name": cfg["name"], "step": cfg["step"],
        "expiry": expiry, "expiries": expiries[:12],
        "spot": spot, "pcr": pcr, "max_pain": pain_strike,
        "support": max(rows, key=lambda x: x["pe_oi"])["strike"],
        "resistance": max(rows, key=lambda x: x["ce_oi"])["strike"],
        "rows": rows,
        "events": events,
    }


@app.route("/api/candles")
def api_candles():
    symbol, cfg = get_cfg()
    interval = request.args.get("interval", "5")
    days = int(request.args.get("days", 5))
    try:
        return jsonify(cached(("c", symbol, interval, days), 15,
                              lambda: fetch_candles(cfg, interval, days)))
    except Exception as e:
        return err_json(e)


@app.route("/api/oi")
def api_oi():
    symbol, cfg = get_cfg()
    expiry = request.args.get("expiry", "")
    try:
        return jsonify(cached(("oi", symbol, expiry), 30,   # API limit: 1 req / 3s
                              lambda: fetch_option_chain(symbol, cfg, expiry)))
    except Exception as e:
        return err_json(e)


@app.route("/health")
def health():
    return "ok"


PAGE = """
<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Index OI Profile</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 body{margin:0;background:#0f1115;color:#e6e6e6;font-family:system-ui,sans-serif}
 header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid #222}
 button,select{background:#1b1f27;color:#ddd;border:1px solid #333;padding:6px 12px;border-radius:6px;font-size:14px}
 button.on{background:#4f46e5;border-color:#4f46e5}
 #wrap{position:relative;height:62vh;min-height:360px}
 #chart{position:absolute;inset:0}
 #ov{position:absolute;left:0;top:0;pointer-events:none;z-index:3}
 .stats{display:flex;flex-wrap:wrap;gap:10px;padding:10px 14px}
 .card{background:#161a21;border:1px solid #262b35;border-radius:8px;padding:8px 12px}
 .card b{display:block;font-size:18px}
 .card span{font-size:12px;color:#8b93a1}
 .legend{font-size:12px;color:#8b93a1;padding:4px 14px}
 #err{display:none;margin:8px 14px 0;padding:8px 12px;border-radius:8px;background:#3b1212;border:1px solid #7f1d1d;color:#fecaca;font-size:14px}
 .alerts{margin:6px 14px 0;background:#161a21;border:1px solid #262b35;border-radius:8px;padding:8px 12px}
 .alerts h3{margin:2px 0 6px;font-size:15px}
 .al{padding:6px 0;border-top:1px solid #222;font-size:13px;line-height:1.35}
 .al:first-of-type{border-top:0}
 .bull{color:#4ade80}.bear{color:#f87171}
 .tables{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;padding:10px 14px 24px}
 .tbl{background:#161a21;border:1px solid #262b35;border-radius:8px;padding:8px 10px}
 .tbl h3{margin:2px 0 8px;font-size:15px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th{color:#8b93a1;font-weight:500;text-align:left;padding:4px 3px}
 td{padding:5px 3px;border-top:1px solid #222}
 .ce{color:#f87171}.pe{color:#4ade80}.dim{color:#8b93a1}
</style></head><body>
<header>
 <select id="symSel"><option value="NIFTY">NIFTY 50</option><option value="BANKNIFTY">BANK NIFTY</option><option value="SENSEX">SENSEX</option></select>
 <select id="expSel"></select>
 <span id="px"></span>
 <span style="flex:1"></span>
 <button id="oiBtn" class="on">OI Profile</button>
 <button id="modeBtn">Total OI</button>
 <button data-i="1">1m</button><button data-i="5" class="on">5m</button>
 <button data-i="15">15m</button><button data-i="60">1h</button><button data-i="D">1D</button>
</header>
<div id="err"></div>
<div id="wrap"><div id="chart"></div><canvas id="ov"></canvas></div>
<div class="legend" id="legend"></div>
<div class="stats" id="stats"></div>
<div class="alerts"><h3>Direction: Option OI Unwinding</h3><div id="dir" class="dim">Loading...</div></div>
<div class="alerts"><h3>Alerts: OI vs Price</h3><div id="alerts" class="dim">Loading...</div></div>
<div class="tables">
 <div class="tbl"><h3 class="pe">OI Addition</h3><table id="addT"></table></div>
 <div class="tbl"><h3 class="ce">OI Unwinding</h3><table id="unwT"></table></div>
</div>
<script>
let symbol="NIFTY", expiry="", step=50, interval="5", oiRows=[], showOI=true, mode="tot",
    candles=[], lastD=null, lastDay=[], needFit=false, lines=[];
const errs={c:'',o:''};
function savedExp(){try{return localStorage.getItem('exp_'+symbol)||''}catch(e){return ''}}
expiry=savedExp();  // remember the expiry you picked (falls back to nearest if it has expired)
const box=document.getElementById('chart');
const chart=LightweightCharts.createChart(box,{
 layout:{background:{color:'#0f1115'},textColor:'#cbd5e1'},
 grid:{vertLines:{color:'#1a1e26'},horzLines:{color:'#1a1e26'}},
 timeScale:{timeVisible:true,secondsVisible:false},
 rightPriceScale:{borderColor:'#333'}});
const candle=chart.addCandlestickSeries({upColor:'#22c55e',downColor:'#ef4444',borderVisible:false,wickUpColor:'#22c55e',wickDownColor:'#ef4444'});
new ResizeObserver(()=>chart.applyOptions({width:box.clientWidth,height:box.clientHeight})).observe(box);

const cv=document.getElementById('ov'),ctx=cv.getContext('2d');
const fmt=n=>{const a=Math.abs(n);return a>=1e5?(n/1e5).toFixed(2)+'L':(n/1e3).toFixed(0)+'K'};
const pc=(a,b)=>b?(a-b)/b*100:0;

function showErr(){
 const el=document.getElementById('err');
 const t=[...new Set([errs.c,errs.o].filter(Boolean))].join('  |  ');
 el.textContent=t;el.style.display=t?'block':'none';
}

function legend(){
 document.getElementById('legend').textContent = mode==='tot'
  ? 'OI profile (total): red = Call OI, green = Put OI. Label = strike and % change vs previous day. Lines: Support, Resistance, Max Pain.'
  : 'OI change profile (vs prev day): right side = OI added, left side = OI unwound (minus %). Red = Calls, green = Puts.';
}
legend();

function tag(txt,x,y,align,col){
 ctx.textAlign=align;
 const wd=ctx.measureText(txt).width;
 const bx=align==='right'?x-wd-3:x-3;
 ctx.fillStyle='rgba(15,17,21,0.78)';ctx.fillRect(bx,y-7,wd+6,14);
 ctx.fillStyle=col;ctx.fillText(txt,x,y);
}

function draw(){
 const w=box.clientWidth,h=box.clientHeight,dpr=window.devicePixelRatio||1;
 if(cv.width!==Math.round(w*dpr)||cv.height!==Math.round(h*dpr)){
  cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr);cv.style.width=w+'px';cv.style.height=h+'px';}
 ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
 if(showOI&&oiRows.length){
  let psw=70;try{psw=chart.priceScale('right').width()||70}catch(e){}
  const right=w-psw-2,maxW=w*0.2;
  const ceV=r=>mode==='tot'?r.ce_oi:r.ce_oi-r.ce_prev;
  const peV=r=>mode==='tot'?r.pe_oi:r.pe_oi-r.pe_prev;
  const mx=Math.max(...oiRows.map(r=>Math.max(Math.abs(ceV(r)),Math.abs(peV(r)))))||1;
  const top=[...oiRows].sort((a,b)=>Math.max(Math.abs(ceV(b)),Math.abs(peV(b)))-Math.max(Math.abs(ceV(a)),Math.abs(peV(a)))).slice(0,6).map(r=>r.strike);
  ctx.font='10px system-ui';ctx.textBaseline='middle';
  const bar=(y,wd,hh,v,col)=>{
   if(wd<0.5)return;
   if(mode==='tot'||v>=0){ctx.fillStyle=col+(mode==='tot'?'0.55)':'0.8)');ctx.fillRect(right-wd,y,wd,hh);}
   else{ // unwinding: drawn from the LEFT edge
    ctx.fillStyle=col+'0.45)';ctx.fillRect(0,y,wd,hh);
    ctx.strokeStyle=col+'1)';ctx.lineWidth=1.2;ctx.strokeRect(0.6,y+0.6,wd-1.2,hh-1.2);}
  };
  for(const r of oiRows){
   const y=candle.priceToCoordinate(r.strike),y2=candle.priceToCoordinate(r.strike+step);
   if(y==null||y2==null||y<4||y>h-34)continue;
   const bh=Math.max(2,Math.abs(y-y2)/2-1);
   const cv_=ceV(r),pv_=peV(r);
   const cw=Math.abs(cv_)/mx*maxW,pw=Math.abs(pv_)/mx*maxW;
   bar(y-bh,cw,bh,cv_,'rgba(239,68,68,');
   bar(y,pw,bh,pv_,'rgba(34,197,94,');
   if(mode==='chg'){
    if(Math.abs(y-y2)>=18){
     for(const [side,wd,n,p0,yy,col] of [['CE',cw,r.ce_oi,r.ce_prev,y-bh/2,'#fca5a5'],['PE',pw,r.pe_oi,r.pe_prev,y+bh/2,'#86efac']]){
      const p=pc(n,p0);
      if(wd<2||Math.abs(p)<10)continue;
      const txt=`${r.strike} ${side} ${p>=0?'+':''}${p.toFixed(0)}%`;
      if(n<p0)tag(txt,wd+4,yy,'left',col);
      else tag(txt,right-wd-4,yy,'right',col);
     }
    }
   }else if(top.includes(r.strike)){
    const ceBig=Math.abs(cv_)>=Math.abs(pv_);
    const p=ceBig?pc(r.ce_oi,r.ce_prev):pc(r.pe_oi,r.pe_prev);
    const wd=ceBig?cw:pw;
    tag(`${r.strike} ${p>=0?'+':''}${p.toFixed(0)}%`,right-wd-4,ceBig?y-bh/2:y+bh/2,'right',ceBig?'#fca5a5':'#86efac');
   }
  }
 }
 requestAnimationFrame(draw);
}
requestAnimationFrame(draw);

function setLines(d){
 lines.forEach(l=>{try{candle.removePriceLine(l)}catch(e){}});lines=[];
 [[d.support,'#22c55e','Support'],[d.resistance,'#ef4444','Resistance'],[d.max_pain,'#facc15','Max Pain']].forEach(([p,c,t])=>{
  lines.push(candle.createPriceLine({price:p,color:c,lineWidth:1,lineStyle:2,axisLabelVisible:true,title:t}));});
}

async function loadCandles(){
 const days=interval==="D"?250:(interval==="60"?30:5);
 try{
  const r=await fetch(`/api/candles?symbol=${symbol}&interval=${interval}&days=${days}`);
  const d=await r.json();
  if(d.error){errs.c=d.error;showErr();return}
  errs.c='';showErr();
  candles=d;
  candle.setData(d.map(c=>({time:c.time,open:c.open,high:c.high,low:c.low,close:c.close})));
  if(needFit){chart.timeScale().fitContent();needFit=false;}
  const last=d[d.length-1],prev=d[d.length-2]||last,ch=last.close-prev.close;
  document.getElementById('px').innerHTML=`<b>${last.close.toFixed(2)}</b> <span style="color:${ch>=0?'#22c55e':'#ef4444'}">${ch>=0?'+':''}${ch.toFixed(2)}</span>`;
  renderAlerts();renderDir();
 }catch(e){errs.c='Cannot reach the server.';showErr();}
}

function rowsHtml(list){
 if(!list.length)return '<tr><td class="dim">No data yet</td></tr>';
 return '<tr><th>Time</th><th>Strike</th><th>Change</th><th>%</th></tr>'+list.map(x=>
  `<tr><td class="dim">${x.time}</td><td>${x.strike} <span class="${x.side==='CE'?'ce':'pe'}">${x.side}</span></td>`+
  `<td>${x.delta>0?'+':''}${fmt(x.delta)}</td><td class="${x.delta>0?'pe':'ce'}">${x.pct>0?'+':''}${x.pct.toFixed(1)}%</td></tr>`).join('');
}

function renderAlerts(){
 const el=document.getElementById('alerts');
 if(!lastD||candles.length<5){return}
 const n=candles.length,last=candles[n-1].close,ref=candles[n-4].close;
 const mv=pc(last,ref);
 const dir=mv>0.03?'up':(mv<-0.03?'down':'flat');
 const head=`<div class="al dim">Price ${dir==='up'?'rising':dir==='down'?'falling':'flat'} (${mv>=0?'+':''}${mv.toFixed(2)}% over last 3 candles)</div>`;
 if(dir==='flat'){el.innerHTML=head+'<div class="al dim">No clear price trend, so no OI signal.</div>';return}
 const spot=lastD.spot;
 const pool=lastD.events.map(e=>({...e,src:'live'})).concat(lastDay.filter(e=>Math.abs(e.pct)>=15).map(e=>({...e,src:'day'})));
 const sig=[];
 for(const e of pool){
  if(Math.abs(e.strike-spot)>step*6)continue;
  let s=null;
  if(dir==='up'&&e.side==='CE'&&e.delta<0)s=['Short covering','bull','Call OI unwinding while price rises'];
  else if(dir==='up'&&e.side==='PE'&&e.delta>0)s=['Put writing support','bull','Put OI building while price rises'];
  else if(dir==='down'&&e.side==='PE'&&e.delta<0)s=['Long unwinding','bear','Put OI unwinding while price falls'];
  else if(dir==='down'&&e.side==='CE'&&e.delta>0)s=['Call writing resistance','bear','Call OI building while price falls'];
  if(s)sig.push({...e,s});
 }
 sig.sort((a,b)=>(a.src==='live'?0:1)-(b.src==='live'?0:1)||Math.abs(b.pct)-Math.abs(a.pct));
 const seen=new Set(),out=[];
 for(const x of sig){const k=x.strike+x.side;if(seen.has(k))continue;seen.add(k);out.push(x);if(out.length>=5)break;}
 el.innerHTML=head+(out.length?out.map(x=>
  `<div class="al"><b class="${x.s[1]}">${x.s[0]}</b> at ${x.strike} ${x.side}: ${x.s[2]} `+
  `(${x.delta>0?'+':''}${fmt(x.delta)}, ${x.pct>0?'+':''}${x.pct.toFixed(1)}%) <span class="dim">${x.src==='live'?x.time:'vs prev day'}</span></div>`).join('')
  :'<div class="al dim">No OI change near spot confirms this move yet.</div>');
}

function unwindScore(list){
 let c=0,p=0;const parts=[];
 for(const e of list){
  const w=Math.max(0,1-Math.abs(e.strike-lastD.spot)/(step*10));  // strikes near spot count more
  if(!w)continue;
  const v=-e.delta*w;
  if(e.side==='CE')c+=v;else p+=v;
  parts.push({...e,v});
 }
 const t=c+p;
 return {c,p,total:t,score:t?(c-p)/t:0,parts:parts.sort((a,b)=>b.v-a.v).slice(0,3)};
}
function lbl(s){
 if(s.total<=0)return ['No unwinding','dim'];
 if(s.score>=0.25)return ['Bullish','bull'];
 if(s.score<=-0.25)return ['Bearish','bear'];
 return ['Mixed','dim'];
}
function gauge(s){
 const pos=((s.score+1)/2*100).toFixed(0);
 return `<div style="position:relative;height:10px;border-radius:5px;margin:8px 0 6px;background:linear-gradient(90deg,#ef4444,#374151 50%,#22c55e)"><div style="position:absolute;top:-3px;left:calc(${pos}% - 2px);width:4px;height:16px;background:#fff;border-radius:2px"></div></div>`;
}
function dirBlock(title,s){
 const [t,c]=lbl(s);
 const parts=s.parts.map(x=>`${x.strike} ${x.side} ${x.pct>0?'+':''}${x.pct.toFixed(0)}%`).join(', ');
 return `<div class="al"><b>${title}: <span class="${c}">${t}</span></b> `+
  `<span class="dim">(score ${s.score>=0?'+':''}${s.score.toFixed(2)})</span>${s.total>0?gauge(s):''}`+
  `<div class="dim">Call unwinding ${fmt(s.c)} (bullish) vs Put unwinding ${fmt(s.p)} (bearish)`+(parts?` | Top: ${parts}`:'')+`</div></div>`;
}
function renderDir(){
 const el=document.getElementById('dir');
 if(!lastD){el.textContent='Loading...';return}
 const nowS=Date.now()/1000;
 const live=unwindScore(lastD.events.filter(e=>e.type==='unwind'&&e.ts>=nowS-1800));
 const day=unwindScore(lastDay.filter(e=>e.delta<0));
 const sd=spotDir();
 let conf='';
 const main=live.total>0?live:day;
 if(sd&&main.total>0&&Math.abs(main.score)>=0.25){
  const bull=main.score>0;
  if(sd.dir==='flat')conf='Spot is flat, so wait for price to confirm.';
  else if((bull&&sd.dir==='up')||(!bull&&sd.dir==='down'))conf=`Spot is ${sd.dir==='up'?'rising':'falling'}: confirms the ${bull?'bullish':'bearish'} unwinding.`;
  else conf=`Warning: spot is ${sd.dir==='up'?'rising':'falling'} against the ${bull?'bullish':'bearish'} unwinding (diverging).`;
 }
 el.innerHTML=dirBlock('Live (last 30 min)',live)+dirBlock('Day (vs prev day)',day)+(conf?`<div class="al">${conf}</div>`:'')+
  '<div class="al dim">Call unwinding = short covering (bullish). Put unwinding = long unwinding (bearish). Strikes near spot count more. OI additions are ignored. Live needs about 5 minutes of data.</div>';
}
function spotDir(){
 if(candles.length<5)return null;
 const n=candles.length,mv=pc(candles[n-1].close,candles[n-4].close);
 return {mv,dir:mv>0.03?'up':(mv<-0.03?'down':'flat')};
}

async function loadOI(){
 try{
  const r=await fetch(`/api/oi?symbol=${symbol}&expiry=${expiry}`);
  const d=await r.json();
  if(d.error){errs.o=d.error;showErr();return}
  errs.o='';showErr();
  oiRows=d.rows;lastD=d;step=d.step;
  const sel=document.getElementById('expSel');
  sel.innerHTML=d.expiries.map(x=>`<option value="${x}"${x===d.expiry?' selected':''}>Exp ${x}</option>`).join('');
  setLines(d);
  document.getElementById('stats').innerHTML=
   [['Spot',d.spot.toFixed(2)],['PCR',d.pcr],['Max Pain',d.max_pain],['Support (PE OI)',d.support],['Resistance (CE OI)',d.resistance],['Expiry',d.expiry]]
   .map(x=>`<div class="card"><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');
  const day=[];
  for(const x of d.rows){
   for(const [side,n,p] of [['CE',x.ce_oi,x.ce_prev],['PE',x.pe_oi,x.pe_prev]]){
    if(p>0&&n!==p)day.push({time:'vs prev day',strike:x.strike,side,delta:n-p,pct:pc(n,p)});
   }
  }
  lastDay=day;
  const live=d.events;
  const add=live.filter(e=>e.type==='add').slice(0,6);
  const unw=live.filter(e=>e.type==='unwind').slice(0,6);
  const fill=(arr,dir)=>{
   const extra=day.filter(e=>dir>0?e.delta>0:e.delta<0)
    .sort((a,b)=>dir>0?b.pct-a.pct:a.pct-b.pct)
    .filter(e=>!arr.some(a=>a.strike===e.strike&&a.side===e.side));
   return arr.concat(extra.slice(0,Math.max(0,6-arr.length)));
  };
  document.getElementById('addT').innerHTML=rowsHtml(fill(add,1));
  document.getElementById('unwT').innerHTML=rowsHtml(fill(unw,-1));
  renderAlerts();renderDir();
 }catch(e){errs.o='Cannot reach the server.';showErr();}
}

document.getElementById('symSel').onchange=e=>{
 symbol=e.target.value;expiry=savedExp();oiRows=[];lastD=null;lastDay=[];candles=[];
 lines.forEach(l=>{try{candle.removePriceLine(l)}catch(x){}});lines=[];
 document.getElementById('alerts').textContent='Loading...';
 renderDir();
 needFit=true;loadCandles();loadOI();};
document.getElementById('expSel').onchange=e=>{
 expiry=e.target.value;
 try{localStorage.setItem('exp_'+symbol,expiry)}catch(x){}
 loadOI();};
document.getElementById('oiBtn').onclick=e=>{showOI=!showOI;e.target.classList.toggle('on',showOI);};
document.getElementById('modeBtn').onclick=e=>{
 mode=mode==='tot'?'chg':'tot';
 e.target.textContent=mode==='tot'?'Total OI':'OI Change';
 e.target.classList.toggle('on',mode==='chg');legend();};
document.querySelectorAll('button[data-i]').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('button[data-i]').forEach(x=>x.classList.remove('on'));
 b.classList.add('on');interval=b.dataset.i;loadCandles();});
loadCandles();loadOI();
setInterval(loadCandles,15000);setInterval(loadOI,60000);setInterval(renderDir,30000);
</script></body></html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
