# Third-party notices and release obligations

The project software is GPL-3.0-only. Third-party components retain their licenses. This checklist does not replace their original LICENSE and NOTICE texts.

This repository is extracted from bilibili-asr-archive and retains its GPL-3.0-only license. The removed collectors and their bilibili-api-python / yt-dlp dependencies are not distributed here. For each actual release, collect license texts from the exact distributions included, retain copyright notices, and provide applicable corresponding source and build information.

| Component | Notice handling |
| --- | --- |
| requests, transformers, accelerate, boto3, botocore | Apache-2.0; retain original license and applicable NOTICE |
| soundfile | Python wrapper BSD; bundled libsndfile LGPL, with separate obligations |
| soxr 1.1.0 | LGPL-2.1-or-later; retain libsoxr/PFFFT notices and applicable source |
| certifi, tqdm | Preserve MPL and other applicable file licenses; check secondary-license conditions |
| regex | Apache-2.0 AND CNRI-Python; historical CNRI compatibility remains a release-review item |
| Qwen ASR/aligner models | Model cards state Apache-2.0; identify exact weight revision and include applicable notices |
| JetBrains Mono in HTML | Embedded copyright and SIL OFL-1.1 text must accompany the font bytes |
| FFmpeg, Torch/ROCm, Triton, drivers | Inspect the exact deployed/distributed builds; a Python package's top-level license does not cover every bundled library |

The audit inventory is not a certification of a final wheel, container, model archive or reading site. Existing external deployments are not changed by this source change. Content permissions are separately governed by CONTENT-LICENSE.md.
