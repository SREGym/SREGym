import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pyfiglet
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastmcp import FastMCP
from fastmcp.server.http import create_sse_app
from pydantic import BaseModel
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from starlette.routing import Mount
from uvicorn import Config, Server

from sregym.conductor.event_bus import get_event_bus

_conductor = None

submit_mcp = FastMCP("Submit MCP Server")


@submit_mcp.tool(name="submit")
async def submit_via_conductor(ans: str) -> dict[str, str]:
    """Submit task result to benchmark

    Args:
        ans (str): task result that the agent submits

    Returns:
        dict[str]: acknowledgment of submission status
    """
    if _conductor is None or _conductor.submission_stage not in {"diagnosis", "mitigation", "resolution"}:
        stage = _conductor.submission_stage if _conductor else None
        if stage == "done" and _conductor is not None:
            return {
                "status": "done",
                "text": "All stages have been completed and graded. No further submissions are needed.",
            }
        return {"status": "error", "text": f"Cannot submit at stage: {stage!r}"}

    wrapped = f"```\nsubmit({repr(ans)})\n```"
    max_wait = 60
    for attempt in range(max_wait):
        try:
            await _conductor.submit(wrapped)
            return {"status": "200", "text": "Submission received"}
        except RuntimeError:
            if attempt < max_wait - 1:
                await asyncio.sleep(1)
                continue
            return {"status": "error", "text": "Previous stage is still being evaluated. Try again later."}
        except Exception as e:
            return {"status": "error", "text": f"Grading error: {e}"}


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SREGym Live</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'SF Mono','Fira Code',monospace;background:#0a0e17;color:#e2e8f0;height:100vh;display:grid;grid-template-rows:auto 1fr auto;overflow:hidden}
header{background:#0d1117;border-bottom:1px solid #1f2937;padding:10px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.logo{font-size:16px;font-weight:700;color:#3b82f6;letter-spacing:1px;white-space:nowrap}
.hdr-info{font-size:12px;color:#64748b}
.hdr-info span{color:#e2e8f0;font-weight:600}
.stage-pill{padding:3px 12px;border-radius:20px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-left:auto;white-space:nowrap;background:#1e293b;color:#475569;transition:all .3s}
main{display:grid;grid-template-columns:1fr 320px;overflow:hidden}
.stream-panel{display:flex;flex-direction:column;border-right:1px solid #1f2937;overflow:hidden}
.panel-hdr{background:#0d1117;padding:8px 16px;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#4b5563;border-bottom:1px solid #1f2937;display:flex;justify-content:space-between;align-items:center}
.stream{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:7px;scroll-behavior:smooth}
.msg{border-radius:7px;padding:9px 11px;font-size:12px;line-height:1.55;border-left:3px solid transparent;background:#111827;animation:fi .18s ease}
@keyframes fi{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}
.msg.ai{border-left-color:#8b5cf6}
.msg.tool-call{border-left-color:#3b82f6}
.msg.tool-result{border-left-color:#0ea5e9;background:#0b1523}
.msg.human{border-left-color:#475569}
.msg.submitted{border-left-color:#f59e0b;background:#120e00}
.msg.sep{background:transparent;border:none;text-align:center;font-size:10px;color:#374151;text-transform:uppercase;letter-spacing:1px;padding:2px 0}
.role{font-size:10px;text-transform:uppercase;letter-spacing:.8px;font-weight:700;margin-bottom:3px;display:flex;justify-content:space-between}
.role.ai{color:#a78bfa}.role.tool-call{color:#60a5fa}.role.tool-result{color:#38bdf8}.role.human{color:#64748b}.role.submitted{color:#fbbf24}
.step{color:#374151;font-weight:400}
.body{color:#cbd5e1;white-space:pre-wrap;word-break:break-word;max-height:180px;overflow:hidden}
.body.open{max-height:none}
.more{background:none;border:none;color:#3b82f6;cursor:pointer;font-size:11px;font-family:inherit;margin-top:2px}
.side{display:flex;flex-direction:column;overflow-y:auto;padding:10px;gap:10px}
.card{background:#111827;border-radius:8px;padding:12px;border:1px solid #1f2937}
.card-title{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#4b5563;margin-bottom:8px}
.result{font-size:20px;font-weight:700;margin-bottom:4px}
.result.ok{color:#10b981}.result.fail{color:#ef4444}.result.pending{color:#374151}
.metric{font-size:11px;color:#64748b;margin-top:2px}
.metric span{color:#94a3b8}
.irow{display:flex;justify-content:space-between;font-size:12px;padding:2px 0}
.ilabel{color:#4b5563}.ival{color:#e2e8f0;font-weight:500}
.steps{display:flex;align-items:center;gap:3px;margin-top:6px;flex-wrap:wrap}
.step-node{flex:1;min-width:60px;padding:4px 6px;border-radius:4px;font-size:10px;text-align:center;text-transform:uppercase;letter-spacing:.4px;font-weight:600;background:#1a2030;color:#374151;border:1px solid #1f2937;transition:all .3s}
.step-node.active{background:#0f2040;color:#60a5fa;border-color:#3b82f6}
.step-node.done{background:#031a0e;color:#34d399;border-color:#065f46}
.sep-arrow{color:#374151;font-size:10px}
footer{background:#0d1117;border-top:1px solid #1f2937;padding:6px 18px;display:flex;align-items:center;gap:12px;font-size:11px;color:#4b5563}
.dot{width:7px;height:7px;border-radius:50%;background:#1f2937;display:inline-block;flex-shrink:0}
.dot.on{background:#10b981;box-shadow:0 0 5px #10b981}.dot.busy{background:#f59e0b}
.clr{margin-left:auto;background:#1a2030;border:1px solid #1f2937;color:#64748b;padding:2px 9px;border-radius:4px;cursor:pointer;font-size:11px;font-family:inherit}
.clr:hover{background:#1f2937;color:#e2e8f0}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:#1f2937;border-radius:3px}
</style>
</head>
<body>
<header>
  <div class="logo">&#9651; SREGym Live</div>
  <div class="hdr-info">Problem: <span id="h-pid">&#8212;</span></div>
  <div class="hdr-info">App: <span id="h-app">&#8212;</span></div>
  <div class="hdr-info">NS: <span id="h-ns">&#8212;</span></div>
  <div class="stage-pill" id="h-stage">IDLE</div>
</header>
<main>
  <div class="stream-panel">
    <div class="panel-hdr">Agent Reasoning<span id="msg-count">0 messages</span></div>
    <div class="stream" id="stream"></div>
  </div>
  <div class="side">
    <div class="card">
      <div class="card-title">Run Info</div>
      <div class="irow"><span class="ilabel">Problem</span><span class="ival" id="s-pid">&#8212;</span></div>
      <div class="irow"><span class="ilabel">App</span><span class="ival" id="s-app">&#8212;</span></div>
      <div class="irow"><span class="ilabel">Namespace</span><span class="ival" id="s-ns">&#8212;</span></div>
      <div class="irow"><span class="ilabel">Stage</span><span class="ival" id="s-stage">&#8212;</span></div>
    </div>
    <div class="card">
      <div class="card-title">Stage Progress</div>
      <div class="steps" id="steps"></div>
    </div>
    <div class="card">
      <div class="card-title">Diagnosis</div>
      <div class="result pending" id="d-res">&#8212;</div>
      <div class="metric" id="d-acc"></div>
      <div class="metric" id="d-ttl"></div>
    </div>
    <div class="card">
      <div class="card-title">Mitigation</div>
      <div class="result pending" id="m-res">&#8212;</div>
      <div class="metric" id="m-ttm"></div>
    </div>
  </div>
</main>
<footer>
  <div class="dot" id="dot"></div>
  <span id="ws-lbl">Connecting&#8230;</span>
  <span>&#8226;</span>
  <span id="ev-count">0 events</span>
  <span>&#8226;</span>
  <span id="last-ev">&#8212;</span>
  <button class="clr" onclick="clearStream()">Clear</button>
</footer>
<script>
const STAGE_COLORS={setup:['#1e293b','#64748b'],diagnosis:['#1a1100','#f59e0b'],mitigation:['#0a1628','#3b82f6'],tearing_down:['#1a0a0a','#f87171'],done:['#001a0d','#10b981']};
let S={stages:[],cur:null,done:[],evCount:0,msgCount:0,seen:new Set()};
let ws,retryDelay=1000;

function connect(){
  setConn('busy');
  ws=new WebSocket('ws://'+location.host+'/ws');
  ws.onopen=()=>{setConn('on');retryDelay=1000};
  ws.onmessage=e=>{
    try{const ev=JSON.parse(e.data);S.evCount++;
      document.getElementById('ev-count').textContent=S.evCount+' events';
      document.getElementById('last-ev').textContent=ev.type;
      route(ev);}catch(_){}
  };
  ws.onclose=()=>{setConn('off');setTimeout(connect,retryDelay);retryDelay=Math.min(retryDelay*1.5,10000)};
  ws.onerror=()=>ws.close();
}

function setConn(s){
  document.getElementById('dot').className='dot'+(s==='on'?' on':s==='busy'?' busy':'');
  document.getElementById('ws-lbl').textContent={on:'Connected',busy:'Connecting\u2026',off:'Reconnecting\u2026'}[s];
}

function route(ev){
  const d=ev.data;
  if(ev.type==='problem_start'){reset(d.problem_id);}
  else if(ev.type==='app_deployed'||ev.type==='problem_ready'){updateInfo(d);if(d.stages)initStages(d.stages);}
  else if(ev.type==='stage_change'){setStage(d.stage);}
  else if(ev.type==='submission_received'){addSubmit(d.stage,d.solution);}
  else if(ev.type==='oracle_result'){updateOracle(d);}
  else if(ev.type==='teardown'){setStage('tearing_down');}
  else if(ev.type==='problem_done'){setStage('done');if(d.results)finalResults(d.results);}
  else if(ev.type==='agent_event'){agentEvent(d);}
}

function reset(pid){
  S.seen=new Set();S.done=[];clearStream();
  set('h-pid',pid);set('s-pid',pid);
  set('d-res','\u2014');set('d-acc','');set('d-ttl','');
  set('m-res','\u2014');set('m-ttm','');
  document.getElementById('d-res').className='result pending';
  document.getElementById('m-res').className='result pending';
  setStage('setup');
}

function updateInfo(d){
  if(d.app_name){set('h-app',d.app_name);set('s-app',d.app_name);}
  if(d.namespace){set('h-ns',d.namespace);set('s-ns',d.namespace);}
  if(d.problem_id){set('h-pid',d.problem_id);set('s-pid',d.problem_id);}
}

function initStages(stages){
  S.stages=stages;
  const el=document.getElementById('steps');
  el.innerHTML=stages.map((s,i)=>(i>0?'<span class="sep-arrow">\u203a</span>':'')+`<div class="step-node" id="sn-${s}">${s}</div>`).join('');
}

function setStage(stage){
  S.cur=stage;
  set('s-stage',stage);
  const c=STAGE_COLORS[stage]||['#1e293b','#475569'];
  const pill=document.getElementById('h-stage');
  pill.style.background=c[0];pill.style.color=c[1];
  pill.textContent=stage.toUpperCase().replace('_',' ');
  S.stages.forEach(s=>{
    const el=document.getElementById('sn-'+s);if(!el)return;
    el.className='step-node'+(s===stage?' active':S.done.includes(s)?' done':'');
  });
}

function updateOracle(d){
  if(d.stage==='diagnosis'){
    const ok=d.success;
    const el=document.getElementById('d-res');
    el.textContent=ok?'\u2713 Correct':'\u2717 Incorrect';
    el.className='result '+(ok?'ok':'fail');
    if(d.accuracy!=null)set('d-acc','Accuracy: <span>'+d.accuracy+'%</span>');
    if(d.ttl!=null)set('d-ttl','TTL: <span>'+d.ttl.toFixed(1)+'s</span>');
    if(!S.done.includes('diagnosis'))S.done.push('diagnosis');
    setStage(S.cur);
  }else if(d.stage==='mitigation'){
    const ok=d.success;
    const el=document.getElementById('m-res');
    el.textContent=ok?'\u2713 Restored':'\u2717 Failed';
    el.className='result '+(ok?'ok':'fail');
    if(d.ttm!=null)set('m-ttm','TTM: <span>'+d.ttm.toFixed(1)+'s</span>');
    if(!S.done.includes('mitigation'))S.done.push('mitigation');
    setStage(S.cur);
  }
}

function finalResults(r){
  if(r.Diagnosis)updateOracle({stage:'diagnosis',...r.Diagnosis,ttl:r.TTL});
  if(r.Mitigation)updateOracle({stage:'mitigation',...r.Mitigation,ttm:r.TTM});
}

function agentEvent(data){
  if(!data)return;
  if(data.type==='event'){
    const key=data.stage+':'+data.event_index;
    if(S.seen.has(key))return;
    S.seen.add(key);
    if(data.last_message)addMsg(data.last_message,data.num_steps);
  }else if(data.type==='stage_start'){
    addSep(data.stage);
  }
}

function addSep(stage){
  const d=document.createElement('div');
  d.className='msg sep';d.textContent='\u2500\u2500 '+stage+' \u2500\u2500';
  push(d);
}

function addMsg(msg,step){
  if(!msg)return;
  const t=(msg.type||'').toLowerCase();
  const content=msg.content||'';
  let cls='human',roleLabel='Human';
  if(t.includes('ai')||t.includes('assistant')){
    cls='ai';roleLabel='\ud83e\udd16 AI';
    if(content.includes('tool_calls')||content.includes('tool_use')){cls='tool-call';roleLabel='\ud83d\udd27 Tool Call';}
  }else if(t.includes('tool')){cls='tool-result';roleLabel='\ud83d\udccb Result';}
  else if(t.includes('system')){cls='human';roleLabel='\u2699 System';}
  const id='mc'+S.msgCount;
  const trunc=content.length>700;
  const shown=trunc?content.slice(0,700)+'\u2026':content;
  const d=document.createElement('div');
  d.className='msg '+cls;
  d.innerHTML='<div class="role '+cls+'">'+roleLabel+(step!=null?'<span class="step">step '+step+'</span>':'')+'</div>'
    +'<div class="body" id="'+id+'">'+esc(shown)+'</div>'
    +(trunc?'<button class="more" onclick="expand(\''+id+'\',this,'+JSON.stringify(content)+')">Show more</button>':'');
  S.msgCount++;push(d);
  document.getElementById('msg-count').textContent=S.msgCount+' messages';
}

function addSubmit(stage,sol){
  const d=document.createElement('div');
  d.className='msg submitted';
  d.innerHTML='<div class="role submitted">\ud83d\udce4 Submitted ('+esc(stage||'?')+')</div><div class="body open">'+esc(sol||'\u2014')+'</div>';
  S.msgCount++;push(d);
  document.getElementById('msg-count').textContent=S.msgCount+' messages';
}

function expand(id,btn,full){document.getElementById(id).textContent=full;document.getElementById(id).className='body open';btn.remove();}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function set(id,html){const el=document.getElementById(id);if(el)el.innerHTML=html;}
function push(el){const s=document.getElementById('stream');s.appendChild(el);s.scrollTop=s.scrollHeight;}
function clearStream(){document.getElementById('stream').innerHTML='';S.msgCount=0;S.seen=new Set();document.getElementById('msg-count').textContent='0 messages';}

connect();
</script>
</body>
</html>"""


async def _watch_jsonl_files() -> None:
    """Tail AGENT_LOGS_DIR for new JSONL lines and emit them as agent_event."""
    bus = get_event_bus()
    watched: dict[str, int] = {}

    while True:
        logs_dir = os.environ.get("AGENT_LOGS_DIR")
        if logs_dir:
            logs_path = Path(logs_dir)
            if logs_path.is_dir():
                for jsonl_path in logs_path.rglob("*.jsonl"):
                    key = str(jsonl_path)
                    offset = watched.get(key, 0)
                    try:
                        with jsonl_path.open("r") as f:
                            f.seek(offset)
                            for raw in f:
                                raw = raw.strip()
                                if raw:
                                    try:
                                        bus.emit("agent_event", json.loads(raw))
                                    except json.JSONDecodeError:
                                        pass
                            watched[key] = f.tell()
                    except OSError:
                        pass
        await asyncio.sleep(0.5)


@asynccontextmanager
async def _lifespan(app):
    bus = get_event_bus()
    bus.set_loop(asyncio.get_event_loop())
    watcher = asyncio.create_task(_watch_jsonl_files())
    yield
    watcher.cancel()


app = FastAPI(
    lifespan=_lifespan,
    routes=[
        Mount("/submit_mcp", app=create_sse_app(submit_mcp, "/messages/", "/sse")),
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Namespaces excluded from the galaxy dashboard (infrastructure, not workloads)
_SYSTEM_NAMESPACES = frozenset({
    "kube-system", "kube-public", "kube-node-lease", "default",
    "chaos-mesh", "khaos", "monitoring", "cert-manager",
    "metallb-system", "ingress-nginx", "local-path-storage",
})

_server: Server | None = None
_shutdown_event = threading.Event()

logger = logging.getLogger("all.sregym.conductor_api")


class _ShutdownNoiseFilter(logging.Filter):
    """Suppress expected CancelledError tracebacks from uvicorn during shutdown."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Case 1: exc_info carries the exception object directly.
        if record.exc_info and record.exc_info[1] is not None:
            import asyncio

            if isinstance(record.exc_info[1], asyncio.CancelledError):
                return False
        # Case 2: uvicorn formats the traceback as a plain string message
        # (e.g. logger.error(traceback.format_exc())) with no exc_info.
        # The string will end with "asyncio.exceptions.CancelledError".
        return "CancelledError" not in record.getMessage()


def request_shutdown():
    """
    Signal the API server to shut down.
    Safe to call from any thread and idempotent.
    """
    logger.warning("Shutting down API server...")

    # Suppress expected CancelledError noise from uvicorn tearing down
    # long-lived SSE connections during shutdown
    for name in ("uvicorn.error", "uvicorn"):
        logging.getLogger(name).addFilter(_ShutdownNoiseFilter())

    _shutdown_event.set()
    if _server is not None:
        # force_exit skips waiting for long-lived connections (like MCP SSE)
        # to close gracefully — the agent is already cleaned up at this point
        _server.force_exit = True
        _server.should_exit = True


def set_conductor(c):
    """Inject the shared Conductor instance."""
    global _conductor
    _conductor = c


class SubmitRequest(BaseModel):
    solution: str


@app.post("/submit")
async def submit_solution(req: SubmitRequest):
    allowed = {"diagnosis", "mitigation", "resolution"}
    if _conductor is None or _conductor.submission_stage not in allowed:
        stage = _conductor.submission_stage if _conductor else None
        if stage == "done" and _conductor is not None:
            logger.debug("Submit received at stage 'done' — problem already graded, returning final results")
            return {
                "status": "done",
                "message": "All stages have been completed and graded. No further submissions are needed.",
            }
        logger.error(f"Cannot submit at stage: {stage!r}")
        raise HTTPException(status_code=400, detail=f"Cannot submit at stage: {stage!r}")

    # Use repr() to properly escape special characters in the solution string
    wrapped = f"```\nsubmit({repr(req.solution)})\n```"
    logger.debug(f"Wrapped submit content: {wrapped}")

    # The conductor evaluates submissions asynchronously. If a previous stage
    # is still being evaluated, waiting_for_agent will be False and submit()
    # raises RuntimeError.  Retry for up to 60s to handle this race.
    max_wait = 60
    for attempt in range(max_wait):
        try:
            await _conductor.submit(wrapped)
            return {"status": "200", "message": "Submission received"}
        except RuntimeError:
            if attempt < max_wait - 1:
                logger.debug("Conductor not ready for submission yet, retrying in 1s...")
                await asyncio.sleep(1)
                continue
            logger.error("Conductor did not become ready for submission within timeout")
            raise HTTPException(
                status_code=503,
                detail="Previous stage is still being evaluated. Try again later.",
            ) from None
        except Exception as e:
            logger.error(f"Grading error: {e}")
            raise HTTPException(status_code=400, detail=f"Grading error: {e}") from e


@app.get("/status")
async def get_status():
    if _conductor is None:
        logger.error("No problem has been started")
        raise HTTPException(status_code=400, detail="No problem has been started")
    stage = _conductor.submission_stage
    logger.debug(f"API returns Current stage: {stage}")
    return {"stage": stage}


@app.get("/get_app")
async def get_app():
    if _conductor is None:
        logger.error("No problem has been started")
        raise HTTPException(status_code=400, detail="No problem has been started")
    app_inst = _conductor.app
    logger.debug(f"API returns App instance: {app_inst}")
    return {"app_name": app_inst.app_name, "namespace": app_inst.namespace, "descriptions": str(app_inst.description)}


@app.get("/get_problem")
async def get_problem():
    if _conductor is None:
        logger.error("No problem has been started")
        raise HTTPException(status_code=400, detail="No problem has been started")
    problem_id = _conductor.problem_id
    logger.debug(f"API returns Problem ID: {problem_id}")
    return {"problem_id": problem_id}


def _pod_status(pod) -> str:
    """Extract a meaningful status string from a V1Pod object."""
    phase = (pod.status and pod.status.phase) or "Unknown"
    if pod.status and pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            if cs.state:
                if cs.state.waiting and cs.state.waiting.reason:
                    return cs.state.waiting.reason
                if cs.state.terminated and cs.state.terminated.reason:
                    return cs.state.terminated.reason
    return phase


@app.get("/cluster/namespaces")
async def cluster_namespaces():
    """Return all non-system namespaces with their pods and statuses."""
    if _conductor is None:
        return {"namespaces": []}

    def _fetch():
        result = []
        try:
            ns_list = _conductor.kubectl.list_namespaces()
            for ns in ns_list.items:
                name = ns.metadata.name
                if name in _SYSTEM_NAMESPACES:
                    continue
                try:
                    pods = _conductor.kubectl.list_pods(name)
                    pod_list = [
                        {"name": p.metadata.name, "status": _pod_status(p)}
                        for p in pods.items
                    ]
                except Exception:
                    pod_list = []
                result.append({"name": name, "pods": pod_list})
        except Exception as e:
            logger.error(f"Error fetching cluster namespaces: {e}")
        return result

    namespaces = await asyncio.to_thread(_fetch)
    return {"namespaces": namespaces}


@app.get("/cluster/fault")
async def cluster_fault():
    """Return the currently active fault from the running problem."""
    if _conductor is None or _conductor.problem is None:
        return {"active": False}
    p = _conductor.problem
    return {
        "active": True,
        "problem_id": _conductor.problem_id,
        "namespace": getattr(p, "namespace", None),
        "root_cause": getattr(p, "root_cause", None),
        "fault_injected": getattr(p, "fault_injected", False),
        "stage": _conductor.submission_stage,
    }


@app.websocket("/ws")
async def websocket_events(ws: WebSocket) -> None:
    """Stream conductor lifecycle events and agent trace to browser clients."""
    await ws.accept()
    bus = get_event_bus()
    q = bus.subscribe()
    try:
        for msg in bus.get_history():
            await ws.send_text(msg)
        while True:
            try:
                text = await asyncio.wait_for(q.get(), timeout=25)
                await ws.send_text(text)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        bus.unsubscribe(q)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Serve the live agent reasoning dashboard."""
    return HTMLResponse(content=_DASHBOARD_HTML)


def run_api(conductor):
    """
    Start the API server and block until request_shutdown() is called.
    """
    global _server
    set_conductor(conductor)
    logger.debug(f"API server is binded to the conductor {conductor}")

    # Load from .env with defaults
    host = os.getenv("API_BIND_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))

    logger.debug(f"API server starting on http://{host}:{port}")

    console = Console()
    art = pyfiglet.figlet_format("SREGym")
    console.print(Panel(art, title="SREGym API Server", subtitle=f"http://{host}:{port}", style="bold green"))
    console.print(
        Markdown(
            """
**Available Endpoints**
- **POST /submit**: `{ "solution": "<your-solution>" }` → grades the current stage
- **GET /status**: returns `{ "stage": "setup" | "diagnosis" | "mitigation" | "resolution" | "tearing_down" | "done" }`
"""
        )
    )

    config = Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=5,
    )
    config.install_signal_handlers = False
    server = Server(config)
    _server = server  # expose to request_shutdown()

    # watcher thread: when _shutdown_event is set, flip server.should_exit
    def _watch():
        _shutdown_event.wait()
        logger.debug("API server shutdown event received")
        server.should_exit = True

    threading.Thread(target=_watch, name="api-shutdown-watcher", daemon=True).start()

    try:
        logger.debug("API server is running")
        server.run()  # blocks until should_exit becomes True
    finally:
        # cleanup for potential reuse
        _shutdown_event.clear()
        _server = None
