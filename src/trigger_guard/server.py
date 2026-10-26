"""Bounded extension of the V1 loopback text adapter (single API worker)."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
import time
import uuid

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .concurrency import Admission, ByteBudget, OutputBuffer, Overloaded, TransportFailure
from .egress import EgressProgram
from .protocol import Publication


class CompletionRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    model:str='trigger-guard-lab'
    prompt:str=Field(min_length=1,max_length=32768)
    n:int=Field(default=1,ge=1,le=1)
    max_tokens:int=Field(default=128,ge=1,le=256)
    temperature:float=Field(default=0,ge=0,le=2)
    top_p:float=Field(default=1,gt=0,le=1)
    seed:int=42
    stream:bool=True


class TimedStreamingResponse(StreamingResponse):
    def __init__(self,*args,send_timeout=5,on_close=None,**kwargs):
        self.send_timeout=send_timeout
        self.on_close=on_close
        super().__init__(*args,**kwargs)

    async def stream_response(self,send):
        async def timed(message):
            # Measures an actual blocked ASGI transport send, not token generation.
            await asyncio.wait_for(send(message),self.send_timeout)
        await super().stream_response(timed)

    async def __call__(self,scope,receive,send):
        try:
            await super().__call__(scope,receive,send)
        finally:
            with anyio.CancelScope(shield=True):
                try:await self.body_iterator.aclose()
                finally:
                    if self.on_close is not None:await self.on_close()


class BodyLimit:
    def __init__(self,app,limit=131072):
        self.app=app;self.limit=limit

    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('method')!='POST':
            return await self.app(scope,receive,send)
        chunks=[];size=0
        while True:
            message=await receive()
            if message['type']=='http.disconnect':
                return
            size+=len(message.get('body',b''))
            if size>self.limit:
                return await Response('request body too large',status_code=413)(scope,receive,send)
            chunks.append(message)
            if not message.get('more_body',False):
                break
        async def bounded_receive():
            if chunks:return chunks.pop(0)
            return await receive()
        await self.app(scope,bounded_receive,send)


def make_app(engine,config,policy,configure=None):
    bounds=config.get('bounds',{})
    admission=Admission(**bounds.get('admission',{}))
    admission_ceiling=(admission.engine_limit,admission.waiting_limit)
    budget=ByteBudget(bounds.get('global_pending_bytes',8*1024*1024))
    per_request=bounds.get('per_request_pending_bytes',128*1024)
    cleanup_timeout=bounds.get('cleanup_timeout',10)
    program=EgressProgram(policy['rules'])
    trace_dir=Path(config['trace_dir']);trace_dir.mkdir(parents=True,exist_ok=True)
    active={};retiring={};cleanup_failures=[]
    app=FastAPI()
    app.add_middleware(BodyLimit)
    state=app.state
    state.admission=admission;state.budget=budget;state.active=active;state.retiring=retiring
    effective_engine={}
    if hasattr(engine,'vllm_config'):
        vc=engine.vllm_config
        effective_engine=dict(model=vc.model_config.model,dtype=str(vc.model_config.dtype),
            max_model_len=vc.model_config.max_model_len,enforce_eager=vc.model_config.enforce_eager,
            kv_cache_dtype=vc.cache_config.cache_dtype,
            enable_prefix_caching=vc.cache_config.enable_prefix_caching,
            max_num_seqs=vc.scheduler_config.max_num_seqs,
            max_num_batched_tokens=vc.scheduler_config.max_num_batched_tokens,
            async_scheduling=vc.scheduler_config.async_scheduling,
            tensor_parallel_size=vc.parallel_config.tensor_parallel_size,
            pipeline_parallel_size=vc.parallel_config.pipeline_parallel_size,
            num_speculative_tokens=vc.speculative_config.num_speculative_tokens if vc.speculative_config else 0,
            speculative_method=vc.speculative_config.method if vc.speculative_config else None)

    def persist_trace(uid,trace):
        if (trace.get('t_native_reclaimed_ns') and trace.get('t_publication_closed_ns')
                and not trace.get('persisted')):
            trace['persisted']=True
            with (trace_dir/(uid+'.json')).open('x',encoding='utf-8') as f:
                json.dump(trace,f,ensure_ascii=False)

    async def reclaim_loop():
        while True:
            if retiring:
                ids=list(retiring)
                try:
                    if hasattr(engine,'engine_core'):
                        snap=await asyncio.wait_for(engine.engine_core.call_utility_async(
                            'trigger_guard_resources',ids),cleanup_timeout)
                    else:
                        snap=await engine.resource_snapshot(ids)
                    for uid in snap['reclaimed']:
                        item=retiring.pop(uid,None)
                        if item:
                            lease,started,trace=item
                            admission.release_engine(lease)
                            trace['t_native_reclaimed_ns']=time.perf_counter_ns()
                            trace['cleanup_seconds']=(trace['t_native_reclaimed_ns']-started)/1e9
                            persist_trace(uid,trace)
                    if any((time.perf_counter_ns()-item[1])/1e9>cleanup_timeout for item in retiring.values()):
                        raise TimeoutError('native resource reclamation deadline')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    admission.failed=True
                    if not cleanup_failures:cleanup_failures.append(type(exc).__name__+': '+str(exc))
            await asyncio.sleep(.05)

    @asynccontextmanager
    async def lifespan(_app):
        reclaimer=asyncio.create_task(reclaim_loop())
        yield
        for task in list(active.values()):task.cancel()
        if active:
            await asyncio.wait(list(active.values()),timeout=cleanup_timeout)
        reclaimer.cancel()
        await asyncio.gather(reclaimer,return_exceptions=True)
        if hasattr(engine,'shutdown'):engine.shutdown()
    app.router.lifespan_context=lifespan

    @app.get('/health')
    async def health():
        nonlocal policy,program,trace_dir
        if hasattr(engine,'check_health'):await engine.check_health()
        control_path=config.get('runtime_control_path')
        if (configure and control_path and not active and not retiring and not admission.streams
                and not admission.inflight and not admission.waiters):
            control=json.loads(Path(control_path).read_text(encoding='utf-8'))
            if control['version']!=config.get('control_version'):
                limits=(control.get('engine_limit',admission.engine_limit),
                        control.get('waiting_limit',admission.waiting_limit))
                if (type(limits[0]) is not int or type(limits[1]) is not int or
                        not 1<=limits[0]<=admission_ceiling[0] or not 0<=limits[1]<=admission_ceiling[1]):
                    raise ValueError('private admission setting exceeds startup budget')
                new_policy=configure(control)
                new_program=EgressProgram(new_policy['rules'])
                new_dir=Path(control['trace_dir']).resolve()
                root=Path(config['experiment_root']).resolve()
                if not new_dir.is_relative_to(root):raise ValueError('trace outside experiment root')
                new_dir.mkdir(parents=True,exist_ok=True)
                policy,program,trace_dir=new_policy,new_program,new_dir
                admission.engine_limit,admission.waiting_limit=limits
                config.update(mode=control['mode'],policy_path=control['policy_path'],
                              group=control['group'],control_version=control['version'])
        # Readiness must not enqueue a blocking utility behind cold kernel
        # compilation or generation. Reclaimer owns per-request confirmation;
        # the idle/drain check below obtains the full native snapshot.
        native={'probe_state':'deferred_while_busy'} if active or retiring else {}
        if hasattr(engine,'engine_core') and not active and not retiring:
            native=await asyncio.wait_for(engine.engine_core.call_utility_async('trigger_guard_resources',[]),cleanup_timeout)
        return dict(status='failed' if admission.failed else 'ok',active_requests=len(active),
            retiring_requests=len(retiring),admission=admission.snapshot(),mode=config['mode'],
            admission_config=dict(engine_limit=admission.engine_limit,waiting_limit=admission.waiting_limit,
                                  stream_limit=admission.stream_limit),
            pending_bytes=budget.used,pending_peak=budget.peak,cleanup_failures=cleanup_failures,
            policy_sha256=hashlib.sha256(json.dumps(policy,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
            policy_rule_count=len(policy['rules']),control_version=config.get('control_version'),native=native,
            engine_args=config.get('engine_args'),effective_engine=effective_engine,
            evidence=config.get('evidence','real_model'))

    @app.get('/metrics')
    async def metrics():
        from prometheus_client import generate_latest,CONTENT_TYPE_LATEST
        raw=generate_latest()
        fields={'guard_active_requests':len(active),'guard_retiring_requests':len(retiring),
                'output_pending_bytes':budget.used,'output_pending_peak_bytes':budget.peak,
                'guard_admission_rejected_total':admission.rejected}
        from vllm.v1.engine.output_processor import RequestOutputCollector
        fields.update(collector_pending_bytes=RequestOutputCollector.pending_bytes_total,
                      collector_pending_peak_bytes=RequestOutputCollector.pending_bytes_peak,
                      collector_overflow_total=RequestOutputCollector.overflow_total)
        raw+=''.join(f'{k} {v}\n' for k,v in fields.items()).encode()
        return Response(raw,media_type=CONTENT_TYPE_LATEST)

    @app.post('/abort/{uid}')
    async def abort(uid:str):
        if uid in active:active[uid].cancel()
        return {'ok':True}

    @app.post('/v1/completions')
    async def completions(body:CompletionRequest,request:Request):
        from vllm.sampling_params import SamplingParams,RequestOutputKind
        if body.model!='trigger-guard-lab':raise HTTPException(400,'unknown model')
        try:admission.open_stream()
        except Overloaded as exc:raise HTTPException(503,str(exc),headers={'Retry-After':'1'})
        entered=time.perf_counter_ns()
        try:
            tokenizer=(engine.input_processor.renderer.get_tokenizer() if hasattr(engine,'input_processor') else engine.tokenizer)
            prompt_ids=tokenizer.encode(body.prompt,add_special_tokens=False)
            if len(prompt_ids)+body.max_tokens>config.get('engine_args',{}).get('max_model_len',4096):
                raise HTTPException(400,'prompt and output exceed fixed model context')
            sampling=SamplingParams(max_tokens=body.max_tokens,temperature=body.temperature,
                top_p=body.top_p,seed=body.seed,output_kind=RequestOutputKind.DELTA)
            lease=await admission.acquire(len(prompt_ids)+body.max_tokens,len(body.prompt.encode()),
                                           bounds.get('admission_timeout',2))
        except (Overloaded,TimeoutError) as exc:
            admission.close_stream()
            raise HTTPException(429,type(exc).__name__+': '+str(exc),headers={'Retry-After':'1'})
        except BaseException:
            admission.close_stream();raise
        # User header is only correlation. Every attempt receives a fresh native
        # lifecycle; vLLM appends its own randomized internal identity as well.
        uid=str(uuid.uuid4())
        trace=dict(request_id=uid,evidence=config.get('evidence','real_model'),
            t_ingress_ns=entered,t_admitted_ns=time.perf_counter_ns(),
            completion_tokens=0,prompt_tokens=len(prompt_ids),notice_count=0,
            core_guard_hits=0,egress_only_hits=0,core_event=None,outcome='pending')
        if config.get('candidate_trace'):trace['synthetic_model_token_ids']=[]
        channel=OutputBuffer(budget,per_request)
        current_policy=policy;current_mode=config['mode'];current_program=program
        producer_started=False

        def chunk(text='',finish=None):
            return dict(id=uid,object='text_completion',created=int(time.time()),model=body.model,
                choices=[dict(index=0,text=text,logprobs=None,finish_reason=finish)])
        def frame(data):return ('data: '+json.dumps(data,ensure_ascii=False)+'\n\n').encode()

        async def produce():
            nonlocal producer_started
            producer_started=True
            pub=None;stream=None;native_done=False
            try:
                pub=Publication(current_policy,current_mode,program=current_program)
                stream=engine.generate(prompt={'prompt_token_ids':prompt_ids},sampling_params=sampling,request_id=uid)
                async with asyncio.timeout(bounds.get('request_deadline',180)):
                    async for output in stream:
                        native_done=output.finished
                        if not output.outputs:
                            if output.finished and not pub.done:raise RuntimeError('terminal without completion')
                            continue
                        if len(output.outputs)!=1:raise RuntimeError('unsupported multiple completions')
                        c=output.outputs[0]
                        event=getattr(c,'trigger_guard_event',None)
                        trace['completion_tokens']+=len(c.token_ids)
                        if config.get('candidate_trace'):trace['synthetic_model_token_ids'].extend(c.token_ids)
                        if event:trace['core_event']=event
                        if is_dataclass(output.metrics):trace['native_engine_metrics']=asdict(output.metrics)
                        if c.finish_reason in {'error','abort'}:raise RuntimeError('engine ended without normal completion')
                        before=time.perf_counter_ns()
                        emitted=pub.process(c.text,event,c.finish_reason)
                        trace['egress_cpu_ns']=trace.get('egress_cpu_ns',0)+time.perf_counter_ns()-before
                        terminal={'notice':None,'finish':None}
                        for kind,value in emitted:
                            if kind=='model':channel.put(frame(chunk(text=value)))
                            elif kind=='notice':
                                terminal['notice']=value;trace['t_notice_enqueue_ns']=time.perf_counter_ns()
                            elif kind=='finish':terminal['finish']=value
                        if pub.done:
                            channel.finish(terminal)
                            trace['outcome']='policy' if pub.notice_count else 'normal'
                            break
                    if not pub.done:raise RuntimeError('stream ended without finish')
            except asyncio.CancelledError:
                trace['outcome']='cancelled'
                channel.fail(TransportFailure('cancelled'))
            except Exception as exc:
                trace['outcome']='error';trace['error']=type(exc).__name__+': '+str(exc)
                channel.fail(exc)
            finally:
                with anyio.CancelScope(shield=True):
                    try:
                        async with asyncio.timeout(cleanup_timeout):
                            if stream is not None and not native_done:
                                await engine.abort(uid)
                            if stream is not None:await stream.aclose()
                    except Exception as exc:
                        admission.failed=True
                        trace['cleanup_error']=type(exc).__name__
                    finally:
                        trace.update(notice_count=pub.notice_count if pub else 0,core_guard_hits=pub.core_hits if pub else 0,
                            egress_only_hits=pub.egress_only_hits if pub else 0,t_app_generation_end_ns=time.perf_counter_ns())
                        active.pop(uid,None)
                        # Reclaimer checks the real scheduler after abort. HTTP
                        # closure alone never returns an engine admission slot.
                        retiring[uid]=(lease,time.perf_counter_ns(),trace)

        task=asyncio.create_task(produce());active[uid]=task
        publication_close_lock=asyncio.Lock()
        async def close_publication():
            with anyio.CancelScope(shield=True):
                async with publication_close_lock:
                    if trace.get('t_publication_closed_ns'):return
                    never_started=not producer_started
                    if not task.done() and channel.terminal is None:task.cancel()
                    try:await asyncio.wait_for(task,cleanup_timeout)
                    except asyncio.CancelledError:
                        if not never_started:admission.failed=True
                    except TimeoutError:admission.failed=True
                    finally:
                        if never_started:
                            # No await separates the flag check and cancellation.
                            # The engine generator was never entered, so this
                            # lease has no native resource to await or abort.
                            active.pop(uid,None);admission.release_engine(lease)
                            now=time.perf_counter_ns()
                            trace.update(outcome='cancelled',t_app_generation_end_ns=now,
                                t_native_reclaimed_ns=now,cleanup_seconds=0,
                                reclamation_proof='producer_never_started')
                        channel.close();admission.close_stream()
                        trace['t_publication_closed_ns']=time.perf_counter_ns()
                        persist_trace(uid,trace)
        async def wire():
            try:
                while (payload:=await channel.next()) is not None:
                    yield payload
                    channel.sent()
                terminal=channel.terminal
                if terminal['notice'] is not None:
                    trace['t_notice_yield_ns']=time.perf_counter_ns()
                    yield frame(chunk(text=terminal['notice']))
                yield frame(chunk(finish=terminal['finish']))
                yield b'data: [DONE]\n\n'
                trace['t_publication_done_ns']=time.perf_counter_ns()
            finally:
                await close_publication()
        if body.stream:
            return TimedStreamingResponse(wire(),media_type='text/event-stream',
                send_timeout=bounds.get('consumer_no_progress_seconds',5),on_close=close_publication,
                headers={'x-request-id':uid})
        # Reuse the exact publication path for non-streaming; output is bounded by
        # model context/max_tokens and the same byte budgets.
        parts=[];finish=None
        async for payload in wire():
            if payload==b'data: [DONE]\n\n':continue
            obj=json.loads(payload.decode()[6:]);item=obj['choices'][0]
            parts.append(item['text']);finish=item['finish_reason'] or finish
        result=chunk(''.join(parts),finish)
        result['usage']=dict(prompt_tokens=len(prompt_ids),completion_tokens=trace['completion_tokens'],
                             total_tokens=len(prompt_ids)+trace['completion_tokens'])
        return result
    return app
