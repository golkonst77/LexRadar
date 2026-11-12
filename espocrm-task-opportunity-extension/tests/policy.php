<?php
namespace Espo\Core\Exceptions { class BadRequest extends \RuntimeException {
        public string $body = '';
        static function createWithBody($reason, $body): self {
            $e = new self($reason); $e->body = $body; return $e;
        }
    } }
namespace {
require __DIR__ . '/../files/custom/Espo/Custom/Tools/LexRadar/Policy.php';
require __DIR__ . '/../install.php';
use Espo\Custom\Tools\LexRadar\Policy;
function check(bool $ok): void { if (!$ok) throw new RuntimeException('Assertion failed'); }
function rejects(callable $fn): void { try { $fn(); } catch (RuntimeException $e) { return; } throw new RuntimeException('Expected rejection'); }
$task = ['id'=>'task1','name'=>'Контрольный звонок LexRadar · Клиника', 'description'=>"LexRadar followup Email/email1\r\nТелефон: не указан", 'parentType'=>'Account','parentId'=>'account1','assignedUserId'=>'user1','cCallResult'=>'Заинтересовались'];
try {
    Policy::defaults(array_replace($task, ['cCallResult' => null, 'callResult' => 'Заинтересовались']));
    throw new RuntimeException('Expected missing cCallResult rejection');
} catch (Espo\Core\Exceptions\BadRequest $e) {
    check($e->getMessage() === 'LexRadar validation failed.');
    check(json_decode($e->body, true)['message'] === 'Сначала сохраните результат звонка «Заинтересовались».');
}
$d = Policy::defaults($task);
check($d['accountId'] === 'account1' && $d['assignedUserId'] === 'user1' && $d['cLrSourceTaskId'] === 'task1');
check(!isset($d['amount']) && !isset($d['closeDate']));
foreach ([['parentType'=>'Contact'], ['accountId'=>'other'], ['parentId'=>null], ['assignedUserId'=>null], ['cCallResult'=>'Не звонили'], ['description'=>'LexRadar fake'], ['name'=>'Other Task']] as $change) {
    rejects(fn () => Policy::defaults(array_replace($task, $change)));
}
check(Policy::defaults(array_replace($task,['parentId'=>null,'parentType'=>null,'accountId'=>'oldAccount']))['accountId'] === 'oldAccount');
Policy::requireUserValues((object)['amount'=>0,'amountCurrency'=>'RUB','closeDate'=>'2026-11-30']);
foreach ([[], ['amount'=>null], ['amount'=>-1], ['amount'=>INF], ['closeDate'=>'2026-02-30'], ['closeDate'=>'tomorrow'], ['amountCurrency'=>'']] as $change) {
    $data = $change === [] ? new stdClass() : (object)array_replace(['amount'=>100,'amountCurrency'=>'RUB','closeDate'=>'2026-11-30'],$change);
    rejects(fn () => Policy::requireUserValues($data));
}
$old = json_decode('{"fields":{"customExisting":{"type":"varchar"}},"stage":["A","B"],"menu":{"detail":{"buttons":["__APPEND__",{"name":"existing"}]}}}');
$patch = json_decode('{"fields":{"cLrSourceTask":{"type":"link"}},"menu":{"detail":{"buttons":["__APPEND__",{"name":"lrCreateOpportunity"}]}}}');
$result = mergePatch($old,$patch);
check(isset($result->fields->customExisting) && $result->stage === ['A','B']);
check(count($result->menu->detail->buttons) === 3);
check(mergePatch(unserialize(serialize($result)),$patch) == $result);
rejects(fn()=>mergePatch(json_decode('{"x":1}'),json_decode('{"x":2}')));
echo "Policy, validation and non-destructive JSON merge: OK\n";
}
