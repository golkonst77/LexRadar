<?php
namespace Espo\Custom\Hooks\Opportunity;

use Espo\Core\Acl;
use Espo\Core\Exceptions\{BadRequest, Forbidden};
use Espo\Core\Hook\Hook\BeforeSave;
use Espo\Custom\Tools\LexRadar\{Policy, SchemaGuard};
use Espo\ORM\{Entity, EntityManager};
use Espo\ORM\Repository\Option\SaveOptions;

class LexRadarSource implements BeforeSave
{
    public static int $order = 99;

    public function __construct(private EntityManager $entityManager, private Acl $acl, private SchemaGuard $schemaGuard) {}

    public function beforeSave(Entity $entity, SaveOptions $options): void
    {
        if (!$entity->isNew()) {
            if ($entity->isAttributeChanged('cLrSourceTaskId')) {
                throw new BadRequest('Нельзя менять или очищать исходную задачу LexRadar.');
            }
            return;
        }
        $id = $entity->get('cLrSourceTaskId');
        if (!$id) {
            return;
        }
        $this->schemaGuard->check();
        $task = $this->entityManager->getEntityById('Task', $id);
        if (!$task || !$this->acl->check($task, 'read') || !$this->acl->check('Opportunity', 'create') ||
            in_array('cCallResult', $this->acl->getScopeForbiddenFieldList('Task', 'read'), true)) {
            throw new Forbidden();
        }
        $defaults = Policy::defaults((array) $task->getValueMap());
        foreach (['accountId', 'assignedUserId', 'stage'] as $field) {
            if ($entity->get($field) !== $defaults[$field]) {
                throw new BadRequest('Сделка должна соответствовать исходной задаче.');
            }
        }
        Policy::requireUserValues($entity->getValueMap());
        // The UNIQUE index is the final guard for other API/import writers too.
    }
}
