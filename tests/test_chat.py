"""Chat authorization and persistence regression tests, without Azure credentials."""
import base64
import hashlib
import hmac
import os
import time
import types
import unittest
from unittest.mock import Mock, patch
from test_reliability import functions


class ChatTests(unittest.TestCase):
    def sessions(self):
        ns = functions('chat_signing_key', 'issue_chat_token', 'read_chat_session')
        ns.update(base64=base64, hashlib=hashlib, hmac=hmac, os=os, time=time)
        return ns

    def test_signed_session_rejects_tampering_expiry_and_missing_token(self):
        ns = self.sessions()
        with patch.dict(os.environ, {'CHAT_SESSION_SECRET': 'test-only-key'}):
            token = ns['issue_chat_token']('Client', 7, 'Client Seven')
            request = lambda t: types.SimpleNamespace(headers={'Authorization': 'Bearer ' + t})
            self.assertEqual(ns['read_chat_session'](request(token))['id'], 7)
            self.assertIsNone(ns['read_chat_session'](request(token + 'x')))
            self.assertIsNone(ns['read_chat_session'](request('')))
            with patch.object(time, 'time', return_value=time.time() + 28801):
                self.assertIsNone(ns['read_chat_session'](request(token)))

    def handler(self, session):
        ns = functions('chat', 'close_sql_resources')
        conn, cursor = Mock(), Mock()
        conn.cursor.return_value = cursor
        ns.update(read_chat_session=lambda req: session,
                  chat_response=lambda body, status=200: (status, body),
                  get_sql_connection=Mock(return_value=conn),
                  client_message_to_dict=lambda row: row)
        return ns, conn, cursor

    def req(self, method='GET', client_id=None, data=None):
        return types.SimpleNamespace(method=method,
            params={} if client_id is None else {'clientId': str(client_id)},
            get_json=lambda: data)

    def test_legacy_notes_cannot_bypass_chat_authentication(self):
        ns = functions('handle_client_messages')
        ns.update(chat_response=lambda body, status=200: (status, body), get_sql_connection=Mock())
        self.assertEqual(ns['handle_client_messages'](self.req())[0], 410)
        ns['get_sql_connection'].assert_not_called()

    def test_unsigned_requests_and_other_clients_are_rejected_before_sql(self):
        for session, expected in ((None, 401), ({'role': 'Client', 'id': 7}, 403)):
            ns, _, _ = self.handler(session)
            self.assertEqual(ns['chat'](self.req(client_id=8))[0], expected)
            ns['get_sql_connection'].assert_not_called()

    def test_client_without_id_only_reads_own_history(self):
        ns, conn, cursor = self.handler({'role': 'Client', 'id': 7})
        cursor.fetchone.return_value = object()
        cursor.fetchall.return_value = [{'id': 1}]
        status, body = ns['chat'](self.req())
        self.assertEqual(status, 200)
        self.assertEqual(body['messages'], [{'id': 1}])
        self.assertEqual(cursor.execute.call_args.args[1], 7)
        conn.close.assert_called_once()

    def test_sender_cannot_be_forged_and_admin_reply_commits(self):
        for role in ('Client', 'Admin'):
            ns, conn, cursor = self.handler({'role': role, 'id': 7, 'name': 'Verified Name'})
            cursor.fetchone.side_effect = [object(), {'id': 42, 'message': 'Hello'}]
            status, body = ns['chat'](self.req('POST', data={
                'clientId': 7, 'message': ' Hello ', 'senderType': 'Impersonator', 'senderName': 'Fake'}))
            self.assertEqual(status, 201)
            self.assertEqual(cursor.execute.call_args.args[1:], (7, role, 'Verified Name', 'Hello'))
            conn.commit.assert_called_once()
            conn.close.assert_called_once()

    def test_invalid_payloads_do_not_reach_database(self):
        for data in ([], {'message': []}, {'message': ''}, {'message': 'x'*2001}):
            ns, _, _ = self.handler({'role': 'Client', 'id': 7})
            self.assertEqual(ns['chat'](self.req('POST', data=data))[0], 400)
            ns['get_sql_connection'].assert_not_called()

    def test_query_failure_closes_resources_and_does_not_leak_exception(self):
        ns, conn, cursor = self.handler({'role': 'Client', 'id': 7})
        cursor.execute.side_effect = RuntimeError('sensitive database detail')
        with self.assertLogs(level='ERROR'):
            status, body = ns['chat'](self.req())
        self.assertEqual(status, 500)
        self.assertNotIn('sensitive', body['message'])
        conn.close.assert_called_once()
        cursor.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()

class ChatNotificationTests(unittest.TestCase):
    req = ChatTests.req
    def notification_handler(self, session):
        ns = functions('chat_notifications', 'close_sql_resources')
        conn, cursor = Mock(), Mock()
        conn.cursor.return_value = cursor
        ns.update(read_chat_session=lambda req: session,
                  chat_response=lambda body, status=200: (status, body),
                  get_sql_connection=Mock(return_value=conn), ensure_chat_reads=Mock())
        return ns, conn, cursor

    def test_alerts_reject_unsigned_and_other_client_read_updates(self):
        for session, status in ((None, 401), ({'role': 'Client', 'id': 7}, 403)):
            ns, _, _ = self.notification_handler(session)
            result = ns['chat_notifications'](self.req('PATCH', data={'clientId': 8, 'lastMessageId': 10}))
            self.assertEqual(result[0], status)
            ns['get_sql_connection'].assert_not_called()

    def test_read_update_is_scoped_and_monotonic(self):
        ns, conn, cursor = self.notification_handler({'role': 'Client', 'id': 7})
        cursor.fetchone.return_value = object()
        result = ns['chat_notifications'](self.req('PATCH', data={'clientId': 7, 'lastMessageId': 10}))
        self.assertEqual(result[0], 200)
        validate = cursor.execute.call_args_list[-2].args
        self.assertEqual(validate[1:], (10, 7, 'Admin'))
        write = cursor.execute.call_args.args
        self.assertEqual(write[1:], ('Client', 7, 7, 10))
        self.assertIn('target.LastReadId < source.LastReadId', write[0])
        conn.close.assert_called_once()

    def test_unread_query_scopes_client_and_incoming_sender(self):
        ns, conn, cursor = self.notification_handler({'role': 'Client', 'id': 7})
        cursor.fetchall.return_value = []
        status, body = ns['chat_notifications'](self.req())
        self.assertEqual(status, 200)
        self.assertEqual(body['unreadCount'], 0)
        self.assertEqual(cursor.execute.call_args.args[1:], ('Client', 7, 'Admin', 'Client', 7))

    def test_cannot_acknowledge_nonexistent_message(self):
        ns, conn, cursor = self.notification_handler({'role': 'Admin', 'id': 2})
        cursor.fetchone.return_value = None
        result = ns['chat_notifications'](self.req('PATCH', data={'clientId': 7, 'lastMessageId': 999}))
        self.assertEqual(result[0], 404)
        self.assertNotIn('MERGE', cursor.execute.call_args.args[0])
