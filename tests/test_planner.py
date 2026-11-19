"""Tests for the deterministic offline project planner."""

from __future__ import annotations

import unittest

from stackflowos.projects.planner import (
    PlannedTask,
    brief_for,
    milestones_for,
    plan_for,
)

SOFTWARE_ROLES = ["architect", "architect", "coder", "tester", "coder", "reviewer"]
RESEARCH_ROLES = ["researcher", "researcher", "researcher", "reviewer"]
OPS_ROLES = ["tester", "coder", "tester", "reviewer"]
FALLBACK_ROLES = ["architect", "architect", "coder", "tester", "reviewer"]


class PlannerRecipeTests(unittest.TestCase):
    def test_software_recipe(self) -> None:
        plan = plan_for("build a code tool")
        self.assertEqual(len(plan), 6)
        self.assertEqual([task.role for task in plan], SOFTWARE_ROLES)
        self.assertEqual([task.title for task in plan],
                         ["需求与边界", "架构设计", "核心实现", "测试与自检",
                          "文档", "评审"])
        self.assertEqual(milestones_for("build a code tool"),
                         ["需求与设计", "实现", "验证与交付"])
        self.assertTrue(all(task.milestone for task in plan))

    def test_software_recipe_chinese(self) -> None:
        plan = plan_for("实现一个游戏")
        self.assertEqual([task.role for task in plan], SOFTWARE_ROLES)
        self.assertEqual(milestones_for("实现一个游戏"),
                         ["需求与设计", "实现", "验证与交付"])
        self.assertIn("游戏", plan[0].description)

    def test_research_recipe(self) -> None:
        plan = plan_for("调研 AI agent 市场并写报告")
        self.assertEqual([task.role for task in plan], RESEARCH_ROLES)
        self.assertEqual([task.title for task in plan],
                         ["资料收集", "关键问题梳理", "结论报告", "评审"])
        self.assertEqual(milestones_for("调研 AI agent 市场并写报告"),
                         ["调研", "结论与评审"])

    def test_ops_recipe(self) -> None:
        plan = plan_for("部署修复线上故障")
        self.assertEqual([task.role for task in plan], OPS_ROLES)
        self.assertEqual([task.title for task in plan],
                         ["现状检查", "修复方案", "执行与验证", "回归"])
        self.assertEqual(milestones_for("部署修复线上故障"),
                         ["诊断", "修复", "回归验证"])

    def test_english_keyword_families(self) -> None:
        self.assertEqual([t.role for t in plan_for("research the market")],
                         RESEARCH_ROLES)
        self.assertEqual([t.role for t in plan_for("deploy and fix the bug")],
                         OPS_ROLES)
        self.assertEqual([t.role for t in plan_for("implement an API")],
                         SOFTWARE_ROLES)

    def test_fallback_recipe(self) -> None:
        plan = plan_for("整理下周安排")
        self.assertEqual(len(plan), 5)
        self.assertEqual([task.role for task in plan], FALLBACK_ROLES)
        self.assertEqual(milestones_for("整理下周安排"),
                         ["目标与方案", "执行", "验证"])

    def test_title_contributes_context(self) -> None:
        plan = plan_for("", title="开发网站")
        self.assertEqual([task.role for task in plan], SOFTWARE_ROLES)
        self.assertIn("开发网站", plan[0].description)

    def test_plan_shape_invariants(self) -> None:
        for goal in ("build app", "调研报告", "运维修复", "随便做点什么"):
            plan = plan_for(goal)
            self.assertGreaterEqual(len(plan), 4)
            self.assertLessEqual(len(plan), 7)
            milestone_names = milestones_for(goal)
            for task in plan:
                self.assertIsInstance(task, PlannedTask)
                self.assertIn(task.role, {"architect", "coder", "tester",
                                          "researcher", "reviewer"})
                self.assertGreaterEqual(task.priority, 0)
                self.assertLessEqual(task.priority, 3)
                self.assertIn(task.milestone, milestone_names)
                self.assertTrue(task.title)
                self.assertTrue(task.description)

    def test_plan_is_stable(self) -> None:
        goal = "build a code tool"
        first = plan_for(goal)
        second = plan_for(goal)
        self.assertEqual(first, second)
        self.assertEqual([task.to_dict() for task in first],
                         [task.to_dict() for task in second])
        self.assertEqual(milestones_for(goal), milestones_for(goal))


class BriefTests(unittest.TestCase):
    def _tasks(self) -> list[dict]:
        plan = plan_for("build a code tool")
        tasks = [task.to_dict() for task in plan]
        tasks[0]["status"] = "done"
        tasks[1]["status"] = "doing"
        return tasks

    def test_brief_contains_counts_and_focus(self) -> None:
        tasks = self._tasks()
        brief = brief_for("Demo", "build a code tool", tasks, done=1, total=6)
        self.assertIn("Demo", brief)
        self.assertIn("共 6 项", brief)
        self.assertIn("已完成 1 项", brief)
        self.assertIn("进行中 1 项", brief)
        self.assertIn("待办 4 项", brief)
        self.assertIn("当前重点：架构设计", brief)
        self.assertIn("architect", brief)
        self.assertIn("目标：build a code tool", brief)
        # Deterministic for identical input.
        self.assertEqual(brief, brief_for("Demo", "build a code tool", tasks,
                                          done=1, total=6))

    def test_brief_empty_project(self) -> None:
        brief = brief_for("Empty", "nothing yet", [], done=0, total=0)
        self.assertIn("Empty", brief)
        self.assertIn("尚未拆解任务", brief)
        self.assertEqual(brief, brief_for("Empty", "nothing yet", [], done=0, total=0))

    def test_brief_all_done(self) -> None:
        tasks = [task.to_dict() for task in plan_for("调研报告")]
        for task in tasks:
            task["status"] = "done"
        brief = brief_for("Research", "调研报告", tasks, done=4, total=4)
        self.assertIn("已完成 4 项", brief)
        self.assertIn("100%", brief)
        self.assertIn("全部完成", brief)


if __name__ == "__main__":
    unittest.main()
