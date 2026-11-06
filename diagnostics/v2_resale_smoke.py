"""Controlled single-pass Resale diagnostic. Default is read-only check-only mode."""

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from hashlib import sha256
from pathlib import Path

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:sys.path.insert(0,str(PROJECT_ROOT))

from arbitrage.database_config import isolated_database_paths
from arbitrage.v2.contracts import (
    ManagerContextLimits, ManagerProposal, Producer, ResearchRequest, ResaleProviderConfig,
    ResaleResearchInput, ResaleResearchResult, WorkflowLimits,
)
from arbitrage.v2.live_resale import (
    INSTRUCTIONS, PROMPT_VERSION, LiveResaleAdapter, LiveResaleWorkflow, OpenAIResaleClient,
    output_schema, prepare_request,
)
from arbitrage.v2.manager import OfflineManager
from arbitrage.v2.persistence import V2Repository
from arbitrage.v2.resale_audit import history
from arbitrage.v2.resale_delegation import create_resale_delegation, validate_delegation
from arbitrage.v2.workflow import OfflineWorkflow
from diagnostics.v2_manager_smoke import synthetic_opportunity


MODEL='gpt-5.4-mini'
CLAIM='.resale-smoke.claim'


def configuration(model):
    if model!=MODEL:raise ValueError('This diagnostic supports only gpt-5.4-mini; no substitution')
    return ResaleProviderConfig(model=model,max_provider_invocations=1,max_research_passes=1,
        max_web_search_calls=3,max_output_tokens=6000,timeout_seconds=45,max_request_bytes=100000,
        max_response_bytes=200000,max_total_tokens=1000000)


def research_request():
    return ResearchRequest(kind='RESEARCH_REQUEST',requested_role='RESALE',max_invocations=1,
        objective="Find attributable public eBay resale-market evidence for Nike Pegasus 41 men's US size 10 shoes.",
        target_subject="Nike Pegasus 41 men's US 10, reported unworn with original box; FD2722-099 is synthetic/unverified.",
        scope=(
            'eBay only: prioritize completed-sale claims with completion basis and realized-price limitations.',
            'Separate active asks, sold labels, ended listings and hidden accepted-offer prices; never infer a paid price from an ask.',
            'Use exact or close comparables with material size, width, colorway, condition and packaging differences disclosed.',
            'Preserve attributable source URLs, annotations, uncertainty and missing evidence; no usable evidence is valid.',
            'Do not research Ross inventory, acquisition availability, tax, travel, fee schedules, shipping-cost estimates or other marketplaces.',
            'No profitability calculation, purchase recommendation, listing or other commercial action.'))


def validate_paths(business,sessions):
    """No writes. Only unused private immediate children of an OS temporary root."""
    for path in (Path(business),Path(sessions)):
        if not path.is_absolute() or '..' in path.parts:raise ValueError('Absolute paths without traversal required')
        for ancestor in (path,*path.parents):
            if ancestor.is_symlink() and not (
                    ancestor==Path('/tmp') and ancestor.resolve()==Path('/private/tmp')
                    or ancestor==Path('/var') and ancestor.resolve()==Path('/private/var')):
                raise ValueError('Symlink database paths or directory escapes forbidden')
    paths=isolated_database_paths(business,sessions);root=paths.business.parent
    if paths.sessions.parent!=root or paths.business.name!='arbitrage.db' or paths.sessions.name!='sessions.db':
        raise ValueError('Use arbitrage.db and sessions.db in the same temporary directory')
    if root.is_relative_to(PROJECT_ROOT.resolve()):raise ValueError('Repository/protected paths forbidden')
    if (root.parent not in {Path('/tmp').resolve(),Path(tempfile.gettempdir()).resolve()}
            or not re.fullmatch(r'v2-resale-live\.[A-Za-z0-9_-]{6,}',root.name)):
        raise ValueError('Use a newly created OS temporary directory named v2-resale-live.XXXXXX')
    if not root.is_dir():raise ValueError('Temporary directory must already exist; no automatic creation')
    info=root.stat()
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ValueError('Directory must be private (0700) and owned by current user')
    if list(root.iterdir()):raise ValueError('Directory nonempty/reused/ambiguous; preserve it, do not rerun')
    return paths


def validate_prepared(prepared,config):
    if config!=configuration(config.model):raise ValueError('Controlled budgets cannot change or be omitted')
    body=json.loads(prepared.api_json)
    if (prepared.prompt_version!='v2-resale-public-research-v2' or PROMPT_VERSION!=prepared.prompt_version
            or body.get('model')!=MODEL or body.get('input',[{}])[0].get('content')!=INSTRUCTIONS
            or body.get('tools')!=[dict(type='web_search',external_web_access=True,search_context_size='low')]
            or body.get('tool_choice')!='required' or body.get('max_tool_calls')!=3
            or body.get('include')!=['web_search_call.action.sources'] or body.get('truncation')!='disabled'
            or body.get('max_output_tokens')!=6000 or body.get('parallel_tool_calls') is not False
            or body.get('store') is not False or body.get('stream') is not False
            or body.get('text',{}).get('format')!=dict(type='json_schema',name='resale_research_result',strict=True,schema=output_schema())):
        raise ValueError('Unsupported prompt/schema/tool/provider configuration')


def setup_delegation(repo):
    accepted=OfflineWorkflow(repo).accept(synthetic_opportunity(),WorkflowLimits(max_specialist_invocations=1,
        max_attempts_per_step=1,max_workflow_steps=1,max_calculation_attempts=1),operation_key='smoke-intake')
    eid=accepted.evaluation.evaluation_id
    context=OfflineManager(repo).build_context(eid,(),limits=ManagerContextLimits(max_artifacts=5,max_characters=40000),operation_key='smoke-context')
    proposal=ManagerProposal(opportunity_id=accepted.opportunity.opportunity_id,evaluation_id=eid,context=context.reference,
        author_name='Application-authored controlled diagnostic fixture',rationale='One narrow eBay research pass, not a verified offer.',
        evidence_references=(),limitations=('Original intake is hypothetical; no market evidence supplied.',),
        uncertainties=('Style, condition and offer are synthetic/unverified.',),intent=research_request())
    application=repo.record_manager_decision(context.reference,proposal.model_dump_json(),
        producer=Producer(kind='APPLICATION',name='Controlled synthetic Manager fixture'),operation_key='smoke-manager-decision')
    delegation=create_resale_delegation(repo,eid,application.payload.decision,intended_marketplace='eBay',operation_key='smoke-delegation')
    return eid,delegation.reference


def validate_smoke_delegation(repo,eid,reference):
    with repo._connection() as c:
        artifact=validate_delegation(repo,c,reference,eid)
        if (artifact.payload.research_request!=research_request() or artifact.payload.intended_marketplace!='eBay'
                or artifact.payload.original_intake!=synthetic_opportunity()):
            raise ValueError('Exact synthetic intake and accepted narrow RESALE request required')
        return artifact


def default_client(config):
    # Only after double authorization, fresh-path claim and accepted-delegation validation.
    from dotenv import load_dotenv
    from openai import OpenAI
    load_dotenv(PROJECT_ROOT/'.env',override=False)
    return OpenAIResaleClient(OpenAI(max_retries=0,timeout=config.timeout_seconds))


def summarize(repo,eid,delegation,end,error):
    audits=history(repo,eid);starts=[a for a in audits if a.payload.stage=='STARTED']
    received=[a for a in audits if a.payload.stage=='RECEIVED'];validated=[a for a in audits if a.payload.stage=='VALIDATED']
    final=audits[-1].payload if audits else None;raw=received[-1].payload if received else None
    parsed=None;parse_errors=[]
    if raw and raw.output_text is not None:
        try:parsed=ResaleResearchResult.model_validate_json(raw.output_text)
        except ValueError as failure:
            parse_errors=[dict(location=e['loc'],type=e['type']) for e in failure.errors()] if hasattr(failure,'errors') else [dict(type=type(failure).__name__)]
    snapshot=repo.get_workflow(eid)
    outputs=[a.payload.output_references[0] for a in snapshot.attempts if a.payload.output_references]
    publication=repo.get_artifact(outputs[-1]) if outputs else None
    if publication is None and starts:
        from arbitrage.v2.workflow import output_operation_key
        publication=repo.get_operation_artifact(output_operation_key(repo.get_artifact(starts[-1].payload.research_pass)))
    tools=json.loads(raw.tool_metadata_json or '[]') if raw else []
    provider_body={}
    if raw and raw.raw_response_json:
        try:provider_body=json.loads(raw.raw_response_json)
        except ValueError:pass
    actual_status=provider_body.get('status') if isinstance(provider_body,dict) else None
    queue=next((entry for entry in repo.list_review_queue(limit=100).entries if entry.evaluation.evaluation_id==eid),None)
    return dict(status='ACCEPTED' if publication is not None and error is None and end is not None else 'NOT_ACCEPTED_OR_INTERRUPTED',
        opportunity_id=delegation.payload.opportunity_id,evaluation_id=eid,manager_decision=delegation.payload.manager_decision.model_dump(mode='json'),
        delegation=delegation.reference.model_dump(mode='json'),delegation_id=delegation.payload.delegation_id,
        context_sha256=delegation.payload.context_sha256,prompt_version=final.prompt_version if final else PROMPT_VERSION,
        provider_status=actual_status,adapter_status=raw.status if raw else None,provider_model=final.provider_model if final else None,
        provider_request_id=final.provider_request_id if final else None,provider_response_id=final.provider_response_id if final else None,
        provider_invocation_count=len(received) if len(starts)==len(received) else None,
        durable_provider_intents=len(starts),research_pass_intents=len({a.payload.research_pass for a in starts}),
        research_pass_ids=[a.payload.research_pass.model_dump(mode='json') for a in starts],
        provider_call_sequences=[a.payload.call_sequence for a in starts],provider_success=actual_status=='completed' if actual_status else None,
        provider_schema_tool_acceptance='REQUEST_COMPLETED' if actual_status=='completed' else 'NOT_CONFIRMED',
        schema_version=final.schema_version_name if final else None,
        provider_error_details=provider_body if raw and raw.status=='PROVIDER_ERROR' else None,
        structural_validity=parsed is not None if raw else None,parse_errors=parse_errors,
        source_attribution='VALIDATED_NOT_INDEPENDENTLY_VERIFIED' if validated else 'REJECTED' if final and final.status=='INVALID_SOURCE_ATTRIBUTION' else 'NOT_VALIDATED',
        evidence_quality='HUMAN_REVIEW_REQUIRED',persistence_success=publication is not None,
        accepted_comparables=len(publication.payload.comparable_references) if publication else 0,
        completed_sale_limitations=[dict(type=f.comparable.price_evidence.evidence_type.value,
            completion_basis=f.comparable.price_evidence.completion_basis,realized_price_basis=f.comparable.price_evidence.realized_price_basis,
            limitations=f.comparable.limitations) for f in parsed.findings] if parsed else [],
        web_search_calls=len(tools) if raw and raw.tool_metadata_json is not None else None,
        web_search_usage_available=raw is not None and raw.tool_metadata_json is not None,
        tool_statuses=[t.get('status') for t in tools],
        citations=[c.model_dump(mode='json') for c in raw.citations] if raw else [],
        token_usage=final.usage.model_dump(mode='json') if final and final.usage else None,
        audit_stages=[a.payload.stage for a in audits],audit_status=final.status if final else None,
        request_sha256=final.request_sha256 if final else None,publication=publication.reference.model_dump(mode='json') if publication else None,
        workflow_receipt=end.reference.model_dump(mode='json') if end else None,
        workflow_attempt_statuses=[a.payload.status for a in snapshot.attempts if a.payload.status!='STARTED'],
        evaluation_state=snapshot.evaluation.execution_state.value,review_state=queue.review_state if queue else None,
        unresolved_runs=len(snapshot.unresolved_runs),failure=final.failure.model_dump(mode='json') if final and final.failure else None,
        execution_error=error,no_automatic_replay=True,commercial_actions=False)


def run_once(business,sessions,config,*,authorize_live,client_factory=None):
    """Recheck policy independently of argparse; fake injection is for deterministic tests only."""
    if authorize_live is not True:raise ValueError('Explicit application authorization required')
    if not isinstance(config,ResaleProviderConfig) or config!=configuration(config.model):raise ValueError('Unsupported provider budgets/model')
    paths=validate_paths(business,sessions);root=paths.business.parent
    signature=(root.stat().st_dev,root.stat().st_ino)
    # Never remove this marker, including on setup failure or interruption.
    directory_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(directory_fd)
        if (info.st_dev,info.st_ino)!=signature or os.listdir(directory_fd):raise ValueError('Directory changed before claim')
        fd=os.open(CLAIM,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory_fd)
        with os.fdopen(fd,'w') as claim:claim.write('V2 controlled Resale diagnostic claimed; preserve state, never automatically replay.\n')
    finally:os.close(directory_fd)
    if root.is_symlink() or (root.stat().st_dev,root.stat().st_ino)!=signature:raise ValueError('Directory changed during acquisition')
    repo=V2Repository(paths.business,session_database_path=paths.sessions);repo.initialize_schema()
    eid,reference=setup_delegation(repo);delegation=validate_smoke_delegation(repo,eid,reference)
    prepared=prepare_request(ResaleResearchInput(delegation=reference,context=delegation.payload),config)
    validate_prepared(prepared,config)
    client=None;end=None;error=None;interrupted=False
    try:
        if root.is_symlink() or (root.stat().st_dev,root.stat().st_ino)!=signature:raise ValueError('Directory changed before dispatch')
        client=(client_factory or default_client)(config)
        end=LiveResaleWorkflow(repo,adapter=LiveResaleAdapter(client),mode='LIVE').execute(reference,config,
            operation_key='smoke-resale-call',authorize_live=authorize_live)
    except (Exception,KeyboardInterrupt) as failure:
        error=type(failure).__name__;interrupted=isinstance(failure,KeyboardInterrupt)
    finally:
        if isinstance(client,OpenAIResaleClient):
            try:client.sdk_client.close()
            except Exception as failure:error=error or type(failure).__name__
    try:report=summarize(repo,eid,delegation,end,error)
    except Exception:report=dict(status='REPORT_UNAVAILABLE_PRESERVE_STORAGE',evaluation_id=eid,execution_error=error,no_automatic_replay=True)
    report.update(business_db=str(paths.business),sessions_db=str(paths.sessions))
    return report,130 if interrupted else 0 if report['status']=='ACCEPTED' else 1


def main(argv=None,*,client_factory=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--business-db',type=Path,required=True)
    parser.add_argument('--sessions-db',type=Path,required=True)
    parser.add_argument('--model',required=True,choices=[MODEL])
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--authorize-live',action='store_true')
    args=parser.parse_args(argv)
    if args.run!=args.authorize_live or args.check_only and (args.run or args.authorize_live):
        parser.error('Live requires BOTH --run and --authorize-live; --check-only conflicts with either flag')
    try:
        config=configuration(args.model);paths=validate_paths(args.business_db,args.sessions_db)
        generated=output_schema()
        if (PROMPT_VERSION!='v2-resale-public-research-v2' or generated.get('additionalProperties') is not False
                or 'findings' not in generated.get('properties',{}) or 'proposal_json' in generated.get('properties',{})
                or set(generated.get('required',()))!=set(generated.get('properties',{}))):
            raise ValueError('Unexpected prompt or strict schema policy')
        schema=json.dumps(generated,sort_keys=True)
        if args.run:
            report,code=run_once(paths.business,paths.sessions,config,authorize_live=True,client_factory=client_factory)
            print(json.dumps(report));return code
        print(json.dumps(dict(status='CHECK_ONLY_NO_CALL_NO_DB_WRITES',business_db=str(paths.business),sessions_db=str(paths.sessions),
            config=config.model_dump(mode='json'),prompt_version=PROMPT_VERSION,schema_sha256=sha256(schema.encode()).hexdigest(),
            tools=[dict(type='web_search',external_web_access=True,search_context_size='low')],tool_choice='required',sdk_retries=0,
            live_authorization_required=True,commercial_tools=[],live_cli_enabled=True,
            model_web_search_strict_compatibility='UNVERIFIED_LIVE',research_request=research_request().model_dump(mode='json'),
            delegation_status='CREATED_AND_VALIDATED_ONLY_IN_AUTHORIZED_EXECUTION',
            intake=synthetic_opportunity().model_dump(mode='json'))))
        return 0
    except (ValueError,OSError):parser.error('Preflight/setup rejected; preserve any claimed directory. No retry. Check paths, policy and durable state.')


if __name__=='__main__':raise SystemExit(main())
