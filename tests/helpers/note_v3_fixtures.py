"""Shared valid payload builders for GeneratedNote 3.0 contract tests."""

from __future__ import annotations


def _base_payload() -> dict[str, object]:
    return {
        "schema_version": "3.0",
        "task_id": "20260713-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "classification_evidence_ids": ["tr_0001"],
        "title": "结构化学习笔记",
        "summary": {
            "text": "材料介绍了一个可复查的学习主题。",
            "evidence_ids": ["tr_0001"],
        },
        "ai_supplements": [],
    }


def concept_payload(*, reuse_frame: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        **_base_payload(),
        "note_type": "concept_explanation",
        "concepts": [
            {
                "title": "Agent Loop",
                "explanation": {
                    "text": "这种循环让模型按观察、决策和行动推进任务。",
                    "evidence_ids": ["tr_0002"],
                },
            },
            {
                "title": "RAG",
                "explanation": {
                    "text": "这种机制在回答前检索外部材料以补充上下文。",
                    "evidence_ids": ["tr_0003"],
                },
            },
        ],
        "background": {
            "text": "材料先说明了智能体需要外部上下文的原因。",
            "evidence_ids": ["tr_0001"],
        },
        "relationships": [
            {
                "text": "检索结果可以为循环的决策阶段提供上下文。",
                "evidence_ids": ["tr_0003"],
            }
        ],
        "misconceptions": [
            {
                "text": "循环决策不等同于固定脚本。",
                "evidence_ids": ["tr_0004"],
            }
        ],
        "review": {
            "text": "说明循环决策如何使用检索提供的上下文。",
            "evidence_ids": ["tr_0004"],
        },
    }
    if reuse_frame:
        payload["summary"] = {
            "text": "材料解释了循环决策与检索增强的协作关系。",
            "evidence_ids": ["tr_0001"],
        }
        concepts = payload["concepts"]
        assert isinstance(concepts, list)
        concepts[0]["explanation"]["evidence_ids"] = [  # type: ignore[index]
            "tr_0002",
            "fr_0001",
        ]
        concepts[1]["explanation"]["evidence_ids"] = [  # type: ignore[index]
            "tr_0003",
            "fr_0001",
        ]
    else:
        payload["summary"] = {
            "text": "材料解释了循环决策与检索增强的协作关系。",
            "evidence_ids": ["tr_0001", "fr_0001"],
        }
    return payload


def resource_payload() -> dict[str, object]:
    return {
        **_base_payload(),
        "note_type": "resource_share",
        "summary": {
            "text": "材料分享了一个用于组织学习资料的工具。",
            "evidence_ids": ["tr_0001"],
        },
        "resources": [
            {
                "name": {
                    "text": "Resource Tool",
                    "evidence_ids": ["tr_0001"],
                },
                "value": {
                    "text": "该工具可以集中整理学习资料。",
                    "evidence_ids": ["tr_0002"],
                },
                "suitable_for": {
                    "text": "适合需要按主题归档资料的学习者。",
                    "evidence_ids": ["tr_0002"],
                },
                "access_or_usage": {
                    "text": "打开资源地址后按页面说明使用。",
                    "evidence_ids": ["tr_0003"],
                },
                "locator": {
                    "url": "https://resource.example/tool",
                    "evidence_ids": ["tr_0001"],
                },
                "limitations": [
                    {
                        "text": "部分功能需要登录。",
                        "evidence_ids": ["tr_0004"],
                    }
                ],
            }
        ],
        "reminders": [
            {
                "text": "使用前先确认当前服务条款。",
                "evidence_ids": ["tr_0004"],
            }
        ],
    }


def practical_payload() -> dict[str, object]:
    return {
        **_base_payload(),
        "note_type": "practical_tutorial",
        "summary": {
            "text": "材料演示了从准备到检查的完整配置流程。",
            "evidence_ids": ["tr_0001"],
        },
        "goal": {
            "text": "完成材料演示的配置流程。",
            "evidence_ids": ["tr_0001"],
        },
        "prerequisites": [
            {
                "text": "准备一个可编辑的配置文件。",
                "evidence_ids": ["tr_0001"],
            }
        ],
        "steps": [
            {
                "order": 1,
                "title": "打开设置",
                "action": {
                    "text": "打开设置页面。",
                    "evidence_ids": ["tr_0001", "fr_0001"],
                },
                "expected_result": {
                    "text": "页面显示配置选项。",
                    "evidence_ids": ["fr_0001"],
                },
            },
            {
                "order": 2,
                "title": "保存配置",
                "action": {
                    "text": "确认参数并保存配置。",
                    "evidence_ids": ["tr_0002"],
                },
                "expected_result": None,
            },
        ],
        "troubleshooting": [
            {
                "symptom": {
                    "text": "保存按钮不可用。",
                    "evidence_ids": ["tr_0003"],
                },
                "resolution": {
                    "text": "补全必填参数后再次保存。",
                    "evidence_ids": ["tr_0003"],
                },
            }
        ],
        "completion_checks": [
            {
                "text": "重新打开页面后配置仍然生效。",
                "evidence_ids": ["tr_0004"],
            }
        ],
        "cautions": [
            {
                "text": "保存前不要关闭设置页面。",
                "evidence_ids": ["tr_0003"],
            }
        ],
    }


def long_resource_payload() -> dict[str, object]:
    payload = resource_payload()
    payload["resources"] = [
        {
            "name": {
                "text": f"学习资源 {index}",
                "evidence_ids": [f"tr_{index:04d}"],
            },
            "value": {
                "text": f"资源 {index} 提供独立的学习价值。",
                "evidence_ids": [f"tr_{index:04d}"],
            },
            "suitable_for": None,
            "access_or_usage": None,
            "locator": None,
            "limitations": [],
        }
        for index in range(1, 6)
    ]
    payload["reminders"] = []
    return payload


def short_practical_payload() -> dict[str, object]:
    payload = practical_payload()
    payload["prerequisites"] = []
    payload["troubleshooting"] = []
    payload["completion_checks"] = []
    payload["cautions"] = []
    return payload


def v3_payloads() -> list[dict[str, object]]:
    return [concept_payload(), resource_payload(), practical_payload()]
