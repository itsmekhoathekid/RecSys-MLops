from copy import deepcopy
import pytest
from jenkins.python.llm_agent_cd.candidate_recovery import plan


def source():
    return [{'product_id':15,'is_active':True,'popularity_score':26,
             'feature_timestamp':'2026-03-28T00:00:00Z','created_timestamp':'2026-03-29T00:00:00Z','feature_version':'item-v1','source_event_id':None}]


def test_recovery_preserves_real_scores_without_personalized_data():
    rows=source();before=deepcopy(rows);p=plan(rows)
    assert p['scores']=={'15':26.0} and p['target']=='candidate:popular:global'
    assert rows==before and len(p['source_checksum'])==64


@pytest.mark.parametrize('change',[{'product_id':-1},{'popularity_score':float('nan')},
    {'popularity_score':None},{'created_timestamp':''},{'is_active':False}])
def test_recovery_rejects_unproven_or_empty_source(change):
    rows=source();rows[0].update(change)
    with pytest.raises(ValueError):plan(rows)


def test_recovery_rejects_duplicate_source_items():
    with pytest.raises(ValueError):plan(source()*2)
