"""通行规划引擎。

时间规则
========
封闭记录是半开区间 ``[start, end)``，路段通行区间也是半开
``[enter, leave)``（``leave = enter + travel_seconds``）。
二者重叠当且仅当::

    enter < close_end and close_start < leave

因此允许恰好在封闭结束时刻 ``end`` 进入路段，也允许在 ``end`` 之前
停在节点上等候；通行区间一旦开始就不能与任何封闭区间重叠。

按顺序停靠点
============
每个停靠点指定节点、停留窗口 ``[earliest_start, latest_start]`` 与
非负停留秒数 ``dwell_seconds``。抵达该节点后可在节点上等候窗口开放，
开始停留时刻 ``start`` 满足
``earliest_start <= start <= latest_start``，结束时刻
``end = start + dwell_seconds``，结束后才能继续前往下一站。途经
**尚未轮到**的停靠节点不算完成停靠，只有在当前路段的目标站点上执行
“停留转移”才推进停靠计数。停靠点时间窗是封闭区间，两端都可停靠。

算法
====
第一阶段：时间依赖的标号设定（Dijkstra 变种）。
``earliest_departure`` 在 FIFO 假设下（晚出发不会更早通过同一条边）
保证最早到达时刻正确。存在按顺序停靠点时，对每个路段（相邻两次停靠
之间，以及最后一次停靠到终点）依次计算最早到达；抵达停靠点后在窗口
内尽早开始停留（晚于最晚时刻则该站不可停留），停留结束再作为下一路
段的出发时刻，最终得到终点最早到达 ``T``。

第二阶段：在“到达终点不晚于 T”的状态空间做多标签搜索，状态为
``(节点, 已完成停靠数)``，标签为 ``(到达时刻, 路段编号序列)``，按
该二元组的字典序出堆。停靠点产生零路段的“停留转移”：仅当处在当前
轮到的停靠点节点上时，才能完成停留并把停靠计数加一（途经尚未轮到的
站点不停留、不计数）。边的通行秒数为正，走边时到达时刻严格增大，
停靠计数有上界，搜索必然终止；最后在终点（全部停靠已完成）的标签
集合里取二元组最小项。第二阶段用于处理“节点/停靠点等候把不同路径
的到达时刻拉平”后，单标号算法或逐段独立平局可能选错字典序路径的
问题——平局比较始终针对**全程**路段编号序列，不逐段独立打破平局。

为什么不能只做一次 Dijkstra
---------------------------
节点等待会把较晚到达的路径“拉平”到相同的后续出发时刻，于是在某个
中间节点上“时刻更优”的路径，续接同一条后缀后可能与另一条路径同时
到达终点，而路段编号序列反而更大。故字典序平局判定必须在第二阶段
对完整序列进行。
"""
from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass
from typing import NamedTuple, Optional, Sequence

from .db import Snapshot


@dataclass(frozen=True)
class Edge:
    id: str
    src: str
    dst: str
    seconds: int
    has_stairs: bool


class Stop(NamedTuple):
    """一次查询中按顺序停靠的站点。"""

    node: str
    earliest_start: int      # 最早开始停留时刻（含）
    latest_start: int        # 最晚开始停留时刻（含）
    dwell_seconds: int       # 非负停留秒数；0 表示时刻打卡式停靠


class Segment(NamedTuple):
    edge_id: str
    from_node: str
    to_node: str
    enter_time: int          # 进入该路段的时刻（在起点等候后的实际出发时刻）
    leave_time: int          # 离开该路段（到达下一节点）的时刻
    travel_seconds: int
    waiting_seconds: int     # 进入前在 from_node 的等候秒数（不含停留时间）
    has_stairs: bool


class StopVisit(NamedTuple):
    """停靠点在最终路线中的实际执行时刻。"""

    node: str
    index: int
    arrival_time: int        # 抵达该站点的时刻
    start_time: int          # 开始停留时刻
    end_time: int            # 结束停留时刻
    earliest_start: int
    latest_start: int
    dwell_seconds: int
    waiting_seconds: int     # 抵达后等候窗口开放的秒数


class Route(NamedTuple):
    origin: str
    destination: str
    departure_time: int      # 用户请求的出发时刻
    arrival_time: int        # 最早到达时刻
    avoid_stairs: bool
    segments: tuple[Segment, ...]
    stops: tuple[StopVisit, ...] = ()


class NoRouteError(Exception):
    """没有在约束下可行的路线。"""


def _overlaps(enter: int, leave: int, start: int, end: int) -> bool:
    """[enter, leave) 与 [start, end) 是否重叠。"""
    return enter < end and start < leave


class RouteEngine:
    def __init__(self, snapshot: Snapshot):
        self.version_id = snapshot.version_id
        self._adj: dict[str, list[Edge]] = defaultdict(list)
        self._edges: dict[str, Edge] = {}
        for row in snapshot.edges:
            edge = Edge(row.id, row.src, row.dst, row.seconds, row.has_stairs)
            if edge.id in self._edges:
                raise ValueError(f"路段编号在版本内重复: {edge.id}")
            self._edges[edge.id] = edge
            self._adj[edge.src].append(edge)
        for edges in self._adj.values():
            edges.sort(key=lambda e: e.id)

        self._closures: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for c in snapshot.closures:
            self._closures[c.edge_id].append((c.start, c.end))
        for intervals in self._closures.values():
            intervals.sort()

    def _neighbors(self, node: str, avoid_stairs: bool) -> list[Edge]:
        edges = self._adj.get(node, ())
        if avoid_stairs:
            return [e for e in edges if not e.has_stairs]
        return list(edges)

    def earliest_departure(self, edge: Edge, ready_at: int) -> int:
        """在 ``ready_at`` 或之后，最早可以进入该路段的时刻。

        若按当前时刻进入会与某条封闭区间重叠，则把进入时刻推迟到该封闭
        区间结束（半开区间，结束时刻立即可通行）后重新检查。
        """
        t = ready_at
        for start, end in self._closures.get(edge.id, ()):
            if end <= t:
                continue
            if start >= t + edge.seconds:
                break  # 已按 start 排序，后面的封闭更不可能重叠
            if _overlaps(t, t + edge.seconds, start, end):
                t = end
        return t

    @staticmethod
    def _stop_start(stop: Stop, arrival: int) -> Optional[int]:
        """抵达停靠点后的最早可开始停留时刻；晚于最晚时刻返回 None。"""
        if arrival > stop.latest_start:
            return None
        return max(arrival, stop.earliest_start)

    # ------------------------------------------------------------------
    # 第一阶段：最早到达时刻（按停靠顺序逐路段推进）
    # ------------------------------------------------------------------
    def _earliest_arrival_map(
        self, origin: str, departure: int, avoid_stairs: bool
    ) -> dict[str, int]:
        dist: dict[str, int] = {origin: departure}
        heap: list[tuple[int, str]] = [(departure, origin)]
        while heap:
            t, u = heapq.heappop(heap)
            if t != dist[u]:
                continue
            for edge in self._neighbors(u, avoid_stairs):
                enter = self.earliest_departure(edge, t)
                arrival = enter + edge.seconds
                if arrival < dist.get(edge.dst, float("inf")):
                    dist[edge.dst] = arrival
                    heapq.heappush(heap, (arrival, edge.dst))
        return dist

    def _earliest_deadline(
        self,
        origin: str,
        destination: str,
        departure: int,
        avoid_stairs: bool,
        stops: Sequence[Stop],
    ) -> int:
        """逐路段求最早到达；任何一站无法按时停留或终点不可达即无路线。"""
        clock = departure
        current = origin
        for i, stop in enumerate(stops):
            dist = self._earliest_arrival_map(current, clock, avoid_stairs)
            if stop.node not in dist:
                raise NoRouteError(
                    f"无法在当前步道图与封闭记录下到达第 {i + 1} 个停靠点 {stop.node!r}"
                    + ("（已避开台阶路段）" if avoid_stairs else "")
                )
            arrival = dist[stop.node]
            start = self._stop_start(stop, arrival)
            if start is None:
                raise NoRouteError(
                    f"第 {i + 1} 个停靠点 {stop.node!r} 最早抵达时刻已晚于最晚开始停留时刻，"
                    "无法按时停留"
                )
            clock = start + stop.dwell_seconds
            current = stop.node

        dist = self._earliest_arrival_map(current, clock, avoid_stairs)
        if destination not in dist:
            raise NoRouteError(
                "在当前步道图与封闭记录下不存在可行路线"
                + ("（已避开台阶路段）" if avoid_stairs else "")
            )
        return dist[destination]

    # ------------------------------------------------------------------
    # 第二阶段：最早到达前提下，全程路段编号序列字典序最小
    # ------------------------------------------------------------------
    @staticmethod
    def _dominates(t1: int, s1: tuple[str, ...], t2: int, s2: tuple[str, ...]) -> bool:
        """标签 1 是否支配标签 2（对任意同后缀都不会更差）。

        两条规则，都要避开“前缀陷阱”——较短序列追加后缀后字典序可能翻转：

        1. 到达时刻相同：正秒数下图中不存在零耗时环，同一状态下同到达
           时刻的两条不同序列不可能互为前缀，可直接按字典序裁剪；
        2. 到达时刻严格更早：只有**等长**序列才安全（等长时首个不同
           元素的比较与追加同后缀后的完整比较一致）；长度不同且互为
           前缀的必须同时保留。
        """
        if t1 > t2:
            return False
        if t1 == t2:
            return s1 < s2
        # t1 < t2
        if len(s1) != len(s2):
            return False
        return s1 < s2

    def _lex_smallest(
        self,
        origin: str,
        destination: str,
        departure: int,
        avoid_stairs: bool,
        stops: Sequence[Stop],
        deadline: int,
    ) -> tuple[str, ...]:
        """在所有“到达终点不晚于 deadline”的路线中取全程序列字典序最小者。

        状态为 ``(节点, stage)``，``stage`` 是已完成的停靠数；停靠转移
        不产生路段编号，只推进 ``stage``。完整展开搜索（正秒数 +
        deadline 剪枝保证终止），最后在 ``(destination, 总站数)`` 的
        标签集合里取二元组最小项。
        """
        n_stops = len(stops)
        # 每个状态保留一组互不支配的标签
        labels: dict[tuple[str, int], list[tuple[int, tuple[str, ...]]]] = defaultdict(list)
        labels[(origin, 0)].append((departure, ()))
        heap: list[tuple[int, tuple[str, ...], str, int]] = [
            (departure, (), origin, 0)
        ]

        def push(state, t, seq):
            if t > deadline:
                return
            existing = labels[state]
            if (t, seq) in existing:
                return
            if any(self._dominates(et, es, t, seq) for et, es in existing):
                return
            # 新标签支配旧标签则移除（前缀不可比的保留）
            labels[state] = [
                (et, es) for et, es in existing
                if not self._dominates(t, seq, et, es)
            ]
            labels[state].append((t, seq))
            heapq.heappush(heap, (t, seq, state[0], state[1]))

        while heap:
            t, seq, u, stage = heapq.heappop(heap)
            if (t, seq) not in labels.get((u, stage), ()):
                continue  # 已被支配后移除的过期标签

            # 1) 停靠转移：当前节点正是当前轮到的站点时，可停留并推进一站
            if stage < n_stops and stops[stage].node == u:
                stop = stops[stage]
                start = self._stop_start(stop, t)
                if start is not None:
                    end = start + stop.dwell_seconds
                    if end <= deadline:
                        push((u, stage + 1), end, seq)

            # 2) 走边（途经尚未轮到的站点不停留、不计数）
            for edge in self._neighbors(u, avoid_stairs):
                enter = self.earliest_departure(edge, t)
                leave = enter + edge.seconds
                if leave > deadline:
                    continue  # 不可能参与“最早到达 T”的路线
                push((edge.dst, stage), leave, seq + (edge.id,))

        best = min(
            labels.get((destination, n_stops), ()),
            default=None,
            key=lambda item: (item[0], item[1]),
        )
        if best is None:
            raise NoRouteError("内部错误：最早到达已知，但字典序选路失败")
        return best[1]

    # ------------------------------------------------------------------
    # 按选定序列重建逐段/逐站时刻（与第二阶段同样的确定性最早停留）
    # ------------------------------------------------------------------
    def _reconstruct(
        self,
        origin: str,
        departure: int,
        avoid_stairs: bool,
        stops: Sequence[Stop],
        best_seq: tuple[str, ...],
    ) -> tuple[list[Segment], list[StopVisit], int, str]:
        segments: list[Segment] = []
        visits: list[StopVisit] = []
        node = origin
        clock = departure
        i = 0
        for stage, stop in enumerate(stops):
            # 路段 stage：走到当前停靠点（可途经未来站点而不停留）
            while node != stop.node:
                edge = self._edges[best_seq[i]]
                i += 1
                enter = self.earliest_departure(edge, clock)
                leave = enter + edge.seconds
                segments.append(
                    Segment(
                        edge_id=edge.id,
                        from_node=node,
                        to_node=edge.dst,
                        enter_time=enter,
                        leave_time=leave,
                        travel_seconds=edge.seconds,
                        waiting_seconds=enter - clock,
                        has_stairs=edge.has_stairs,
                    )
                )
                clock = leave
                node = edge.dst
            arrival = clock
            start = self._stop_start(stop, arrival)
            if start is None:  # 理论不可达：第二阶段已保证可行性
                raise NoRouteError(
                    f"第 {stage + 1} 个停靠点 {stop.node!r} 无法按时停留（内部重建失败）"
                )
            end = start + stop.dwell_seconds
            visits.append(
                StopVisit(
                    node=stop.node,
                    index=stage,
                    arrival_time=arrival,
                    start_time=start,
                    end_time=end,
                    earliest_start=stop.earliest_start,
                    latest_start=stop.latest_start,
                    dwell_seconds=stop.dwell_seconds,
                    waiting_seconds=start - arrival,
                )
            )
            clock = end
        # 末段：最后一次停留（或起点）到终点
        while i < len(best_seq):
            edge = self._edges[best_seq[i]]
            i += 1
            enter = self.earliest_departure(edge, clock)
            leave = enter + edge.seconds
            segments.append(
                Segment(
                    edge_id=edge.id,
                    from_node=node,
                    to_node=edge.dst,
                    enter_time=enter,
                    leave_time=leave,
                    travel_seconds=edge.seconds,
                    waiting_seconds=enter - clock,
                    has_stairs=edge.has_stairs,
                )
            )
            clock = leave
            node = edge.dst
        return segments, visits, clock, node

    # ------------------------------------------------------------------
    def plan(
        self,
        origin: str,
        destination: str,
        departure: int,
        avoid_stairs: bool = False,
        stops: Optional[Sequence[Stop]] = None,
    ) -> Route:
        stop_list = tuple(stops or ())
        deadline = self._earliest_deadline(
            origin, destination, departure, avoid_stairs, stop_list
        )
        best_seq = self._lex_smallest(
            origin, destination, departure, avoid_stairs, stop_list, deadline
        )
        segments, visits, arrival, final_node = self._reconstruct(
            origin, departure, avoid_stairs, stop_list, best_seq
        )
        if final_node != destination or arrival != deadline:
            raise NoRouteError("内部错误：路线重建结果与最早到达不一致")

        return Route(
            origin=origin,
            destination=destination,
            departure_time=departure,
            arrival_time=deadline,
            avoid_stairs=avoid_stairs,
            segments=tuple(segments),
            stops=tuple(visits),
        )


def build_engine(snapshot: Snapshot) -> RouteEngine:
    return RouteEngine(snapshot)
