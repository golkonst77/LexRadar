<?php
// Operator-only, read-only database/metadata check; never run by the offline tests.
if (PHP_SAPI !== 'cli') exit(1);
$root = $argv[1] ?? '/var/www/html';
chdir($root);
require $root . '/bootstrap.php';
$app = new Espo\Core\Application();
$container = $app->getContainer();
$container->get('injectableFactory')->create(Espo\Custom\Tools\LexRadar\SchemaGuard::class)->check();
$metadata = $container->get('metadata');
if (!in_array(Espo\Custom\Tools\LexRadar\Policy::STAGE,
    $metadata->get(['entityDefs', 'Opportunity', 'fields', 'stage', 'options']) ?? [], true)) {
    throw new RuntimeException('Stage value missing; do not substitute a translation label for the stored enum value.');
}
if (!in_array('Заинтересовались', $metadata->get(['entityDefs', 'Task', 'fields', 'cCallResult', 'options']) ?? [], true)) {
    throw new RuntimeException('cCallResult stored value missing.');
}
echo "Schema guard and existing enum values: OK. No records created.\n";
