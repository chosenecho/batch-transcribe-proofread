import copy
import pytest
from bili_asr.editorial_quality import check_source_quality
from bili_asr.workflow_errors import JobExecutionError

def prepared(text, duration=900_000, kind='asr-local'):
    return dict(snapshot=dict(base=dict(source_kind=kind,transcript_id=2537,
        segments=[dict(start_ms=0,end_ms=duration,text=text)])))

@pytest.mark.parametrize('count,duration',[(6,875_000),(22,3_675_000)])
def test_long_filler_only_asr_is_rejected_without_mutation(count,duration):
    p=prepared((chr(0x55ef)+chr(0x3002))*count,duration);before=copy.deepcopy(p)
    with pytest.raises(JobExecutionError,match='editorial_source_repetition'):
        check_source_quality(p)
    assert p==before

@pytest.mark.parametrize('text,duration,kind',[
    ((chr(0x55ef)+chr(0x3002))*6,5_000,'asr-local'),
    ((chr(0x55ef)+chr(0x3002))*6,900_000,'subtitle-cc'),
    ('A substantive statement. '+chr(0x55ef)*6,900_000,'asr-local'),
    (chr(0x55ef)*5,900_000,'asr-local'),
])
def test_normal_speech_short_fillers_and_other_sources_are_not_rejected(text,duration,kind):
    check_source_quality(prepared(text,duration,kind))
