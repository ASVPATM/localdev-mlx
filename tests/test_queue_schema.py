from __future__ import annotations

from localdev_mlx.schemas import MachineTaskQueue, PlannedTask, PlannedTaskStatus


def test_machine_queue_round_trip() -> None:
    queue = MachineTaskQueue(
        tasks=[
            PlannedTask(
                id="FOUNDATION-001",
                title="Initialize package",
                kind="feature",
                description="Create the bounded package foundation.",
                worker_tier="local",
                acceptance_summary=["CLI help works"],
            )
        ]
    )
    restored = MachineTaskQueue.model_validate_json(queue.model_dump_json())
    assert restored.tasks[0].status == PlannedTaskStatus.PENDING
    assert restored.tasks[0].id == "FOUNDATION-001"
