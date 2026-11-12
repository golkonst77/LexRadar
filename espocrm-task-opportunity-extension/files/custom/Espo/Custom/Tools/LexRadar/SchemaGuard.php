<?php
namespace Espo\Custom\Tools\LexRadar;

use Espo\Core\Exceptions\Conflict;
use Espo\ORM\EntityManager;
use PDO;

class SchemaGuard
{
    public function __construct(private EntityManager $entityManager) {}

    public function check(): void
    {
        $pdo = $this->entityManager->getPDO();
        if ($pdo->getAttribute(PDO::ATTR_DRIVER_NAME) !== 'mysql') {
            throw new Conflict('Расширение требует MySQL/MariaDB с InnoDB.');
        }
        $engines = $pdo->query("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ('task', 'opportunity')")->fetchAll(PDO::FETCH_KEY_PAIR);
        if (count($engines) !== 2 || count(array_filter($engines, fn ($engine) => strtoupper($engine) === 'INNODB')) !== 2) {
            throw new Conflict('Task и Opportunity должны использовать InnoDB.');
        }
        $indexes = [];
        foreach ($pdo->query('SHOW INDEX FROM opportunity')->fetchAll(PDO::FETCH_ASSOC) as $row) {
            if ((int) $row['Non_unique'] === 0) {
                $indexes[$row['Key_name']][(int) $row['Seq_in_index']] = $row['Column_name'];
            }
        }
        foreach ($indexes as $columns) {
            if (count($columns) === 1 && reset($columns) === 'c_lr_source_task_id') return;
        }
        throw new Conflict('Нет UNIQUE индекса исходной Task. Выполните rebuild и проверку схемы.');
    }
}
