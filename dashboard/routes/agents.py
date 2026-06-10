import asyncio
import subprocess
import sys
import threading
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, StreamingResponse

from db.state_store import (
    create_agent_run, append_agent_run_log, finish_agent_run, get_agent_run,
)

router = APIRouter()


def _run_agent_thread(agent: str, run_id: int) -> None:
    cmd = [sys.executable, "-m", f"agents.{agent}.main"]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        buf = []
        for line in proc.stdout:
            buf.append(line)
            if len(buf) >= 5:
                append_agent_run_log(run_id, "".join(buf))
                buf.clear()
        if buf:
            append_agent_run_log(run_id, "".join(buf))
        proc.wait()
        finish_agent_run(run_id, "success" if proc.returncode == 0 else "error")
    except Exception as e:
        append_agent_run_log(run_id, f"ERROR: {e}\n")
        finish_agent_run(run_id, "error")


@router.post("/agents/casey/run", response_class=HTMLResponse)
async def run_casey():
    run_id = create_agent_run("casey")
    t = threading.Thread(target=_run_agent_thread, args=("casey", run_id), daemon=True)
    t.start()
    return HTMLResponse(_run_started_html(run_id, "casey"))


@router.post("/agents/oca/run", response_class=HTMLResponse)
async def run_oca():
    run_id = create_agent_run("oca")
    t = threading.Thread(target=_run_agent_thread, args=("oca", run_id), daemon=True)
    t.start()
    return HTMLResponse(_run_started_html(run_id, "oca"))


def _run_started_html(run_id: int, agent: str) -> str:
    return f"""
<div id="run-status-{agent}" class="flex items-center gap-2 text-sm text-slate-600">
  <svg class="w-4 h-4 animate-spin text-blue-500" fill="none" viewBox="0 0 24 24">
    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path>
  </svg>
  Running {agent.upper()}…
</div>
<script>
  (function() {{
    const logEl = document.getElementById('agent-log');
    const src = new EventSource('/agents/run/{run_id}/stream');
    src.onmessage = function(e) {{
      if (e.data === '[DONE]') {{
        src.close();
        const statusEl = document.getElementById('run-status-{agent}');
        if (statusEl) statusEl.innerHTML = '<span class="text-green-600 font-medium">✓ Run complete</span>';
        return;
      }}
      if (e.data === '[ERROR]') {{
        src.close();
        const statusEl = document.getElementById('run-status-{agent}');
        if (statusEl) statusEl.innerHTML = '<span class="text-red-600 font-medium">✗ Run failed</span>';
        return;
      }}
      if (logEl) {{
        logEl.textContent += e.data + '\\n';
        logEl.scrollTop = logEl.scrollHeight;
      }}
    }};
    src.onerror = function() {{ src.close(); }};
  }})();
</script>
"""


@router.get("/agents/run/{run_id}/stream")
async def stream_run(run_id: int):
    async def event_generator():
        sent_len = 0
        max_polls = 720   # 6 minutes max (720 × 500ms)
        polls = 0
        while polls < max_polls:
            run = get_agent_run(run_id)
            if run is None:
                yield "data: [ERROR]\n\n"
                return
            log = run.get("log") or ""
            if len(log) > sent_len:
                chunk = log[sent_len:]
                sent_len += len(chunk)
                for line in chunk.splitlines():
                    yield f"data: {line}\n\n"
            if run["status"] in ("success", "error"):
                sentinel = "[DONE]" if run["status"] == "success" else "[ERROR]"
                yield f"data: {sentinel}\n\n"
                return
            await asyncio.sleep(0.5)
            polls += 1
        yield "data: [ERROR]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
