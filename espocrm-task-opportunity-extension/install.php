<?php
/** Filesystem-only installer. Never connects to CRM or its database. */
declare(strict_types=1);

function mergePatch(mixed $current, mixed $patch, string $path = ''): mixed
{
    if (is_object($patch)) {
        if (!is_object($current)) {
            throw new RuntimeException("Object expected at $path");
        }
        foreach (get_object_vars($patch) as $key => $value) {
            $current->$key = property_exists($current, $key)
                ? mergePatch($current->$key, $value, "$path/$key") : $value;
        }
        return $current;
    }
    if (is_array($patch)) {
        if (!is_array($current)) {
            throw new RuntimeException("Array expected at $path");
        }
        foreach ($patch as $item) {
            if ($item === '__APPEND__') continue;
            $matched = false;
            foreach ($current as $old) {
                if ($old == $item) { $matched = true; break; }
                if (is_object($old) && is_object($item) &&
                    ((isset($item->name) && ($old->name ?? null) === $item->name) ||
                     (isset($item->route) && ($old->route ?? null) === $item->route &&
                      ($old->method ?? null) === $item->method))) {
                    throw new RuntimeException("Conflicting array item at $path");
                }
            }
            if (!$matched) $current[] = $item;
        }
        return $current;
    }
    if ($current !== $patch) throw new RuntimeException("Conflicting value at $path");
    return $current;
}

function writeAtomic(string $path, string $bytes, int $mode = 0644): void
{
    if (!is_dir(dirname($path)) && !mkdir(dirname($path), 0755, true)) {
        throw new RuntimeException('Cannot create directory');
    }
    $tmp = tempnam(dirname($path), '.lr-');
    if (!$tmp || file_put_contents($tmp, $bytes) !== strlen($bytes) || !chmod($tmp, $mode) || !rename($tmp, $path)) {
        throw new RuntimeException("Write failed: $path");
    }
}

function safeTarget(string $root, string $rel): string
{
    if (str_contains($rel, '..') || str_starts_with($rel, '/')) throw new RuntimeException('Invalid path');
    $path = $root;
    foreach (explode('/', $rel) as $part) {
        $path .= '/' . $part;
        if (is_link($path)) throw new RuntimeException("Symlink refused: $path");
    }
    return $path;
}

function installMain(): void
{
    $opts = getopt('', ['root:', 'apply', 'backup:', 'rollback', 'disable']);
    $root = realpath($opts['root'] ?? '');
    if (!$root || !is_file($root . '/bootstrap.php')) throw new RuntimeException('Use --root=/path/to/espo');
    $lock = fopen($root . '/data/lexradar-extension-install.lock', 'c');
    if (!$lock || !flock($lock, LOCK_EX | LOCK_NB)) throw new RuntimeException('Installer is busy');
    $backup = $opts['backup'] ?? '';
    if (isset($opts['rollback'])) {
        $manifest = json_decode(file_get_contents($backup . '/manifest.json'), true, 512, JSON_THROW_ON_ERROR);
        if ($manifest['root'] !== $root) throw new RuntimeException('Backup belongs to another root');
        foreach ($manifest['files'] as $rel => $record) {
            $path = safeTarget($root, $rel);
            if (!is_file($path) || hash_file('sha256', $path) !== $record['after']) {
                throw new RuntimeException("Changed since install: $rel; manual reconciliation required");
            }
        }
        if (!isset($opts['apply'])) { echo "Rollback plan OK; add --apply to restore files.\n"; return; }
        foreach ($manifest['files'] as $rel => $record) {
            $path = safeTarget($root, $rel);
            if ($record['existed']) writeAtomic($path, file_get_contents($backup . '/before/' . $rel), $record['mode']);
            elseif (!unlink($path)) throw new RuntimeException("Cannot remove $rel");
        }
        echo "Files restored. Follow README database-preserving rollback instructions.\n";
        return;
    }
    if (isset($opts['disable'])) {
        $paths = [
            'custom/Espo/Custom/Resources/metadata/clientDefs/Task.json',
            'custom/Espo/Custom/Resources/routes.json',
        ];
        if (isset($opts['apply']) && (!$backup || !str_starts_with($backup, '/') || file_exists($backup) ||
            str_starts_with($backup . '/', $root . '/'))) {
            throw new RuntimeException('Use a NEW absolute --backup outside web root');
        }
        $changes = [];
        foreach ($paths as $rel) {
            $path = safeTarget($root, $rel);
            $old = file_get_contents($path);
            $data = json_decode($old, false, 512, JSON_THROW_ON_ERROR);
            if (str_ends_with($rel, 'Task.json')) {
                $data->menu->detail->buttons = array_values(array_filter($data->menu->detail->buttons,
                    fn ($item) => !is_object($item) || ($item->name ?? '') !== 'lrCreateOpportunity'));
            } else {
                $data = array_values(array_filter($data, fn ($item) =>
                    ($item->actionClassName ?? '') !== 'Espo\\Custom\\Tools\\LexRadar\\TaskOpportunityAction'));
            }
            $changes[$rel] = [$old, json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n"];
        }
        if (!isset($opts['apply'])) { echo "Disable plan: button and routes only; schema and links retained.\n"; return; }
        mkdir($backup, 0700, true);
        foreach ($changes as $rel => [$old, $new]) {
            writeAtomic($backup . '/' . $rel, $old, 0600);
            $path = safeTarget($root, $rel);
            writeAtomic($path, $new, fileperms($path) & 0777);
        }
        echo "Button and routes disabled; rebuild is safe, source schema retained.\n";
        return;
    }
    $config = require $root . '/data/config.php';
    if (($config['version'] ?? null) !== '10.0.6') throw new RuntimeException('Only EspoCRM 10.0.6 is supported');
    $plan = [];
    foreach (['patches', 'files'] as $kind) {
        $prefix = __DIR__ . '/' . $kind . '/';
        foreach (new RecursiveIteratorIterator(new RecursiveDirectoryIterator($prefix, FilesystemIterator::SKIP_DOTS)) as $file) {
            if (!$file->isFile()) continue;
            $rel = substr($file->getPathname(), strlen($prefix));
            $target = safeTarget($root, $rel);
            $old = is_file($target) ? file_get_contents($target) : null;
            $new = file_get_contents($file->getPathname());
            if ($kind === 'patches') {
                $patch = json_decode($new, false, 512, JSON_THROW_ON_ERROR);
                $current = $old === null ? (is_array($patch) ? [] : new stdClass()) : json_decode($old, false, 512, JSON_THROW_ON_ERROR);
                $new = json_encode(mergePatch($current, $patch, $rel), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
                if ($old !== null && json_decode($old) == json_decode($new)) continue;
            } elseif ($old !== null && $old !== $new) {
                throw new RuntimeException("Existing code differs: $rel");
            }
            if ($old === $new) continue;
            $plan[$rel] = ['before' => $old, 'after' => $new, 'mode' => $old === null ? 0644 : fileperms($target) & 0777];
        }
    }
    foreach ($plan as $rel => $unused) echo "$rel\n";
    if (!$plan) { echo "Already installed; no changes.\n"; return; }
    if (!isset($opts['apply'])) { echo "PLAN ONLY. No CRM/DB connection.\n"; return; }
    if (!$backup || file_exists($backup) || str_starts_with($backup . '/', $root . '/')) {
        throw new RuntimeException('Provide a NEW absolute --backup directory outside the web root');
    }
    if (!str_starts_with($backup, '/')) throw new RuntimeException('Absolute backup path required');
    if (!mkdir($backup, 0700, true)) throw new RuntimeException('Cannot create backup');
    $manifest = ['root' => $root, 'files' => []];
    foreach ($plan as $rel => $record) {
        if ($record['before'] !== null) writeAtomic($backup . '/before/' . $rel, $record['before'], 0600);
        $manifest['files'][$rel] = ['existed' => $record['before'] !== null, 'after' => hash('sha256', $record['after']), 'mode' => $record['mode']];
    }
    writeAtomic($backup . '/manifest.json', json_encode($manifest, JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR), 0600);
    foreach ($plan as $rel => $record) writeAtomic(safeTarget($root, $rel), $record['after'], $record['mode']);
    echo "Installed files only. Rebuild and schema verification are mandatory before opening CRM.\n";
}
if (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    try { installMain(); } catch (Throwable $e) { fwrite(STDERR, $e->getMessage() . "\n"); exit(1); }
}
