"""Offline PHP broker assertions: no WordPress bootstrap or site/database access."""
import json
from pathlib import Path
import subprocess

PLUGIN = Path(__file__).resolve().parents[3] / "WP2/wp-content/plugins/arb-admin"


def test_php_order_allowlist_site_validation_restore_and_item_caps():
    script = r"""<?php
    define('WPINC','offline');
    class ARB_Logo_Admin_API { public static function authorize(){ return true; } }
    class WP_Error { public $status; function __construct($code,$message,$data){ $this->status=$data['status']; } }
    class WP_REST_Response { public $data; function __construct($data,$status){ $this->data=$data; } }
    class Request { public $v; function __construct($v){$this->v=$v;} function get_param($key){return $this->v[$key]??null;} }
    $switches=0; $restores=0;
    function get_current_network_id(){return 1;}
    function get_sites($args){
        if($args['network_id']!==1 || $args['number']!==1){throw new Exception('Unbounded site lookup');}
        return $args['site__in']===[9] ? [(object)['path'=>'/test/']] : [];
    }
    function switch_to_blog($id){global $switches; $switches++;}
    function restore_current_blog(){global $restores; $restores++;}
    function is_wp_error($value){return $value instanceof WP_Error;}
    class FakeProduct {function get_sku(){return str_repeat('S',3000);}}
    class FakeItem {
        function get_product(){return new FakeProduct;}
        function get_quantity(){return 2;}
        function get_total(){return '20';}
        function get_meta($key){
            if($key==='customer_logos'){return 'ignored|C1|BK,ignored|C2|WH';}
            if(strpos($key,'_fdm4_location')===0){return 'Chest';}
            throw new Exception('Unexpected item metadata: '.$key);
        }
    }
    class FakeOrder {
        function get_items($kind){if($kind!=='line_item')throw new Exception('Wrong items');return array_fill(0,120,new FakeItem);}
        function get_item_count(){return 240;}
        function get_status(){return 'processing';}
        function get_date_created(){return new DateTimeImmutable('2026-09-01T12:00:00-04:00');}
        function get_date_modified(){return null;}
        function get_currency(){return 'USD';}
        function get_total(){return '2000';}
        function get_payment_method(){return 'card';}
        function get_payment_method_title(){return 'Card';}
        function get_meta($key){
            if($key==='_fdm4_payment_info_json'){return json_encode(['fdm4_payment_method_code'=>'WooCC','email'=>'PRIVATE','billing'=>['name'=>'PRIVATE']]);}
            if(in_array($key,['po_number','order_po_number','po2go_order_processed','_punchout_po_number','punchout_order_id'],true)){return '';}
            throw new Exception('Unexpected order metadata: '.$key);
        }
        function __call($name,$args){throw new Exception('Forbidden order read: '.$name);}
    }
    function wc_get_order($id){if($id===8)throw new Exception('Read failed'); return $id===7 ? new FakeOrder : false;}
    define('ARRAY_A','ARRAY_A');
    class FakeDB {
        public $base_prefix='arb_';
        function prepare($sql,...$args){
            foreach(['SELECT *','email','notes','vn_data','billing','shipping'] as $forbidden){if(stripos($sql,$forbidden)!==false)throw new Exception('Forbidden SQL');}
            if($args!==[9,7])throw new Exception('Unscoped order SQL');
            return $sql;
        }
        function get_row($sql,$mode){return ['status'=>'processing','created_at'=>'2026-09-01','updated_at'=>'2026-09-02','vn_so_id'=>'123','in_vn'=>1,'vn_attempts'=>2,'email'=>'PRIVATE','notes'=>'PRIVATE','billing'=>['name'=>'PRIVATE']];}
    }
    $wpdb=new FakeDB;
    """ + "require " + json.dumps(str(PLUGIN / "arb-logo-admin-diagnostics.php")) + ";\n" + r"""
    $result=ARB_Logo_Admin_Diagnostics::order(new Request(['blog_id'=>'9','order_id'=>'7']));
    if($result instanceof WP_Error)throw new Exception('Order diagnostic failed');
    $forbidden=['customer_name','name','email','phone','address','notes','ip','user_id','billing','shipping'];
    function check_keys($value,$forbidden){if(!is_array($value))return;foreach($value as $key=>$child){if(in_array($key,$forbidden,true))throw new Exception('Forbidden key');check_keys($child,$forbidden);}}
    check_keys($result->data,$forbidden);
    if(strpos(json_encode($result->data),'PRIVATE')!==false)throw new Exception('Private value leaked');
    $missing=ARB_Logo_Admin_Diagnostics::order(new Request(['blog_id'=>'10','order_id'=>'7']));
    $failed=ARB_Logo_Admin_Diagnostics::order(new Request(['blog_id'=>'9','order_id'=>'8']));
    $bad=ARB_Logo_Admin_Diagnostics::order(new Request(['blog_id'=>'0','order_id'=>'7']));
    echo json_encode(['data'=>$result->data,'missing'=>$missing->status,'failed'=>$failed->status,'bad'=>$bad->status,'switches'=>$switches,'restores'=>$restores]);
    """
    completed=subprocess.run(['php'],input=script,text=True,capture_output=True,timeout=10,check=True)
    result=json.loads(completed.stdout)
    assert result['switches']==result['restores']==2
    assert (result['missing'],result['failed'],result['bad'])==(404,503,400)
    data=result['data']
    assert data['created_gmt']=='2026-09-01T16:00:00Z'
    assert len(data['items'])==100 and data['truncated']
    assert len(data['items'][0]['sku'])==100
    assert data['items'][0]['embellishment']['logo_codes']==['C1','C2']
    assert data['payment']['method_code']=='WooCC'
    assert data['fdm4']['last_error'] is None


def test_php_diagnostics_register_get_only_with_shared_authorize():
    source=(PLUGIN/'arb-logo-admin-api.php').read_text()
    start=source.index("foreach ( array( 'product', 'category', 'store', 'sync-log', 'order' )")
    block=source[start:source.index("register_rest_route( 'arb/v1', '/logo-admin/auth'",start)]
    assert "'methods'             => 'GET'" in block
    assert "'permission_callback' => array( __CLASS__, 'authorize' )" in block
    assert source.count('diag/(?:product|category|store|sync-log|order)')==2
