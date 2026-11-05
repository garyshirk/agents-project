import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from diagnostics import v2_resale_smoke as smoke
from arbitrage.v2.live_resale import LiveResaleAdapter, LiveResaleWorkflow, prepare_request
from arbitrage.v2.contracts import ResaleResearchInput
from arbitrage.v2.persistence import V2PersistenceError, V2Repository
from arbitrage.v2.resale_audit import history
from tests.v2_live_resale_fixtures import FakeProvider, provider_reply, result_from
from tests.v2_resale_fixtures import ResaleFixture


class ResaleDiagnosticTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(prefix='v2-resale-live.');self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        self.business=self.root/'arbitrage.db';self.sessions=self.root/'sessions.db'
        self.args=['--business-db',str(self.business),'--sessions-db',str(self.sessions),'--model',smoke.MODEL]
        self.client=FakeProvider();self.factory=Mock(return_value=self.client)

    def main(self,flags=()):
        output=io.StringIO()
        with redirect_stdout(output),redirect_stderr(io.StringIO()),patch('socket.socket',side_effect=AssertionError('No network')):
            code=smoke.main([*self.args,*flags],client_factory=self.factory)
        return code,json.loads(output.getvalue())

    def run_fake(self,**options):
        with patch('socket.socket',side_effect=AssertionError('No network')):
            return smoke.run_once(self.business,self.sessions,smoke.configuration(smoke.MODEL),
                authorize_live=True,client_factory=self.factory,**options)

    def repo(self):return V2Repository(self.business,session_database_path=self.sessions)

    def test_default_check_only_is_read_only(self):
        before=self.root.stat().st_mtime_ns
        with patch('openai.OpenAI',side_effect=AssertionError('No SDK construction')):
            code,report=self.main()
        self.assertEqual(code,0);self.assertEqual(report['status'],'CHECK_ONLY_NO_CALL_NO_DB_WRITES')
        self.assertEqual(list(self.root.iterdir()),[]);self.assertEqual(self.root.stat().st_mtime_ns,before)
        self.factory.assert_not_called();self.assertFalse(self.client.calls)
        self.assertEqual(report['config']['max_provider_invocations'],1)
        self.assertEqual(report['model_web_search_strict_compatibility'],'UNVERIFIED_LIVE')

    def test_explicit_check_only(self):
        self.assertEqual(self.main(['--check-only'])[0],0)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_each_live_flag_alone_and_conflicts_rejected(self):
        for flags in (['--run'],['--authorize-live'],['--check-only','--run','--authorize-live'],['--check-only','--authorize-live']):
            with self.subTest(flags=flags),self.assertRaises(SystemExit):self.main(flags)
        self.assertEqual(list(self.root.iterdir()),[]);self.factory.assert_not_called()

    def test_both_flags_reach_only_injected_fake_provider(self):
        code,report=self.main(['--run','--authorize-live'])
        self.assertEqual(code,0);self.assertEqual(report['status'],'ACCEPTED')
        self.assertEqual(report['provider_invocation_count'],1);self.assertEqual(report['research_pass_intents'],1)
        self.assertEqual(len(self.client.calls),1);self.assertEqual(report['accepted_comparables'],2)
        self.assertEqual(report['review_state'],'PENDING');self.assertFalse(report['commercial_actions'])
        self.assertEqual(report['evidence_quality'],'HUMAN_REVIEW_REQUIRED')

    def test_application_authorization_cannot_be_bypassed(self):
        with self.assertRaises(ValueError):smoke.run_once(self.business,self.sessions,smoke.configuration(smoke.MODEL),
            authorize_live=False,client_factory=self.factory)
        self.assertEqual(list(self.root.iterdir()),[]);self.factory.assert_not_called()

    def test_protected_and_development_paths_rejected(self):
        root=smoke.PROJECT_ROOT
        for directory in (root,root/'dev_data/v2'):
            with self.subTest(directory=directory),self.assertRaises(ValueError):
                smoke.validate_paths(directory/'arbitrage.db',directory/'sessions.db')

    def test_historical_and_wrong_prefix_rejected(self):
        for directory in ('/tmp/v2-manager-smoke.Cb8O1U','/tmp/v2-manager-smoke.7Kr43P',
                          '/tmp/v2-manager-smoke.Ooc7SW','/tmp/v2-manager-smoke.91j7Cw','/tmp/v2-resale-check.QZJfKN'):
            with self.subTest(directory=directory),self.assertRaises(ValueError):
                smoke.validate_paths(Path(directory)/'arbitrage.db',Path(directory)/'sessions.db')

    def test_nonempty_and_zero_length_database_rejected(self):
        for content in (b'',b'prior data'):
            self.business.write_bytes(content)
            with self.assertRaises(ValueError):smoke.validate_paths(self.business,self.sessions)
        self.assertEqual(self.business.read_bytes(),b'prior data')

    def test_any_existing_marker_or_log_rejected(self):
        (self.root/'prior.log').write_text('preserve')
        with self.assertRaises(ValueError):smoke.validate_paths(self.business,self.sessions)
        self.assertEqual((self.root/'prior.log').read_text(),'preserve')

    def test_traversal_and_relative_paths_rejected(self):
        for path in (Path('relative/arbitrage.db'),self.root/'..'/self.root.name/'arbitrage.db'):
            with self.subTest(path=path),self.assertRaises(ValueError):smoke.validate_paths(path,self.sessions)

    def test_symlink_files_and_directories_rejected(self):
        other=tempfile.TemporaryDirectory(prefix='v2-resale-live.');self.addCleanup(other.cleanup)
        target=Path(other.name)
        self.business.symlink_to(target/'unknown.db')
        with self.assertRaises(ValueError):smoke.validate_paths(self.business,self.sessions)
        alias=target/'alias';alias.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):smoke.validate_paths(alias/'arbitrage.db',alias/'sessions.db')

    def test_mismatched_identical_missing_and_nonprivate_paths_rejected(self):
        for business,sessions in ((self.business,self.business),(self.business,self.root/'wrong.db'),
                (self.business,self.root.parent/'sessions.db'),
                (self.root/'missing'/'arbitrage.db',self.root/'missing'/'sessions.db')):
            with self.subTest(paths=(business,sessions)),self.assertRaises(ValueError):smoke.validate_paths(business,sessions)
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):smoke.validate_paths(self.business,self.sessions)

    def test_model_and_provider_budgets_are_fixed(self):
        with self.assertRaises(ValueError):smoke.configuration('another-model')
        cfg=smoke.configuration(smoke.MODEL)
        for config in (None,cfg.model_copy(update={'max_research_passes':2}),
                       cfg.model_copy(update={'max_provider_invocations':2}),cfg.model_copy(update={'timeout_seconds':60})):
            with self.subTest(config=config),self.assertRaises(ValueError):
                smoke.run_once(self.business,self.sessions,config,authorize_live=True,client_factory=self.factory)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_unknown_tools_retries_and_budget_cli_overrides_rejected(self):
        for args in (['--tools','purchase'],['--retries','1'],['--max-provider-invocations','2']):
            with self.subTest(args=args),self.assertRaises(SystemExit):self.main(args)
        self.factory.assert_not_called()

    def test_accepted_manager_and_scope_validated_before_dispatch(self):
        def callback(prepared,cfg):
            repo=self.repo();data=ResaleResearchInput.model_validate_json(json.loads(prepared.api_json)['input'][1]['content'])
            delegation=smoke.validate_smoke_delegation(repo,data.context.evaluation_id,data.delegation)
            decision=repo.get_artifact(delegation.payload.manager_decision)
            self.assertEqual(decision.payload.proposal.intent.requested_role,'RESALE')
            self.assertEqual(decision.payload.proposal.intent.max_invocations,1)
            self.assertEqual(data.context.original_intake.scenario_kind,'HYPOTHETICAL')
            self.assertEqual(history(repo,data.context.evaluation_id)[-1].payload.stage,'STARTED')
            self.assertEqual(cfg,smoke.configuration(smoke.MODEL))
            self.assertEqual(json.loads(prepared.api_json)['max_tool_calls'],3)
            return provider_reply(result_from(prepared))
        self.client.response=callback
        self.assertEqual(self.run_fake()[1],0)

    def test_unaccepted_decision_rejected_before_client_construction(self):
        def setup(repo):
            fixture=ResaleFixture(repo)
            return fixture.eid,fixture.context.reference
        with patch.object(smoke,'setup_delegation',side_effect=setup),self.assertRaises(V2PersistenceError):self.run_fake()
        self.factory.assert_not_called();self.assertTrue((self.root/smoke.CLAIM).exists())

    def test_wrong_role_rejected_before_client_construction(self):
        wrong=smoke.research_request().model_copy(update={'requested_role':'SOURCING'})
        with patch.object(smoke,'research_request',return_value=wrong),self.assertRaises(V2PersistenceError):self.run_fake()
        self.factory.assert_not_called()

    def test_cross_evaluation_delegation_rejected(self):
        def setup(repo):
            first=ResaleFixture(repo,suffix='one');second=ResaleFixture(repo,suffix='two')
            return first.eid,second.delegation.reference
        with patch.object(smoke,'setup_delegation',side_effect=setup),self.assertRaises(V2PersistenceError):self.run_fake()
        self.factory.assert_not_called()

    def test_unsupported_prepared_tools_rejected(self):
        def prepare(request,cfg):
            prepared=prepare_request(request,cfg);body=json.loads(prepared.api_json)
            body['tools']=[dict(type='function',name='purchase')]
            return prepared.model_copy(update={'api_json':json.dumps(body)})
        with patch.object(smoke,'prepare_request',side_effect=prepare),self.assertRaises(ValueError):self.run_fake()
        self.factory.assert_not_called()

    def test_duplicate_directory_cannot_dispatch_again(self):
        self.run_fake()
        with self.assertRaises(ValueError):self.run_fake()
        self.assertEqual(len(self.client.calls),1)

    def test_refusal_and_incomplete_status_are_not_publication(self):
        from arbitrage.v2.live_resale import ResaleProviderReply
        for status in ('REFUSAL','INCOMPLETE'):
            with self.subTest(status=status),tempfile.TemporaryDirectory(prefix='v2-resale-live.') as directory:
                root=Path(directory);client=FakeProvider(ResaleProviderReply(status=status))
                report,code=smoke.run_once(root/'arbitrage.db',root/'sessions.db',smoke.configuration(smoke.MODEL),
                    authorize_live=True,client_factory=lambda cfg:client)
                self.assertEqual(code,1);self.assertEqual(report['failure']['category'],status)
                self.assertFalse(report['persistence_success']);self.assertEqual(len(client.calls),1)

    def test_timeout_preserves_ambiguity_without_retry(self):
        self.client.response=TimeoutError()
        report,code=self.run_fake()
        self.assertEqual(code,1);self.assertEqual(report['failure']['category'],'TIMEOUT')
        self.assertIsNone(report['web_search_calls']);self.assertFalse(report['web_search_usage_available'])
        self.assertEqual(report['audit_status'],'TIMEOUT');self.assertEqual(len(self.client.calls),1)
        self.assertTrue(history(self.repo(),report['evaluation_id'])[-1].payload.ambiguous)

    def test_attribution_failure_no_evidence_published(self):
        self.client.response=lambda p,c:provider_reply(result_from(p)).model_copy(update={'citations':()})
        report,code=self.run_fake()
        self.assertEqual(code,1);self.assertEqual(report['source_attribution'],'REJECTED')
        self.assertTrue(report['structural_validity']);self.assertFalse(report['persistence_success'])

    def test_publication_rollback_and_recovery_are_explicit(self):
        def callback(prepared,cfg):
            with closing(sqlite3.connect(self.business)) as c:
                c.execute("CREATE TRIGGER fail_comp BEFORE INSERT ON v2_artifacts WHEN NEW.artifact_type='RESALE_COMPARABLE' "
                    "BEGIN SELECT RAISE(ABORT,'Fixture failure'); END")
            return provider_reply(result_from(prepared))
        self.client.response=callback
        report,code=self.run_fake();repo=self.repo()
        self.assertEqual(code,1);self.assertFalse(report['persistence_success']);self.assertEqual(report['unresolved_runs'],1)
        self.assertFalse(repo.list_comparable_history(report['evaluation_id']))
        workflow=LiveResaleWorkflow(repo,adapter=LiveResaleAdapter(FakeProvider(AssertionError('No replay'))))
        workflow.recover(repo.get_workflow(report['evaluation_id']).unresolved_runs[0],decision='RECONCILE',operation_key='recover')
        self.assertEqual(history(repo,report['evaluation_id'])[-1].payload.status,'NOT_APPLIED')
        self.assertEqual(len(self.client.calls),1)

    def test_interruption_preserved_for_restart_reconciliation(self):
        self.client.response=KeyboardInterrupt()
        report,code=self.run_fake();self.assertEqual(code,130)
        self.assertIsNone(report['provider_invocation_count']);self.assertEqual(report['durable_provider_intents'],1)
        repo=self.repo();run=repo.get_workflow(report['evaluation_id']).unresolved_runs[0]
        LiveResaleWorkflow(repo,adapter=LiveResaleAdapter(FakeProvider(AssertionError('No replay')))).recover(
            run,decision='ABANDON',operation_key='recover')
        self.assertEqual(history(repo,report['evaluation_id'])[-1].payload.status,'AMBIGUOUS')

    def test_review_queue_no_estimate_and_no_extra_manager_call(self):
        report,code=self.run_fake();repo=self.repo()
        self.assertEqual(code,0);self.assertFalse(repo.list_calculation_history(report['evaluation_id']))
        self.assertFalse(repo.list_reviews(report['evaluation_id']))
        self.assertIsNone(repo.get_current_summary(report['evaluation_id']))
        artifacts=repo.list_artifacts(report['evaluation_id'])
        self.assertEqual(sum(a.artifact_type=='MANAGER_DECISION' for a in artifacts),1)
        self.assertEqual(sum(a.artifact_type=='MODEL_CALL' for a in artifacts),0)
        self.assertEqual(report['audit_stages'],['STARTED','RECEIVED','VALIDATED','FINISHED'])

    def test_schema_configuration_checked_without_creating_database(self):
        with patch.object(smoke,'output_schema',return_value={'type':'object','properties':{}}),self.assertRaises(SystemExit):
            self.main()
        self.assertEqual(list(self.root.iterdir()),[]);self.factory.assert_not_called()
