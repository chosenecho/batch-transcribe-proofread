"""Local description observations create editions without changing any prose."""
from copy import deepcopy
import json

import pytest

from bili_asr.canonical_json import digest
from bili_asr.export_snapshot import ExportSnapshotError, _validate_snapshot
from bili_asr.publication import edit_edition, get_edition, publish_edition
from bili_asr.publication_export import export_publication_drafts, export_publications
from bili_asr.publication_origins import validate_origins
from bili_asr.services.source_supplement import (
    apply_source_supplement, check_source_supplement, plan_source_supplement,
)
from bili_asr.services.archive_snapshot import check_snapshot, restore_snapshot, save_snapshot
from bili_asr.archive_session import ArchiveSession, ArchiveAccessMode
from tests.test_source_supplement import preserved, table_rows
from tests.support.content_permissions import grant_test_permission
from tests.test_publication import approve

DESCRIPTION = '中文简介\r\n引用: "哲学" \\ archive\nhttps://example.org/reference'


def describe(connection, editions, value=DESCRIPTION, observed=1234):
    with connection:
        for edition in editions:
            bvid = edition['content']['source']['externalVideoId']
            connection.execute('INSERT INTO video_details(bvid,"desc",observed_at) VALUES (?,?,9999) '
                'ON CONFLICT(bvid) DO UPDATE SET "desc"=excluded."desc",observed_at=9999', (bvid, value))
            if observed is not None:
                connection.execute("INSERT INTO source_metadata_observations VALUES ('bilibili',?,'details','desc','present',?,?,NULL,?) "
                    "ON CONFLICT(platform,external_id,operation,field) DO UPDATE SET value_json=excluded.value_json,last_success_at=excluded.last_success_at",
                    (bvid, observed, observed, json.dumps(value, ensure_ascii=False)))


def plan_for(connection, root, editions):
    return plan_source_supplement(connection, edition_ids=[item['edition_id'] for item in editions],
                                  artifact_roots=(root,), field='description')


@pytest.mark.parametrize('value,observed', [(DESCRIPTION,1234), ('',1234), (DESCRIPTION,None)])
def test_description_preserves_body_summary_history_and_honest_time(preserved, tmp_path, value, observed):
    connection, root, editions = preserved
    edited = edit_edition(connection, edition_id=editions[0]['edition_id'], markdown_text='Human prose.\n',
        metadata={'summary':'Human summary', 'tags':['editorial']}, actor='editor',note='clarification')
    describe(connection,[edited],value,observed)
    protected = ('editorial_inputs','editorial_revisions','editorial_model_calls','publication_releases',
                 'workflow_jobs','workflow_attempts','document_artifacts','manuscript_import_baselines')
    before = table_rows(connection,protected)
    plan = plan_for(connection,root,[edited])
    assert not plan['blocked']
    evidence = plan['entries'][0]['supplement']['evidence']
    assert evidence['observedAt'] == observed
    result = apply_source_supplement(connection,plan=plan,artifact_roots=(root,),actor='operator')
    child = get_edition(connection,result['editions'][0]['editionId'])
    expected = deepcopy(edited['content'])
    expected['source']['metadata']['description'] = value
    assert child['content'] == expected
    assert child['content']['markdown'].encode() == edited['content']['markdown'].encode()
    assert child['review_status'] == 'pending-review'
    assert child['current_release_id'] == edited['current_release_id']
    assert child['revision_id'] == edited['revision_id']
    assert table_rows(connection,protected) == before

    assert get_edition(connection,edited['edition_id'])['content'] == edited['content']
    assert check_source_supplement(connection,edition_id=child['edition_id'],artifact_roots=(root,))['observedAt'] == observed
    later = edit_edition(connection,edition_id=child['edition_id'],markdown_text='Later human prose.\n',actor='editor',note='later')
    assert later['content']['source']['metadata']['description'] == value
    assert apply_source_supplement(connection,plan=plan,artifact_roots=(root,),actor='operator')['idempotent']
    assert get_edition(connection,later['edition_id'])['current_edition_id'] == later['edition_id']
    assert plan_for(connection,root,[later])['skipped'][0]['reason'] == 'already-known'
    # Current database updates cannot rewrite an already frozen edition.
    describe(connection,[later],'New observed description',2000)
    assert get_edition(connection,later['edition_id'])['content']['source']['metadata']['description'] == value


def test_description_evidence_requires_new_profile_and_is_bound_to_catalog(preserved, tmp_path):
    connection,root,editions = preserved
    describe(connection,editions)
    plan = plan_for(connection,root,editions)
    result = apply_source_supplement(connection,plan=plan,artifact_roots=(root,),actor='operator')
    ids = [item['editionId'] for item in result['editions']]
    for edition_id in ids:
        grant_test_permission(connection, get_edition(connection, edition_id), (root,))
    with pytest.raises(ExportSnapshotError,match='require universal-origin-v2'):
        export_publication_drafts(connection,artifact_roots=(root,),output=tmp_path/'old-profile',
            contract_profile='universal-origin-v1',edition_ids=ids)
    output = tmp_path/'new-profile'
    export_publication_drafts(connection,artifact_roots=(root,),output=output,
        contract_profile='universal-origin-v2',edition_ids=ids)
    assert _validate_snapshot(output,'publication-draft-export')
    origins = json.loads((output/'origins.json').read_text(encoding='utf-8'))
    articles = json.loads((output/'catalog.json').read_text(encoding='utf-8'))['articles']
    fact = next(item for item in origins['entries'][0]['metadataEvidence'] if item['field']=='description')
    assert fact['observedAt'] == 1234
    assert articles[0]['sourceMetadata']['description'] == DESCRIPTION
    broken = deepcopy(origins)
    broken['entries'][0]['metadataEvidence'][next(i for i,item in enumerate(broken['entries'][0]['metadataEvidence']) if item['field']=='description')]['observedAt'] = 9999
    with pytest.raises(ExportSnapshotError,match='evidence differs'):
        validate_origins(broken,articles,'publication-draft')
    # Explicit approval/release follows the normal workflow; the migration
    # command itself has neither approved nor released these editions.
    edition = get_edition(connection,ids[0])
    approve(connection,edition)
    release = publish_edition(connection,edition_id=ids[0],artifact_roots=(root,),write_root=root,
        actor='publisher',expected_release_id=edition['current_release_id'])
    published = tmp_path/'publication'
    export_publications(connection,artifact_roots=(root,),output=published,
        contract_profile='universal-origin-v2',release_ids=[release['release_id']])
    assert _validate_snapshot(published,'publication-export')


def test_unknown_stale_and_rehashed_invented_description_are_blocked(preserved):
    connection,root,editions = preserved
    with connection:
        connection.execute('DELETE FROM video_details')
    assert len(plan_for(connection,root,editions)['blocked']) == len(editions)
    describe(connection,editions)
    plan = plan_for(connection,root,editions)
    protected = ('publication_editions','publication_heads','source_metadata_supplements','source_supplement_batches')
    before = table_rows(connection,protected)
    broken = deepcopy(plan)
    supplement = broken['entries'][-1]['supplement']
    supplement['evidence']['value'] = 'Invented description'
    supplement['supplementId'] = digest(supplement['evidence'])
    broken['planDigest'] = digest({key:value for key,value in broken.items() if key!='planDigest'})
    with pytest.raises(ValueError,match='evidence changed or was invented'):
        apply_source_supplement(connection,plan=broken,artifact_roots=(root,),actor='operator')
    assert table_rows(connection,protected) == before
    describe(connection,editions,'Changed source description',2000)
    with pytest.raises(ValueError,match='evidence changed'):
        apply_source_supplement(connection,plan=plan,artifact_roots=(root,),actor='operator')
    assert table_rows(connection,protected) == before


def test_description_cli_plan_apply_check_and_snapshot_restore(preserved,tmp_path,capsys):
    from bili_asr.cli import main
    connection,root,editions = preserved
    describe(connection,editions)
    selection,plan_path = tmp_path/'selection.json',tmp_path/'plan.json'
    selection.write_text(json.dumps({'editionIds':[item['edition_id'] for item in editions]}),encoding='utf-8')
    common = ['publication','supplement-source']
    args = ['--archive-root',str(root),'--format','json']
    assert main(common+['plan',*args,'--field','description','--selection-file',str(selection),'--out',str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)['fieldStats']['field'] == 'description'
    assert main(common+['apply',*args,'--plan-file',str(plan_path),'--actor','operator']) == 0
    result = json.loads(capsys.readouterr().out)
    assert main(common+['check',*args,'--edition-id',result['editions'][0]['editionId']]) == 0
    assert json.loads(capsys.readouterr().out)['description'] == DESCRIPTION
    connection.close()
    package = tmp_path/'description.zip'
    save_snapshot(root,package)
    assert check_snapshot(package)['valid']
    restored = tmp_path/'restored'
    restore_snapshot(package,restored)
    with ArchiveSession(restored,mode=ArchiveAccessMode.READ) as session:
        for item in result['editions']:
            checked = check_source_supplement(session.connection,edition_id=item['editionId'],artifact_roots=(restored,))
            assert checked['valid'] and checked['description'] == DESCRIPTION


def test_failed_latest_request_keeps_success_time_and_invalid_source_is_blocked(preserved):
    connection,root,editions = preserved
    describe(connection,editions)
    with connection:
        connection.execute("UPDATE source_metadata_observations SET state='unavailable',observed_at=9999,error_code='source_unavailable' WHERE field='desc'")
    planned = plan_for(connection,root,editions)
    assert not planned['blocked']
    assert all(item['supplement']['evidence']['observedAt'] == 1234 for item in planned['entries'])
    describe(connection,editions,'x'*65537,2000)
    planned = plan_for(connection,root,editions)
    assert len(planned['blocked']) == len(editions) and not planned['entries']


def test_new_profile_exports_old_title_and_new_description_origins_together(preserved, tmp_path):
    connection, root, editions = preserved
    title_plan = plan_source_supplement(connection, edition_ids=[editions[0]['edition_id']],
                                        artifact_roots=(root,))
    title_result = apply_source_supplement(connection, plan=title_plan, artifact_roots=(root,), actor='operator')
    grant_test_permission(connection, get_edition(connection, title_result['editions'][0]['editionId']), (root,))
    describe(connection, [editions[1]])
    description_plan = plan_for(connection, root, [editions[1]])
    description_result = apply_source_supplement(connection, plan=description_plan, artifact_roots=(root,), actor='operator')
    grant_test_permission(connection, get_edition(connection, description_result['editions'][0]['editionId']), (root,))
    ids = [title_result['editions'][0]['editionId'], description_result['editions'][0]['editionId']]
    output = tmp_path / 'mixed-profiles'
    export_publication_drafts(connection, artifact_roots=(root,), output=output,
                              contract_profile='universal-origin-v2', edition_ids=ids)
    assert _validate_snapshot(output, 'publication-draft-export')
    origins = json.loads((output / 'origins.json').read_bytes())
    assert {item['metadataSupplement']['evidence']['field'] for item in origins['entries']} == {'partTitle', 'description'}
