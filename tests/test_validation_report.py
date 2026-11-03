import pytest

from scripts.check_validation_report import validate


@pytest.mark.parametrize("outcome", ["failure", "error", "skipped"])
def test_blocked_probe_is_not_a_successful_release_check(tmp_path, outcome):
    path = tmp_path / 'result.xml'
    path.write_text(f'<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
                    f'<testcase><{outcome}/></testcase></testsuite></testsuites>')
    with pytest.raises(ValueError, match='did not pass'):
        validate(path)


@pytest.mark.parametrize("body", [
    '<testsuites/>',
    '<testsuite tests="2" failures="0" errors="0" skipped="0"><testcase/></testsuite>',
    '<testsuite tests="1" failures="1" errors="0" skipped="0"><testcase/></testsuite>',
])
def test_empty_incomplete_or_failed_evidence_is_rejected(tmp_path, body):
    path = tmp_path / 'result.xml'
    path.write_text(body)
    with pytest.raises(ValueError):
        validate(path)


def test_real_success_requires_completed_testcases(tmp_path):
    path = tmp_path / 'result.xml'
    path.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase/></testsuite>')
    assert validate(path) == 1
