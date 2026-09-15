from pathlib import Path
import sys
import pytest
from jenkins.python.llm_agent_cd import global_config_dispatch as dispatch


def test_entrypoint_only_dispatches_global_config_through_jenkins():
    entry = Path('ops/helm/deploy_global_model_config.sh').read_text()
    assert 'global_config_dispatch' in entry
    assert 'helm upgrade' not in entry
    assert 'kubectl' not in entry
    assert 'global_model_config_locked.sh' not in entry


def test_global_job_loads_checksum_pinned_source_credential():
    source = Path('jenkins/GlobalModelConfig.Jenkinsfile').read_text()
    assert "credentialsId: 'recsys-workflow-source'" in source
    assert 'sha256sum -c -' in source
    assert '__SOURCE_BUNDLE_SHA256__' in source
    assert 'deployment/recsys-ab-router' not in source
    assert 'deployment/recsys-workflow-router' not in source
    assert "lock(resource: 'recsys-production-release')" in source


def test_locked_global_deploy_uses_stdlib_json_on_jenkins_agent():
    source = Path('jenkins/scripts/deploy/global_model_config_locked.sh').read_text()
    assert 'import json,sys' in source
    assert 'import sys,yaml' not in source


def test_invalid_resource_fails_before_credentials(tmp_path, monkeypatch):
    value = tmp_path / 'values.yaml'
    value.write_text('modelConfig:\n  name: default-model-config\n')
    monkeypatch.setattr(sys, 'argv', ['dispatch', str(value)])
    monkeypatch.setattr(dispatch, 'secret', lambda *a: pytest.fail('must validate before credentials'))
    with pytest.raises(ValueError, match='fixed'):
        dispatch.main()


def test_jenkins_unavailable_has_no_direct_fallback(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['dispatch'])
    def unavailable(*args):
        raise RuntimeError('Jenkins unavailable')
    monkeypatch.setattr(dispatch, 'secret', unavailable)
    with pytest.raises(RuntimeError, match='unavailable'):
        dispatch.main()


def test_jenkins_startup_preserves_scoped_permissions():
    init = Path('infra/helm/recsys-ci/templates/jenkins-init-configmap.yaml').read_text()
    security = init.split('basic-security.groovy: |', 1)[1].split('seed-github', 1)[0]
    assert 'new FullControlOnceLoggedInAuthorizationStrategy()' not in security
    assert 'instance.authorizationStrategy instanceof FullControlOnceLoggedInAuthorizationStrategy' in security
    assert 'PermissionEntry(AuthorizationType.USER, it.id)' in security
    assert 'matrix-auth:3.3' in Path('infra/helm/recsys-ci/values.yaml').read_text()


def test_dispatch_migration_keeps_job_build_separate_from_admin():
    source = Path('ops/gcp/jenkins_workflow_access.py').read_text()
    assert 'acl.add(Item.READ,entry); acl.add(Item.BUILD,entry)' in source
    assert 'strategy.add(Jenkins.READ,entry)' in source
    assert 'strategy.add(Jenkins.ADMINISTER,entry)' not in source
    assert 'workflow-authorization-before-matrix.xml' in source
    assert "existing.isEmpty() || (existing.size() == 1" in source
    assert "existing[0].name == 'workflow-dispatch-v1'" in source
    assert "revokeToken(existing[0].uuid)" in source


def test_manual_rollback_accepts_empty_candidate_parameters():
    source = Path('jenkins/LLMWorkflowCD.Jenkinsfile').read_text()
    assert '--candidate "${AB_CANDIDATE:-}"' in source
    assert '--experiment-id "${AB_EXPERIMENT_ID:-}"' in source
