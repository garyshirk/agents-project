"""Check-only preparation for a separately authorized future Resale test. No executable live CLI path."""

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:sys.path.insert(0,str(PROJECT_ROOT))

from arbitrage.database_config import isolated_database_paths
from arbitrage.v2.contracts import ResaleProviderConfig
from arbitrage.v2.live_resale import PROMPT_VERSION, output_schema


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--business-db',type=Path,required=True)
    parser.add_argument('--sessions-db',type=Path,required=True)
    parser.add_argument('--model',required=True)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--authorize-live',action='store_true')
    args=parser.parse_args(argv)
    if args.run:parser.error('Checkpoint 7B-1 is check-only; no live execution path. Obtain separate 7B-2 authorization.')
    paths=isolated_database_paths(args.business_db,args.sessions_db)
    config=ResaleProviderConfig(model=args.model,max_provider_invocations=1,max_research_passes=1,
        max_web_search_calls=3,max_output_tokens=6000,timeout_seconds=45,max_request_bytes=100000,
        max_response_bytes=200000,max_total_tokens=1000000)
    schema=json.dumps(output_schema(),sort_keys=True)
    print(json.dumps(dict(status='CHECK_ONLY_NO_CALL_NO_DB_WRITES',business_db=str(paths.business),sessions_db=str(paths.sessions),
        config=config.model_dump(mode='json'),strategy='A: one provider call / research pass; combination not yet live verified',
        prompt_version=PROMPT_VERSION,schema_sha256=sha256(schema.encode()).hexdigest(),schema_bytes=len(schema.encode()),
        tools=[dict(type='web_search',external_web_access=True,search_context_size='low')],tool_choice='required',
        sdk_retries=0,live_authorization_required=True,commercial_tools=[],live_cli_enabled=False)))
    return 0


if __name__=='__main__':raise SystemExit(main())
