"""Правила синхронизации: событие создаёт только значимое изменение.

Логика подписи живёт в шаблонах (FullRoom.html, plan_table.html), поэтому
проверяется тем же JavaScript-движком, который её исполняет. Если Node.js в
системе нет, тесты пропускаются: обычный прогон от этого не падает.
"""

import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'tests', 'js', 'check_signature.js')

# На этой машине Node лежит в рантайме DSH, а не в PATH.
FALLBACK_NODE = os.path.join(
    os.path.expanduser('~'), '.dsh', 'dsh-runtimes', 'dsh-primary-runtime',
    'dependencies', 'node', 'bin', 'node.exe',
)


def node_binary():
    for candidate in (shutil.which('node'), shutil.which('node.exe'), FALLBACK_NODE):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


class SyncRuleTests(unittest.TestCase):
    def test_only_meaningful_changes_are_sent(self):
        node = node_binary()
        if node is None:
            self.skipTest('Node.js не найден: проверка правил подписи пропущена')
        result = subprocess.run(
            [node, SCRIPT], cwd=ROOT, capture_output=True,
            encoding='utf-8', errors='replace', timeout=120
        )
        output = (result.stdout or '') + (result.stderr or '')
        self.assertEqual(result.returncode, 0, output)
        self.assertIn('значимыми изменениями', output)


if __name__ == '__main__':
    unittest.main()
