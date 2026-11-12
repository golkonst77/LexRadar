<?php
namespace Espo\Custom\Tools\LexRadar;

use Espo\Core\Exceptions\BadRequest;

class Policy
{
    public const STAGE = 'LexRadar · Клиент заинтересован';

    private static function badRequest(string $message): BadRequest
    {
        return BadRequest::createWithBody('LexRadar validation failed.',
            json_encode(['message' => $message], JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE));
    }

    public static function defaults(array $task): array
    {
        $name = $task['name'] ?? '';
        if ($name !== 'Контрольный звонок LexRadar' &&
            !str_starts_with($name, 'Контрольный звонок LexRadar · ')) {
            throw self::badRequest('Это не задача контрольного звонка LexRadar.');
        }
        if (!preg_match('/\ALexRadar followup Email\/[a-zA-Z0-9]+(?:\r?\n|\z)/', $task['description'] ?? '')) {
            throw self::badRequest('Нет точного маркера задачи LexRadar.');
        }
        if (($task['cCallResult'] ?? null) !== 'Заинтересовались') {
            throw self::badRequest('Сначала сохраните результат звонка «Заинтересовались».');
        }
        $parentType = $task['parentType'] ?? null;
        $parentId = $task['parentId'] ?? null;
        $accountId = $task['accountId'] ?? null;
        if ($parentId && $parentType !== 'Account') {
            throw self::badRequest('Родитель задачи не является организацией.');
        }
        if ($parentType === 'Account' && $parentId) {
            if ($accountId && $accountId !== $parentId) {
                throw self::badRequest('Конфликт связей организации в задаче.');
            }
            $accountId = $parentId;
        }
        if (!$accountId || empty($task['assignedUserId'])) {
            throw self::badRequest('В задаче нужны организация и ответственный.');
        }
        return [
            'name' => 'LexRadar · ' . ($task['accountName'] ?? $task['parentName'] ?? $accountId),
            'accountId' => $accountId,
            'assignedUserId' => $task['assignedUserId'],
            'stage' => self::STAGE,
            'cLrSourceTaskId' => $task['id'],
        ];
    }

    public static function requireUserValues(\stdClass $data): void
    {
        if (!property_exists($data, 'amount') || !is_numeric($data->amount) ||
            !is_finite((float) $data->amount) || (float) $data->amount < 0 ||
            empty($data->amountCurrency) || !is_string($data->amountCurrency)) {
            throw self::badRequest('Введите сумму и валюту сделки.');
        }
        $date = $data->closeDate ?? null;
        if (!is_string($date) || !preg_match('/\A(\d{4})-(\d{2})-(\d{2})\z/', $date, $m) ||
            !checkdate((int) $m[2], (int) $m[3], (int) $m[1])) {
            throw self::badRequest('Введите дату закрытия сделки.');
        }
    }
}
