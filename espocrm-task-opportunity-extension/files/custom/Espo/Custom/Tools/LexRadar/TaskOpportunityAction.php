<?php
namespace Espo\Custom\Tools\LexRadar;

use Espo\Core\Api\{Action, Request, Response, ResponseComposer};
use Espo\Core\Acl;
use Espo\Core\Exceptions\{BadRequest, Conflict, Forbidden, NotFound};
use Espo\Core\Record\ServiceContainer;
use Espo\Core\Utils\Metadata;
use Espo\ORM\EntityManager;
use Espo\ORM\Query\SelectBuilder;

class TaskOpportunityAction implements Action
{
    public function __construct(
        private EntityManager $entityManager,
        private Acl $acl,
        private ServiceContainer $services,
        private Metadata $metadata,
        private SchemaGuard $schemaGuard,
    ) {}

    public function process(Request $request): Response
    {
        $id = $request->getRouteParam('id');
        if (!$id || !preg_match('/\A[a-zA-Z0-9]+\z/', $id)) {
            throw new BadRequest('Invalid Task ID.');
        }
        if (!$this->acl->check('Task', 'read') || !$this->acl->check('Opportunity', 'create')) {
            throw new Forbidden();
        }
        $this->schemaGuard->check();
        $result = $this->entityManager->getTransactionManager()->run(function () use ($request, $id) {
            // A locking read sees the previous committed winner, even with REPEATABLE READ.
            $task = $this->entityManager->getRDBRepository('Task')->forUpdate()->where(['id' => $id])->findOne();
            if (!$task) {
                throw new NotFound();
            }
            if (!$this->acl->check($task, 'read') ||
                array_intersect(['name', 'description', 'parent', 'account', 'assignedUser', 'cCallResult'],
                    $this->acl->getScopeForbiddenFieldList('Task', 'read'))) {
                throw new Forbidden();
            }
            $query = SelectBuilder::create()->from('Opportunity')->withDeleted()->forUpdate()
                ->where(['cLrSourceTaskId' => $id])->build();
            $existing = $this->entityManager->getRDBRepository('Opportunity')->clone($query)->findOne();
            if ($existing) {
                if ($existing->get('deleted') || !$this->acl->check($existing, 'read')) {
                    throw new Conflict('Связанная сделка удалена или недоступна. Обратитесь к администратору.');
                }
                return $request->getMethod() === 'GET'
                    ? ['id' => $existing->getId()]
                    : $this->services->get('Opportunity')->read($existing->getId())->getValueMap();
            }
            $defaults = Policy::defaults((array) $task->getValueMap());
            if (!in_array(Policy::STAGE,
                $this->metadata->get(['entityDefs', 'Opportunity', 'fields', 'stage', 'options']) ?? [], true)) {
                throw new Conflict('Требуемая стадия отсутствует в metadata.');
            }
            $account = $this->entityManager->getEntityById('Account', $defaults['accountId']);
            $owner = $this->entityManager->getEntityById('User', $defaults['assignedUserId']);
            if (!$account || !$owner || !$owner->get('isActive') || !$this->acl->check($account, 'read') ||
                !$this->acl->check($owner, 'read')) {
                throw new Forbidden('Организация или ответственный недоступны.');
            }
            $defaults['accountName'] = $account->get('name');
            $defaults['assignedUserName'] = $owner->get('name');
            $defaults['name'] = 'LexRadar · ' . $account->get('name');
            $defaults['cLrSourceTaskName'] = $task->get('name');
            if ($request->getMethod() === 'GET') {
                return ['attributes' => $defaults];
            }
            $data = $request->getParsedBody();
            Policy::requireUserValues($data);
            // Keep all standard/custom form fields; never trust the client for these bindings.
            unset($data->id, $data->createdById, $data->modifiedById);
            foreach ($defaults as $key => $value) {
                $data->$key = $value;
            }
            $created = $this->services->get('Opportunity')->create($data)->getEntity();
            if ($created->get('cLrSourceTaskId') !== $id ||
                $created->get('accountId') !== $defaults['accountId'] ||
                $created->get('assignedUserId') !== $defaults['assignedUserId'] ||
                $created->get('stage') !== Policy::STAGE) {
                throw new Conflict('CRM отклонила обязательные поля связи. Транзакция отменена.');
            }
            return $created->getValueMap();
        });
        return ResponseComposer::json($result);
    }
}
