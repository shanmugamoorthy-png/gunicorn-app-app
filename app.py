import os
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

CLIENT_ID = os.environ.get("DHAN_CLIENT_ID", "")
ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
BASE = "https://api.dhan.co/v2"
IST = timezone(timedelta(hours=5, minutes=30))

NIFTY_ID = "13"  # NIFTY 50 index security id
SEGMENT = "IDX_I"

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


def ema(values, period):
    k = 2 / (period + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def fetch_candles(interval, days):
    now = datetime.now(IST)
    if interval == "D":
        url = f"{BASE}/charts/historical"
        body = {
            "securityId": NIFTY_ID,
            "exchangeSegment": SEGMENT,
            "instrument": "INDEX",
            "expiryCode": 0,
            "fromDate": (now - timedelta(days=days)).strftime("%Y-%m-%d"),
            "toDate": now.strftime("%Y-%m-%d"),
        }
    else:
        url = f"{BASE}/charts/intraday"
        body = {
            "securityId": NIFTY_ID,
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
    candles = []
    cum_pv = cum_v = 0.0
    last_day = None
    for i, t in enumerate(ts):
        o, h, l, c = d["open"][i], d["high"][i], d["low"][i], d["close"][i]
        v = d["volume"][i] if d.get("volume") else 0
        day = datetime.fromtimestamp(t, IST).date()
        if day != last_day:  # VWAP resets daily
            cum_pv = cum_v = 0.0
            last_day = day
        tp = (h + l + c) / 3
        cum_pv += tp * v
        cum_v += v
        vwap = cum_pv / cum_v if cum_v else c
        candles.append(
            {
                # lightweight-charts shows UTC, so shift to IST
                "time": int(t) + 19800,
                "open": o, "high": h, "low": l, "close": c,
                "volume": v, "vwap": round(vwap, 2),
            }
        )
    closes = [c["close"] for c in candles]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    for i, c in enumerate(candles):
        c["ema20"] = round(e20[i], 2)
        c["ema50"] = round(e50[i], 2)
    return candles


def fetch_option_chain():
    r = requests.post(
        f"{BASE}/optionchain/expirylist",
        json={"UnderlyingScrip": int(NIFTY_ID), "UnderlyingSeg": SEGMENT},
        headers=headers(), timeout=20,
    )
    r.raise_for_status()
    expiry = r.json()["data"][0]
    r = requests.post(
        f"{BASE}/optionchain",
        json={"UnderlyingScrip": int(NIFTY_ID), "UnderlyingSeg": SEGMENT, "Expiry": expiry},
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
    rows = [x for x in rows if abs(x["strike"] - atm) <= 1000]  # +-20 strikes

    tot_ce = sum(x["ce_oi"] for x in rows)
    tot_pe = sum(x["pe_oi"] for x in rows)
    pcr = round(tot_pe / tot_ce, 2) if tot_ce else 0

    # max pain
    best, pain_strike = None, None
    for s in rows:
        pain = sum(max(0, s["strike"] - x["strike"]) * x["ce_oi"] +
                   max(0, x["strike"] - s["strike"]) * x["pe_oi"] for x in rows)
        if best is None or pain < best:
            best, pain_strike = pain, s["strike"]

    return {
        "expiry": expiry, "spot": spot, "pcr": pcr, "max_pain": pain_strike,
        "support": max(rows, key=lambda x: x["pe_oi"])["strike"],
        "resistance": max(rows, key=lambda x: x["ce_oi"])["strike"],
        "rows": rows,
    }


@app.route("/api/candles")
def api_candles():
    interval = request.args.get("interval", "5")
    days = int(request.args.get("days", 5))
    try:
        data = cached(("c", interval, days), 15, lambda: fetch_candles(interval, days))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/oi")
def api_oi():
    try:
        return jsonify(cached("oi", 30, fetch_option_chain))  # API limit: 1 req / 3s
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return "ok"


PAGE = """
<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nifty Advanced Chart</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
 body{margin:0;background:#0f1115;color:#e6e6e6;font-family:system-ui,sans-serif}
 header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid #222}
 button{background:#1b1f27;color:#ddd;border:1px solid #333;padding:6px 12px;border-radius:6px}
 button.on{background:#4f46e5;border-color:#4f46e5}
 #chart{height:60vh;min-height:340px}
 .stats{display:flex;flex-wrap:wrap;gap:10px;padding:10px 14px}
 .card{background:#161a21;border:1px solid #262b35;border-radius:8px;padding:8px 12px}
 .card b{display:block;font-size:18px}
 .card span{font-size:12px;color:#8b93a1}
 .oi{padding:0 14px 20px;height:320px}
 #err{color:#f87171;padding:6px 14px}
 .legend{font-size:12px;color:#8b93a1;padding:0 14px}
</style></head><body>
<header>
 <strong>NIFTY 50</strong>
 <span id="px"></span>
 <span style="flex:1"></span>
 <button data-i="1">1m</button><button data-i="5" class="on">5m</button>
 <button data-i="15">15m</button><button data-i="60">1h</button><button data-i="D">1D</button>
</header>
<div id="err"></div>
<div id="chart"></div>
<div class="legend">Yellow = VWAP, Blue = EMA20, Orange = EMA50</div>
<div class="stats" id="stats"></div>
<div class="oi"><canvas id="oiChart"></canvas></div>
<script>
let interval="5";
const chart=LightweightCharts.createChart(document.getElementById('chart'),{
 layout:{background:{color:'#0f1115'},textColor:'#cbd5e1'},
 grid:{vertLines:{color:'#1a1e26'},horzLines:{color:'#1a1e26'}},
 timeScale:{timeVisible:true,secondsVisible:false},
 rightPriceScale:{borderColor:'#333'}});
const candle=chart.addCandlestickSeries({upColor:'#22c55e',downColor:'#ef4444',borderVisible:false,wickUpColor:'#22c55e',wickDownColor:'#ef4444'});
const vwap=chart.addLineSeries({color:'#facc15',lineWidth:2,priceLineVisible:false});
const e20=chart.addLineSeries({color:'#38bdf8',lineWidth:1,priceLineVisible:false});
const e50=chart.addLineSeries({color:'#fb923c',lineWidth:1,priceLineVisible:false});
const vol=chart.addHistogramSeries({priceFormat:{type:'volume'},priceScaleId:'v'});
chart.priceScale('v').applyOptions({scaleMargins:{top:0.82,bottom:0}});
new ResizeObserver(()=>chart.applyOptions({width:document.getElementById('chart').clientWidth})).observe(document.getElementById('chart'));

async function loadCandles(){
 const days=interval==="D"?250:(interval==="60"?30:5);
 const r=await fetch(`/api/candles?interval=${interval}&days=${days}`);
 const d=await r.json();
 if(d.error){document.getElementById('err').textContent=d.error;return}
 document.getElementById('err').textContent='';
 candle.setData(d.map(c=>({time:c.time,open:c.open,high:c.high,low:c.low,close:c.close})));
 vwap.setData(interval==="D"?[]:d.map(c=>({time:c.time,value:c.vwap})));
 e20.setData(d.map(c=>({time:c.time,value:c.ema20})));
 e50.setData(d.map(c=>({time:c.time,value:c.ema50})));
 vol.setData(d.map(c=>({time:c.time,value:c.volume,color:c.close>=c.open?'#22c55e55':'#ef444455'})));
 const last=d[d.length-1],prev=d[d.length-2]||last;
 const ch=last.close-prev.close;
 document.getElementById('px').innerHTML=`<b>${last.close.toFixed(2)}</b> <span style="color:${ch>=0?'#22c55e':'#ef4444'}">${ch>=0?'+':''}${ch.toFixed(2)}</span>`;
}
let oiChart;
async function loadOI(){
 const r=await fetch('/api/oi');const d=await r.json();
 if(d.error){return}
 document.getElementById('stats').innerHTML=
  [['Spot',d.spot.toFixed(2)],['PCR',d.pcr],['Max Pain',d.max_pain],['Support (PE OI)',d.support],['Resistance (CE OI)',d.resistance],['Expiry',d.expiry]]
  .map(x=>`<div class="card"><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');
 const labels=d.rows.map(x=>x.strike);
 const data={labels,datasets:[
  {label:'Call OI',data:d.rows.map(x=>x.ce_oi),backgroundColor:'#ef4444'},
  {label:'Put OI',data:d.rows.map(x=>x.pe_oi),backgroundColor:'#22c55e'}]};
 if(oiChart){oiChart.data=data;oiChart.update()}
 else oiChart=new Chart(document.getElementById('oiChart'),{type:'bar',data,
  options:{maintainAspectRatio:false,plugins:{legend:{labels:{color:'#cbd5e1'}}},
  scales:{x:{ticks:{color:'#8b93a1'}},y:{ticks:{color:'#8b93a1'}}}}});
}
document.querySelectorAll('button[data-i]').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('button[data-i]').forEach(x=>x.classList.remove('on'));
 b.classList.add('on');interval=b.dataset.i;loadCandles();});
loadCandles();loadOI();
setInterval(loadCandles,15000);setInterval(loadOI,60000);
</script></body></html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
