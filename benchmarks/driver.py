"""Bounded closed/open-loop driver with planned-arrival accounting and no retries."""
import asyncio
import codecs
import json
import math
from pathlib import Path
import random
import sys
import time
import uuid

import httpx

from benchmarks.reference_oracle import LiteralOracle


class SSEParser:
    def __init__(self):
        self.decoder=codecs.getincrementaldecoder('utf-8')('strict')
        self.buffer='';self.done=False
    def feed(self,raw,final=False):
        self.buffer+=self.decoder.decode(raw,final=final)
        if len(self.buffer)>1048576:raise ValueError('SSE frame budget')
        self.buffer=self.buffer.replace('\r\n','\n')
        result=[]
        while '\n\n' in self.buffer:
            frame,self.buffer=self.buffer.split('\n\n',1)
            values=[line[5:].lstrip(' ') for line in frame.split('\n') if line.startswith('data:')]
            if not values:continue
            payload='\n'.join(values)
            if self.done:raise ValueError('frame after DONE')
            if payload=='[DONE]':self.done=True;result.append(None)
            else:result.append(json.loads(payload))
        if final and self.buffer.strip():raise ValueError('incomplete final SSE frame')
        return result


def workload(tokenizer,profile='S',kind='no_trigger',index=0):
    targets={'short':32,'S':512,'M':2048,'L_bound':3584,'O_bound':1024}
    outputs={'short':64,'S':64,'M':256,'L_bound':256,'O_bound':256}
    if kind=='early':user='只输出这两个汉字，不加引号，不解释：花朵'
    elif kind=='middle':user='请逐字复制，不要解释：今天我们一起散步，在公园看到了花朵，然后走回家。'
    elif kind=='late':user='请逐字复制，不要解释：早晨大家整理好书包，沿着河边缓缓前进，经过石桥和草地，最后看到一片花朵。'
    elif kind=='near_miss':user='Repeat exactly: ZXQ_COMMON_PREFIX_SHARED_PATH_000000X_END. This is an identifier.'
    else:user=['Explain binary search in English. Give a clear example and several steps.',
               'Explain why the sky is blue in English. Use a few complete sentences.',
               'List the first twelve positive integers and explain how to add them in English.'][index%3]
    def template(s):return tokenizer.apply_chat_template([{'role':'user','content':s}],tokenize=False,
        add_generation_prompt=True,enable_thinking=False)
    base=template(user)
    # Padding is synthetic supplied context, not output. Trim to the existing
    # context limit; no max_model_len changes to fit a requested profile.
    if profile!='short':
        target=targets[profile]
        filler='Background note: a numbered record describes a quiet river and a small bridge. '
        padded=(filler*((target//15)+4))+'\nTask: '+user
        base=template(padded)
        ids=tokenizer.encode(base,add_special_tokens=False)
        while len(ids)>target and padded.startswith(filler):
            padded=padded[len(filler):];base=template(padded);ids=tokenizer.encode(base,add_special_tokens=False)
    ids=tokenizer.encode(base,add_special_tokens=False)
    return dict(prompt=base,prompt_tokens=len(ids),max_tokens=outputs[profile],profile=profile,case=kind)


async def request_one(client,url,item,policy,planned,case_id,slow=0,disconnect=False,no_read=False):
    oracle=policy.get('_oracle') or LiteralOracle(policy['rules'])
    row=dict(case_id=case_id,profile=item['profile'],case=item['case'],t_planned_arrival=planned,
             N=len(policy['rules']),policy_version=policy['policy_version'],
             t_client_dispatch=time.perf_counter(),status='pending',prompt_tokens=item['prompt_tokens'],
             max_tokens=item['max_tokens'],slow_read_seconds=slow,disconnect_requested=disconnect,
             no_read_then_disconnect=no_read,
             retry_count=0,actual_positive=None,evidence='real_inference_wire')
    row.update(t_client_headers=None,t_client_first_content=None,t_client_notice_first=None,
               t_client_notice_complete=None,t_client_notice_first_byte=None,t_client_notice_last_byte=None,
               t_client_final=None)
    text=[];chunks=[];finishes=0;parser=SSEParser();notice=policy['notice'];observed=''
    notice_wire=json.dumps(notice,ensure_ascii=False)[1:-1].encode('utf-8')
    raw_tail=b'';raw_count=0;segments=[]
    try:
        async with client.stream('POST',url+'/v1/completions',headers={'x-request-id':str(uuid.uuid4())},
            json=dict(prompt=item['prompt'],max_tokens=item['max_tokens'],temperature=0,top_p=1,seed=42,stream=True)) as response:
            row['t_client_headers']=time.perf_counter();row['http_status']=response.status_code
            row['request_id']=response.headers.get('x-request-id')
            if response.status_code!=200:
                row['status']='rejected' if response.status_code in {429,503} else 'http_error'
                row['error_body']=(await response.aread())[:1024].decode(errors='replace')
            elif no_read:
                # Deliberately receive no body for longer than the server send
                # timeout. Kernel buffers may still absorb this bounded output;
                # this alone does NOT prove a blocked ASGI send occurred.
                await asyncio.sleep(6)
                row['status']='cancelled_by_client'
            else:
                async for raw in response.aiter_bytes():
                    now=time.perf_counter()
                    segments.append((raw_count,raw_count+len(raw),now))
                    if row['t_client_notice_last_byte'] is None:
                        joined=raw_tail+raw;found=joined.find(notice_wire)
                        if found>=0:
                            first_offset=raw_count-len(raw_tail)+found
                            row['t_client_notice_first_byte']=next(t for start,end,t in segments if end>first_offset)
                            row['t_client_notice_last_byte']=now
                        raw_tail=joined[-max(0,len(notice_wire)-1):]
                    raw_count+=len(raw)
                    for obj in parser.feed(raw):
                        if obj is None:continue
                        if obj.get('id'):row['request_id']=obj['id']
                        # usage-only/empty choices carry no content and are legal.
                        for choice in obj.get('choices',[]):
                            value=choice.get('text','')
                            if value:
                                text.append(value);chunks.append(now)
                                if row['t_client_first_content'] is None:row['t_client_first_content']=now
                                observed+=value
                                if row['t_client_notice_first'] is None and notice[:4] in observed:
                                    row['t_client_notice_first']=now
                                if row['t_client_notice_complete'] is None and notice in observed:
                                    row['t_client_notice_complete']=now
                            finishes+=int(choice.get('finish_reason') is not None)
                    if disconnect:
                        row['status']='cancelled_by_client';break
                    if slow:await asyncio.sleep(slow)
                if not disconnect:
                    parser.feed(b'',final=True)
                    row['status']='completed' if parser.done and finishes==1 else 'protocol_error'
    except Exception as exc:
        row['status']='client_error';row['error']=type(exc).__name__+': '+str(exc)
    row['t_client_final']=time.perf_counter()
    visible=''.join(text)
    row.update(visible_text=visible,notice_count=visible.count(notice),finish_count=finishes,
        done=parser.done,wire_leak=oracle.first(visible) is not None,
        chunk_intervals=[b-a for a,b in zip(chunks,chunks[1:])],chunk_count=len(chunks),
        client_dispatch_delay=row['t_client_dispatch']-planned)
    row['loadgen_late']=row['client_dispatch_delay']>.05
    return row


async def run_load(url,policy,items,out,mode='closed-loop',concurrency=4,count=64,rate=None,
                   arrival='fixed',seed=42,slow_fraction=0,disconnect_fraction=0,max_outstanding=64,
                   duration=None,no_read_fraction=0):
    if url!='http://127.0.0.1:18871':raise ValueError('target outside allowlist')
    out.mkdir(parents=True,exist_ok=False)
    rng=random.Random(seed);rows=[];pending=set();offered=0
    # Compile the reference outside request timings. Avoid O(N) per request setup.
    oracle=LiteralOracle(policy['rules'])
    policy=dict(policy,_oracle=oracle)
    async with httpx.AsyncClient(timeout=httpx.Timeout(240,pool=1),trust_env=False,
        limits=httpx.Limits(max_connections=max_outstanding,max_keepalive_connections=max_outstanding)) as client:
        start=time.perf_counter()
        async def one(i,planned):
            row=await request_one(client,url,items[i%len(items)],policy,planned,f'{seed}-{i}',
                slow=.05 if rng.random()<slow_fraction else 0,
                disconnect=rng.random()<disconnect_fraction,no_read=rng.random()<no_read_fraction)
            rows.append(row)
            with (out/'requests.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        if mode=='closed-loop':
            next_index=0
            async def worker():
                nonlocal next_index,offered
                while next_index<count and (duration is None or time.perf_counter()-start<duration):
                    i=next_index;next_index+=1;offered+=1
                    await one(i,time.perf_counter())
            await asyncio.gather(*[worker() for _ in range(concurrency)])
        else:
            if not rate or rate<=0:raise ValueError('open-loop rate must come from a measured baseline')
            # Entire arrival schedule is determined before dispatch.
            offsets=[];offset=0
            for i in range(count):
                if duration is not None and offset>duration:break
                offsets.append(offset)
                step=rng.expovariate(rate) if arrival=='poisson' else 1/rate
                offset+=0 if arrival=='burst' and i%16<15 else (16/rate if arrival=='burst' else step)
            for i,offset in enumerate(offsets):
                planned=start+offset;offered+=1
                await asyncio.sleep(max(0,planned-time.perf_counter()))
                pending={t for t in pending if not t.done()}
                if len(pending)>=max_outstanding:
                    row=dict(case_id=f'{seed}-{i}',status='loadgen_dropped',t_planned_arrival=planned,
                             t_client_final=time.perf_counter(),reason='outstanding_budget')
                    rows.append(row)
                    with (out/'requests.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(row)+'\n')
                    continue
                pending.add(asyncio.create_task(one(i,planned)))
            if pending:await asyncio.gather(*pending)
    elapsed=time.perf_counter()-start
    counts={key:sum(r['status']==key for r in rows) for key in sorted({r['status'] for r in rows})}
    summary=dict(N=len(policy['rules']),policy_version=policy['policy_version'],offered=offered,
        sent=sum(r['status']!='loadgen_dropped' for r in rows),outcomes=counts,
        elapsed_seconds=elapsed,completed_rps=counts.get('completed',0)/elapsed,
        wire_leaks=sum(r.get('wire_leak',False) for r in rows),
        notices=sum(r.get('notice_count',0) for r in rows),
        loadgen_late=sum(r.get('loadgen_late',False) for r in rows),mode=mode,concurrency=concurrency,
        requested_rate=rate,arrival=arrival,seed=seed,max_outstanding=max_outstanding,
        normal_completed=sum(r['status']=='completed' and not r.get('notice_count') for r in rows),
        policy_completed=sum(r['status']=='completed' and r.get('notice_count',0)==1 for r in rows),
        planned_arrival_span_seconds=(max(r['t_planned_arrival'] for r in rows)-min(r['t_planned_arrival'] for r in rows)) if rows else 0,
        target_duration_seconds=duration,
        duration_target_met=(duration is None or (bool(rows) and max(r['t_planned_arrival'] for r in rows)-start>=duration*.99)),
        absolute_slo=None,goodput=None,capacity_status='PROVISIONAL_CAPACITY')
    assert offered==len(rows)
    (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    if summary['wire_leaks']:raise RuntimeError('CORRECTNESS_NOT_MET: visible literal leakage')
    return summary
