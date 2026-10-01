import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from fastapi.testclient import TestClient
from repo_agent import core, api

@pytest.fixture
def snapshot(tmp_path):
    repo=tmp_path/'repo'; repo.mkdir()
    (repo/'service.py').write_text('def greet(name):\n    return "hello " + name\n\ndef main():\n    return greet("world")\n')
    (repo/'link.py').symlink_to('/etc/passwd')
    core.run(['git','init',str(repo)])
    core.run(['git','-C',str(repo),'add','.'])
    core.run(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@example.com','commit','-m','fixture'])
    sha=core.run(['git','-C',str(repo),'rev-parse','HEAD']).strip()
    (tmp_path/'snapshot.json').write_text(json.dumps({'url':'https://github.com/example/repo','commit':sha}))
    core.run([sys.executable,'-m','graphify','extract',str(repo),'--code-only','--out',str(tmp_path/'extracted')],cwd=tmp_path)
    graph=json.loads((tmp_path/'extracted/graphify-out/graph.json').read_text())
    for node in graph['nodes']:
        if Path(node.get('source_file','')).is_absolute():
            node['source_file']=str(Path(node['source_file']).relative_to(repo))
    (tmp_path/'graph.json').write_text(json.dumps(graph))
    return tmp_path

def test_url_validation():
    for url in ['file:///tmp/repo','https://github.com.evil/repo/name','https://user:pass@github.com/a/b','https://github.com/a/b?x=1']:
        with pytest.raises(ValueError): core.identity(url,'a'*40)
    assert core.identity('https://github.com/a/b.git','A'*40)[:2]==('https://github.com/a/b','a'*40)
    with pytest.raises(ValueError): core.identity('https://github.com/a/b','main')

def test_graphify_and_immutable_sources(snapshot):
    evidence=core.retrieve(snapshot,'greet')
    assert evidence and 'greet' in evidence[0]['text']
    (snapshot/'repo/service.py').write_text('modified worktree')
    assert core.source(snapshot,'service.py',1,2).startswith('def greet')
    for path in ['../etc/passwd','/etc/passwd','link.py']:
        with pytest.raises(ValueError): core.source(snapshot,path,1,1)
    answer={'claims':[{'text':'A greeting function exists','citations':[evidence[0]['id']]}],'proposal':[],'limitations':[]}
    assert core.verify(answer,evidence,snapshot)
    answer['claims'][0]['citations']=['S999']
    with pytest.raises(ValueError): core.verify(answer,evidence,snapshot)

def test_context_budget():
    ctx=core.Context(mock=True)
    messages, selected, count=ctx.assemble('Explain', [{'id':'S1','text':'x'*10000}],window=2048,output=256)
    assert not selected and count+256+128<=2048
    with pytest.raises(ValueError): ctx.assemble('x'*10000,[],window=2048)

def test_api_mock_and_auth(snapshot,monkeypatch,tmp_path_factory):
    key='a'*24
    root=tmp_path_factory.mktemp('snapshots'); snapshot.rename(root/key)
    monkeypatch.setattr(api,'ROOT',root)
    monkeypatch.setenv('AGENT_API_KEY','test')
    monkeypatch.delenv('SIE_BASE_URL',raising=False)
    with TestClient(api.app) as client:
        assert client.post('/questions',json={'repository_id':key,'question':'greet'}).status_code==401
        response=client.post('/questions',headers={'Authorization':'Bearer test'},json={'repository_id':key,'question':'greet'})
        assert response.status_code==200, response.text
        assert response.json()['sources'] and response.json()['mode']=='mock'
        assert response.json()['inference_usage'] is None
        assert client.get('/metrics').status_code==200

def test_ingestion_pipeline(snapshot, tmp_path_factory, monkeypatch):
    original=core.run
    sha=json.loads((snapshot/'snapshot.json').read_text())['commit']
    def local_fetch(args, cwd=None, timeout=120):
        if 'fetch' in args:
            subprocess.run(['git','-C',args[2],'fetch',str(snapshot/'repo'),sha],check=True,capture_output=True)
            return ''
        return original(args,cwd=cwd,timeout=timeout)
    monkeypatch.setattr(core,'run',local_fetch)
    monkeypatch.setenv('PATH',str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'])
    root=tmp_path_factory.mktemp('ingest')
    key=core.ingest(root,'https://github.com/example/repo',sha)
    assert core.retrieve(root/key,'greet')
    assert core.ingest(root,'https://github.com/example/repo',sha)==key

def test_sie_embeddings_stay_in_agent(monkeypatch):
    import asyncio
    import httpx
    from repo_agent import sie
    original=httpx.AsyncClient
    requests=[]
    def handler(request):
        assert request.url.path=='/v1/embeddings'
        assert request.headers.get('authorization') is None
        requests.append(request)
        if len(requests)==1:
            return httpx.Response(503,json={'detail':'model loading'})
        return httpx.Response(200,json={'data':[{'index':2,'embedding':[1.,0.]},{'index':0,'embedding':[1.,0.]},{'index':1,'embedding':[0.,1.]}]})
    async def no_sleep(_):
        pass
    monkeypatch.setenv('SIE_BASE_URL','http://sie')
    monkeypatch.setattr(sie.asyncio,'sleep',no_sleep)
    monkeypatch.setattr(sie.httpx,'AsyncClient',lambda **kwargs: original(transport=httpx.MockTransport(handler),**kwargs))
    result=asyncio.run(sie.rerank('question',[{'id':'S1','text':'first'},{'id':'S2','text':'second'}]))
    assert len(requests)==2
    assert [x['id'] for x in result]==['S2','S1']

def test_swagger_bearer_security(monkeypatch):
    monkeypatch.setenv('AGENT_API_KEY', 'local-development-only')
    with TestClient(api.app) as client:
        schema = client.get('/openapi.json').json()
        assert schema['components']['securitySchemes']['HTTPBearer'] == {
            'type': 'http', 'description': api.bearer.model.description, 'scheme': 'bearer'
        }
        for route in ('/repositories', '/questions'):
            assert schema['paths'][route]['post']['security'] == [{'HTTPBearer': []}]
            assert not any(p['name'].lower() == 'authorization' for p in schema['paths'][route]['post'].get('parameters', []))
        body = {'repository_id': '0' * 24, 'question': 'test'}
        for header in ('', 'Bearer wrong', 'Basic local-development-only'):
            response = client.post('/questions', json=body, headers={'Authorization': header})
            assert response.status_code == 401
            assert response.headers['www-authenticate'] == 'Bearer'
        response = client.post('/questions', json=body, headers={'Authorization': 'Bearer local-development-only'})
        assert response.status_code == 404  # Authentication passed; snapshot does not exist.

def test_scope_and_overlap(tmp_path, monkeypatch):
    nodes = [dict(id=str(i), source_file=path, source_location=f'L{line}', label='k3s')
             for i, (path, line) in enumerate([('class9/setup.sh', 1), ('class9/setup.sh', 18),
                 ('class9/setup.sh', 32), ('class9b/setup.sh', 1)])]
    (tmp_path/'graph.json').write_text(json.dumps({'nodes':nodes,'edges':[{'source':'0','target':'3'}]}))
    (tmp_path/'snapshot.json').write_text(json.dumps({'url':'https://github.com/a/b','commit':'a'*40}))
    monkeypatch.setattr(core, 'source', lambda snapshot,path,start,end,clamp=False: '\n'.join(str(i) for i in range(start,end+1)))
    assert core.retrieval_scope(tmp_path, 'How does class9 set up k3s?') == 'class9'
    evidence = core.retrieve(tmp_path, 'How does class9 set up k3s?')
    assert len(evidence) == 1
    assert (evidence[0]['path'],evidence[0]['start'],evidence[0]['end']) == ('class9/setup.sh',1,56)
    assert core.retrieve(tmp_path,'k3s',scope='class9b')[0]['path'] == 'class9b/setup.sh'
    with pytest.raises(ValueError): core.retrieve(tmp_path,'k3s',scope='../class9')
    assert core.retrieval_scope(tmp_path,'Compare class9 and class9b') is None


def test_home_page():
    with TestClient(api.app) as client:
        page = client.get('/')
        assert page.status_code == 200
        assert 'text/html' in page.headers['content-type']
        assert 'Repository reader' in page.text
        assert 'generated GraphServe API key' in page.text
        assert 'This page does not share authorization entered in Swagger.' in page.text


def test_real_inference_repairs_uncited_output(snapshot, monkeypatch, tmp_path_factory):
    import httpx
    key = 'b' * 24
    root = tmp_path_factory.mktemp('real-snapshots')
    snapshot.rename(root / key)
    monkeypatch.setattr(api, 'ROOT', root)
    monkeypatch.setattr(api, 'MOCK', False)
    monkeypatch.setenv('AGENT_API_KEY', 'test')
    monkeypatch.delenv('SIE_BASE_URL', raising=False)
    calls = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, json):
            calls.append(json)
            citation = [] if len(calls) == 1 else ['S1']
            proposal = [] if len(calls) == 1 else [
                {'text': 'An unsupported change', 'citations': []}
            ]
            content = {'claims': [{'text': 'A greeting function exists', 'citations': citation}],
                       'proposal': proposal, 'limitations': []}
            request = httpx.Request('POST', url)
            return httpx.Response(200, request=request, json={
                'choices': [{'message': {'content': __import__('json').dumps(content)}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
            })

    monkeypatch.setattr(api.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(api.app) as client:
        monkeypatch.setattr(api, 'context', core.Context(mock=True))
        response = client.post('/questions', headers={'Authorization': 'Bearer test'},
                               json={'repository_id': key, 'question': 'greet'})
    assert response.status_code == 200, response.text
    assert len(calls) == 2
    assert 'VALIDATION RETRY' in calls[1]['messages'][0]['content']
    assert response.json()['inference_attempts'] == 2
    assert response.json()['inference_usage_total']['total_tokens'] == 30
    assert response.json()['answer']['proposal'] == []
    assert 'Omitted 1 model-generated item' in response.json()['answer']['limitations'][-1]
