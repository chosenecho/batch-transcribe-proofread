"""Processing roles. Source collection lives in the original repository."""
from bili_asr.workflow_models import JobKind

PROCESSING_KINDS = (JobKind.ASR, JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT, JobKind.PUBLISH)
WORKER_ROLES = {
    "asr": (JobKind.ASR,),
    "editorial": (JobKind.PROOFREAD, JobKind.RENDER_DOCUMENT),
    "cpu": (JobKind.PUBLISH,),
}
