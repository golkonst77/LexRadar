<?php
namespace Espo\Core\Exceptions { class BadRequest extends \RuntimeException {
        public string $body = '';
        static function createWithBody($reason, $body): self {
            $e = new self($reason); $e->body = $body; return $e;
        }
    } class Forbidden extends \RuntimeException {} }
namespace Espo\Core\Hook\Hook { interface BeforeSave { public function beforeSave(\Espo\ORM\Entity $e, \Espo\ORM\Repository\Option\SaveOptions $o): void; } }
namespace Espo\ORM\Repository\Option { class SaveOptions {} }
namespace Espo\Core { class Acl { function check(...$args) { return true; } function getScopeForbiddenFieldList(...$args) { return []; } } }
namespace Espo\ORM {
    class Entity {
        function __construct(public array $data, public bool $new=true, public bool $changed=false) {}
        function isNew() { return $this->new; }
        function get($k) { return $this->data[$k]??null; }
        function getValueMap() { return (object)$this->data; }
        function isAttributeChanged($k) { return $this->changed; }
    }
    class EntityManager { function getEntityById($type,$id) { return new Entity(['id'=>$id,'name'=>'Контрольный звонок LexRadar','description'=>'LexRadar followup Email/email1','accountId'=>'account1','assignedUserId'=>'user1','cCallResult'=>'Заинтересовались']); } }
}
namespace Espo\Custom\Tools\LexRadar { class SchemaGuard { function check(): void {} } }
namespace {
require __DIR__.'/../files/custom/Espo/Custom/Tools/LexRadar/Policy.php';
require __DIR__.'/../files/custom/Espo/Custom/Hooks/Opportunity/LexRadarSource.php';
$hook=new Espo\Custom\Hooks\Opportunity\LexRadarSource(new Espo\ORM\EntityManager(),new Espo\Core\Acl(),new Espo\Custom\Tools\LexRadar\SchemaGuard());
$o=new Espo\ORM\Repository\Option\SaveOptions();
function rejects($fn) { try {$fn();} catch(RuntimeException $e) {return;} throw new RuntimeException('Expected rejection'); }
$hook->beforeSave(new Espo\ORM\Entity([]),$o);
rejects(fn()=>$hook->beforeSave(new Espo\ORM\Entity([],false,true),$o));
$values=['cLrSourceTaskId'=>'task1','accountId'=>'account1','assignedUserId'=>'user1','stage'=>Espo\Custom\Tools\LexRadar\Policy::STAGE,'amount'=>100,'amountCurrency'=>'RUB','closeDate'=>'2026-11-30'];
$hook->beforeSave(new Espo\ORM\Entity($values),$o);
foreach (['accountId','assignedUserId','stage'] as $field) {
    rejects(fn()=>$hook->beforeSave(new Espo\ORM\Entity(array_replace($values,[$field=>'forged'])),$o));
}
$hook->beforeSave(new Espo\ORM\Entity($values,false,false),$o);
echo "Actual hook: ordinary deals, immutable source, forged bindings, later edits: OK\n";
}
