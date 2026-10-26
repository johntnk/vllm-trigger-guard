"""New bounded adapter with real TCP and synthetic model outputs."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
import unittest

import httpx
import uvicorn
from starlette.requests import ClientDisconnect

import tempfile
from unittest.mock import patch
from types import ModuleType
from trigger_guard.server import make_app

class MockSamplingParams:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

mock_vllm = ModuleType('vllm')
mock_sampling = ModuleType('vllm.sampling_params')
mock_sampling.SamplingParams = MockSamplingParams
mock_sampling.RequestOutputKind = SimpleNamespace(DELTA='delta')

POLICY={'rules':[{'rule_id':'r','text':'花朵'}],'notice':'\n\n【提示】我不能继续了。'}


class FakeEngine:
    tokenizer=SimpleNamespace(encode=lambda s,add_special_tokens=False:list(map(ord,s)))
    def __init__(self):self.active=set();self.aborted=set()
    async def check_health(self):pass
    async def abort(self,uid):
        self.aborted.add(uid)
        await asyncio.sleep(.01)
        self.active.discard(uid)
    async def resource_snapshot(self,ids):return {'reclaimed':[i for i in ids if i not in self.active]}
    async def generate(self,prompt,sampling_params,request_id):
        text=''.join(map(chr,prompt['prompt_token_ids']))
        self.active.add(request_id)
        try:
            if text=='hit':chunks=['花','朵','later']
            elif text=='long':chunks=['safe ']*256
            elif text=='overflow':chunks=['X'*4000]*32
            elif text=='prefill':
                await asyncio.sleep(.15);chunks=['safe']
            else:chunks=['safe']
            for i,chunk in enumerate(chunks):
                await asyncio.sleep(.003)
                if request_id in self.aborted:break
                finish='length' if i==len(chunks)-1 else None
                c=SimpleNamespace(text=chunk,token_ids=[7],trigger_guard_event=None,finish_reason=finish)
                yield SimpleNamespace(outputs=[c],finished=finish is not None,metrics=None)
        finally:self.active.discard(request_id)


class HttpTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules=patch.dict(sys.modules, {'vllm':mock_vllm, 'vllm.sampling_params':mock_sampling})
        cls.modules.start()
        cls.temp=tempfile.TemporaryDirectory()
        cls.engine=FakeEngine()
        cls.trace=Path(cls.temp.name)/'http_mock'
        cls.config=dict(trace_dir=str(cls.trace),mode='core_egress',evidence='mock_engine_real_http',
            bounds=dict(admission=dict(engine_limit=4,stream_limit=16,waiting_limit=4),
                admission_timeout=.05,cleanup_timeout=1,request_deadline=5,
                consumer_no_progress_seconds=.05,per_request_pending_bytes=12000,global_pending_bytes=96000))
        cls.app=make_app(cls.engine,cls.config,POLICY)
        sock=socket.socket();sock.bind(('127.0.0.1',0))
        cls.url=f'http://127.0.0.1:{sock.getsockname()[1]}'
        cls.server=uvicorn.Server(uvicorn.Config(cls.app,log_level='critical'))
        cls.thread=threading.Thread(target=cls.server.run,kwargs={'sockets':[sock]},daemon=True);cls.thread.start()
        until=time.monotonic()+5
        while not cls.server.started and time.monotonic()<until:time.sleep(.01)
        if not cls.server.started:raise RuntimeError('mock HTTP startup')

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit=True
        cls.thread.join(5)
        cls.modules.stop()
        cls.temp.cleanup()

    async def idle(self):
        async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
            for _ in range(60):
                data=(await client.get(self.url+'/health')).json()
                if not data['active_requests'] and not data['retiring_requests'] and not data['admission']['streams']:
                    self.assertEqual(data['admission']['engine_inflight'],0)
                    self.assertEqual(data['pending_bytes'],0);self.assertFalse(data['cleanup_failures'])
                    return data
                await asyncio.sleep(.025)
        self.fail('resources did not drain')

    async def test_HC02_18_22_24_same_external_id_new_attempt(self):
        ids=[]
        async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
            for _ in range(3):
                result=await client.post(self.url+'/v1/completions',headers={'x-request-id':'same-user-id'},
                                         json={'prompt':'hit'})
                self.assertEqual(result.status_code,200)
                frames=[json.loads(s[6:]) for s in result.text.split('\n\n') if s.startswith('data: {')]
                text=''.join(f['choices'][0]['text'] for f in frames)
                self.assertEqual(text,'花'+POLICY['notice']);self.assertIn('[DONE]',result.text)
                self.assertEqual(sum(f['choices'][0]['finish_reason'] is not None for f in frames),1)
                ids.append(frames[0]['id'])
        self.assertEqual(len(set(ids)),3);await self.idle()

    async def test_HC17_disconnect_and_peer_progress(self):
        async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
            async with client.stream('POST',self.url+'/v1/completions',json={'prompt':'long'}) as response:
                async for line in response.aiter_lines():
                    if line.startswith('data: '):break
            result=await client.post(self.url+'/v1/completions',json={'prompt':'normal','stream':False})
            self.assertEqual(result.json()['choices'][0]['text'],'safe')
        await self.idle()

    async def test_HC27_overload_recovers_without_restart(self):
        async with httpx.AsyncClient(timeout=5,limits=httpx.Limits(max_connections=32)) as client:
            results=await asyncio.gather(*[client.post(self.url+'/v1/completions',json={'prompt':'prefill'}) for _ in range(24)])
            statuses=[r.status_code for r in results]
            self.assertIn(200,statuses);self.assertTrue(any(s in {429,503} for s in statuses))
            self.assertTrue(all(s in {200,429,503} for s in statuses))
        await self.idle()
        async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
            result=await client.post(self.url+'/v1/completions',json={'prompt':'prefill'})
            self.assertEqual(result.status_code,200);self.assertIn('[DONE]',result.text)
        await self.idle()

    async def test_HC17_actual_send_timeout_not_prefill_timeout(self):
        # Exercise the ASGI send boundary directly: a stalled socket send is
        # different from the TCP peer sleeping while kernel buffers absorb data.
        app=make_app(FakeEngine(),{**self.config,'trace_dir':str(self.trace/'blocked_send')},POLICY)
        request_body=json.dumps({'prompt':'long'}).encode();called=False
        async def receive():
            nonlocal called
            if not called:
                called=True;return {'type':'http.request','body':request_body,'more_body':False}
            await asyncio.sleep(10);return {'type':'http.disconnect'}
        async def send(message):
            if message['type']=='http.response.body':await asyncio.sleep(10)
        scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'method':'POST',
               'path':'/v1/completions','raw_path':b'/v1/completions','query_string':b'',
               'headers':[(b'content-type',b'application/json')],'scheme':'http',
               'server':('127.0.0.1',1),'client':('127.0.0.1',2),'http_version':'1.1'}
        async with app.router.lifespan_context(app):
            with self.assertRaises((TimeoutError,ClientDisconnect)):
                await asyncio.wait_for(app(scope,receive,send),2)
            for _ in range(60):
                if not app.state.retiring and not app.state.active:break
                await asyncio.sleep(.025)
            self.assertEqual(app.state.admission.inflight,0)
            self.assertEqual(app.state.admission.streams,0)
            self.assertEqual(app.state.budget.used,0)

    async def test_health_does_not_wait_for_busy_native_engine(self):
        engine=FakeEngine();calls=[]
        async def busy_utility(*args):
            calls.append(args)
            await asyncio.sleep(1)
            return {}
        engine.engine_core=SimpleNamespace(call_utility_async=busy_utility)
        app=make_app(engine,{**self.config,'trace_dir':str(self.trace/'busy_health')},POLICY)
        app.state.active['synthetic-active-request']=object()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',trust_env=False) as client:
            result=await asyncio.wait_for(client.get('/health'),.2)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['native']['probe_state'],'deferred_while_busy')
        self.assertEqual(calls,[])

    async def test_HC16_17_disconnect_or_timeout_before_body_iteration_reclaims_every_budget(self):
        for failure in ['disconnect','timeout']:
            app=make_app(FakeEngine(),{**self.config,'trace_dir':str(self.trace/('header_'+failure))},POLICY)
            body=json.dumps({'prompt':'prefill'}).encode();received=False
            async def receive():
                nonlocal received
                if not received:
                    received=True;return {'type':'http.request','body':body,'more_body':False}
                await asyncio.sleep(10);return {'type':'http.disconnect'}
            async def send(message):
                if message['type']=='http.response.start':
                    if failure=='disconnect':raise OSError('closed before first body')
                    await asyncio.sleep(10)
            scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'method':'POST',
                'path':'/v1/completions','raw_path':b'/v1/completions','query_string':b'',
                'headers':[(b'content-type',b'application/json')],'scheme':'http','server':('127.0.0.1',1),
                'client':('127.0.0.1',2),'http_version':'1.1'}
            async with app.router.lifespan_context(app):
                with self.assertRaises((ClientDisconnect,OSError,TimeoutError)):
                    await asyncio.wait_for(app(scope,receive,send),2)
                for _ in range(60):
                    if not app.state.active and not app.state.retiring:break
                    await asyncio.sleep(.025)
                self.assertEqual(app.state.admission.streams,0)
                self.assertEqual(app.state.admission.inflight,0)
                self.assertEqual(app.state.budget.used,0)
                self.assertFalse(app.state.admission.failed)

    async def test_private_admission_tuning_is_drained_and_within_startup_budget(self):
        root=(self.trace/'admission_control').resolve();root.mkdir(exist_ok=True)
        control=root/'control.json'
        control.write_text(json.dumps(dict(version='next',group='v2',mode='core_egress',
            policy_path=str(root/'policy.json'),trace_dir=str(root/'traces'),engine_limit=2,waiting_limit=1)))
        config={**self.config,'trace_dir':str(root/'initial'),'runtime_control_path':str(control),
                'control_version':'initial','experiment_root':str(root)}
        app=make_app(FakeEngine(),config,POLICY,configure=lambda _:POLICY)
        app.state.active['busy']=object()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',trust_env=False) as client:
            first=(await client.get('/health')).json()
            self.assertEqual(first['admission_config']['engine_limit'],4)
            app.state.active.clear()
            second=(await client.get('/health')).json()
            self.assertEqual(second['admission_config']['engine_limit'],2)
            self.assertEqual(second['admission_config']['waiting_limit'],1)
            data=json.loads(control.read_text());data.update(version='too-large',engine_limit=5)
            control.write_text(json.dumps(data))
            with self.assertRaises(ValueError):await client.get('/health')
            self.assertEqual(app.state.admission.engine_limit,2)


if __name__=='__main__':unittest.main(verbosity=2)
