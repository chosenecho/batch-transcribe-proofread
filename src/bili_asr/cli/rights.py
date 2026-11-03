"""Explicit permission evidence and part-wide takedown holds."""
from bili_asr.archive_modes import ArchiveAccessMode
from bili_asr.cli.registry import ArtifactPolicy


def rights_command(connection, args):
    from bili_asr.content_rights import (
        initialize_rights,
        record_rights,
        require_publication_enabled,
        rights_status,
    )
    require_publication_enabled()
    from bili_asr.publication import get_edition
    action = args.rights_action
    if action == 'init':
        initialize_rights(connection)
        from bili_asr.compliance import compliance_enabled
        return {'initialized': True, 'existing_content': 'private-only-until-authorized'
                if compliance_enabled() else 'permissions-not-inferred'}
    if action == 'show':
        return rights_status(connection, get_edition(connection, args.edition_id))
    permission = None
    if action == 'grant':
        from bili_asr.cli.json_input import _json_object
        permission = _json_object(args.permission_file)
    return record_rights(connection, action=action, actor=args.actor, note=args.note,
                         edition_id=getattr(args, 'edition_id', None),
                         video_part_id=getattr(args, 'video_part_id', None), permission=permission,
                         artifact_roots=args.artifact_roots.read_bases() if action == 'grant' else ())


def add_rights_parser(actions, *, archive_root, common):
    parser = actions.add_parser('rights', help='Record content permissions independently of editorial review')
    parser.set_defaults(requires_publication_enabled=True)
    subparsers = parser.add_subparsers(dest='rights_action', required=True)
    init = subparsers.add_parser('init', help='Add empty rights ledger without changing existing evidence')
    common(init, archive_root, database=ArchiveAccessMode.WRITE)
    show = subparsers.add_parser('show', help='Inspect current permission or takedown hold')
    common(show, archive_root)
    show.add_argument('--edition-id', required=True)
    for action in ('grant', 'revoke', 'hold', 'clear'):
        sub = subparsers.add_parser(action)
        common(sub, archive_root, actor=True, note=True, database=ArchiveAccessMode.WRITE,
               artifacts=ArtifactPolicy.READ if action == 'grant' else ArtifactPolicy.NONE)
        if action in {'grant', 'revoke'}:
            sub.add_argument('--edition-id', required=True)
        else:
            sub.add_argument('--video-part-id', required=True, type=int)
        if action == 'grant':
            sub.add_argument('--permission-file', required=True, help='Private evidence and exact permission scope JSON')
