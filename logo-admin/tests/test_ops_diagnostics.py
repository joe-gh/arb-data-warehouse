"""Operational reads, isolated failures, owner scope, and exact external-store undo."""
import json
import time
from uuid import uuid4
from types import SimpleNamespace
from decimal import Decimal

import pytest
from pydantic import ValidationError
from fastapi.encoders import jsonable_encoder
from psycopg2.extras import Json

from authorization import AccessContext
from db import database
import queries
import wp_bridge
import staging
from mutations import MutationScope
from snapshots import states_equal
from tests.test_rule_agent_tools import _admin, _session, _snapshot, _round_trip, USER
from tests.test_warehouse_ops_tools import _read, _rule, _apply
from tool_registry import execute_read_tool


@pytest.fixture
def mapped_store():
    _admin("DELETE FROM woo.store_blog_map WHERE fdm4_store='S_TEST'")
    _admin("INSERT INTO woo.store_blog_map (blog_id,fdm4_store,blog_path,blog_name) VALUES (900101,'S_TEST','/ops-test/','Ops Test')")
    yield 900101
    _admin("DELETE FROM woo.store_blog_map WHERE blog_id=900101")


def service(function, **kwargs):
    with database.cursor() as cursor:
        return jsonable_encoder(function(cursor,**kwargs))


def test_product_state_parent_siblings_order_totals_and_bounds():
    result = _read("get_product_state",{"store":"S_TEST","sku":"STYLE-1-RED"})
    assert result["parent"]["sku"]=="STYLE-1"
    assert result["totals"]=={"total":4,"variations":3,"in_stock":3,"active":3}
    assert [r["color"] for r in result["variations"]]==["Blue","Green","Red"]
    for row in result["rows"]:
        assert {"base_price","stock","web_active","item_status","changed_at","refreshed_at"}<=row.keys()
    assert _read("get_product_state",{"store":"S_TEST","style":"STYLE-1","limit":2})["truncated"]
    _admin("UPDATE woo.store_product_state SET name=repeat('x',4000), is_active=false WHERE sku='STYLE-1-RED'")
    result = _read("get_product_state",{"store":"S_TEST","style":"STYLE-1"})
    assert result["totals"]["active"]==2
    assert max(len(r["name"] or '') for r in result["rows"])<=1024
    for args in ({"store":"NOPE","style":"STYLE-1"},{"store":"S_TEST","style":"NOPE"}):
        with pytest.raises(queries.QueryNotFound):
            _read("get_product_state",args)
    with pytest.raises(ValidationError):
        _read("get_product_state",{"store":"S_TEST"})


def test_product_state_large_style_keeps_parent_and_caps_rows():
    _admin("""INSERT INTO woo.store_product_state(fdm4_store,catalog_id,sku,kind,style_code,color,size,price,stock,payload,content_hash)
        SELECT 'S_TEST','S_TEST_catalog','OPS-'||n,'variation','STYLE-1',lpad(n::text,4,'0'),'M',10,1,'{}','ops'
        FROM generate_series(1,510) n""")
    result = _read("get_product_state",{"store":"S_TEST","style":"STYLE-1"})
    assert len(result["rows"])==500 and result["parent"] and result["truncated"]
    assert result["totals"]["variations"]==513


def test_stock_uses_item_number_join_and_floors_each_warehouse():
    try:
        _admin('DELETE FROM fdm4.item WHERE "style-code"=%s',('OPS-STOCK',))
        _admin('DELETE FROM fdm4."inv-balance" WHERE "item-number" LIKE %s',('OPS-ITEM-%',))
        for n in range(30):
            _admin('INSERT INTO fdm4.item ("item-number","upc-code","style-code","color-code","size-code") VALUES (%s,%s,%s,%s,%s)',(f'OPS-ITEM-{n}',f'OPS-SKU-{n}','OPS-STOCK','RED',str(n)))
            for warehouse,onhand,committed in [('A','10','3'),('B','2','5')]:
                _admin('INSERT INTO fdm4."inv-balance" ("item-number",warehouse,"inv-bal",committed,"web-active",allocated,"on-order",backordered) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',(f'OPS-ITEM-{n}',warehouse,onhand,committed,'Y','1','4','0'))
        result = _read('get_stock',{'style':'OPS-STOCK'})
        assert len(result['rows'])==60 and not result['truncated']
        assert {r['available'] for r in result['rows']}=={7}
        assert {r['warehouse_available'] for r in result['rows']}=={0,7}
        assert {r['web_active'] for r in result['rows']}=={'Y'}
        assert len(_read('get_stock',{'style':'OPS-STOCK','size_code':'2'})['rows'])==2
        with pytest.raises(queries.QueryNotFound):
            _read('get_stock',{'style':'MISSING-STOCK'})
    finally:
        _admin('DELETE FROM fdm4.item WHERE "style-code"=%s',('OPS-STOCK',))
        _admin('DELETE FROM fdm4."inv-balance" WHERE "item-number" LIKE %s',('OPS-ITEM-%',))


def test_history_merges_actors_sources_filters_and_own_cards(mapped_store):
    mine = staging.new_change_set(_session(),USER)
    staging.stage_write(mine['id'],'set_stock_override',{'style_code':'STYLE-1','mode':'fake'},'mine',USER,max_items=50)
    other_session, other_set = uuid4(),uuid4()
    _admin("INSERT INTO logo.agent_chat_session(id,user_login,title,expires_at) VALUES (%s,'admin-two','Other',now()+interval '1 day')",(other_session,))
    _admin("INSERT INTO logo.agent_change_set(id,session_id,user_login,expires_at) VALUES (%s,%s,'admin-two',now()+interval '1 day')",(other_set,other_session))
    _admin("INSERT INTO logo.agent_change_set_item(id,change_set_id,user_login,call_id,tool_name,arguments,sort_order) VALUES (%s,%s,'admin-two','other','set_stock_override',%s,0)",(uuid4(),other_set,Json({'style_code':'STYLE-1'})))
    _admin("INSERT INTO logo.audit_log(actor,action,fdm4_store,product_style,detail) VALUES ('admin-two','assignment_updated','S_TEST','STYLE-1',%s)",(Json({'changes':{'logo_code':{'from':'OLD','to':'C1'}}}),))
    _admin("INSERT INTO logo.audit_log(at,actor,action,fdm4_store,product_style) VALUES (now()-interval '100 days','old','assignment_updated','S_TEST','STYLE-1')")
    _admin("INSERT INTO logo.bulk_batch(fdm4_store,logo_code,created_by) VALUES ('S_TEST','C1','bulk-operator')")
    _admin("INSERT INTO logo.bulk_batch(fdm4_store,logo_code,created_by,undone_at) VALUES ('S_TEST','C2','bulk-operator',now())")
    _admin("UPDATE woo.price_rule SET stores=ARRAY['S_TEST'],updated_by='pricer' WHERE rule_id=%s",(_rule(),))
    _admin("INSERT INTO woo.sync_exclusion(fdm4_store,style_code,updated_by) VALUES ('S_TEST','STYLE-1','freezer')")
    _admin("INSERT INTO catmgr.audit_log(actor,action,entity,entity_key,detail) VALUES ('categorizer','changed','term','3',%s)",(Json({'blog_id':mapped_store}),))
    result = _read('get_change_history',{'store':'S_TEST'})
    assert {'logo.audit_log','logo.bulk_batch','woo.price_rule','woo.sync_exclusion','catmgr.audit_log'} <= {r['source'] for r in result['rows']}
    batches = [r for r in result['rows'] if r['source']=='logo.bulk_batch']
    assert len(batches)==3 and {'bulk_applied','bulk_undone'} <= {r['action'] for r in batches}, 'an undone batch keeps its applied row'
    assert {'admin-two','pricer','freezer','categorizer'} <= {a['actor'] for a in result['actors']}
    assert 'old' not in {a['actor'] for a in result['actors']}
    result = _read('get_change_history',{'style':'STYLE-1'})
    assert str(mine['id']) in {r['change_set_id'] for r in result['rows']}
    assert str(other_set) not in json.dumps(result)
    assert [r['at'] for r in result['rows']]==sorted([r['at'] for r in result['rows']],reverse=True)
    result = _read('get_change_history',{'logo_code':'C1','actor':'admin-two'})
    assert len(result['rows'])==1 and result['rows'][0]['source']=='logo.audit_log'
    result = _read('get_change_history',{'rule_id':_rule()})
    assert 'woo.price_rule' in {r['source'] for r in result['rows']}
    result = execute_read_tool('get_change_history',{},AccessContext(USER,USER),SimpleNamespace(catmgr_enabled=True,catmgr_view_users=frozenset({'someone-else'})))
    assert 'catmgr.audit_log' not in {r['source'] for r in result['rows']}
    result = execute_read_tool('get_change_history',{},AccessContext(USER,USER),SimpleNamespace(catmgr_enabled=True,catmgr_view_users=frozenset()))
    assert 'catmgr.audit_log' in {r['source'] for r in result['rows']}, 'an empty allow-list means everyone who can see the editor'
    assert _read('get_change_history',{'limit':1})['truncated']
    with pytest.raises(ValidationError):
        _read('get_change_history',{'user_login':'admin-two'})


def test_audit_prices_matches_each_live_rule_preview_and_freeze():
    _admin("UPDATE woo.price_rule SET active=true,stackable=true,stores=ARRAY['S_TEST'],priority=1 WHERE rule_id=%s",(_rule(),))
    _admin("INSERT INTO woo.price_rule(name,active,stackable,priority,effect_type,effect_value,stores) VALUES ('Second',true,true,2,'flat',2,ARRAY['S_TEST'])")
    _admin("UPDATE woo.store_product_state SET base_price=10,price=50 WHERE fdm4_store='S_TEST' AND kind='variation'")
    result = _read('audit_store_prices',{'store':'S_TEST','limit':1})
    assert result['summary']['evaluated']==4 and result['summary']['changed']==4
    assert len(result['sample'])==1 and result['sample'][0]['before_price']==10
    assert result['truncated'] and not result['frozen']
    for rule in result['per_rule']:
        assert rule['affected']==_read('preview_price_rule',{'rule_id':rule['rule_id']})['summary']['affected']
        assert str(rule['rule_id']) in result['rule_names']
    _admin("INSERT INTO woo.sync_exclusion(fdm4_store,style_code,active,scope) VALUES ('S_TEST','',true,'pricing')")
    assert _read('audit_store_prices',{'store':'S_TEST'})['frozen']


def test_bridge_wrappers_get_only_encoded_parameters(monkeypatch):
    calls=[]
    monkeypatch.setattr(wp_bridge,'wp_admin_call',lambda path,**kwargs:calls.append((path,kwargs)) or {})
    wp_bridge.wp_diag_product(5,sku='x&y')
    wp_bridge.wp_diag_category(5,slug='trees')
    wp_bridge.wp_diag_store(5)
    wp_bridge.wp_diag_sync_log(5,limit=100)
    wp_bridge.wp_diag_order(5,order_id=20)
    assert len(calls)==5 and all(kw['method']=='GET' for _,kw in calls)
    assert 'sku=x%26y' in calls[0][0]
    from config import get_settings
    assert all(kw['timeout']==min(get_settings().wp_http_timeout,10) for _,kw in calls), 'diag calls get the same budget as product links'


def test_wordpress_mapping_soft_failures_and_order_allowlist(mapped_store,monkeypatch):
    forbidden={'customer_name':'PRIVATE','email':'PRIVATE','phone':'PRIVATE','address':'PRIVATE','notes':'PRIVATE','ip':'PRIVATE','user_id':999,'billing':{'name':'PRIVATE'},'shipping':{'name':'PRIVATE'}}
    captured=[]
    def order(blog,**kwargs):
        captured.append((blog,kwargs))
        return {'found':True,'order_id':12,'blog_id':blog,'status':'processing',**forbidden,
                'payment':{'method_code':'WooCC','gateway_id':'card',**forbidden},
                'items':[{'sku':'SKU','qty':1,'line_total':'20','embellishment':{'logo_codes':['C1'],'placements':['Chest'],**forbidden},**forbidden}],
                'fdm4':{'status':'processing','last_error':{'email':'PRIVATE'},**forbidden}}
    monkeypatch.setattr(wp_bridge,'wp_diag_order',order)
    result=_read('get_order_status',{'store':'S_TEST','order_id':12})
    assert captured==[(mapped_store,{'order_id':12})]
    assert result['payment']['method_code']=='WooCC' and result['fdm4']['last_error'] is None
    def keys(value):
        if isinstance(value,dict):
            return set(value).union(*(keys(v) for v in value.values()))
        if isinstance(value,list):
            return set().union(*(keys(v) for v in value))
        return set()
    assert keys(result).isdisjoint(forbidden) and 'PRIVATE' not in json.dumps(result)
    monkeypatch.setattr(wp_bridge,'wp_diag_product',lambda *a,**kw:{'found':True,'status':'private','variations':[{'sku':'x'*5000}]*300})
    product=_read('wp_product_check',{'store':'S_TEST','style':'STYLE-1'})
    assert product['blog_id']==mapped_store and len(product['variations'])==200
    assert len(product['variations'][0]['sku'])==1024
    def failed(*a,**kw): raise RuntimeError('SECRET')
    monkeypatch.setattr(wp_bridge,'wp_diag_store',failed)
    monkeypatch.setattr(wp_bridge,'wp_diag_sync_log',lambda *a,**kw:{'lines':[]})
    assert not _read('wp_store_check',{'store':'S_TEST'})['available']
    assert _read('wp_store_check',{'store':'S_TEST'})['sync_log']['available']
    monkeypatch.setattr(wp_bridge,'wp_diag_order',failed)
    assert _read('get_order_status',{'blog_id':mapped_store,'order_id':12})=={'available':False,'reason':'WordPress order diagnostics are unavailable'}
    assert not _read('wp_product_check',{'store':'MISSING','style':'STYLE-1'})['available']


@pytest.mark.parametrize('name,positive,negative',[
    ('no_logos',"DELETE FROM logo.assignment WHERE fdm4_store='S_TEST' AND product_style='STYLE-1'", "UPDATE woo.store_product_state SET is_active=false WHERE fdm4_store='S_TEST'"),
    ('colors_unclassified',"DELETE FROM logo.color_class WHERE color_code IN ('RED','BLU','GRN')", "UPDATE woo.store_product_state SET is_active=false WHERE fdm4_store='S_TEST'"),
    ('rules_expiring',"UPDATE woo.price_rule SET active=true,effective_until=current_date+2,stores=ARRAY['S_TEST']", "UPDATE woo.price_rule SET active=false"),
    ('stores_frozen',"INSERT INTO woo.sync_exclusion(fdm4_store,style_code,created_at) VALUES ('S_TEST','',now()-interval '9 days')", "UPDATE woo.sync_exclusion SET active=false WHERE fdm4_store='S_TEST'"),
    ('stock_overrides_stale',"INSERT INTO woo.stock_override(style_code,mode) VALUES ('OPS-STALE','fake') ON CONFLICT(style_code) DO UPDATE SET mode='fake'", "DELETE FROM woo.stock_override WHERE style_code='OPS-STALE'"),
])
def test_issue_checks_positive_and_clean_negative(name,positive,negative):
    _admin(positive)
    args={'checks':[name],'store':None if name=='stock_overrides_stale' else 'S_TEST'}
    result=_read('find_issues',args)['checks'][0]
    assert result['available'],result
    assert result['count']>0 and result['sample'] and result['how_to_fix']
    _admin(negative)
    # Other retained harness overrides do not concern this fixture.
    result=_read('find_issues',args)['checks'][0]
    if name=='stock_overrides_stale':
        assert all(r['style_code']!='OPS-STALE' for r in result['sample'])
    else:
        assert result['count']==0,result


def test_category_issues_latest_snapshot_access_and_clean(mapped_store):
    _admin("INSERT INTO catmgr.snapshot(env,blog_id,version) VALUES ('dev',%s,2)",(mapped_store,))
    _admin("INSERT INTO catmgr.wp_uncategorized_product(env,blog_id,product_id,sku,snapshot_version) VALUES ('dev',%s,1,'SKU',2),('dev',%s,2,'OLD',1)",(mapped_store,mapped_store))
    args={'store':'S_TEST','checks':['uncategorized_products']}
    result=_read('find_issues',args)['checks'][0]
    assert result['available'] and result['count']==1
    assert not execute_read_tool('find_issues',args,AccessContext(USER,USER),SimpleNamespace(catmgr_enabled=True,catmgr_view_users=frozenset({'someone-else'})))['checks'][0]['available']
    assert execute_read_tool('find_issues',args,AccessContext(USER,USER),SimpleNamespace(catmgr_enabled=True,catmgr_view_users=frozenset()))['checks'][0]['available']
    _admin("UPDATE catmgr.snapshot SET version=3 WHERE blog_id=%s",(mapped_store,))
    assert _read('find_issues',args)['checks'][0]['count']==0


def healthy_product():
    return {'found':True,'sku':'STYLE-1','status':'publish','variations':[{'sku':f'STYLE-1-{c}','status':'publish','price':'10','stock_quantity':1} for c in ('RED','BLU','GRN')]}


@pytest.mark.parametrize('case',['healthy','frozen','excluded','private','price'])
def test_explain_cases(mapped_store,monkeypatch,case):
    live=healthy_product()
    if case=='frozen':
        _admin("INSERT INTO woo.sync_exclusion(fdm4_store,style_code,scope) VALUES ('S_TEST','','pricing')")
    if case=='excluded':
        _admin("INSERT INTO woo.store_mix_store(fdm4_store,mode,active) VALUES ('S_TEST','list',true)")
    if case=='private': live['status']='private'
    if case=='price': live['variations'][0]['price']='13'
    monkeypatch.setattr(wp_bridge,'wp_diag_product',lambda *a,**kw:live)
    result=_read('explain_product',{'store':'S_TEST','style':'STYLE-1'})
    assert result['intent']['visible'] is True and result['wordpress']['available']
    findings=' '.join(result['findings'])
    if case=='healthy': assert result['findings']==[],result
    else: assert {'frozen':'freeze','excluded':'excluded','private':'private','price':'price 13'}[case] in findings


def test_wordpress_issues_positive_clean_and_sql_failure_isolated(mapped_store,monkeypatch):
    monkeypatch.setattr(wp_bridge,'wp_diag_product',lambda *a,**kw:{'found':True,'status':'private'})
    result=_read('find_issues',{'store':'S_TEST','checks':['wordpress_mismatch']})['checks'][0]
    assert result['count']>0
    def healthy(*a,**kw):
        return healthy_product() if kw['style']=='STYLE-1' else {'found':True,'sku':'STYLE-2','status':'publish','variations':[{'sku':'STYLE-2-RED','status':'publish','price':'12','stock_quantity':1}]}
    monkeypatch.setattr(wp_bridge,'wp_diag_product',healthy)
    assert _read('find_issues',{'store':'S_TEST','checks':['wordpress_mismatch']})['checks'][0]['count']==0
    with database.cursor() as cursor:
        failed=queries._ops_section(cursor,lambda:cursor.execute('SELECT nonexistent_ops_column'))
        assert failed['available'] is False
        assert queries.get_product_state(cursor,store='S_TEST',style='STYLE-1')['found']
    start=time.monotonic()
    result=_read('find_issues',{'store':'S_TEST'})
    assert len(result['checks'])==7 and time.monotonic()-start<10


def test_external_store_both_scopes_preview_apply_undo_and_delete():
    _admin("DELETE FROM woo.virtual_catalog_store WHERE fdm4_store='S_TEST'")
    scopes=(MutationScope('virtual_catalog_store_row',{'fdm4_store':'S_TEST'}),MutationScope('store_mix_store_row',{'fdm4_store':'S_TEST'}))
    def during():
        assert _admin("SELECT stock_override FROM woo.virtual_catalog_store WHERE fdm4_store='S_TEST'")==[(Decimal(9999),)]
        assert _admin("SELECT mode,active FROM woo.store_mix_store WHERE fdm4_store='S_TEST'")==[('all',True)]
    _round_trip('set_external_mix_store',{'store':'S_TEST','source':'square'},scopes,'external-set',during=during)
    _admin("INSERT INTO woo.virtual_catalog_store(fdm4_store,catalog_id,note,stock_override,created_at) VALUES ('S_TEST','OLD','retain',42,'2020-01-01')")
    try:
        _round_trip('remove_external_mix_store',{'store':'S_TEST'},scopes[:1],'external-delete',during=lambda:None)
        assert _admin("SELECT stock_override,note FROM woo.virtual_catalog_store WHERE fdm4_store='S_TEST'")==[(Decimal(42),'retain')]
    finally:
        _admin("DELETE FROM woo.virtual_catalog_store WHERE fdm4_store='S_TEST'")


@pytest.mark.parametrize('path,args',[
    ('/api/product-state',{'store':'S_TEST','style':'STYLE-1'}),('/api/change-history',{'store':'S_TEST'}),
    ('/api/store-price-audit',{'store':'S_TEST'}),('/api/issues',{'store':'S_TEST','checks':['no_logos']}),
    ('/api/product-explanation',{'store':'S_TEST','style':'STYLE-1'}),('/api/wordpress/store-check',{'store':'S_TEST'}),
    ('/api/wordpress/product-check',{'store':'S_TEST','style':'STYLE-1'}),('/api/order-status',{'store':'S_TEST','order_id':12})])
def test_new_read_routes(client_as,path,args,monkeypatch):
    monkeypatch.setattr(wp_bridge,'wp_admin_call',lambda *a,**kw:{'found':False})
    assert client_as().get(path,params=args).status_code==200


def test_history_global_and_tier_rules_match_store_scope():
    _admin("UPDATE woo.price_rule SET stores=NULL,styles=NULL,updated_by='global-pricer' WHERE rule_id=%s",(_rule(),))
    assert any(r['source']=='woo.price_rule' for r in _read('get_change_history',{'store':'S_TEST','style':'STYLE-1'})['rows'])
    _admin("UPDATE woo.price_rule SET store_tiers=ARRAY['Corporate'] WHERE rule_id=%s",(_rule(),))
    assert any(r['source']=='woo.price_rule' for r in _read('get_change_history',{'store':'S_TEST'})['rows'])
    _admin("UPDATE woo.price_rule SET excl_stores=ARRAY['S_TEST'] WHERE rule_id=%s",(_rule(),))
    assert not any(r['source']=='woo.price_rule' for r in _read('get_change_history',{'store':'S_TEST'})['rows'])


def test_external_sql_policies_already_cover_each_required_list():
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    sql=(root/'sql/diagnostics/agent-write-preflight.sql').read_text()
    assert sql.count("('woo', 'virtual_catalog_store'")==7
    assert 'woo.virtual_catalog_store' in (root/'sql/logo_admin_role.sql').read_text()


def test_new_mcp_read_proxies_use_get_and_omit_absent_values(monkeypatch):
    import mcp_server
    calls=[]
    monkeypatch.setattr(mcp_server,'_call',lambda method,path,**kw:calls.append((method,path,kw)) or {})
    for name,args in [('get_product_state',{'store':'S_TEST','style':'STYLE-1'}),('get_change_history',{}),
                      ('get_stock',{'style':'STYLE-1'}),('audit_store_prices',{'store':'S_TEST'}),
                      ('wp_product_check',{'store':'S_TEST','sku':'SKU'}),('wp_store_check',{'store':'S_TEST'}),
                      ('get_order_status',{'blog_id':1,'order_id':2}),('find_issues',{}),('explain_product',{'store':'S_TEST','style':'STYLE-1'})]:
        getattr(mcp_server,name)(**args)
    assert len(calls)==9 and all(method=='GET' for method,_path,_kw in calls)
    assert all(v is not None for _method,_path,kw in calls for v in kw['params'].values())


def test_composed_section_recovers_from_bounding_failure(monkeypatch):
    real=queries._bounded_category_value
    def failed(value,limit):
        if value=="bad": raise ValueError("Cannot render")
        return real(value,limit)
    monkeypatch.setattr(queries,"_bounded_category_value",failed)
    with database.cursor() as cursor:
        assert queries._ops_section(cursor,lambda:"bad")["available"] is False
        assert queries.get_product_state(cursor,store="S_TEST",style="STYLE-1")["found"]


def test_wordpress_stock_status_disagreement_and_clipping(mapped_store,monkeypatch):
    live=healthy_product()
    live['variations'][0]['stock_status']='outofstock'
    monkeypatch.setattr(wp_bridge,'wp_diag_product',lambda *a,**kw:live)
    result=_read('explain_product',{'store':'S_TEST','style':'STYLE-1'})
    assert any('stock status outofstock' in line for line in result['findings'])
    live['variations']*=100
    assert _read('wp_product_check',{'store':'S_TEST','style':'STYLE-1'})['truncated']


def test_category_history_matches_persisted_snapshot_entity_key(mapped_store):
    _admin("INSERT INTO catmgr.audit_log(actor,action,entity,entity_key,detail) VALUES ('snapshotter','snapshot_import','snapshot',%s,'{}')",(f'dev:{mapped_store}',))
    result=_read('get_change_history',{'store':'S_TEST'})
    assert any(r['source']=='catmgr.audit_log' and r['actor']=='snapshotter' for r in result['rows'])


def test_context_reads_dispatch_through_the_agent_path(monkeypatch):
    from config import get_settings
    from tool_registry import execute_agent_tool
    for key, value in {'AGENT_ENABLED':'true','AGENT_WRITES_ENABLED':'false','AGENT_ALLOWED_USERS':USER,
                       'OPENAI_API_KEY':'test-key','OPENAI_MODEL':'test-model','CATMGR_ENABLED':'true','CATMGR_VIEW_USERS':USER,
                       'CATMGR_PROD_URL':'https://category.example.test','CATMGR_PROD_USER':'fixture','CATMGR_PROD_APP_PASSWORD':'fixture-password'}.items():
        monkeypatch.setenv(key,value)
    get_settings.cache_clear()
    try:
        history=execute_agent_tool('get_change_history',{'store':'S_TEST'},AccessContext(USER,USER),get_settings())
        issues=execute_agent_tool('find_issues',{'store':'S_TEST','checks':['no_logos']},AccessContext(USER,USER),get_settings())
    finally:
        get_settings.cache_clear()
    assert 'rows' in history and 'actors' in history
    assert issues['checks'][0]['check']=='no_logos' and issues['checks'][0]['available']


def test_order_status_by_blog_id_requires_a_mapped_store(mapped_store,monkeypatch):
    def never(*a,**kw): pytest.fail('bridge called for an unmapped blog')
    monkeypatch.setattr(wp_bridge,'wp_diag_order',never)
    assert _read('get_order_status',{'blog_id':1,'order_id':12})=={'available':False,'reason':'WordPress site is not a mapped store'}
    assert _read('get_order_status',{'blog_id':mapped_store+1,'order_id':12})['available'] is False
    with pytest.raises((ValidationError,queries.QueryValidationError)):
        _read('get_order_status',{'blog_id':0,'order_id':12})


def test_explain_reports_missing_state_as_unavailable_not_disagreement(mapped_store,monkeypatch):
    monkeypatch.setattr(wp_bridge,'wp_diag_product',lambda *a,**kw:healthy_product())
    def broken(cursor,**kwargs): raise queries.QueryNotFound('Product not found')
    monkeypatch.setattr(queries,'get_product_state',broken)
    result=_read('explain_product',{'store':'S_TEST','style':'STYLE-1'})
    assert result['intent']['state']['available'] is False and result['wordpress']['available']
    assert not any('expects it hidden' in f or 'expects it visible' in f for f in result['findings']), result['findings']
    assert any(f.startswith('State:') for f in result['findings'])


def test_issue_checks_ignore_rows_outside_the_current_catalog():
    _admin("""INSERT INTO woo.store_product_state SELECT (r).* FROM (
        SELECT jsonb_populate_record(s, '{"catalog_id":"OPS-OTHER-CATALOG","style_code":"OPS-OTHER","sku":"OPS-OTHER-1"}'::jsonb) AS r
          FROM woo.store_product_state s WHERE s.fdm4_store='S_TEST' AND s.kind='parent' AND s.is_active LIMIT 1) q""")
    try:
        with database.cursor() as cursor:
            cursor.execute("SELECT count(*) AS n FROM woo.store_product_state WHERE fdm4_store='S_TEST' AND catalog_id='OPS-OTHER-CATALOG' AND is_active")
            assert cursor.fetchone()['n']==1
        for store in ('S_TEST',None):
            result=_read('find_issues',{'store':store,'checks':['no_logos'],'limit':200})['checks'][0]
            assert result['available'] and 'OPS-OTHER' not in {r['style'] for r in result['sample']}
        with pytest.raises(queries.QueryNotFound):
            _read('find_issues',{'store':'NO-SUCH-STORE','checks':['no_logos']})
    finally:
        _admin("DELETE FROM woo.store_product_state WHERE catalog_id='OPS-OTHER-CATALOG'")


def test_statement_timeout_bound_never_loosens_an_enclosing_section():
    assert queries._timeout_seconds('0') is None and queries._timeout_seconds('') is None
    assert queries._timeout_seconds('3s')==3 and queries._timeout_seconds('1500')==1.5 and queries._timeout_seconds('2min')==120
    with database.cursor() as cursor:
        cursor.execute("SELECT set_config('statement_timeout','3s',true)")
        queries._bound_statement_timeout(cursor,30)
        cursor.execute("SELECT current_setting('statement_timeout') AS t"); assert cursor.fetchone()['t']=='3s'
        queries._bound_statement_timeout(cursor,1)
        cursor.execute("SELECT current_setting('statement_timeout') AS t"); assert cursor.fetchone()['t']=='1s'
        cursor.execute("SELECT set_config('statement_timeout','0',true)")
        queries._bound_statement_timeout(cursor,20)
        cursor.execute("SELECT current_setting('statement_timeout') AS t"); assert cursor.fetchone()['t']=='20s'
