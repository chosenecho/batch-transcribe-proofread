"""Pure versioned Bilibili metadata normalization, reusable without SDK imports."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import replace

from bili_asr.canonical_json import canonical
from bili_asr.metadata_acceptance import SourceFieldReceipt, binding, binding_tuple
from bili_asr.metadata_policy import MetadataFieldObservation
from bili_asr.source_observations import PlatformObservation
from bili_asr.sources.models import (
    GatewayShapeError,
    VideoMetadataRead,
    VideoPart,
    VideoSummary,
    VideoTag,
)

_BVID_PATTERN = re.compile(r"^BV[a-zA-Z0-9]{10}$")

def _extract_page_items(response: object) -> list:
    """Extract the video item array from the arc/search inner data."""

    if not isinstance(response, Mapping):
        raise GatewayShapeError(detail="response is not a mapping")
    container = response.get("list")
    if isinstance(container, Mapping):
        items = container.get("vlist")
        if not isinstance(items, list):
            raise GatewayShapeError(detail="list.vlist is not an array")
        return items
    if isinstance(container, list):
        return container
    raise GatewayShapeError(detail="response has no video list")

def _read_observed_total(response: Mapping) -> int | None:
    """Read the total video count when the response carries it."""

    page = response.get("page")
    if page is None:
        return None
    if not isinstance(page, Mapping):
        raise GatewayShapeError(detail="page is not a mapping")
    count = page.get("count")
    if count is None:
        return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise GatewayShapeError(detail="page.count is not a valid total")
    return count

def _read_pubdate(item: Mapping) -> int:
    """Read the publish timestamp; ``created`` first, ``pubdate`` fallback."""

    for field in ("created", "pubdate"):
        value = item.get(field)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GatewayShapeError(detail=f"{field} is not a timestamp")
            return value
    raise GatewayShapeError(detail="no publish timestamp")

def _read_optional_aid(item: Mapping) -> int | None:
    """Read the aid when present; absent stays absent, never speculative."""

    value = item.get("aid")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GatewayShapeError(detail="aid is not a positive av number")
    return value

def _read_optional_text(
    item: Mapping,
    field: str,
) -> str | None:
    """Read a projected text value; field DTOs validate its own text rules.

    Description permits prose line breaks, whereas URLs and names keep their
    scalar validation. Unrepresentable prose is a shape error, never absence.
    The raw observation keeps whitespace and explicit empty values separately.
    """

    value = item.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise GatewayShapeError(detail=f"video item has no valid {field}")
    text = value.strip()
    return text or None

def _read_optional_typeid(item: Mapping) -> int | None:
    """Read the list item's ``typeid`` when present; absent stays absent.

    The spelling is this endpoint's own — the view endpoint names the same
    thing ``tid`` — and the DTO field keeps the stored column's name.  A
    present-and-unusable value is a bounded shape error: the storage contract
    checks ``tid IS NULL OR tid > 0``, and failing here keeps a bad category id
    from surfacing as an ``IntegrityError`` inside a page transaction after the
    rest of the page normalized cleanly.
    """

    value = item.get("typeid")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GatewayShapeError(detail="video item has no valid typeid")
    return value

def _normalize_video_summary_item(item: object, requested_mid: int) -> VideoSummary:
    """Convert one vlist item into a validated summary DTO.

    The uploader name is read when the item carries one and stays ``None`` when
    it does not: absence is a fact about this response rather than something to
    paper over here, because the placeholder the user record falls back to is
    the ingestor's decision, not this boundary's.

    The category and cover are read the same way, from the keys **this** list
    endpoint uses (``typeid``/``pic``/``description``), and they are never
    filled from a second call: the task stores what a response already received
    carried, and an item that omitted them establishes nothing about them.
    """

    if not isinstance(item, Mapping):
        raise GatewayShapeError(detail="video item is not a mapping")
    bvid = item.get("bvid")
    if not isinstance(bvid, str) or _BVID_PATTERN.fullmatch(bvid) is None:
        raise GatewayShapeError(detail="video item has no valid bvid")
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        raise GatewayShapeError(detail="video item has no title")
    owner_mid = item.get("mid")
    if isinstance(owner_mid, bool) or not isinstance(owner_mid, int):
        raise GatewayShapeError(detail="video item has no owner mid")
    if owner_mid != requested_mid:
        raise GatewayShapeError(
            detail="video item owner mid does not match the requested user"
        )
    author = item.get("author")
    if author is not None and (not isinstance(author, str) or not author.strip()):
        # Present but unusable is a shape error, like the title's arm above;
        # only the absent case is legitimate (see the docstring).
        raise GatewayShapeError(detail="video item has no valid author")
    try:
        return VideoSummary(
            bvid=bvid,
            aid=_read_optional_aid(item),
            title=title.strip(),
            pubdate=_read_pubdate(item),
            mid=owner_mid,
            author=None if author is None else author.strip(),
            pic=_read_optional_text(item, "pic"),
            desc=_read_optional_text(item, "description"),
            tid=_read_optional_typeid(item),
        )
    except (TypeError, ValueError) as exc:
        # The DTO rejects text the storage contract cannot hold either — a title
        # carrying a control character, for instance — and that is a bounded
        # shape error at this boundary, like every sibling normalizer's, rather
        # than a raw validation error escaping the gateway.
        raise GatewayShapeError(detail="video item is not normalizable") from exc

def _normalize_video_part_item(
    item: object, bvid: str, video_title_fallback: str
) -> VideoPart:
    """Convert one pagelist element into a validated part DTO."""

    if not isinstance(item, Mapping):
        raise GatewayShapeError(detail="page item is not a mapping")
    api_page = item.get("page")
    if isinstance(api_page, bool) or not isinstance(api_page, int) or api_page < 1:
        raise GatewayShapeError(detail="page item has no one-based page")
    cid = item.get("cid")
    if isinstance(cid, bool) or not isinstance(cid, int) or cid < 1:
        raise GatewayShapeError(detail="page item has no positive cid")
    part_title = item.get("part")
    if not isinstance(part_title, str):
        raise GatewayShapeError(detail="page item has no title")
    # Display titles stay on one output line, but a valid upstream line break
    # must not prevent every part on this metadata page from being collected.
    part_title = re.sub(r"[\r\n]+", " ", part_title).strip()
    if not part_title:
        # Single-part uploads may carry an empty "part": the video's own title
        # is the part's display label, like the upload UI shows.
        part_title = video_title_fallback
    duration_seconds = item.get("duration")
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or duration_seconds < 0
    ):
        raise GatewayShapeError(detail="page item has no duration")
    try:
        return VideoPart(
            bvid=bvid,
            page_index=api_page - 1,
            cid=cid,
            title=part_title,
            duration_ms=math.floor(duration_seconds * 1000),
        )
    except (TypeError, ValueError) as exc:
        raise GatewayShapeError(detail="page item is not normalizable") from exc

def _normalize_video_parts(
    pages: object, bvid: str, video_title_fallback: str = ""
) -> tuple[VideoPart, ...]:
    """Convert the pagelist array into validated part DTOs."""

    if not isinstance(pages, list):
        raise GatewayShapeError(detail="response is not an array")
    if len(pages) > 1 or not video_title_fallback:
        # A multi-part video must name every part itself; only a single-part
        # upload may inherit the video title as its label.
        video_title_fallback = ""
    return tuple(
        _normalize_video_part_item(item, bvid, video_title_fallback) for item in pages
    )

def _normalize_video_tags(entries: object) -> tuple[VideoTag, ...]:
    """Convert the tag array into validated tag DTOs.

    A video with no tags answers with an empty array, which is an honest
    observation rather than a failure.  ``tag_id`` and ``tag_name`` are
    required — an entry missing either cannot be identified or displayed, so
    it is a bounded shape error rather than a dropped row: silently skipping
    one would report "this video has N-1 tags" as if upstream had said so.
    """

    if not isinstance(entries, list):
        raise GatewayShapeError(detail="tag response is not an array")
    return tuple(_normalize_video_tag(entry) for entry in entries)

def _normalize_video_tag(entry: object) -> VideoTag:
    """Convert one documented tag entry into a validated tag DTO."""

    if not isinstance(entry, Mapping):
        raise GatewayShapeError(detail="tag entry is not a mapping")
    tag_id = entry.get("tag_id")
    if isinstance(tag_id, bool) or not isinstance(tag_id, int) or tag_id < 1:
        raise GatewayShapeError(detail="tag entry has no positive tag_id")
    tag_name = entry.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name.strip():
        raise GatewayShapeError(detail="tag entry has no tag_name")
    tag_type = entry.get("tag_type")
    if not isinstance(tag_type, str) or not tag_type.strip():
        raise GatewayShapeError(detail="tag entry has no tag_type")
    try:
        return VideoTag(
            tag_id=tag_id,
            tag_name=tag_name.strip(),
            tag_type=tag_type.strip(),
        )
    except (TypeError, ValueError) as exc:
        raise GatewayShapeError(detail="tag entry is not normalizable") from exc

def _complete_summary_from_detail(
    summary: VideoSummary, detail: object
) -> VideoSummary:
    """Fill the summary's missing aid from its detail response.

    Only ``aid`` is taken from the detail; every other field stays exactly as
    the list response delivered it — the uploader name, category and cover
    included, because the detail is not asked for them and this rebuild is the
    only constructor between the page boundary and the ingestor.  Dropping
    ``author`` here would silently discard a name the page did carry on exactly
    the aid-less entries this path exists for, and the same holds for
    ``pic``/``desc``/``tid``: the detail response does carry ``tid``/``pic``/
    ``desc``, but filling them from it would make these fields come from a
    second call on some entries and the page on others, which is the
    distinction the task exists to keep.  A detail owned by another user, or
    one naming another video, is a bounded shape error.
    """

    if not isinstance(detail, Mapping):
        raise GatewayShapeError(detail="detail is not a mapping")
    detail_aid = detail.get("aid")
    if isinstance(detail_aid, bool) or not isinstance(detail_aid, int) or detail_aid < 1:
        raise GatewayShapeError(detail="detail has no aid")
    detail_owner = detail.get("owner")
    detail_mid = None
    if isinstance(detail_owner, Mapping):
        candidate = detail_owner.get("mid")
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            detail_mid = candidate
    if detail_mid is None or detail_mid != summary.mid:
        raise GatewayShapeError(
            detail="detail owner mid does not match the summary"
        )
    detail_bvid = detail.get("bvid")
    if not isinstance(detail_bvid, str) or detail_bvid != summary.bvid:
        raise GatewayShapeError(detail="detail bvid does not match the summary")
    return replace(summary, aid=detail_aid)


def normalize_video_metadata(detail: object, bvid: str) -> VideoMetadataRead:
    """The exact detail-to-DTO rule shared by acquisition and offline checks."""
    if not isinstance(detail, Mapping) or detail.get("bvid") != bvid or not isinstance(detail.get("owner"), Mapping):
        raise GatewayShapeError()
    owner = detail["owner"]
    item = {"bvid": bvid, "aid": detail.get("aid"), "title": detail.get("title"),
            "mid": owner.get("mid"), "author": owner.get("name"),
            "pubdate": detail.get("pubdate"), "pic": detail.get("pic"),
            "description": detail.get("desc"), "typeid": detail.get("tid")}
    summary = _normalize_video_summary_item(item, owner.get("mid"))
    fields = []
    for field in ("title", "pubdate", "aid", "pic", "desc", "tid"):
        value = detail.get(field)
        if field not in detail or value is None:
            state, value = "missing", None
        elif isinstance(value, str) and not value.strip():
            state, value = "empty", None
        else:
            value = getattr(summary, field)
            state = "present" if value is not None else "missing"
        fields.append(MetadataFieldObservation(field, state, value))
    parts = (_normalize_video_parts(detail["pages"], bvid, summary.title) if "pages" in detail else None)
    return VideoMetadataRead(summary, tuple(fields), parts)


def parts_value(parts: tuple[VideoPart, ...]) -> list[dict]:
    values = {part.page_index: {"pageIndex": part.page_index, "cid": part.cid,
              "title": part.title, "durationMs": part.duration_ms} for part in parts}
    return [values[index] for index in sorted(values)]


def tags_value(tags: tuple[VideoTag, ...]) -> list[dict]:
    values = {tag.tag_id: {"tagId": tag.tag_id, "tagName": tag.tag_name, "tagType": tag.tag_type} for tag in tags}
    return [values[index] for index in sorted(values)]


def _at_pointer(payload: object, pointer: str) -> object:
    if not pointer:
        return payload
    result = payload
    try:
        for key in pointer[1:].split("/"):
            if isinstance(result, list) and key.isdecimal():
                result = result[int(key)]
            elif isinstance(result, dict):
                result = result[key.replace("~1", "/").replace("~0", "~")]
            else:
                raise ValueError("source receipt pointer does not resolve")
    except (IndexError, KeyError) as error:
        raise ValueError("source receipt pointer does not resolve") from error
    return result


def receipt_values(observation: PlatformObservation, normalizer: str, context: dict) -> tuple[dict, dict, int | None]:
    """Return usable DTO values and their registered paths; no history guessing."""
    envelope = observation.envelope
    if envelope["platform"] != "bilibili" or envelope["state"] != "success":
        raise ValueError("metadata receipt requires successful Bilibili source evidence")
    payload = json.loads(observation.payload_json or "null")
    bvid = context.get("bvid")
    if not isinstance(bvid, str) or _BVID_PATTERN.fullmatch(bvid) is None:
        raise ValueError("invalid metadata receipt video identity")
    operation, endpoint, scope = envelope["operation"], envelope["endpoint"], envelope["scope"]
    if normalizer == "bilibili-summary/v1":
        if (set(context) != {"bvid", "ownerMid", "itemPointer"} or operation != "creator-page"
                or endpoint != "user/info/video" or set(scope) != {"creatorId", "pageNumber", "pageSize"}
                or not scope["creatorId"].isdecimal() or int(scope["creatorId"]) < 1
                or type(context["ownerMid"]) is not int or context["ownerMid"] < 1
                or not isinstance(context["itemPointer"], str)
                or re.fullmatch(r"/list/(?:vlist/)?[0-9]+", context["itemPointer"]) is None):
            raise ValueError("invalid summary receipt acquisition context")
        item = _at_pointer(payload, context["itemPointer"])
        summary = _normalize_video_summary_item(item, context["ownerMid"])
        if summary.bvid != bvid:
            raise ValueError("source receipt selected another video")
        keys = {"title": "title", "aid": "aid", "pic": "pic", "desc": "description", "tid": "typeid",
                "pubdate": "created" if item.get("created") is not None else "pubdate"}
        values = {field: getattr(summary, field) for field in keys if getattr(summary, field) is not None}
        return values, {field: context["itemPointer"] + "/" + keys[field] for field in values}, summary.mid
    if set(scope) != {"externalVideoId"} or scope["externalVideoId"] != bvid:
        raise ValueError("source receipt scope differs from accepted video")
    if normalizer == "bilibili-details/v1":
        if set(context) != {"bvid"} or operation != "details" or endpoint != "video/info/detail":
            raise ValueError("invalid details receipt acquisition context")
        read = normalize_video_metadata(payload, bvid)
        values = {field.field: field.value for field in read.fields if field.state in {"present", "empty"}}
        return values, {field: "/" + field for field in values}, read.summary.mid
    if normalizer == "bilibili-completed-aid/v1":
        if (set(context) != {"bvid", "ownerMid"} or operation != "details" or endpoint != "video/info/detail"
                or type(context["ownerMid"]) is not int or context["ownerMid"] < 1):
            raise ValueError("invalid completed-aid receipt acquisition context")
        completed = _complete_summary_from_detail(VideoSummary(bvid, None, "receipt", 0, context["ownerMid"]), payload)
        return {"aid": completed.aid}, {"aid": "/aid"}, completed.mid
    if normalizer == "bilibili-parts/v1":
        if set(context) != {"bvid", "fallbackTitle", "documentPointer"} or not isinstance(context["fallbackTitle"], str):
            raise ValueError("invalid parts receipt acquisition context")
        if operation == "parts" and endpoint == "video/info/pages" and context["documentPointer"] == "":
            parts, owner = _normalize_video_parts(payload, bvid, context["fallbackTitle"]), None
        elif operation == "details" and endpoint == "video/info/detail" and context["documentPointer"] == "/pages":
            read = normalize_video_metadata(payload, bvid)
            if read.parts is None or read.summary.title != context["fallbackTitle"]:
                raise ValueError("parts fallback differs from verified detail DTO")
            parts, owner = read.parts, read.summary.mid
        else:
            raise ValueError("parts receipt acquisition scope differs")
        return {"parts": parts_value(parts)}, {"parts": context["documentPointer"]}, owner
    if normalizer == "bilibili-tags/v1":
        if set(context) != {"bvid"} or operation != "tags" or endpoint != "video/info/tags":
            raise ValueError("invalid tags receipt acquisition context")
        return {"tags": tags_value(_normalize_video_tags(payload))}, {"tags": ""}, None
    raise ValueError("unknown Bilibili source normalizer")


def verify_source_receipt(receipt: SourceFieldReceipt, observation: PlatformObservation,
                          *, bvid: str, owner_mid: int | None = None, normalized: tuple | None = None) -> None:
    document = receipt.document
    if (document["context"].get("bvid") != bvid
            or binding_tuple(document["source"]) != binding_tuple(binding(observation))):
        raise ValueError("accepted metadata source digest or identity differs")
    values, pointers, source_owner = normalized or receipt_values(observation, document["normalizer"], document["context"])
    field = document["field"]
    if (field not in values or canonical(values[field]) != canonical(document["value"])
            or pointers[field] != document["jsonPointer"]
            or (owner_mid is not None and source_owner is not None and source_owner != owner_mid)):
        raise ValueError("accepted metadata differs from its source DTO or owner")


def bind_summary(summary: VideoSummary, observation: PlatformObservation | None,
                 item: Mapping, pointer: str) -> VideoSummary:
    if observation is None:
        return summary
    keys = {"title": "title", "pubdate": "created" if item.get("created") is not None else "pubdate",
            "aid": "aid", "pic": "pic", "desc": "description", "tid": "typeid"}
    context = {"bvid": summary.bvid, "ownerMid": summary.mid, "itemPointer": pointer}
    receipts = tuple(SourceFieldReceipt.create(field=field, value=getattr(summary, field), observation=observation,
                     pointer=pointer + "/" + key, normalizer="bilibili-summary/v1", context=context)
                     for field, key in keys.items() if getattr(summary, field) is not None)
    return replace(summary, source_receipts=receipts)


def bind_completed(summary: VideoSummary, observation: PlatformObservation | None) -> VideoSummary:
    if observation is None:
        return replace(summary, source_receipts=tuple(receipt for receipt in summary.source_receipts if receipt.field != "aid"))
    receipt = SourceFieldReceipt.create(field="aid", value=summary.aid, observation=observation,
        pointer="/aid", normalizer="bilibili-completed-aid/v1", context={"bvid": summary.bvid, "ownerMid": summary.mid})
    return replace(summary, source_receipts=tuple(item for item in summary.source_receipts if item.field != "aid") + (receipt,))


def bind_parts(parts: tuple[VideoPart, ...], observation: PlatformObservation | None,
               bvid: str, fallback: str, pointer: str = "") -> tuple[VideoPart, ...]:
    if observation is None:
        return parts
    receipt = SourceFieldReceipt.create(field="parts", value=parts_value(parts), observation=observation,
        pointer=pointer, normalizer="bilibili-parts/v1",
        context={"bvid": bvid, "fallbackTitle": fallback, "documentPointer": pointer})
    return tuple(replace(part, source_receipt=receipt) for part in parts)


def bind_metadata(read: VideoMetadataRead, observation: PlatformObservation | None) -> VideoMetadataRead:
    if observation is None:
        return read
    bvid = read.summary.bvid
    receipts = tuple(SourceFieldReceipt.create(field=field.field, value=field.value, observation=observation,
                     pointer="/" + field.field, normalizer="bilibili-details/v1", context={"bvid": bvid})
                     for field in read.fields if field.state in {"present", "empty"})
    return replace(read, summary=replace(read.summary, source_receipts=receipts),
                   parts=None if read.parts is None else bind_parts(read.parts, observation, bvid, read.summary.title, "/pages"))
