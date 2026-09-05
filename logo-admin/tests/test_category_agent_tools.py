"""Category writes stay draft-only through preview, apply, and exact undo."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import json
import subprocess

import psycopg2
import pytest
from pydantic import ValidationError

import categories_draft as draft
import categories_mapping as mapping
import categories_planner as planner
import categories_service
from authorization import AccessContext, HUMAN_ONLY_COMMANDS
from config import get_settings
from db import database
from domain import Conflict, InvalidCommand, PreviewDrift
from mutations import MutationScope
from snapshots import snapshot_scopes, restore_state, states_equal, scope_from_dict, validate_snapshot_state
from staging import stage_write, apply_change_set, undo_change_set, new_change_set, refresh_change_set
from tool_registry import execute_agent_tool, execute_read_tool, UnknownTool, agent_tool_schemas
from tests.test_rule_agent_tools import _admin, _session, USER
from tests.test_categories_mapping import _snapshot as seed_snapshot, _seed_product_state


@pytest.fixture(autouse=True)
def category_access(monkeypatch):
    for key, value in {
        'AGENT_ENABLED':'true', 'AGENT_WRITES_ENABLED':'true', 'AGENT_ALLOWED_USERS':USER,
        'OPENAI_API_KEY':'test-key', 'OPENAI_MODEL':'test-model', 'AGENT_WRITE_TOOLS':'',
        'CATMGR_ENABLED':'true', 'CATMGR_VIEW_USERS':USER,
        'CATMGR_PROD_URL':'https://category.example.test', 'CATMGR_PROD_USER':'fixture',
        'CATMGR_PROD_APP_PASSWORD':'fixture-password',
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    # These tools must never make a WordPress request, even when configured.
    def no_network(*args, **kwargs):
        pytest.fail('Category draft tools attempted a WordPress request')
    monkeypatch.setattr(categories_service, 'fetch_export', no_network)
    yield
    get_settings.cache_clear()


def write(fn, *args, **kwargs):
    with database.cursor(write=True, actor='fixture') as cursor:
        return fn(cursor, *args, **kwargs, actor='fixture')


def read(fn, *args, **kwargs):
    with database.cursor() as cursor:
        return fn(cursor, *args, **kwargs)


def node(name, parent=None, slug=None):
    return write(draft.create_node, name=name, parent_id=parent, slug=slug)


def state():
    # Every editable category table, including IDs, timestamps and audit stamps.
    result = {}
    with database.cursor() as cursor:
        for table, key in [('node','node_id'), ('node_store_override','override_id'),
                           ('slug_map','old_slug'), ('assignment_rule','rule_id'),
                           ('product_assignment','id'), ('uncategorized_ack','sku')]:
            cursor.execute(f'SELECT to_jsonb(t) AS row FROM catmgr.{table} t ORDER BY {key}')
            result[table] = [r['row'] for r in cursor.fetchall()]
    return result


def stage(name, arguments, set_id=None, call_id=None):
    cs = set_id or new_change_set(_session(), USER)['id']
    return stage_write(cs, name, {'env':'prod', **arguments}, call_id or str(uuid4()), USER, max_items=50)


def apply(staged):
    return apply_change_set(staged['id'], USER, revision=staged['revision'],
                            confirmed_hash=staged['preview_hash'], acknowledge_hard_delete=False)


def scenario(name):
    seed_snapshot()
    _seed_product_state()
    root = node('Clothing')
    child = node('Pants', root['node_id'])
    node('Shirts', root['node_id'])
    node('Footwear')
    write(mapping.set_mapping, old_slug='men-s', action='map', target_node_id=root['node_id'], is_primary=True)
    write(mapping.set_mapping, old_slug='men-s-bottoms', action='map', target_node_id=root['node_id'], is_primary=False)
    override = write(draft.set_override, blog_id=1, kind='extra_node', name='Saws', slug='saws', parent_node_id=root['node_id'])
    write(mapping.set_mapping, old_slug='saws', action='store_custom', override_id=override['override_id'])
    rule = write(mapping.set_rule, node_id=child['node_id'], spec={'field':'sku','op':'prefix','value':'BOOT'})
    write(mapping.set_assignments, node_id=child['node_id'], skus=['112'], mode='add')
    aid = _admin("SELECT id FROM catmgr.product_assignment WHERE sku='112'")[0][0]
    write(planner.set_acks, skus=['ZERO'])
    return {
        'cat_decide': {'rows':[{'old_slug':'footwear-work-boots','action':'move','target_slug':'footwear'}, {'old_slug':'saws','action':'delete'}]},
        'cat_undo_decision': {'old_slug':'men-s-bottoms'},
        'cat_make_surviving': {'old_slug':'men-s-bottoms','target_slug':'clothing'},
        'cat_create_category': {'name':'Accessories','parent':{'path':'Clothing'},'position':0},
        'cat_rename_category': {'category':{'slug':'clothing'},'name':'Workwear','new_slug':'workwear'},
        'cat_move_category': {'category':{'slug':'pants'},'parent':{'slug':'footwear'},'position':0},
        'cat_delete_category': {'category':{'slug':'clothing'},'cascade':True},
        'cat_set_store_override': {'override_id':override['override_id'],'blog_id':1,'kind':'store_only','name':'Tools','slug':'tools','parent':{'slug':'clothing'}},
        'cat_delete_store_override': {'override_id':override['override_id']},
        'cat_accept_uncategorized': {'skus':['ZERO','NEW'],'note':'Deliberate'},
        'cat_unaccept_uncategorized': {'skus':['ZERO']},
        'cat_set_rule': {'rule_id':rule['rule_id'],'category':{'slug':'pants'},'spec':{'field':'sku','op':'equals','value':'112'}},
        'cat_delete_rule': {'rule_id':rule['rule_id']},
        'cat_assign_styles': {'category':{'slug':'pants'},'skus':['112','408045'],'mode':'keep_out'},
        'cat_delete_assignment': {'assignment_id':aid},
    }[name]


TOOLS = ['cat_decide','cat_undo_decision','cat_make_surviving','cat_create_category','cat_rename_category',
         'cat_move_category','cat_delete_category','cat_set_store_override','cat_delete_store_override',
         'cat_accept_uncategorized','cat_unaccept_uncategorized','cat_set_rule','cat_delete_rule',
         'cat_assign_styles','cat_delete_assignment']


@pytest.mark.parametrize('name', TOOLS)
def test_each_tool_preview_apply_and_exact_undo(name):
    args = scenario(name)
    before = state()
    audit_before = _admin('SELECT count(*) FROM catmgr.audit_log')[0][0]
    staged = stage(name, args)
    assert state() == before, 'rolled-back preview leaked category rows'
    assert _admin('SELECT count(*) FROM catmgr.audit_log')[0][0] == audit_before
    assert staged['preview_diff']['count'] > 0
    assert apply(staged)['status'] == 'applied'
    assert state() != before
    assert _admin('SELECT count(*) FROM catmgr.audit_log WHERE actor=%s AND action=%s', ('agent:'+USER, name))[0][0] == 1
    if name == 'cat_move_category':
        after = state()['node']
        assert next(n for n in after if n['slug']=='shirts')['sort_order'] == 10
        assert next(n for n in after if n['slug']=='pants')['parent_id'] == next(n for n in after if n['slug']=='footwear')['node_id']
    if name == 'cat_delete_category':
        assert not state()['assignment_rule'] and not state()['product_assignment']
        assert not state()['slug_map'] or all(r['old_slug']=='saws' for r in state()['slug_map'])
    if name == 'cat_make_surviving':
        assert _admin("SELECT old_slug FROM catmgr.slug_map WHERE is_primary") == [('men-s-bottoms',)]
    if name == 'cat_set_store_override':
        assert state()['node_store_override'][0]['previous_slug'] == 'saws'
    if name == 'cat_set_rule':
        assert staged['preview_diff']['category_rule_impacts'][0]['count'] == 1
    assert undo_change_set(staged['id'], USER)['status'] == 'undone'
    assert state() == before, 'exact undo lost a row, identity or audit stamp'


@pytest.mark.parametrize('name', TOOLS)
def test_each_write_refuses_category_permission_change(name, monkeypatch):
    args = scenario(name)
    monkeypatch.setenv('CATMGR_VIEW_USERS','someone-else')
    get_settings.cache_clear()
    before = state()
    with pytest.raises(UnknownTool):
        stage(name,args)
    with pytest.raises(UnknownTool):
        execute_agent_tool(name, {'env':'prod',**args}, AccessContext(USER,USER), get_settings(), session_id=_session(), call_id='denied')
    assert state() == before


def queued_run(status='queued'):
    return _admin("INSERT INTO catmgr.run (env,status,created_by,target_blogs) VALUES ('prod',%s,'fixture',ARRAY[1]) RETURNING run_id", (status,))[0][0]


@pytest.mark.parametrize('status', ['queued', 'running', 'paused'])
@pytest.mark.parametrize('name', TOOLS)
def test_each_write_refuses_active_run(name, status):
    args = scenario(name)
    queued_run(status)
    before = state()
    with pytest.raises(InvalidCommand, match='queued, running, paused or restoring'):
        stage(name,args)
    assert state() == before


def test_finished_runs_do_not_block_and_the_fence_message_names_the_run():
    node('Root')
    for status in ('completed', 'failed', 'cancelled'):
        queued_run(status)
    staged = stage('cat_create_category', {'name':'New'})
    assert staged['preview_diff']['count'] > 0
    rid = queued_run('paused')
    with pytest.raises(InvalidCommand, match=f'run #{rid} is paused'):
        stage('cat_create_category', {'name':'Another'})


def test_empty_view_allowlist_denies_writes_but_keeps_reads(monkeypatch):
    monkeypatch.setenv('CATMGR_VIEW_USERS', '')
    get_settings.cache_clear()
    node('Root')
    before = state()
    with pytest.raises(UnknownTool):
        stage('cat_create_category', {'name':'New'})
    with pytest.raises(UnknownTool):
        execute_agent_tool('cat_create_category', {'env':'prod','name':'New'}, AccessContext(USER,USER), get_settings(), session_id=_session(), call_id='empty-list')
    assert state() == before
    assert execute_read_tool('cat_tree', {'env':'prod'}, AccessContext(USER,USER), get_settings())


@pytest.mark.parametrize('kind,table,key', [
    ('catmgr_slug_map_row','slug_map','old_slug'), ('catmgr_override_row','node_store_override','override_id'),
    ('catmgr_ack_row','uncategorized_ack','sku'), ('catmgr_rule_row','assignment_rule','rule_id'),
    ('catmgr_assignment_row','product_assignment','id'),
])
def test_kernel_each_row_scope_round_trip(kind,table,key):
    scenario('cat_decide')
    row = state()[table][0]
    scope = MutationScope(kind,{key:row[key]})
    before = read(snapshot_scopes,(scope,))
    with database.cursor(write=True, actor='fixture') as cursor:
        # Update avoids deliberately invoking a dependent cascade in a row-only scope.
        cursor.execute(f"UPDATE catmgr.{table} SET note='changed' WHERE {key}=%s",(row[key],)) if table!='node_store_override' else cursor.execute('UPDATE catmgr.node_store_override SET sort_order=999 WHERE override_id=%s',(row[key],))
        # An override's children must be explicitly captured when it is replaced.
        if table=='node_store_override':
            cursor.execute('DELETE FROM catmgr.slug_map WHERE override_id=%s',(row[key],))
        restore_state(cursor,before,expected_scopes=(scope,))
    assert states_equal(read(snapshot_scopes,(scope,)),before)


@pytest.mark.parametrize('operation',['move','cascade'])
def test_whole_draft_round_trip(operation):
    seed_snapshot()
    root=node('Root'); child=node('Child',root['node_id']); other=node('Other',root['node_id'])
    write(mapping.set_mapping,old_slug='men-s',action='map',target_node_id=child['node_id'])
    write(draft.set_override,blog_id=1,kind='rename',node_id=child['node_id'],name='Store name')
    scope=(MutationScope('catmgr_draft',{}),)
    before=read(snapshot_scopes,scope)
    with database.cursor(write=True,actor='fixture') as cursor:
        if operation=='move':
            draft.move_node(cursor,other['node_id'],parent_id=root['node_id'],position=0,actor='fixture')
        else:
            draft.delete_node(cursor,root['node_id'],cascade=True,actor='fixture')
        restore_state(cursor,before,expected_scopes=scope)
    assert states_equal(read(snapshot_scopes,scope),before)


def test_draft_combined_row_cap():
    _admin("INSERT INTO catmgr.node (name,slug) SELECT 'Category '||g,'cat-'||g FROM generate_series(1,1001) g")
    _admin("INSERT INTO catmgr.slug_map (old_slug,action) SELECT 'old-'||g,'delete' FROM generate_series(1,1000) g")
    with pytest.raises(InvalidCommand,match='2,000-row'):
        stage('cat_create_category',{'name':'One more'})
    with pytest.raises(InvalidCommand,match='2,000-row'):
        read(snapshot_scopes,(MutationScope('catmgr_draft',{}),))


def test_draft_journal_rejects_missing_table_and_duplicate_rows():
    node('Root')
    before=read(snapshot_scopes,(MutationScope('catmgr_draft',{}),))
    with pytest.raises(InvalidCommand,match='missing a table'):
        validate_snapshot_state(before[:1])
    bad=deepcopy(before); bad[0]['rows'] += bad[0]['rows']
    with pytest.raises(InvalidCommand,match='repeats a business row'):
        validate_snapshot_state(bad)


def test_create_idempotency_refresh_cumulative_reference_and_undo():
    before=state()
    first=stage('cat_create_category',{'name':'Parent'},call_id='create')
    same=stage_write(first['id'],'cat_create_category',{'env':'prod','name':'Parent'},'create',USER,max_items=50)
    assert same['idempotent']
    second=stage('cat_create_category',{'name':'Child','parent':{'slug':'parent'}},set_id=first['id'])
    refreshed=refresh_change_set(second['id'],USER)
    assert state()==before
    apply(refreshed)
    assert len(state()['node'])==2
    undo_change_set(second['id'],USER)
    assert state()==before


@pytest.mark.parametrize('name,args',[
    ('cat_set_rule',{'category':{'slug':'clothing'},'spec':{'field':'sku','op':'prefix','value':'BOOT'}}),
    ('cat_set_store_override',{'blog_id':1,'kind':'rename','category':{'slug':'clothing'},'name':'Store Clothing'}),
    ('cat_set_store_override',{'blog_id':1,'kind':'hide','category':{'slug':'clothing'}}),
    ('cat_set_store_override',{'blog_id':1,'kind':'store_only','name':'Store Extra','parent':{'slug':'clothing'}}),
])
def test_new_rule_and_override_id_reservations(name,args):
    scenario('cat_decide');before=state()
    staged=stage(name,args)
    apply(refresh_change_set(staged['id'],USER))
    undo_change_set(staged['id'],USER)
    assert state()==before


def test_delete_with_products_requires_explicit_permission_and_bad_batch_is_atomic():
    scenario('cat_decide');before=state()
    args={'rows':[{'old_slug':'men-s','action':'delete'}]}
    with pytest.raises(InvalidCommand,match='allow_products=true'):
        stage('cat_decide',args)
    staged=stage('cat_decide',{**args,'allow_products':True});apply(staged);undo_change_set(staged['id'],USER)
    assert state()==before
    args={'rows':[{'old_slug':'saws','action':'keep'},{'old_slug':'men-s','action':'move','target_slug':'missing'}]}
    with pytest.raises(InvalidCommand,match='target'):
        stage('cat_decide',args)
    assert state()==before


def test_implicit_decision_has_nothing_to_undo():
    seed_snapshot(); node('Saws')
    with pytest.raises(InvalidCommand,match='no mapping'):
        stage('cat_undo_decision',{'old_slug':'saws'})


@pytest.mark.parametrize('change',['queued','running','restore','permission','allowlist'])
@pytest.mark.parametrize('moment',['apply','undo'])
def test_fences_rechecked_after_stage_and_apply(change,moment,monkeypatch):
    node('Root'); staged=stage('cat_create_category',{'name':'New'})
    if moment=='undo': apply(staged)
    before=state()
    if change=='permission': monkeypatch.setenv('CATMGR_VIEW_USERS','other')
    elif change=='allowlist': monkeypatch.setenv('AGENT_WRITE_TOOLS','set_logo_name')
    elif change=='restore':
        rid=queued_run('paused')
        _admin("INSERT INTO catmgr.run_job (run_id,blog_id,seq,payload,progress) VALUES (%s,1,1,'{}','{\"restore\":{\"status\":\"running\"}}')",(rid,))
    else: queued_run(change)
    get_settings.cache_clear()
    with pytest.raises((InvalidCommand,UnknownTool)):
        apply(staged) if moment=='apply' else undo_change_set(staged['id'],USER)
    assert state()==before


def test_undo_create_refuses_new_cascading_dependents():
    staged=stage('cat_create_category',{'name':'Root'});apply(staged)
    nid=state()['node'][0]['node_id']
    write(mapping.set_rule,node_id=nid,spec={'field':'sku','op':'equals','value':'112'})
    before=state()
    with pytest.raises(InvalidCommand,match='outside its reviewed scopes'):
        undo_change_set(staged['id'],USER)
    assert state()==before


def test_scope_discovery_refuses_new_cascade_rows_before_apply():
    args=scenario('cat_delete_category'); staged=stage('cat_delete_category',args)
    nid=next(n['node_id'] for n in state()['node'] if n['slug']=='clothing')
    write(mapping.set_assignments,node_id=nid,skus=['LATE'],mode='add')
    before=state()
    with pytest.raises(InvalidCommand,match='related rows changed'):
        apply(staged)
    assert state()==before


def test_lookup_full_path_and_counts_mapping_pages_and_read_routes(client_as):
    scenario('cat_decide')
    context=AccessContext(USER,USER); settings=get_settings()
    result=execute_read_tool('cat_node_lookup',{'env':'prod','path':'Clothing / Pants'},context,settings)
    assert result['node']['slug']=='pants'
    assert result['node']['products']==0
    assert client_as().get('/api/categories/node-lookup',params={'env':'prod','slug':'pants'}).json()==result
    rows=execute_read_tool('cat_mapping_rows',{'env':'prod','filter':['men-s','men-s-bottoms'],'limit':1},context,settings)
    assert len(rows['rows'])==1 and rows['next_offset']==1 and rows['truncated']
    assert rows['rows'][0]['products']==1 and rows['rows'][0]['blog_ids']==[1]
    page=client_as().get('/api/categories/mapping-rows',params={'env':'prod','slugs':['men-s','men-s-bottoms'],'limit':1}).json()
    assert page==rows
    for filt in ['undecided','empty','store_only']:
        assert execute_read_tool('cat_mapping_rows',{'env':'prod','filter':filt},context,settings)['rows']
    with pytest.raises(InvalidCommand,match='missing or ambiguous'):
        execute_read_tool('cat_node_lookup',{'env':'prod','slug':'absent'},context,settings)
    with pytest.raises(ValidationError):
        execute_read_tool('cat_node_lookup',{'env':'prod','slug':'pants','path':'Clothing / Pants'},context,settings)


@pytest.mark.parametrize('name,args',[
    ('cat_node_lookup',{'slug':'pants'}),('cat_mapping_rows',{'filter':'undecided'}),
])
def test_new_reads_permissions(name,args,monkeypatch):
    monkeypatch.setenv('CATMGR_VIEW_USERS','other');get_settings.cache_clear()
    with pytest.raises(UnknownTool):
        execute_read_tool(name,{'env':'prod',**args},AccessContext(USER,USER),get_settings())


def test_mcp_read_proxies(monkeypatch):
    import mcp_server
    calls=[]
    monkeypatch.setattr(mcp_server,'_call',lambda *a,**kw:calls.append((a,kw)) or {})
    mcp_server.cat_node_lookup('prod',slug='pants')
    mcp_server.cat_mapping_rows('prod',slugs=['saws'],limit=10)
    assert calls==[(('GET','/api/categories/node-lookup'),{'params':{'env':'prod','slug':'pants'}}),
                   (('GET','/api/categories/mapping-rows'),{'params':{'env':'prod','filter':'undecided','slugs':['saws'],'limit':10,'offset':0}})]


def test_all_human_only_category_actions_excluded_and_private_fields_closed():
    names={s['name'] for s in agent_tool_schemas(writes_enabled=True)}
    expected={'cat_snapshot_import','cat_draft_seed','cat_run_create','cat_run_start','cat_run_pause','cat_run_resume',
              'cat_run_cancel','cat_job_retry','cat_job_skip','cat_restore_blog','cat_job_restore','cat_freeze_set','cat_lock','cat_unlock','cat_drift_audit'}
    assert expected <= HUMAN_ONLY_COMMANDS and names.isdisjoint(expected)
    for name in TOOLS:
        schema=next(s for s in agent_tool_schemas(writes_enabled=True) if s['name']==name)
        assert '_category_state' not in schema['parameters']['properties']
    with pytest.raises(ValidationError):
        stage('cat_create_category',{'name':'x','_category_state':{}})


@pytest.mark.parametrize('name,args',[
    ('cat_decide',{'rows':[{'old_slug':'saws','action':'delete'}]*201}),
    ('cat_assign_styles',{'category':{'slug':'x'},'skus':['a']*201,'mode':'add'}),
    ('cat_accept_uncategorized',{'skus':['a']*201}),
    ('cat_unaccept_uncategorized',{'skus':['a']*201}),
])
def test_call_bounds(name,args):
    with pytest.raises(ValidationError): stage(name,args)


def test_draft_card_has_all_labels_safe_summary_and_reminder():
    from tests.test_agent_ui_security import _assistant_javascript
    source=_assistant_javascript()
    for name in TOOLS: assert 'case "'+name+'"' in source
    assert 'buildCategoryDraftSummary' in source and 'Mapping rows affected:' in source
    assert 'draftTables.includes(c.table)' in source
    assert 'Draft changes saved. Press Check the plan.' in source
    assert 'Decisions removed (check which are undecided)' in source


def test_model_dispatch_stages_category_write_without_applying():
    seed_snapshot()
    before = state()
    result = execute_agent_tool('cat_decide', {'env':'prod', 'rows':[{'old_slug':'saws','action':'keep'}]},
                                AccessContext(USER,USER), get_settings(), session_id=_session(), call_id='model-stage')
    assert result['staged'] is True and result['revision'] == 1
    assert state() == before


def test_existing_override_cannot_silently_ignore_a_different_target():
    root=node('Root'); other=node('Other')
    override=write(draft.set_override,blog_id=1,kind='rename',node_id=root['node_id'],name='Local')
    before=state()
    with pytest.raises(InvalidCommand,match='cannot change its category target'):
        stage('cat_set_store_override', {'blog_id':1,'kind':'rename','override_id':override['override_id'],
              'category':{'slug':'other'},'name':'Wrong target'})
    assert state()==before


def test_rule_rejects_invalid_regex_and_overlong_source():
    node('Root')
    args={'category':{'slug':'root'},'spec':{'field':'sku','op':'regex','value':'['}}
    with pytest.raises(InvalidCommand,match='invalid regex'):
        stage('cat_set_rule',args)
    args['spec']={'field':'sku','op':'equals','value':'A','source':['old']*201}
    with pytest.raises(ValidationError):
        stage('cat_set_rule',args)


def test_review_summary_executes_with_dom_nodes_and_untrusted_text():
    source=(Path(__file__).resolve().parents[1]/'static/app.js').read_text()
    describe=source[source.index('  function describeReviewCommand('):source.index('  function buildReviewChangeTable(')]
    summarize=source[source.index('  function buildCategoryDraftSummary('):source.index('  function renderChangeSet(')]
    changes=[
        {'table':'catmgr.node','before':None,'after':{'name':'Created'}},
        {'table':'catmgr.node','before':{'name':'Before','slug':'old','parent_id':None,'sort_order':10},
         'after':{'name':'After','slug':'new','parent_id':None,'sort_order':10}},
        {'table':'catmgr.node','before':{'name':'Moved','parent_id':None,'sort_order':10},
         'after':{'name':'Moved','parent_id':1,'sort_order':20}},
        {'table':'catmgr.node','before':{'name':'Deleted'},'after':None},
        {'table':'catmgr.slug_map','before':{'old_slug':'<img src=x onerror=alert(1)>'},'after':None},
    ]
    program = """
const assert = require('node:assert/strict');
const text = (x) => String(x ?? '');
const agentNode = (tag, cls, value) => ({tag, cls, textContent: text(value), children: [], append(...nodes) { this.children.push(...nodes); }});
""" + describe + summarize + "\nconst box = buildCategoryDraftSummary(" + json.dumps(changes) + ");" + """
const content = box.children.map((c) => c.textContent).join(' ');
assert.match(content, /Categories created: 1/);
assert.match(content, /renamed or given a new web address: 1/);
assert.match(content, /moved or reordered: 1/);
assert.match(content, /deleted: 1/);
assert.match(content, /Mapping rows affected: 1/);
assert.ok(content.includes('<img src=x onerror=alert(1)>'));
assert.equal(box.children.length, 4);
assert.match(describeReviewCommand({tool_name:'cat_decide', arguments:{rows:[{old_slug:'saws',action:'keep'}]}}, {}), /Keep saws for this store only/);
assert.match(describeReviewCommand({tool_name:'cat_rename_category', arguments:{category:{slug:'old'},new_slug:'new'}}, {}), /redirect/);
"""
    result=subprocess.run(['node','-e',program],text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr


def test_category_read_routes_validate_inputs_and_permissions(client_as, monkeypatch):
    client=client_as()
    for params in [{'env':'prod'}, {'env':'prod','slug':'x','path':'x'}, {'env':'prod','slug':'missing'}]:
        assert client.get('/api/categories/node-lookup',params=params).status_code==422
    assert client.get('/api/categories/node-lookup',params={'env':'dev','slug':'x'}).status_code==404
    assert client.get('/api/categories/mapping-rows',params={'env':'prod','slugs':['x'*201]}).status_code==422
    monkeypatch.setenv('CATMGR_VIEW_USERS','other');get_settings.cache_clear()
    assert client.get('/api/categories/node-lookup',params={'env':'prod','slug':'x'}).status_code==404
    assert client.get('/api/categories/mapping-rows',params={'env':'prod'}).status_code==404


def test_category_tools_are_offered_only_to_callers_the_gate_accepts(monkeypatch):
    def offered(login):
        return {s['name'] for s in agent_tool_schemas(writes_enabled=True, context=AccessContext(login, login), settings=get_settings())}
    names = offered(USER)
    assert set(TOOLS) <= names and {'cat_tree', 'cat_node_lookup', 'cat_mapping_rows'} <= names
    outsider = offered('someone-else')
    assert not any(n.startswith('cat_') for n in outsider) and 'set_logo_name' in outsider
    monkeypatch.setenv('CATMGR_VIEW_USERS', ''); get_settings.cache_clear()
    everyone = offered('someone-else')
    assert 'cat_tree' in everyone and not (set(TOOLS) & everyone), 'an empty allow-list offers category reads but no category writes'
    assert {s['name'] for s in agent_tool_schemas(writes_enabled=True)} >= set(TOOLS), 'no caller means no filtering (registry checks)'
