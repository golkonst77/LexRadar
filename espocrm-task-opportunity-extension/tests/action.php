<?php
// Execute the actual action with small platform doubles, no CRM bootstrap or network.
namespace Espo\Core\Exceptions {
    class BadRequest extends \RuntimeException {
        public string $body = '';
        static function createWithBody($reason, $body): self {
            $e = new self($reason); $e->body = $body; return $e;
        }
    } class Conflict extends \RuntimeException {}
    class Forbidden extends \RuntimeException {} class NotFound extends \RuntimeException {}
}
namespace Espo\Core\Api {
    interface Action { public function process(Request $r): Response; }
    class Request {
        public function __construct(public string $method='POST', public array $body=[]) {}
        public function getRouteParam($key) { return 'task1'; }
        public function getMethod() { return $this->method; }
        public function getParsedBody() { return (object)$this->body; }
    }
    class Response { public function __construct(public mixed $data) {} }
    class ResponseComposer { static function json($v) { return new Response($v); } }
}
namespace Espo\Core {
    class Acl {
        public bool $allow=true;
        public function check($entity,$action) { return $this->allow; }
        public function getScopeForbiddenFieldList(...$args) { return []; }
    }
}
namespace Espo\Core\Utils {
    class Metadata { public function get($path) { return [\Espo\Custom\Tools\LexRadar\Policy::STAGE]; } }
}
namespace Espo\ORM\Query {
    class SelectBuilder {
        public array $where=[];
        static function create() { return new self(); }
        function from($type) { return $this; }
        function withDeleted() { return $this; }
        function forUpdate() { return $this; }
        function where($where) { $this->where=$where; return $this; }
        function build() { return $this; }
    }
}
namespace Espo\ORM {
    class Entity {
        function __construct(public array $data) {}
        function get($k) { return $this->data[$k]??null; }
        function getId() { return $this->get('id'); }
        function getValueMap() { return (object)$this->data; }
    }
    class EntityManager {
        public array $state=[]; public $handle; public bool $active=false;
        function __construct(public string $path) {}
        function getTransactionManager() { return $this; }
        function run($fn) {
            $this->handle=fopen($this->path,'c+'); flock($this->handle,LOCK_EX); $this->active=true;
            $this->state=json_decode(stream_get_contents($this->handle),true) ?: [];
            try { $r=$fn(); rewind($this->handle); ftruncate($this->handle,0); fwrite($this->handle,json_encode($this->state)); fflush($this->handle); return $r; }
            finally { $this->active=false; flock($this->handle,LOCK_UN); fclose($this->handle); }
        }
        function getRDBRepository($type) { return new Repository($this,$type); }
        function getEntityById($type,$id) {
            if ($type==='Account') return new Entity(['id'=>$id,'name'=>'Clinic']);
            if ($type==='User') return new Entity(['id'=>$id,'name'=>'Owner','isActive'=>true]);
        }
    }
    class Repository {
        function __construct(public EntityManager $em,public string $type) {}
        function forUpdate() { if (!$this->em->active) throw new \RuntimeException('Lock outside transaction'); return $this; }
        function where($w) { return $this; }
        function clone($q) { return $this->forUpdate(); }
        function findOne() {
            if ($this->type==='Task') return new Entity(['id'=>'task1','name'=>'Контрольный звонок LexRadar','description'=>'LexRadar followup Email/email1','parentType'=>'Account','parentId'=>'account1','assignedUserId'=>'user1','cCallResult'=>'Заинтересовались']);
            return isset($this->em->state['opportunity']) ? new Entity($this->em->state['opportunity']) : null;
        }
    }
}
namespace Espo\Core\Record {
    class ServiceContainer {
        public bool $fail=false;
        function __construct(public \Espo\ORM\EntityManager $em) {}
        function get($type) { return $this; }
        function create($data) {
            if ($this->fail) throw new \RuntimeException('Simulated validation failure');
            if (isset($this->em->state['opportunity'])) throw new \RuntimeException('Duplicate');
            $this->em->state['count']=($this->em->state['count']??0)+1;
            $this->em->state['opportunity']=(array)$data+['id'=>'opp1'];
            usleep(20000);
            return $this;
        }
        function getEntity() { return new \Espo\ORM\Entity($this->em->state['opportunity']); }
        function read($id) { return $this->getEntity(); }
    }
}
namespace Espo\Custom\Tools\LexRadar {
    class SchemaGuard { function check(): void {} }
}
namespace {
require __DIR__.'/../files/custom/Espo/Custom/Tools/LexRadar/Policy.php';
require __DIR__.'/../files/custom/Espo/Custom/Tools/LexRadar/TaskOpportunityAction.php';
use Espo\Core\Api\Request;
use Espo\Custom\Tools\LexRadar\TaskOpportunityAction;
function check($condition) { if (!$condition) throw new RuntimeException('Assertion failed'); }
function rejects($fn) { try {$fn();} catch (RuntimeException $e) {return;} throw new RuntimeException('Expected refusal'); }
$path=tempnam(sys_get_temp_dir(),'lr-offline-');
$em=new Espo\ORM\EntityManager($path); $acl=new Espo\Core\Acl(); $services=new Espo\Core\Record\ServiceContainer($em);
$action=new TaskOpportunityAction($em,$acl,$services,new Espo\Core\Utils\Metadata(),new Espo\Custom\Tools\LexRadar\SchemaGuard());
$values=['amount'=>1200,'amountCurrency'=>'RUB','closeDate'=>'2026-11-30','accountId'=>'forged','assignedUserId'=>'forged','stage'=>'forged'];
try {
    $preview=$action->process(new Request('GET'))->data;
    check(isset($preview['attributes']) && file_get_contents($path)==='[]');
    rejects(fn()=>$action->process(new Request('POST')));
    $services->fail=true; rejects(fn()=>$action->process(new Request('POST',$values))); $services->fail=false;
    check(json_decode(file_get_contents($path),true)===[]);
    if (!function_exists('pcntl_fork')) throw new RuntimeException('pcntl is required for concurrency test');
    $children=[];
    for ($i=0;$i<8;$i++) {
        $pid=pcntl_fork(); if ($pid===-1) throw new RuntimeException('fork failed');
        if ($pid===0) {
            try { $r=$action->process(new Request('POST',$values))->data; check($r->id==='opp1'); exit(0); }
            catch (Throwable $e) {fwrite(STDERR,$e->getMessage());exit(1);}
        }
        $children[]=$pid;
    }
    foreach($children as $pid) { pcntl_waitpid($pid,$status); check(pcntl_wexitstatus($status)===0); }
    $state=json_decode(file_get_contents($path),true); check($state['count']===1);
    check($state['opportunity']['accountId']==='account1' && $state['opportunity']['assignedUserId']==='user1');
    check($state['opportunity']['stage']===Espo\Custom\Tools\LexRadar\Policy::STAGE);
    check($action->process(new Request('GET'))->data['id']==='opp1');
    // Discarded response, then a fresh action instance: still the same record.
    $action->process(new Request('POST',$values));
    check(json_decode(file_get_contents($path),true)['count']===1);
    $acl->allow=false; rejects(fn()=>$action->process(new Request('POST',$values))); $acl->allow=true;
    $state['opportunity']['deleted']=true; file_put_contents($path,json_encode($state));
    rejects(fn()=>$action->process(new Request('GET')));
    rejects(fn()=>$action->process(new Request('POST',$values)));
    echo "Actual action: preview, rollback, ACL, forged fields, 8 concurrent processes, lost response, repeat, tombstone: OK\n";
} finally { unlink($path); }
}
