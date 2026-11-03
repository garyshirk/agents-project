"""Separately authorized, one-call smoke test. Default is configuration-only, with no DB writes."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0,str(PROJECT_ROOT))

from arbitrage.database_config import isolated_database_paths
from arbitrage.v2.contracts import ManagerContextLimits, ManagerModelConfig, OpportunitySubmission, WorkflowLimits
from arbitrage.v2.live_manager import LiveManager, OpenAIResponsesClient
from arbitrage.v2.manager import OfflineManager
from arbitrage.v2.persistence import V2Repository
from arbitrage.v2.workflow import OfflineWorkflow


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--business-db',required=True,type=Path)
    parser.add_argument('--sessions-db',required=True,type=Path)
    parser.add_argument('--model',required=True)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--authorize-live',action='store_true')
    args=parser.parse_args(argv)
    paths=isolated_database_paths(args.business_db,args.sessions_db)
    cfg=ManagerModelConfig(model=args.model,decision_type='RESEARCH_REQUEST',max_output_tokens=1500,
        timeout_seconds=45,max_invocations=1,max_attempts=1,max_total_tokens=100000,
        max_request_bytes=80000,max_response_bytes=30000,max_estimated_cost_usd=None)
    if not args.run:
        print(json.dumps(dict(status='CHECK_ONLY_NO_CALL_NO_DB_WRITES',configuration=cfg.model_dump(mode='json'),
                              business_db=str(paths.business),sessions_db=str(paths.sessions))))
        return 0
    if not args.authorize_live:
        parser.error('--run requires explicit --authorize-live; environment/key presence is not authorization')
    # A new isolated store prevents accidental repetition of an uncertain smoke test.
    if paths.business.exists() or paths.sessions.exists():
        parser.error('Use new paths in a fresh directory; inspect/recover prior runs instead of rerunning them')
    from dotenv import load_dotenv
    from openai import OpenAI
    load_dotenv(PROJECT_ROOT/'.env',override=False)
    sdk=OpenAI(max_retries=0,timeout=cfg.timeout_seconds)
    repo=V2Repository(paths.business,session_database_path=paths.sessions)
    repo.initialize_schema()
    accepted=OfflineWorkflow(repo).accept(OpportunitySubmission(original_text='Synthetic smoke test: hypothetical product offered for $40. '
        'No verified inventory, identity, resale market, or costs. Propose useful next research only; do not execute it.',
        scenario_kind='HYPOTHETICAL',human_actor='Authorized smoke-test operator'),
        WorkflowLimits(max_specialist_invocations=1,max_attempts_per_step=1,max_workflow_steps=1,max_calculation_attempts=1),operation_key='smoke-intake')
    context=OfflineManager(repo).build_context(accepted.evaluation.evaluation_id,(),
        limits=ManagerContextLimits(max_artifacts=5,max_characters=40000),
        selection_limitations=('Synthetic smoke test; no real research supplied.',),operation_key='smoke-context')
    try:
        result=LiveManager(repo,client=OpenAIResponsesClient(sdk),mode='LIVE').execute(context.reference,
            'Propose one bounded research request for the most consequential missing evidence. Do not research, calculate, purchase, or list.',
            cfg,step_id='smoke-manager',operation_key='smoke-call',authorize_live=True)
        print(result.model_dump_json())
        return 0 if result.status=='ACCEPTED' else 1
    finally:
        sdk.close()


if __name__=='__main__':
    raise SystemExit(main())
