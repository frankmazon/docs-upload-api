"""Isolated regression tests: no Azure services or database credentials required."""
import ast
import json
import logging
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / 'function_app.py'


def functions(*names):
    tree = ast.parse(SOURCE.read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    ns = dict(logging=logging, json=json)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns


class ReliabilityTests(unittest.TestCase):
    def test_cleanup_attempts_connection_close_after_cursor_close_fails(self):
        ns = functions('close_sql_resources')
        cursor, conn = Mock(), Mock()
        cursor.close.side_effect = RuntimeError('driver failure')
        with self.assertLogs(level='ERROR'):
            ns['close_sql_resources'](cursor, conn)
        conn.close.assert_called_once()

    def test_login_preflight_never_connects_to_sql(self):
        ns = functions('login')
        ns.update(func=types.SimpleNamespace(HttpResponse=lambda *a, **k: k),
                  add_cors=lambda response: response, get_sql_connection=Mock())
        self.assertEqual(ns['login'](types.SimpleNamespace(method='OPTIONS'))['status_code'], 204)
        ns['get_sql_connection'].assert_not_called()

    def test_handlers_close_resources_after_query_failure(self):
        for name in ('login', 'get_clients'):
            with self.subTest(name=name):
                ns = functions(name, 'close_sql_resources')
                conn, cursor = Mock(), Mock()
                conn.cursor.return_value = cursor
                cursor.execute.side_effect = RuntimeError('query timeout')
                ns.update(func=types.SimpleNamespace(HttpResponse=lambda *a, **k: k),
                          add_cors=lambda response: response,
                          get_sql_connection=Mock(return_value=conn))
                req = types.SimpleNamespace(method='POST', params={},
                    get_json=lambda: dict(username='test', password='test'))
                with self.assertLogs(level='ERROR'):
                    self.assertEqual(ns[name](req)['status_code'], 500)
                cursor.close.assert_called_once()
                conn.close.assert_called_once()
                ns['get_sql_connection'].assert_called_once_with(query_timeout=30)

    def test_reconcile_throttles_only_after_successful_commit(self):
        ns = functions('refresh_notification_records')
        clock = Mock(return_value=100)
        ns.update(_notification_reconcile_lock=threading.Lock(),
                  _notification_reconciled_at=None, time=types.SimpleNamespace(monotonic=clock),
                  ensure_admin_notifications_table=Mock(),
                  reconcile_client_submission_notifications=Mock(),
                  reconcile_client_document_notifications=Mock())
        conn, cursor = Mock(), Mock()
        conn.commit.side_effect = RuntimeError('commit failed')
        with self.assertRaises(RuntimeError):
            ns['refresh_notification_records'](conn, cursor)
        self.assertIsNone(ns['_notification_reconciled_at'])
        conn.commit.side_effect = None
        ns['refresh_notification_records'](conn, cursor)
        ns['refresh_notification_records'](conn, cursor)
        self.assertEqual(conn.commit.call_count, 2)
        clock.return_value = 161
        ns['refresh_notification_records'](conn, cursor)
        self.assertEqual(conn.commit.call_count, 3)

    def test_query_timeout_setup_failure_closes_connection(self):
        ns = functions('get_sql_connection')
        class Connection:
            close = Mock()
            @property
            def timeout(self):
                return 0
            @timeout.setter
            def timeout(self, value):
                raise RuntimeError('driver rejected timeout')
        conn = Connection()
        ns.update(os=types.SimpleNamespace(getenv=lambda name: 'test'),
                  pyodbc=types.SimpleNamespace(connect=Mock(return_value=conn)))
        with self.assertRaises(RuntimeError):
            ns['get_sql_connection'](query_timeout=30)
        conn.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
