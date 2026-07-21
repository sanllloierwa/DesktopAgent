from src.utils.config import AgentConfig


def test_default_step_budget_covers_desktop_vision_workflows() -> None:
    assert AgentConfig().max_steps == 50
