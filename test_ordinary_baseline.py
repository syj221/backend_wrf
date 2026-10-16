"""Pin restored legacy action bodies and the explicit compatibility boundary."""
import ast
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent


def definitions(source):
    result = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[node.name] = node
        elif isinstance(node, ast.ClassDef):
            result.update({node.name + '.' + item.name: item for item in node.body
                           if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))})
    return result


class OrdinaryBaselineTest(unittest.TestCase):
    def test_restored_sources_match_frozen_baseline(self):
        manifest = json.loads((ROOT / 'docs/ordinary-baseline-origins.json').read_text())
        self.assertEqual(manifest['baseline'], '1ee4c7b')
        guarded = []
        for record in manifest['definitions']:
            path = ROOT / record['file']
            source = path.read_text()
            node = definitions(source)[record['symbol']]
            if record['origin'] == 'compatibility':
                self.assertTrue(record['reason'], record['symbol'])
                continue
            decorators = node.decorator_list
            if record['origin'] == 'baseline_with_guard':
                self.assertEqual(ast.unparse(decorators[0]), 'guard_legacy_action')
                decorators = decorators[1:]
                guarded.append(record['symbol'])
            start = min([node.lineno] + [item.lineno for item in decorators])
            text = ''.join(source.splitlines(True)[start - 1:node.end_lineno])
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),
                             record['baseline_sha256'], record['symbol'])
        self.assertEqual(set(guarded), {'WrfTaskManager.resume_external',
                                       'WrfTaskManager.retry_outputs',
                                       'WrfTaskManager.render_partial'})

    def test_legacy_list_keeps_owner_and_archive_filters(self):
        # Execute only this pure adapter, avoiding application startup and real DBs.
        from types import SimpleNamespace
        from unittest.mock import Mock
        source = (ROOT / 'main.py').read_text()
        node = definitions(source)['_normal_visible_tasks']
        node.returns = None
        for arg in node.args.args:
            arg.annotation = None
        store = SimpleNamespace(page=Mock(return_value={'items': [{'id': 'owned'}]}))
        namespace = {'store': store, '_viewer': lambda request: ('alice', request.role)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                     'ordinary-list-adapter', 'exec'), namespace)
        call = namespace['_normal_visible_tasks']
        self.assertEqual(call(SimpleNamespace(role=1, query_params={}), 50), [{'id': 'owned'}])
        store.page.assert_called_with(50, 'alice', archived=False)
        call(SimpleNamespace(role=3, query_params={'archived': 'true'}), 10)
        store.page.assert_called_with(10, None, archived=True)

        store.active = Mock(return_value=[{'id':'own-active','owner_sub':'alice'}, {'id':'other-active','owner_sub':'bob'}])
        page = call(SimpleNamespace(role=1,query_params={'paginated':'true','include_archived':'true'}),20)
        self.assertEqual([task['id'] for task in page['active_items']],['own-active'])
        store.page.assert_called_with(20,'alice',archived=None)
