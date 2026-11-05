"""Simulated provider replies and fictional .invalid marketplace data. Never live observations."""

import json
from arbitrage.v2.contracts import ResaleProviderConfig, ResaleResearchInput
from arbitrage.v2.live_resale import ResaleProviderReply, source_metadata
from tests.v2_resale_fixtures import result_for


def config(**changes):
    return ResaleProviderConfig(**(dict(model='synthetic-provider-model',max_provider_invocations=1,
        max_research_passes=1,max_web_search_calls=3,max_output_tokens=6000,timeout_seconds=45,
        max_request_bytes=100000,max_response_bytes=200000,max_total_tokens=1000000)|changes))


def result_from(prepared,status='COMPLETED'):
    request=ResaleResearchInput.model_validate_json(json.loads(prepared.api_json)['input'][1]['content'])
    data=result_for(request,status).model_dump(mode='json')
    for observation in data['sources']:
        observation['claim_origin']='SPECIALIST_SOURCE_REPORT'
        observation['source']['capture_method']='PUBLIC_WEB_REPORT'
    for finding in data['findings']:
        finding['comparable']['source']=next(s['source'] for s in data['sources'] if s['source_id']==finding['source_id'])
    return data


def provider_reply(data,*,status='COMPLETED',annotations=True,usage=True):
    text=json.dumps(data)
    urls=[s['source']['url'] for s in data.get('sources',[])]
    tool=dict(type='web_search_call',id='ws_synthetic',status='completed',
        action=dict(type='search',query='Fictional shoe lookup',sources=[dict(type='url',url=u) for u in urls]))
    output=[tool,dict(type='message',content=[dict(type='output_text',text=text,annotations=[
        dict(type='url_citation',url=u,title='Invented marketplace fixture',start_index=0,end_index=1) for u in urls] if annotations else [])])]
    raw=dict(id='resp_synthetic_resale',status='completed',model='synthetic-provider-model',output=output)
    citations,tools,_=source_metadata(raw)
    return ResaleProviderReply(status=status,text=text,raw_response_json=json.dumps(raw),citations=citations,
        tool_metadata_json=tools,response_id='resp_synthetic_resale',request_id='req_synthetic_resale',model='synthetic-provider-model',
        usage=dict(input_tokens=1000,output_tokens=1000,total_tokens=2000) if usage else None)


class FakeProvider:
    is_live=False

    def __init__(self,response=None):self.response=response;self.calls=[]

    def invoke(self,prepared,cfg):
        self.calls.append((prepared,cfg))
        if isinstance(self.response,BaseException):raise self.response
        return self.response(prepared,cfg) if callable(self.response) else self.response or provider_reply(result_from(prepared))
