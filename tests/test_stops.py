"""按顺序停靠点（时间窗 + 停留秒数）的引擎测试。"""
import unittest

from app.db import ClosureRow, EdgeRow, Snapshot
from app.routing import NoRouteError, RouteEngine, Stop


def make_engine(nodes, edges, closures=()):
    return RouteEngine(
        Snapshot(
            version_id=1,
            nodes=list(nodes),
            edges=[EdgeRow(*e) for e in edges],
            closures=[ClosureRow(*c) for c in closures],
        )
    )


def E(eid, u, v, secs, stairs=False):
    return (eid, u, v, secs, stairs)


def S(node, lo, hi, dwell):
    return Stop(node, lo, hi, dwell)


class BasicStopTests(unittest.TestCase):
    def setUp(self):
        # GATE --E1(180)--> PLAZA --E2(240)--> TOWER
        self.eng = make_engine(
            ["GATE", "PLAZA", "TOWER"],
            [E("E1", "GATE", "PLAZA", 180), E("E2", "PLAZA", "TOWER", 240)],
        )

    def test_arrive_before_window_waits_then_dwells(self):
        # 0 出发，180 到 PLAZA；窗口 [300, 600)语义(封闭端点)，停留 120
        r = self.eng.plan("GATE", "TOWER", 0, stops=[S("PLAZA", 300, 600, 120)])
        (visit,) = r.stops
        self.assertEqual(visit.arrival_time, 180)
        self.assertEqual(visit.start_time, 300)       # 等到窗口开放
        self.assertEqual(visit.end_time, 420)
        self.assertEqual(visit.waiting_seconds, 120)
        self.assertEqual(r.arrival_time, 420 + 240)  # 停留结束才能走
        self.assertEqual([s.edge_id for s in r.segments], ["E1", "E2"])
        # E2 的进入时刻 = 停留结束时刻
        self.assertEqual(r.segments[1].enter_time, 420)

    def test_arrive_inside_window_starts_immediately(self):
        r = self.eng.plan("GATE", "TOWER", 0, stops=[S("PLAZA", 100, 600, 60)])
        (visit,) = r.stops
        self.assertEqual((visit.arrival_time, visit.start_time, visit.end_time),
                         (180, 180, 240))
        self.assertEqual(visit.waiting_seconds, 0)

    def test_closed_window_endpoints_are_inclusive(self):
        # 窗口两端相等，恰好该时刻抵达也允许开始
        r = self.eng.plan("GATE", "TOWER", 0, stops=[S("PLAZA", 180, 180, 0)])
        (visit,) = r.stops
        self.assertEqual((visit.start_time, visit.end_time), (180, 180))
        self.assertEqual(r.arrival_time, 420)

    def test_zero_dwell_is_instant_visit(self):
        r = self.eng.plan("GATE", "TOWER", 0, stops=[S("PLAZA", 0, 180, 0)])
        (visit,) = r.stops
        self.assertEqual(visit.dwell_seconds, 0)
        self.assertEqual(visit.end_time, visit.start_time)

    def test_arrive_after_latest_is_no_route(self):
        with self.assertRaises(NoRouteError):
            self.eng.plan("GATE", "TOWER", 0, stops=[S("PLAZA", 0, 179, 0)])

    def test_closure_wait_before_stop_still_feasible(self):
        # E1 在 [0, 300) 封闭：300 进入，480 到 PLAZA
        eng = make_engine(
            ["GATE", "PLAZA", "TOWER"],
            [E("E1", "GATE", "PLAZA", 180), E("E2", "PLAZA", "TOWER", 240)],
            [("E1", 0, 300)],
        )
        r = eng.plan("GATE", "TOWER", 0, stops=[S("PLAZA", 480, 500, 0)])
        self.assertEqual(r.stops[0].arrival_time, 480)
        self.assertEqual(r.arrival_time, 720)

    def test_closure_after_dwell_can_be_waited_out(self):
        # E2 在 [420, 1000) 封闭：停留 420 结束，在 PLAZA 等到 1000 再走
        eng = make_engine(
            ["GATE", "PLAZA", "TOWER"],
            [E("E1", "GATE", "PLAZA", 180), E("E2", "PLAZA", "TOWER", 240)],
            [("E2", 420, 1000)],
        )
        r = eng.plan("GATE", "TOWER", 0, stops=[S("PLAZA", 300, 600, 120)])
        self.assertEqual(r.segments[1].enter_time, 1000)
        self.assertEqual(r.segments[1].waiting_seconds, 1000 - 420)
        self.assertEqual(r.arrival_time, 1240)

    def test_unreachable_stop_is_no_route(self):
        eng = make_engine(
            ["GATE", "PLAZA", "X"],
            [E("E1", "GATE", "PLAZA", 10)],
        )
        with self.assertRaises(NoRouteError):
            eng.plan("GATE", "PLAZA", 0, stops=[S("X", 0, 1000, 0)])


class MultiStopTests(unittest.TestCase):
    def setUp(self):
        # GATE --E1--> PLAZA --E2--> HILL --E3--> TOWER（均 100 秒）
        self.eng = make_engine(
            ["GATE", "PLAZA", "HILL", "TOWER"],
            [E("E1", "GATE", "PLAZA", 100),
             E("E2", "PLAZA", "HILL", 100),
             E("E3", "HILL", "TOWER", 100)],
        )

    def test_two_stops_in_order(self):
        r = self.eng.plan(
            "GATE", "TOWER", 0,
            stops=[S("PLAZA", 500, 600, 60), S("HILL", 0, 1000, 0)],
        )
        v1, v2 = r.stops
        self.assertEqual([v1.node, v2.node], ["PLAZA", "HILL"])
        self.assertEqual((v1.arrival_time, v1.start_time, v1.end_time),
                         (100, 500, 560))
        self.assertEqual((v2.arrival_time, v2.start_time), (660, 660))
        self.assertEqual(r.arrival_time, 760)

    def test_second_stop_window_missed_is_no_route(self):
        # PLAZA 停留到 560 结束，660 才到 HILL，晚于窗口 600
        with self.assertRaises(NoRouteError):
            self.eng.plan(
                "GATE", "TOWER", 0,
                stops=[S("PLAZA", 500, 600, 60), S("HILL", 0, 600, 0)],
            )

    def test_passing_future_stop_does_not_count(self):
        # 唯一路径 PLAZA->HILL->TOWER；要求先停 TOWER 再停 HILL：
        # 第一站可以经过 HILL（尚未轮到，不算停靠），到 TOWER 停留后
        # 必须有 HILL<-... 路径才能完成第二站。
        with self.assertRaises(NoRouteError):
            self.eng.plan(
                "GATE", "HILL", 0,
                stops=[S("TOWER", 0, 1000, 0), S("HILL", 0, 2000, 0)],
            )

    def test_same_node_consecutive_stops(self):
        # 同一节点连续停靠两次：第一次停留后“原地”执行第二次
        r = self.eng.plan(
            "GATE", "TOWER", 0,
            stops=[S("PLAZA", 100, 200, 100), S("PLAZA", 300, 400, 50)],
        )
        v1, v2 = r.stops
        self.assertEqual((v1.start_time, v1.end_time), (100, 200))
        self.assertEqual((v2.arrival_time, v2.start_time, v2.end_time),
                         (200, 300, 350))
        self.assertEqual(r.arrival_time, 550)

    def test_stop_at_origin_starts_after_departure(self):
        # 起点即第一站：出发时刻 50 抵达自身，窗口 [0,100]，停留 100
        r = self.eng.plan(
            "GATE", "TOWER", 50,
            stops=[S("GATE", 0, 100, 100), S("PLAZA", 0, 1000, 0)],
        )
        v1, v2 = r.stops
        self.assertEqual((v1.arrival_time, v1.start_time, v1.end_time),
                         (50, 50, 150))
        self.assertEqual(v2.arrival_time, 250)

    def test_destination_equals_last_stop(self):
        # 终点就是最后一个停靠点：到达终点时刻 = 停留结束时刻
        r = self.eng.plan("GATE", "TOWER", 0, stops=[S("TOWER", 0, 1000, 30)])
        (v,) = r.stops
        self.assertEqual((v.arrival_time, v.end_time), (300, 330))
        self.assertEqual(r.arrival_time, 330)


class LexWithStopsTests(unittest.TestCase):
    def test_global_lex_with_detour_around_stop(self):
        # 验证平局针对**全程**序列、不逐段独立打破：
        # 到停靠点 X 有两条路：
        #   (E1,E2) 共 20 秒到 X；(E0) 直达 100 秒到 X，
        #   但 X 窗口 [100,100]，两条路都在 100 开始停留，差异被等候吸收；
        # X -> TOWER 走 Et(10)。最终同时到达 110，
        # 全程序列 (E0,Et) 字典序小于 (E1,E2,Et)，必须选 E0。
        # 若在“到达 X 的路段”上独立按最早到达打破平局，就会错误选 (E1,E2)。
        nodes = ["GATE", "M", "X", "TOWER"]
        edges = [
            E("E1", "GATE", "M", 10),
            E("E2", "M", "X", 10),
            E("E0", "GATE", "X", 100),
            E("Et", "X", "TOWER", 10),
        ]
        eng = make_engine(nodes, edges)
        r = eng.plan("GATE", "TOWER", 0, stops=[S("X", 100, 100, 0)])
        self.assertEqual(r.arrival_time, 110)
        self.assertEqual(
            [s.edge_id for s in r.segments], ["E0", "Et"]
        )
        self.assertEqual((r.stops[0].arrival_time, r.stops[0].start_time),
                         (100, 100))

    def test_lex_choice_after_stop_uses_full_sequence(self):
        # X 停留后的两条到 TOWER 路线等到达时刻时按编号字典序选
        nodes = ["GATE", "X", "A", "B", "TOWER"]
        edges = [
            E("Eg", "GATE", "X", 10),
            E("E2", "X", "A", 10), E("E4", "A", "TOWER", 100),
            E("E1", "X", "B", 50), E("E3", "B", "TOWER", 60),
        ]
        # X 停留到 60 结束：A 线 60+10+100=170；B 线 60+50+60=170
        # 全序列 (Eg,E1,E3) < (Eg,E2,E4)
        eng = make_engine(nodes, edges)
        r = eng.plan("GATE", "TOWER", 0, stops=[S("X", 60, 60, 0)])
        self.assertEqual(r.arrival_time, 170)
        self.assertEqual([s.edge_id for s in r.segments], ["Eg", "E1", "E3"])


class AvoidStairsWithStopsTests(unittest.TestCase):
    def test_stop_only_reachable_via_stairs_is_no_route(self):
        nodes = ["GATE", "PLAZA"]
        edges = [E("E1", "GATE", "PLAZA", 10, stairs=True)]
        eng = make_engine(nodes, edges)
        with self.assertRaises(NoRouteError):
            eng.plan("GATE", "PLAZA", 0, avoid_stairs=True,
                     stops=[S("PLAZA", 0, 100, 0)])

    def test_avoid_stairs_keeps_flat_route_with_stops(self):
        nodes = ["GATE", "A", "B"]
        edges = [
            E("Es", "GATE", "A", 1, stairs=True),
            E("Ef", "GATE", "A", 100, stairs=False),
            E("Eg", "A", "B", 10, stairs=False),
        ]
        eng = make_engine(nodes, edges)
        r = eng.plan("GATE", "B", 0, avoid_stairs=True,
                     stops=[S("A", 0, 1000, 0)])
        self.assertEqual([s.edge_id for s in r.segments], ["Ef", "Eg"])


if __name__ == "__main__":
    unittest.main()
