"""FastAPI 应用：查询与原子导入 HTTP API。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .db import (
    connect,
    current_version,
    get_snapshot,
    init_db,
    publish_closures,
    publish_graph,
)
from .routing import NoRouteError, build_engine
from .schemas import ValidationError as PayloadValidationError
from .timeutil import format_time


def _segment_to_dict(seg) -> dict:
    return {
        "edge_id": seg.edge_id,
        "from_node": seg.from_node,
        "to_node": seg.to_node,
        "has_stairs": seg.has_stairs,
        "enter_time": format_time(seg.enter_time),
        "enter_time_epoch": seg.enter_time,
        "leave_time": format_time(seg.leave_time),
        "leave_time_epoch": seg.leave_time,
        "travel_seconds": seg.travel_seconds,
        "waiting_seconds": seg.waiting_seconds,
    }


def _stop_to_dict(stop) -> dict:
    return {
        "index": stop.index,
        "node": stop.node,
        "arrival_time": format_time(stop.arrival_time),
        "arrival_time_epoch": stop.arrival_time,
        "start_time": format_time(stop.start_time),
        "start_time_epoch": stop.start_time,
        "end_time": format_time(stop.end_time),
        "end_time_epoch": stop.end_time,
        "earliest_start": format_time(stop.earliest_start),
        "latest_start": format_time(stop.latest_start),
        "dwell_seconds": stop.dwell_seconds,
        "waiting_seconds": stop.waiting_seconds,
    }


def _route_to_dict(route) -> dict:
    result = {
        "origin": route.origin,
        "destination": route.destination,
        "query_departure_time": format_time(route.departure_time),
        "query_departure_time_epoch": route.departure_time,
        "arrival_time": format_time(route.arrival_time),
        "arrival_time_epoch": route.arrival_time,
        # 等候秒数只统计路段进入前因封闭/窗口产生的等待，不含站点停留时间
        "total_waiting_seconds": route.arrival_time
        - route.departure_time
        - sum(s.travel_seconds for s in route.segments)
        - sum(s.dwell_seconds for s in route.stops),
        "avoid_stairs": route.avoid_stairs,
        "edge_sequence": [s.edge_id for s in route.segments],
        "segments": [_segment_to_dict(s) for s in route.segments],
    }
    # 无停靠点时沿用原有返回结构；有停靠点时才增加 stops 字段
    if route.stops:
        result["stops"] = [_stop_to_dict(s) for s in route.stops]
    return result


def _maybe_seed(conn) -> None:
    """启动时初始化：数据库为空则按环境变量导入种子数据。"""
    if current_version(conn) != 0:
        return
    graph_path = config.SEED_GRAPH_FILE
    if graph_path:
        publish_graph(conn, json.loads(Path(graph_path).read_text(encoding="utf-8")))
    elif config.SEED_SAMPLE:
        from .sample_data import SAMPLE_CLOSURES, SAMPLE_GRAPH

        publish_graph(conn, SAMPLE_GRAPH)
    if current_version(conn) == 0:
        return

    closures_path = config.SEED_CLOSURES_FILE
    if closures_path:
        publish_closures(conn, json.loads(Path(closures_path).read_text(encoding="utf-8")))
    elif not graph_path and config.SEED_SAMPLE:
        from .sample_data import SAMPLE_CLOSURES

        publish_closures(conn, SAMPLE_CLOSURES)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 每个请求使用独立连接（SQLite 连接轻量，且避免跨线程共享）
    yield


app = FastAPI(
    title="园区访客通行规划服务",
    version="1.0.0",
    description="离线步道图 + 半开时间区间封闭记录的最早到达路线查询",
    lifespan=lifespan,
)


@app.exception_handler(PayloadValidationError)
async def _validation_handler(request: Request, exc: PayloadValidationError):
    return JSONResponse(status_code=400, content={"error": "invalid_payload", "detail": str(exc)})


@app.get("/health")
async def health():
    conn = connect()
    try:
        init_db(conn)
        version = current_version(conn)
        return {"status": "ok", "data_version": version, "has_data": version != 0}
    finally:
        conn.close()


@app.get("/version")
async def version():
    conn = connect()
    try:
        init_db(conn)
        snap = get_snapshot(conn)
        if snap is None:
            return {"data_version": 0, "nodes": 0, "edges": 0, "closures": 0}
        return {
            "data_version": snap.version_id,
            "nodes": len(snap.nodes),
            "edges": len(snap.edges),
            "closures": len(snap.closures),
        }
    finally:
        conn.close()


@app.post("/api/route")
async def query_route(request: Request):
    """查询最早到达路线。单次请求固定使用进入时的当前数据版本。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400, content={"error": "invalid_payload", "detail": "请求体必须是合法 JSON"}
        )

    conn = connect()
    try:
        init_db(conn)
        # 先固定版本指针，保证整次查询只用同一版本
        snap = get_snapshot(conn)
        if snap is None:
            return JSONResponse(
                status_code=503,
                content={"error": "no_data", "detail": "尚未导入步道图，请先调用导入接口"},
            )

        from .routing import Stop
        from .schemas import normalize_query

        query = normalize_query(payload, snap.node_set)
        engine = build_engine(snap)
        try:
            route = engine.plan(
                origin=query["origin"],
                destination=query["destination"],
                departure=query["departure"],
                avoid_stairs=query["avoid_stairs"],
                stops=[
                    Stop(
                        node=s["node"],
                        earliest_start=s["earliest_start"],
                        latest_start=s["latest_start"],
                        dwell_seconds=s["dwell_seconds"],
                    )
                    for s in query["stops"]
                ],
            )
        except NoRouteError as exc:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "no_route",
                    "detail": str(exc),
                    "data_version": snap.version_id,
                },
            )

        result = _route_to_dict(route)
        result["data_version"] = snap.version_id
        return result
    finally:
        conn.close()


@app.post("/admin/graph")
async def import_graph(request: Request):
    """原子发布新步道图（校验失败保留旧版本）。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400, content={"error": "invalid_payload", "detail": "请求体必须是合法 JSON"}
        )
    conn = connect()
    try:
        init_db(conn)
        old_version = current_version(conn)
        try:
            new_version = publish_graph(conn, payload)
        except PayloadValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_payload",
                    "detail": str(exc),
                    "rejected": True,
                    "active_version": old_version,
                },
            )
        return {
            "status": "published",
            "data_version": new_version,
            "previous_version": old_version,
        }
    finally:
        conn.close()


@app.post("/admin/closures")
async def import_closures(request: Request):
    """原子整体替换封闭记录（校验失败保留旧版本）。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400, content={"error": "invalid_payload", "detail": "请求体必须是合法 JSON"}
        )
    conn = connect()
    try:
        init_db(conn)
        old_version = current_version(conn)
        try:
            new_version = publish_closures(conn, payload)
        except PayloadValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_payload",
                    "detail": str(exc),
                    "rejected": True,
                    "active_version": old_version,
                },
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "invalid_state",
                    "detail": str(exc),
                    "active_version": old_version,
                },
            )
        return {
            "status": "published",
            "data_version": new_version,
            "previous_version": old_version,
        }
    finally:
        conn.close()
