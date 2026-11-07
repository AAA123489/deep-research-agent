"""HTTP 接口。

    cd backend
    python -m app.api            # 或者 uvicorn app.api:app --reload

三个端点：

    GET  /api/health   活着没有、数据库在不在
    GET  /api/metrics  能问哪些指标、能按哪些维度分组、每个维度的真实取值
    POST /api/ask      提问，SSE 流式返回每一步和最后的结果

为什么提问用 SSE 而不是直接返回 JSON：这条链路最值钱的地方是「守卫把人拦下来了」
「第一次写错、改了一次才对」这些过程。攒成一个 JSON 返回就全看不见了。

前端在后端目录之外（frontend/），这里顺手把静态文件也挂上，省得再起一个服务。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Iterator

# 直接 `python backend/app/api.py` 时，sys.path[0] 是 backend/app，
# 下面那些 `from app.xxx import` 就找不到了。把 backend/ 补进去。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.env import load_env
from app.graph.executor import Executor
from app.graph.llm import OpenAICompatGenerator
from app.graph.pipeline import stream as run_stream
from app.semantic import load_registry

# 有 .env 就先拿它补上（命令行里已经设的值优先）。必须在读 CHATBI_DB 之前。
load_env()

ROOT = Path(__file__).resolve().parents[2]
SEMANTIC_DIR = Path(__file__).resolve().parent / "semantic"
FRONTEND_DIR = ROOT / "frontend"
DEFAULT_DB = ROOT / "data" / "ecommerce.duckdb"

app = FastAPI(title="电商经营分析 Agent", version="0.1.0")
registry = load_registry(SEMANTIC_DIR)

DB_PATH = Path(os.environ.get("CHATBI_DB") or DEFAULT_DB)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------------------
# 读接口
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    has_key = bool(os.environ.get("LLM_API_KEY"))
    return {
        "status": "ok" if DB_PATH.is_file() else "no_database",
        "database": str(DB_PATH),
        "model_configured": has_key,
        "metrics": len(registry.visible()),
    }


@app.get("/api/metrics")
def list_metrics() -> dict[str, Any]:
    return {
        "metrics": [
            {
                "key": metric.key,
                "name": metric.name,
                "unit": metric.unit,
                "description": metric.description,
                "synonyms": list(metric.synonyms),
                "dimensions": list(metric.dimensions),
            }
            for metric in registry.visible()
        ],
        "dimensions": [
            {"key": name, "expr": dimension.expr}
            for name, dimension in registry.dimensions.items()
        ],
    }


# ---------------------------------------------------------------------------
# 提问：SSE
# ---------------------------------------------------------------------------


@app.post("/api/ask")
def ask(payload: AskRequest) -> StreamingResponse:
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    if not DB_PATH.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"找不到数据库 {DB_PATH}，先跑 python data/generate.py",
        )

    return StreamingResponse(
        _events(question),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 别让反代把流攒起来
        },
    )


def _events(question: str) -> Iterator[str]:
    """把图里每一步翻译成一条 SSE。

    事件类型只有四种：
        step    某个节点走完了
        result  最终结果（只有一条）
        error   出错了
        done    流结束
    """
    try:
        with Executor(DB_PATH) as executor:
            generator = OpenAICompatGenerator.from_env(
                value_hints=executor.dimension_values(registry)
            )
            for item, state in run_stream(
                question,
                registry=registry,
                executor=executor,
                generator=generator,
            ):
                if item is None:
                    yield _sse("result", _result_payload(state))
                else:
                    yield _sse("step", item)
    except RuntimeError as exc:
        # 没配 LLM_API_KEY 时 from_env 抛的就是这个
        yield _sse("error", {"message": str(exc)})
    except Exception as exc:  # 兜底：别让半个流卡住前端
        yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})

    yield _sse("done", {})


def _result_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": state.get("status"),
        "metric": state.get("metric_key"),
        "answer": state.get("answer"),
        "sql": state.get("sql"),
        "columns": state.get("columns") or [],
        "rows": state.get("rows") or [],
        "chart": state.get("chart"),
        "attempts": state.get("attempts", 0),
    }


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    path = FRONTEND_DIR / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="前端文件不存在")
    return FileResponse(path)


app.mount(
    "/static",
    StaticFiles(directory=FRONTEND_DIR, check_dir=False),
    name="static",
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.api:app", host="127.0.0.1", port=8000, reload=False)
