import pytest

from bili_asr.storage.workflow_state import InvalidJobTransition, validate_job_transition


@pytest.mark.parametrize("current,target", [("queued", "running"), ("running", "failed"), ("failed", "queued"), ("succeeded", "queued")])
def test_allowed_workflow_transitions(current, target):
    validate_job_transition(current, target)


@pytest.mark.parametrize("current,target", [("queued", "succeeded"), ("failed", "running"), ("cancelled", "queued")])
def test_illegal_workflow_transitions_are_rejected(current, target):
    with pytest.raises(InvalidJobTransition):
        validate_job_transition(current, target)
