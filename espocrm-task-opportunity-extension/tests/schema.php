<?php
namespace Espo\Core\Exceptions { class Conflict extends \RuntimeException {} }
namespace Espo\ORM { class EntityManager { function __construct(private \PDO $pdo) {} function getPDO(): \PDO { return $this->pdo; } } }
namespace {
require __DIR__.'/../files/custom/Espo/Custom/Tools/LexRadar/SchemaGuard.php';
if (!class_exists('PDO')) {
    class PDO { const FETCH_DEFAULT=0; const FETCH_KEY_PAIR=12; const FETCH_ASSOC=2; const ATTR_DRIVER_NAME=16; }
    class PDOStatement {}
}
class FakeStatement extends PDOStatement {
    public function __construct(private array $rows) {}
    public function fetchAll(int $mode=PDO::FETCH_DEFAULT, mixed ...$args): array { return $this->rows; }
}
class FakePDO extends PDO {
    public string $driver='mysql';
    public array $engines=['task'=>'InnoDB','opportunity'=>'InnoDB'];
    public array $indexes=[['Non_unique'=>0,'Key_name'=>'source','Seq_in_index'=>1,'Column_name'=>'c_lr_source_task_id']];
    public function __construct() {}
    public function getAttribute(int $attribute): mixed { return $this->driver; }
    public function query(string $query, ?int $fetchMode=null, mixed ...$fetchModeArgs): PDOStatement|false {
        return new FakeStatement(str_contains($query,'information_schema') ? $this->engines : $this->indexes);
    }
}
$p=new FakePDO();
$guard=new Espo\Custom\Tools\LexRadar\SchemaGuard(new Espo\ORM\EntityManager($p));
$guard->check();
function rejects($fn) { try {$fn();} catch (RuntimeException $e) {return;} throw new RuntimeException('Expected refusal'); }
$p->driver='pgsql'; rejects(fn()=>$guard->check()); $p->driver='mysql';
$p->engines['task']='MyISAM'; rejects(fn()=>$guard->check()); $p->engines['task']='InnoDB';
$old=$p->indexes; $p->indexes=[]; rejects(fn()=>$guard->check());
$p->indexes=$old; $p->indexes[]=['Non_unique'=>0,'Key_name'=>'source','Seq_in_index'=>2,'Column_name'=>'deleted'];
rejects(fn()=>$guard->check());
$p->indexes=$old; $p->indexes[0]['Non_unique']=1; rejects(fn()=>$guard->check());
echo "Actual schema guard: InnoDB, missing/nonunique/composite index, wrong driver: OK\n";
}
